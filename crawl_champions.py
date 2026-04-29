"""Crawl lolalytics for every champion × lane and store to SQLite.

Usage:
    .venv/bin/python crawl_champions.py [--tier emerald_plus] [--retry-empty] [--limit N]

State is in lolalytics.db's `scrape_runs` table — re-runs skip combos already
marked 'ok'. Empty/error runs are retried by default unless --skip-failed.
"""

import argparse
import time
import traceback

import httpx

import config
import db
from scrape_lolalytics import scrape_champion

DD_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"


def fetch_champion_list() -> list[str]:
    """Returns champion 'id' strings (e.g. 'Aatrox', 'MonkeyKing'), the same
    keys used by lolalytics URL slugs (lowercase) and Data Dragon assets."""
    versions = httpx.get(DD_VERSIONS_URL, timeout=15).json()
    latest = versions[0]
    url = f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/en_US/champion.json"
    data = httpx.get(url, timeout=15).json()
    return sorted(data["data"].keys())


def champion_to_slug(name: str) -> str:
    """Data Dragon → lolalytics URL slug. Mostly lowercase id; a few special
    cases that diverge from the simple lowercase rule."""
    overrides = {
        "MonkeyKing": "wukong",
        "Nunu":       "nunu",
        "Renata":     "renataglasc",
    }
    return overrides.get(name, name.lower())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", default=config.DEFAULT_TIER)
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N successful (champion, lane) pairs (0 = unlimited)")
    parser.add_argument("--skip-failed", action="store_true",
                        help="Skip combos previously marked empty/error (default: retry them)")
    parser.add_argument("--only", default="",
                        help="Comma-separated champion ids; if set, crawl only these")
    parser.add_argument("--lanes", default="",
                        help="Comma-separated lanes (top, jungle, middle, bottom, support); "
                             "if set, crawl only these (default: all)")
    args = parser.parse_args()

    db.init_db(config.DB_PATH)
    champions = fetch_champion_list()
    if args.only:
        wanted = {c.strip().lower() for c in args.only.split(",") if c.strip()}
        champions = [c for c in champions if c.lower() in wanted]

    lanes = config.LANES
    if args.lanes:
        wanted_lanes = [l.strip().lower() for l in args.lanes.split(",") if l.strip()]
        unknown = [l for l in wanted_lanes if l not in config.LANES]
        if unknown:
            parser.error(f"unknown lane(s): {unknown}. Valid: {config.LANES}")
        lanes = wanted_lanes

    print(f"will crawl {len(champions)} champions × {len(lanes)} lanes "
          f"= {len(champions) * len(lanes)} combos at tier={args.tier}")

    done = 0
    successes = 0
    t_start = time.time()

    for champ_id in champions:
        slug = champion_to_slug(champ_id)
        for lane in lanes:
            done += 1
            with db.connect(config.DB_PATH) as conn:
                if db.already_scraped(conn, champ_id, lane, args.tier):
                    continue
                if args.skip_failed:
                    row = conn.execute(
                        "SELECT status FROM scrape_runs WHERE champion_name=? AND lane=? AND tier=?",
                        (champ_id, lane, args.tier),
                    ).fetchone()
                    if row and row["status"] in ("empty", "error"):
                        continue

            label = f"[{done}] {champ_id}/{lane}"
            try:
                data = scrape_champion(slug, lane, args.tier)
                with db.connect(config.DB_PATH) as conn:
                    matchup_count, has_overall = db.store_scrape_result(
                        conn, {**data, "champion": champ_id}
                    )
                    if has_overall and matchup_count > 0:
                        db.mark_scrape_run(conn, champ_id, lane, args.tier, "ok",
                                           f"{matchup_count} matchups")
                        successes += 1
                        status = "ok"
                    else:
                        db.mark_scrape_run(conn, champ_id, lane, args.tier, "empty",
                                           f"overall={has_overall} matchups={matchup_count}")
                        status = "empty"
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed else 0
                print(f"{label}: {status} (matchups={matchup_count}, "
                      f"avg {rate:.2f} combos/s, successes={successes})")
            except Exception as e:
                traceback.print_exc()
                with db.connect(config.DB_PATH) as conn:
                    db.mark_scrape_run(conn, champ_id, lane, args.tier, "error", str(e)[:200])
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
