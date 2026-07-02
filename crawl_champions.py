"""Crawl lolalytics matchup data for every champion x every lane.

The champion list comes from Riot's Data Dragon — never from lolalytics'
tier-list page, since that's the layout that previously dropped legitimate
champions like Renekton silently. For each (champion, lane) we load the
lolalytics build page and:
  - if the champ's pickrate in that lane is below --min-pickrate (default
    0.1): skip (no DB write, no scrape_runs row);
  - else extract overall + counter + synergy carousels and persist.

Lanes run in parallel. Each lane's worker thread owns one Chromium and
reuses a single page across all champions. Transient navigation timeouts
are retried with exponential backoff.

Examples:
  .venv/bin/python crawl_champions.py                             # full crawl
  .venv/bin/python crawl_champions.py --lanes bottom              # one lane
  .venv/bin/python crawl_champions.py --only Swain --lanes bottom # one champ, ignores PR filter
"""

import argparse
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

import config
import db
from scrape_lolalytics import (
    HEADLESS,
    fetch_champion_list,
    scrape_champion_on_page,
)


# A lane that produces fewer "ok" scrapes than this is treated as a
# structural break — lolalytics changed something and the crawl needs human
# attention before its output replaces the live release.
LANE_OK_FLOOR = 20

# Backoff schedule for transient (timeout) errors during a single scrape.
# Three attempts total: try, sleep 2s, retry, sleep 8s, final retry.
RETRY_BACKOFFS = (2, 8)


def scrape_with_retry(page, slug: str, lane: str, tier: str, label: str,
                      min_pr: float) -> dict:
    """Wrap scrape_champion_on_page with retries for transient navigation
    errors. Layout/extraction problems (no overall, zero rows) are NOT
    retried — they indicate the page is structurally off, so retrying just
    burns time. Those get classified by the caller via the returned data.
    """
    for attempt in range(len(RETRY_BACKOFFS) + 1):
        try:
            return scrape_champion_on_page(
                page, slug, lane, tier,
                min_pickrate_for_matchups=min_pr,
            )
        except PlaywrightTimeoutError as e:
            if attempt >= len(RETRY_BACKOFFS):
                raise
            delay = RETRY_BACKOFFS[attempt]
            print(f"{label}: transient timeout (attempt {attempt + 1}), "
                  f"retrying in {delay}s — {str(e)[:120]}")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def crawl_lane(
    lane: str,
    tier: str,
    champions: list[dict],
    min_pickrate: float,
    only_filter: set[str] | None,
    skip_failed: bool,
    limit: int,
) -> dict:
    """Worker entrypoint. One thread, one playwright/browser/context/page,
    sequential over the full champion list. Returns a per-lane summary.
    """
    pool = champions
    if only_filter:
        pool = [c for c in champions if c["name"].lower() in only_filter]
        details = ", ".join(c["name"] for c in pool)
        print(f"[{lane}] --only -> {len(pool)} champions: {details}")
    else:
        print(f"[{lane}] === start ({len(pool)} candidates) ===")

    # When --only is set the user explicitly named champions, so ignore the
    # pickrate filter — they want the matchup data even for off-meta combos.
    effective_min_pr = 0.0 if only_filter else min_pickrate

    summary = {
        "lane": lane,
        "successes": 0, "errors": 0, "empties": 0, "low_pr": 0, "skipped": 0,
        "fatal": False, "fatal_reason": None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            ctx = browser.new_context(viewport={"width": 2400, "height": 1100})
            page = ctx.new_page()

            for entry in pool:
                slug = entry["slug"]
                name = entry["name"]
                label = f"[{lane}] {name}"

                with db.connect(config.DB_PATH) as conn:
                    if db.already_scraped(conn, name, lane, tier):
                        print(f"{label}: skip — already done")
                        summary["skipped"] += 1
                        continue
                    if skip_failed:
                        row = conn.execute(
                            "SELECT status FROM scrape_runs WHERE champion_name=? AND lane=? AND tier=?",
                            (name, lane, tier),
                        ).fetchone()
                        if row and row["status"] in ("empty", "error"):
                            summary["skipped"] += 1
                            continue

                try:
                    data = scrape_with_retry(page, slug, lane, tier, label, effective_min_pr)
                    overall = data.get("overall") or {}

                    if not overall:
                        # Page didn't load properly — mark error so a
                        # follow-up failures-mode crawl picks it back up.
                        with db.connect(config.DB_PATH) as conn:
                            db.mark_scrape_run(conn, name, lane, tier, "error",
                                               "no overall stats extracted")
                        summary["errors"] += 1
                        print(f"{label}: error — no overall stats")
                        continue

                    if data.get("_skipped_low_pr"):
                        # Intentional skip. No DB write, no scrape_runs row,
                        # so the next full crawl re-checks (PR may climb
                        # back above threshold).
                        summary["low_pr"] += 1
                        pr_text = overall.get("pickrate") or "?"
                        print(f"{label}: low_pr (pr={pr_text})")
                        continue

                    with db.connect(config.DB_PATH) as conn:
                        matchup_count, _ = db.store_scrape_result(
                            conn, {**data, "champion": name}
                        )
                        if matchup_count > 0:
                            db.mark_scrape_run(conn, name, lane, tier, "ok",
                                               f"{matchup_count} matchups")
                            summary["successes"] += 1
                            status = "ok"
                        else:
                            db.mark_scrape_run(conn, name, lane, tier, "empty",
                                               "matchups=0")
                            summary["empties"] += 1
                            status = "empty"
                    print(f"{label}: {status} (matchups={matchup_count}, "
                          f"lane successes={summary['successes']})")
                except Exception as e:
                    traceback.print_exc()
                    summary["errors"] += 1
                    with db.connect(config.DB_PATH) as conn:
                        db.mark_scrape_run(conn, name, lane, tier, "error", str(e)[:200])
                    print(f"{label}: error — {e}")

                if limit and summary["successes"] >= limit:
                    print(f"[{lane}] reached --limit {limit}")
                    break
        finally:
            browser.close()

    print(f"[{lane}] === done: ok={summary['successes']} empty={summary['empties']} "
          f"error={summary['errors']} low_pr={summary['low_pr']} "
          f"skipped={summary['skipped']} ===")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", default=config.DEFAULT_TIER)
    parser.add_argument("--lanes", default="",
                        help="Comma-separated lanes (top, jungle, middle, bottom, support); "
                             "default: all five (run in parallel)")
    parser.add_argument("--min-pickrate", type=float, default=0.1,
                        help="For each (champ, lane), if the lolalytics overall pickrate "
                             "is below this %% the page is skipped (no DB write, no "
                             "matchups). Default 0.1.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop each lane after N successful scrapes (0 = unlimited)")
    parser.add_argument("--skip-failed", action="store_true",
                        help="Skip combos previously marked 'empty' or 'error' "
                             "(default: retry them)")
    parser.add_argument("--only", default="",
                        help="Comma-separated display names to keep "
                             "(case-insensitive, e.g. 'Swain,K\\'Sante'). "
                             "Bypasses --min-pickrate.")
    parser.add_argument("--max-workers", type=int, default=0,
                        help="Concurrent lane workers (default: one per lane). "
                             "Lower if lolalytics rate-limits.")
    args = parser.parse_args()

    lanes = config.LANES
    if args.lanes:
        wanted = [l.strip().lower() for l in args.lanes.split(",") if l.strip()]
        unknown = [l for l in wanted if l not in config.LANES]
        if unknown:
            parser.error(f"unknown lane(s): {unknown}. Valid: {config.LANES}")
        lanes = wanted

    only_filter: set[str] | None = None
    if args.only:
        only_filter = {n.strip().lower() for n in args.only.split(",") if n.strip()}

    db.init_db(config.DB_PATH)

    print("fetching champion list from Data Dragon...")
    try:
        champions = fetch_champion_list()
    except Exception as e:
        print(f"FATAL — Data Dragon fetch failed: {e}")
        traceback.print_exc()
        sys.exit(1)
    print(f"got {len(champions)} champions")

    max_workers = args.max_workers or len(lanes)

    t_start = time.time()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(crawl_lane, lane, args.tier, champions,
                      args.min_pickrate, only_filter, args.skip_failed, args.limit): lane
            for lane in lanes
        }
        for fut in as_completed(futures):
            lane = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                traceback.print_exc()
                results.append({
                    "lane": lane,
                    "successes": 0, "errors": 0, "empties": 0,
                    "low_pr": 0, "skipped": 0,
                    "fatal": True, "fatal_reason": f"worker crashed: {e}",
                })

    elapsed = time.time() - t_start
    print(f"\n=== crawl complete in {elapsed:.0f}s ===")
    for r in sorted(results, key=lambda x: x["lane"]):
        marker = " (FATAL)" if r["fatal"] else ""
        print(f"  {r['lane']}: ok={r['successes']} empty={r['empties']} "
              f"error={r['errors']} low_pr={r['low_pr']} "
              f"skipped={r['skipped']}{marker}")

    with db.connect(config.DB_PATH) as conn:
        print(db.stats(conn))

    # Loud failure check. The OK floor only applies when we actually
    # attempted a full lane crawl — --only or --skip-failed legitimately
    # produce small batches.
    floor_applies = not (only_filter or args.skip_failed)
    bad_lanes = [
        r for r in results
        if r["fatal"] or (floor_applies and r["successes"] < LANE_OK_FLOOR)
    ]

    if bad_lanes:
        print("\nFATAL — the following lanes look structurally broken:")
        for r in bad_lanes:
            reason = r.get("fatal_reason") or f"only {r['successes']} ok (floor={LANE_OK_FLOOR})"
            print(f"  {r['lane']}: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
