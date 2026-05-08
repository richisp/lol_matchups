"""Scrape lolalytics champion matchup data.

Usage:
    .venv/bin/python scrape_lolalytics.py [champion] [lane] [tier]
    .venv/bin/python scrape_lolalytics.py swain bottom all
    .venv/bin/python scrape_lolalytics.py garen top emerald_plus

Tier options: all, challenger, grandmaster, master, master_plus,
              diamond_plus, emerald_plus (default), platinum_plus, gold_plus, etc.

Saves raw row text per position, per tab, to lolalytics_<champion>_<lane>.txt.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

import config
from db import _parse_pct

HEADLESS = os.environ.get("HEADLESS", "0") == "1"

DEFAULT_CHAMPION = "swain"
DEFAULT_LANE = "bottom"
DEFAULT_TIER = config.DEFAULT_TIER

# Riot's Data Dragon champion `id` → lolalytics URL slug. Most ids lowercase
# cleanly into the slug (KSante → ksante, Chogath → chogath); a handful
# don't and need an explicit override.
LOLALYTICS_SLUG_OVERRIDES: dict[str, str] = {
    "MonkeyKing": "wukong",
}


def _ddragon_latest_version() -> str:
    with urllib.request.urlopen(
        "https://ddragon.leagueoflegends.com/api/versions.json", timeout=15
    ) as r:
        return json.loads(r.read().decode())[0]


def fetch_champion_list() -> list[dict]:
    """Pull every champion's display name + lolalytics URL slug from Riot's
    Data Dragon. The crawler iterates this list × every lane instead of
    parsing lolalytics' tier list, so a layout regression there can no
    longer drop legitimate champions (Renekton was missing in the 2026-05-03
    crawl for exactly that reason).

    Returns: [{"name": "Renekton", "slug": "renekton"}, ...]
    """
    version = _ddragon_latest_version()
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    with urllib.request.urlopen(url, timeout=30) as r:
        payload = json.loads(r.read().decode())
    out = [
        {"name": e["name"], "slug": LOLALYTICS_SLUG_OVERRIDES.get(e["id"], e["id"].lower())}
        for e in payload["data"].values()
    ]
    out.sort(key=lambda x: x["name"])
    return out


EXTRACT_OVERALL_JS = r"""
() => {
    // Page header shows: <div>VALUE</div><div class="text-xs ...bbbbbb">LABEL</div>
    // First occurrence of each label is the focal champion's overall stat.
    const labels = {
        'Win Rate':  'winrate',
        'Pick Rate': 'pickrate',
        'Ban Rate':  'banrate',
        'Games':     'games',
        'Tier':      'tier',
    };
    const stats = {};
    for (const [label, key] of Object.entries(labels)) {
        const candidates = [...document.querySelectorAll('div, span')].filter(el => {
            if (el.children.length > 1) return false;
            const t = (el.textContent || '').trim();
            return t === label || t.startsWith(label + '?');
        });
        if (!candidates.length) continue;
        const valueEl = candidates[0].previousElementSibling;
        const value = (valueEl && valueEl.textContent || '').trim();
        if (value) stats[key] = value;
    }
    return stats;
}
"""


def dismiss_consent(page: Page) -> None:
    """lolalytics uses an 'ncmp' GDPR banner. The Accept button gets covered
    by a shadow overlay that intercepts pointer events, so a JS-level click
    is the reliable path."""
    for _ in range(15):
        clicked = page.evaluate(
            """
            () => {
                const banner = document.querySelector('.ncmp__banner.ncmp__active');
                if (!banner) return false;
                const buttons = banner.querySelectorAll('button');
                for (const b of buttons) {
                    const t = (b.textContent || '').trim().toLowerCase();
                    if (t === 'accept' || t === 'accept all' || t === 'agree') {
                        b.click();
                        return true;
                    }
                }
                return false;
            }
            """
        )
        if clicked:
            print("dismissed consent banner")
            page.wait_for_timeout(800)
            return
        page.wait_for_timeout(500)
    print("no consent banner found (already dismissed or not shown)")


def lazy_scroll_page(page: Page, steps: int = 12) -> None:
    """Scroll down progressively to trigger lazy-loaded sections."""
    for _ in range(steps):
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(250)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def click_tab(page: Page, data_type: str) -> bool:
    """Click a tab by its data-type attribute (lolalytics uses these on
    tab divs, e.g. data-type='strong_against', data-type='good_synergy')."""
    clicked = page.evaluate(
        """
        (dt) => {
            const el = document.querySelector(`[data-type="${dt}"]`);
            if (!el) return false;
            el.scrollIntoView({ block: 'center' });
            el.click();
            return true;
        }
        """,
        data_type,
    )
    if clicked:
        page.wait_for_timeout(1500)
        print(f"clicked tab: data-type={data_type!r}")
        return True
    print(f"failed to find tab with data-type={data_type!r}")
    return False


SCROLL_STEP_JS = r"""
async (delta) => {
    // Scroll every horizontal carousel by `delta` pixels (or to end if delta < 0).
    const scrollables = document.querySelectorAll('.cursor-grab.overflow-x-scroll');
    for (const el of scrollables) {
        if (delta < 0) {
            el.scrollLeft = el.scrollWidth;
        } else {
            el.scrollLeft = (el.scrollLeft || 0) + delta;
        }
    }
    await new Promise(r => setTimeout(r, 400));
}
"""


def scroll_rows_step(page: Page, delta_px: int) -> None:
    page.evaluate(SCROLL_STEP_JS, delta_px)


EXTRACT_ROWS_JS = r"""
() => {
    const ANCHORS = ['Counter', 'Synergy'];
    const POSITION_RULES = [
        { key: 'TOP',     re: /\btop\b/i },
        { key: 'JUNGLE',  re: /\bjungle\b/i },
        { key: 'MID',     re: /\b(middle|mid)\b/i },
        { key: 'BOT',     re: /\b(bottom|bot)\b/i },
        { key: 'SUPPORT', re: /\b(support|utility)\b/i },
    ];
    const labels = [...document.querySelectorAll('*')].filter(
        el => el.children.length === 0
              && el.textContent
              && ANCHORS.includes(el.textContent.trim())
    );

    const byPosition = {};
    labels.forEach(label => {
        let row = label.parentElement;
        for (let i = 0; i < 10 && row; i++) {
            if (row.querySelectorAll('img').length >= 5) break;
            row = row.parentElement;
        }
        if (!row) return;

        const laneIcons = [...row.querySelectorAll('img')].filter(img => /lane/i.test(img.alt || ''));
        if (laneIcons.length !== 1) return;
        const laneAlt = laneIcons[0].alt || '';
        let position = null;
        for (const rule of POSITION_RULES) {
            if (rule.re.test(laneAlt)) { position = rule.key; break; }
        }
        if (!position || byPosition[position]) return;

        const champs = [];
        const seen = new Set();
        row.querySelectorAll('a').forEach(link => {
            const img = link.querySelector('img');
            if (!img) return;
            const alt = (img.alt || '').trim();
            if (!alt || /lane/i.test(alt) || alt.length > 30) return;
            if (seen.has(alt)) return;
            const card = link.parentElement;
            if (!card) return;

            const stats = [];
            for (const node of card.childNodes) {
                if (node.nodeType !== 1) continue;
                if (node.tagName === 'A') continue;
                if (node.tagName !== 'DIV') continue;
                const cloned = node.cloneNode(true);
                cloned.querySelectorAll('q\\:template, [hidden], [aria-hidden="true"]').forEach(n => n.remove());
                const t = (cloned.textContent || '').replace(/\s+/g, ' ').trim();
                if (t) stats.push(t);
            }
            seen.add(alt);
            champs.push({ name: alt, stats });
        });
        byPosition[position] = { position, champs };
    });

    const ORDER = ['TOP', 'JUNGLE', 'MID', 'BOT', 'SUPPORT'];
    return ORDER.map(p => byPosition[p]).filter(Boolean);
}
"""


def extract_rows(page: Page) -> list:
    return page.evaluate(EXTRACT_ROWS_JS)


def extract_overall_stats(page: Page) -> dict:
    return page.evaluate(EXTRACT_OVERALL_JS)


def collect_all_rows(page: Page, max_iterations: int = 25, step_px: int = 500) -> list:
    """Repeatedly scroll the carousels and re-extract, merging unique champions.

    Handles three possibilities at once:
      - Cap is data-driven (we'll just merge the same set every iter and stop early).
      - Lazy-loaded on scroll (new cards appear, scrollWidth grows).
      - Virtualized (cards mount/unmount; merging across positions captures all).
    """
    merged: dict[str, dict] = {}

    def merge(rows: list) -> int:
        added = 0
        for r in rows:
            pos = r["position"]
            if pos not in merged:
                merged[pos] = {"position": pos, "champs": [], "_seen": set()}
            entry = merged[pos]
            for c in r["champs"]:
                if c["name"] in entry["_seen"]:
                    continue
                entry["_seen"].add(c["name"])
                entry["champs"].append(c)
                added += 1
        return added

    # First read at scroll=0
    page.wait_for_timeout(600)
    merge(extract_rows(page))

    for i in range(max_iterations):
        scroll_rows_step(page, step_px)
        page.wait_for_timeout(450)
        added = merge(extract_rows(page))
        if added == 0:
            # One more pass: jump to the very end in case a final batch lazy-loads
            scroll_rows_step(page, -1)
            page.wait_for_timeout(700)
            tail_added = merge(extract_rows(page))
            if tail_added == 0:
                break

    # Strip internal _seen and preserve canonical order
    ORDER = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]
    out = []
    for pos in ORDER:
        if pos in merged:
            out.append({"position": pos, "champs": merged[pos]["champs"]})
    return out


def format_section(rows: list, header: str) -> str:
    """Stat order in lolalytics' DOM: WinRate, Delta1, Delta2, PickRate, Games.
    User asked to drop deltas — keep WR, PR, Games."""
    out = [f"=== {header} ===", ""]
    for r in rows:
        pos = r.get("position", "?")
        out.append(f"--- {pos} ({len(r['champs'])} entries) ---")
        out.append(f"  {'Champion':<20}  {'WR':>7}  {'PR':>6}  {'Games':>6}")
        for c in r["champs"]:
            stats = c["stats"]
            wr = stats[0] if len(stats) > 0 else "?"
            pr = stats[3] if len(stats) > 3 else "?"
            games = stats[4] if len(stats) > 4 else "?"
            out.append(f"  {c['name']:<20}  {wr:>7}  {pr:>6}  {games:>6}")
        out.append("")
    return "\n".join(out)


def scrape_champion_on_page(
    page: Page,
    champion: str,
    lane: str,
    tier: str = DEFAULT_TIER,
    *,
    min_pickrate_for_matchups: float = 0.0,
) -> dict:
    """Same contract as scrape_champion, but reuses an existing page.

    If `min_pickrate_for_matchups > 0` and the focal champion's overall pick
    rate in this lane is below it, returns immediately after the overall
    stats — the (slow) `lazy_scroll_page` + tab clicks + carousel scrolling
    are skipped. The result will have `_skipped_low_pr=True` so the caller
    can distinguish "intentional skip" from "page broke before tabs".
    """
    url = f"https://lolalytics.com/lol/{champion}/build/?lane={lane}&tier={tier}"
    print(f"loading: {url}")
    result: dict = {
        "champion": champion,
        "lane": lane,
        "tier": tier,
        "url": url,
        "overall": {},
        "strong_against": [],
        "good_synergy": [],
    }
    page.goto(url, timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    dismiss_consent(page)

    result["overall"] = extract_overall_stats(page)
    print(f"  overall: {result['overall']}")

    if min_pickrate_for_matchups > 0:
        pr = _parse_pct(result["overall"].get("pickrate"))
        if pr is not None and pr < min_pickrate_for_matchups:
            print(f"  skip matchups: pr={pr}% < {min_pickrate_for_matchups}%")
            result["_skipped_low_pr"] = True
            return result

    lazy_scroll_page(page)

    for key, data_type in [("strong_against", "strong_counter"), ("good_synergy", "good_synergy")]:
        if not click_tab(page, data_type):
            continue
        rows = collect_all_rows(page)
        sizes = ', '.join(f"{r['position']}={len(r['champs'])}" for r in rows)
        print(f"  {key}: {len(rows)} rows [{sizes}]")
        result[key] = rows
    return result


def scrape_champion(champion: str, lane: str, tier: str = DEFAULT_TIER) -> dict:
    """Open lolalytics for a champion + lane + tier and return:
        {
          'champion': str, 'lane': str, 'tier': str,
          'url': str,
          'overall': { winrate, pickrate, banrate, games, tier_badge },
          'strong_against': [ { position, champs: [{name, stats}] }, ... ],
          'good_synergy':   [ { position, champs: [{name, stats}] }, ... ],
        }

    Standalone wrapper that owns its own browser. The crawler uses
    scrape_champion_on_page directly to share one browser across many calls.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            ctx = browser.new_context(viewport={"width": 2400, "height": 1100})
            page = ctx.new_page()
            return scrape_champion_on_page(page, champion, lane, tier)
        finally:
            browser.close()


def main() -> None:
    champion = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHAMPION
    lane = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LANE
    tier = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_TIER
    output = Path(f"lolalytics_{champion}_{lane}.txt")

    data = scrape_champion(champion, lane, tier)

    sections = [f"=== {data['champion']} {data['lane']} ({data['tier']}) ==="]
    sections.append(f"Overall: {data['overall']}")
    sections.append("")
    sections.append(format_section(data["strong_against"], "Strong Against"))
    sections.append(format_section(data["good_synergy"], "Good Synergy"))

    output.write_text("\n".join(sections), encoding="utf-8")
    print(f"\nsaved to {output.resolve()}")


if __name__ == "__main__":
    main()
