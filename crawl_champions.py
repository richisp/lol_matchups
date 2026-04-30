"""Crawl lolalytics matchup data, restricted to popular (PR > threshold) champions per lane.

Workflow per lane:
  1. Hit lolalytics' tier list page for that lane × tier.
  2. Pick champions whose pick rate exceeds --min-pickrate.
  3. Scrape each one's full build page and store overall stats + matchups.

State lives in `scrape_runs` so re-runs skip combos already marked 'ok'.

Examples:
  .venv/bin/python crawl_champions.py --lanes bottom              # all popular bots
  .venv/bin/python crawl_champions.py --min-pickrate 2            # only PR > 2%
  .venv/bin/python crawl_champions.py --only Swain --lanes bottom # one champion
"""

import argparse
import time
import traceback

import config
import db
from scrape_lolalytics import fetch_lane_pool, scrape_champion


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", default=config.DEFAULT_TIER)
    parser.add_argument("--lanes", default="",
                        help="Comma-separated lanes (top, jungle, middle, bottom, support); "
                             "default: all five")
    parser.add_argument("--min-pickrate", type=float, default=1.0,
                        help="Skip champions whose pick rate ≤ this %% (default 1.0)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N successful scrapes (0 = unlimited)")
    parser.add_argument("--skip-failed", action="store_true",
                        help="Skip combos previously marked 'empty' or 'error' "
                             "(default: retry them)")
    parser.add_argument("--only", default="",
                        help="Comma-separated display names to keep "
                             "(case-insensitive, e.g. 'Swain,K\\'Sante')")
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

    done = 0
    successes = 0
    t_start = time.time()

    # When --only is set, the pickrate filter is bypassed (the user explicitly
    # named champions, even rare ones like Veigar bot should still be crawled).
    pool_min_pr = 0.0 if only_filter else args.min_pickrate

    for lane in lanes:
        print(f"\n=== {lane.upper()} ===")
        try:
            pool = fetch_lane_pool(lane, args.tier, pool_min_pr)
        except Exception as e:
            print(f"failed to fetch pool for {lane}: {e}")
            traceback.print_exc()
            continue

        if only_filter:
            pool = [p for p in pool if p["name"].lower() in only_filter]
            details = ", ".join(f"{p['name']} (PR {p['pickrate']:.2f}%)" for p in pool)
            print(f"pool: {len(pool)} champion(s) matching --only — {details}")
        else:
            top = ", ".join(p["name"] for p in pool[:10])
            print(f"pool: {len(pool)} champions (PR > {args.min_pickrate}%) — top: {top}")

        for entry in pool:
            done += 1
            slug = entry["slug"]
            name = entry["name"]
            pr = entry["pickrate"]

            with db.connect(config.DB_PATH) as conn:
                if db.already_scraped(conn, name, lane, args.tier):
                    print(f"[{done}] {name}/{lane} (PR {pr:.2f}%): skip — already done")
                    continue
                if args.skip_failed:
                    row = conn.execute(
                        "SELECT status FROM scrape_runs WHERE champion_name=? AND lane=? AND tier=?",
                        (name, lane, args.tier),
                    ).fetchone()
                    if row and row["status"] in ("empty", "error"):
                        continue

            label = f"[{done}] {name}/{lane} (PR {pr:.2f}%)"
            try:
                data = scrape_champion(slug, lane, args.tier)
                with db.connect(config.DB_PATH) as conn:
                    matchup_count, has_overall = db.store_scrape_result(
                        conn, {**data, "champion": name}
                    )
                    if has_overall and matchup_count > 0:
                        db.mark_scrape_run(conn, name, lane, args.tier, "ok",
                                           f"{matchup_count} matchups")
                        successes += 1
                        status = "ok"
                    else:
                        db.mark_scrape_run(conn, name, lane, args.tier, "empty",
                                           f"overall={has_overall} matchups={matchup_count}")
                        status = "empty"
                elapsed = time.time() - t_start
                print(f"{label}: {status} (matchups={matchup_count}, "
                      f"successes={successes}, elapsed={elapsed:.0f}s)")
            except Exception as e:
                traceback.print_exc()
                with db.connect(config.DB_PATH) as conn:
                    db.mark_scrape_run(conn, name, lane, args.tier, "error", str(e)[:200])
                print(f"{label}: error — {e}")

            if args.limit and successes >= args.limit:
                print(f"reached --limit {args.limit}, stopping")
                with db.connect(config.DB_PATH) as conn:
                    print(db.stats(conn))
                return

    with db.connect(config.DB_PATH) as conn:
        print(db.stats(conn))


if __name__ == "__main__":
    main()
