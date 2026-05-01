"""Generate `db-version.json` and `db-version-body.md` after a crawl.

The JSON is the machine-readable manifest the desktop app reads to decide
whether to download a fresher DB. The markdown is the release body for
humans browsing GitHub Releases.
"""

import datetime
import json
import sqlite3
from pathlib import Path

DB = Path("lolalytics.db")
JSON_OUT = Path("db-version.json")
MD_OUT = Path("db-version-body.md")

now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
updated_at = now.isoformat().replace("+00:00", "Z")

with sqlite3.connect(DB) as conn:
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    champs   = count("SELECT COUNT(*) FROM champion_stats")
    matchups = count("SELECT COUNT(*) FROM matchups")
    ok       = count("SELECT COUNT(*) FROM scrape_runs WHERE status='ok'")
    empty    = count("SELECT COUNT(*) FROM scrape_runs WHERE status='empty'")
    err      = count("SELECT COUNT(*) FROM scrape_runs WHERE status='error'")

manifest = {
    "schema_version": 1,
    "updated_at": updated_at,
    "champion_stats_rows": champs,
    "matchup_rows": matchups,
    "scrape_ok": ok,
    "scrape_empty": empty,
    "scrape_error": err,
    "size_bytes": DB.stat().st_size,
}

JSON_OUT.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

MD_OUT.write_text(
    f"""\
Auto-generated DB snapshot — `{updated_at}`.

| Metric | Count |
| --- | ---: |
| Champion-stat rows | {champs:,} |
| Matchup rows | {matchups:,} |
| Scrape OK | {ok:,} |
| Scrape empty | {empty:,} |
| Scrape error | {err:,} |
| DB size | {DB.stat().st_size / 1_000_000:.1f} MB |
""",
    encoding="utf-8",
)

print(json.dumps(manifest, indent=2))
