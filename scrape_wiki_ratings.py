"""Scrape champion classes + attribute ratings from the LoL wiki.

Source: https://wiki.leagueoflegends.com/en-us/List_of_champions/Ratings —
the human-curated upstream that Meraki mirrors, with one thing the JSON
mirror loses: **class priority**. The table has separate Primary and
Secondary class columns (Akshan: Marksman first, Assassin second), while
Meraki's `roles` list is alphabetical. Rows scraped here are stored with
`roles_ranked=1`, which switches the app's comp-fit damping from
count-based to priority-based (config.SUBCLASS_RANK_DAMPING).

The wiki sits behind a bot challenge (Weird Gloop) that blocks plain HTTP
clients AND its own api.php, so this needs a real browser — Playwright.
Crawler-side only; the desktop app's self-heal keeps using Meraki/CDragon.

Run policy: wiki class data changes very rarely (new releases, occasional
reworks), so this only scrapes when champion_stats knows more champions
than champion_attributes has wiki-ranked rows — i.e. a champion appeared
in winrate data without ranked attributes. `--force` bypasses the check
(use after a mid-season class rework).

Usage:
    python scrape_wiki_ratings.py [--force]
"""

import argparse
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

import config
import db

URL = "https://wiki.leagueoflegends.com/en-us/List_of_champions/Ratings"
HEADLESS = os.environ.get("HEADLESS", "0") == "1"

# Structural sanity floor, same idea as fetch_attributes: the table lists
# ~170 champions; far fewer means the page changed shape — don't write.
MIN_EXPECTED = 150

# Rating columns follow the Secondary class column, in this order.
RATING_KEYS = ("damage", "toughness", "control", "mobility", "utility")

EXTRACT_JS = r"""
() => {
    // The ratings table is the one whose header row has Champion + Primary.
    const table = [...document.querySelectorAll('table')].find(t => {
        const h = t.querySelector('tr');
        return h && /Champion/.test(h.textContent) && /Primary/.test(h.textContent);
    });
    if (!table) return null;
    const out = [];
    for (const tr of [...table.querySelectorAll('tr')].slice(1)) {
        const cells = [...tr.querySelectorAll('td, th')];
        if (cells.length < 8) continue;
        // The champion cell carries a clean canonical name attribute.
        const nameEl = cells[0].querySelector('[data-champion]');
        const name = nameEl ? nameEl.getAttribute('data-champion')
                            : cells[0].textContent.trim();
        out.push({
            name,
            primary: cells[1].textContent.trim(),
            secondary: cells[2].textContent.trim(),
            ratings: cells.slice(3, 8).map(c => parseInt(c.textContent.trim(), 10)),
        });
    }
    return out;
}
"""


def _norm(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


def _fetch_json(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _dd_name_map() -> dict[str, tuple[str, str]]:
    """normalized display name -> (canonical display name, riot id)."""
    version = _fetch_json("https://ddragon.leagueoflegends.com/api/versions.json")[0]
    data = _fetch_json(
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    )["data"]
    out: dict[str, tuple[str, str]] = {}
    for dd_id, info in data.items():
        out[_norm(info["name"])] = (info["name"], dd_id)
        out[_norm(dd_id)] = (info["name"], dd_id)
    return out


def needs_run(conn) -> tuple[int, int]:
    """(champs known to winrate data, champs with wiki-ranked attributes)."""
    stats_n = conn.execute(
        "SELECT COUNT(DISTINCT champion_name) FROM champion_stats"
    ).fetchone()[0]
    ranked_n = conn.execute(
        "SELECT COUNT(*) FROM champion_attributes WHERE roles_ranked = 1"
    ).fetchone()[0]
    return stats_n, ranked_n


def scrape() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            page = browser.new_page()
            page.goto(URL, timeout=60000)
            # The bot-challenge interstitial resolves itself in a real
            # browser, then the article loads; wait for the ratings table.
            page.wait_for_selector("table", timeout=45000)
            rows = page.evaluate(EXTRACT_JS)
        finally:
            browser.close()
    return rows or []


def build_rows(raw: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate + map wiki rows onto our schema. Returns (rows, skipped)."""
    names = _dd_name_map()
    rows: list[dict] = []
    skipped: list[str] = []
    for r in raw:
        mapped = names.get(_norm(r.get("name") or ""))
        subs = [s.upper() for s in (r.get("primary"), r.get("secondary"))
                if s and s.strip()]
        ratings = r.get("ratings") or []
        ok = (
            mapped is not None
            and subs
            and all(s in config.SUBCLASS_COMP_FIT for s in subs)
            and len(ratings) == len(RATING_KEYS)
            and all(isinstance(v, int) and 0 <= v <= 3 for v in ratings)
        )
        if not ok:
            skipped.append(r.get("name") or "?")
            continue
        display, riot_id = mapped
        rows.append({
            "champion_name": display,
            "riot_id": riot_id,
            "roles": ",".join(subs),  # priority order: primary first
            **dict(zip(RATING_KEYS, ratings)),
        })
    return rows, skipped


def store(conn, rows: list[dict]) -> None:
    for r in rows:
        cur = conn.execute(
            """
            UPDATE champion_attributes
               SET roles = :roles, roles_ranked = 1, damage = :damage,
                   toughness = :toughness, control = :control,
                   mobility = :mobility, utility = :utility,
                   fetched_at = datetime('now')
             WHERE champion_name = :champion_name
            """,
            r,
        )
        if cur.rowcount == 0:
            # Champion the Meraki fetch didn't know yet — insert what the
            # wiki gives; adaptive_type etc. arrive with the next fetch.
            conn.execute(
                """
                INSERT INTO champion_attributes
                    (champion_name, riot_id, roles, roles_ranked, damage,
                     toughness, control, mobility, utility)
                VALUES (:champion_name, :riot_id, :roles, 1, :damage,
                        :toughness, :control, :mobility, :utility)
                """,
                r,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="scrape even when no new champion appeared")
    args = parser.parse_args()

    db.init_db(config.DB_PATH)
    with db.connect(config.DB_PATH) as conn:
        stats_n, ranked_n = needs_run(conn)
    if not args.force and ranked_n >= stats_n:
        print(f"wiki scrape skipped: {ranked_n} ranked attribute rows already "
              f"cover the {stats_n} champions in winrate data (--force to override)")
        return 0
    print(f"wiki scrape: {ranked_n} ranked rows < {stats_n} champions with winrates")

    try:
        raw = scrape()
    except Exception as e:  # noqa: BLE001 — network/challenge/layout failure
        print(f"wiki scrape failed: {e}", file=sys.stderr)
        return 1
    rows, skipped = build_rows(raw)
    if skipped:
        print(f"warning: skipped {len(skipped)} rows: {', '.join(skipped[:8])}")
    if len(rows) < MIN_EXPECTED:
        print(f"only {len(rows)} usable rows (< {MIN_EXPECTED}) — refusing to write",
              file=sys.stderr)
        return 1

    with db.connect(config.DB_PATH) as conn:
        store(conn, rows)
    print(f"stored wiki-ranked classes/ratings for {len(rows)} champions "
          f"-> {config.DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
