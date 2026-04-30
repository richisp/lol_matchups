"""Scrape lolalytics champion matchup data.

Usage:
    .venv/bin/python scrape_lolalytics.py [champion] [lane] [tier]
    .venv/bin/python scrape_lolalytics.py swain bottom all
    .venv/bin/python scrape_lolalytics.py garen top emerald_plus

Tier options: all, challenger, grandmaster, master, master_plus,
              diamond_plus, emerald_plus (default), platinum_plus, gold_plus, etc.

Saves raw row text per position, per tab, to lolalytics_<champion>_<lane>.txt.
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

import config

HEADLESS = os.environ.get("HEADLESS", "0") == "1"

DEFAULT_CHAMPION = "swain"
DEFAULT_LANE = "bottom"
DEFAULT_TIER = config.DEFAULT_TIER


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


def fetch_lane_pool(lane: str, tier: str = DEFAULT_TIER, min_pickrate: float = 1.0) -> list[dict]:
    """Visit lolalytics' tier list page for a lane × tier and return the
    list of champions whose pick rate exceeds `min_pickrate` (in percent).
    Each entry: {slug, name, pickrate}.

    `slug` is lolalytics' URL slug (e.g. 'kSante' style lowercased) — feed
    straight into scrape_champion(). `name` is the display name (e.g. "K'Sante")
    used as the canonical champion identifier in our DB.
    """
    url = f"https://lolalytics.com/lol/tierlist/?lane={lane}&tier={tier}"
    print(f"fetching pool: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            ctx = browser.new_context(viewport={"width": 2400, "height": 1100})
            page = ctx.new_page()
            page.goto(url, timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            dismiss_consent(page)
            # Scroll the whole page so every row renders.
            for _ in range(20):
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(120)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            entries = page.evaluate(r"""() => {
                const rows = [...document.querySelectorAll('div')]
                    .filter(d => /h-\[52px\]/.test(d.className || ''));
                const out = [];
                for (const row of rows) {
                    const link = row.querySelector('a[href*="/build/"]');
                    if (!link) continue;
                    const m = (link.getAttribute('href') || '').match(/\/lol\/([a-z0-9]+)\/build/i);
                    if (!m) continue;
                    const slug = m[1];
                    const img = row.querySelector('img[alt]');
                    const name = img ? (img.alt || '').trim() : '';
                    // Column index 6 is pick rate (verified empirically).
                    const prText = (row.children[6]?.textContent || '').trim();
                    const pr = parseFloat(prText.replace(',', '')) || 0;
                    if (slug && name) out.push({ slug, name, pickrate: pr });
                }
                return out;
            }""")
        finally:
            browser.close()

    return [e for e in entries if e["pickrate"] > min_pickrate]


def scrape_champion(champion: str, lane: str, tier: str = DEFAULT_TIER) -> dict:
    """Open lolalytics for a champion + lane + tier and return:
        {
          'champion': str, 'lane': str, 'tier': str,
          'url': str,
          'overall': { winrate, pickrate, banrate, games, tier_badge },
          'strong_against': [ { position, champs: [{name, stats}] }, ... ],
          'good_synergy':   [ { position, champs: [{name, stats}] }, ... ],
        }
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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            ctx = browser.new_context(viewport={"width": 2400, "height": 1100})
            page = ctx.new_page()
            page.goto(url, timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            dismiss_consent(page)
            lazy_scroll_page(page)

            result["overall"] = extract_overall_stats(page)
            print(f"  overall: {result['overall']}")

            for key, data_type in [("strong_against", "strong_counter"), ("good_synergy", "good_synergy")]:
                if not click_tab(page, data_type):
                    continue
                rows = collect_all_rows(page)
                sizes = ', '.join(f"{r['position']}={len(r['champs'])}" for r in rows)
                print(f"  {key}: {len(rows)} rows [{sizes}]")
                result[key] = rows
        finally:
            browser.close()
    return result


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
