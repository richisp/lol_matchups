# Commands

All commands assume you run from the project root (`/home/richis/lol_matchups`).

## Setup (one-time)

```bash
# Create venv & install deps
python3 -m venv .venv
.venv/bin/pip install -e .

# Install headless Chromium for Playwright
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
```

## Start the UI

```bash
.venv/bin/python app.py
```

Opens on **http://localhost:5000**. Auto-reloads on file changes (Flask debug mode).

## Crawl matchup data into the DB

> All combos already marked `ok` in `scrape_runs` are skipped automatically.
> Empty/error runs are retried by default; pass `--skip-failed` to skip those too.

```bash
# Full crawl: every popular (PR > 1%) champion in every lane
.venv/bin/python crawl_champions.py

# Run in background, write log to crawl.log
nohup .venv/bin/python crawl_champions.py > crawl.log 2>&1 &
tail -f crawl.log

# Single lane (still respects PR threshold)
.venv/bin/python crawl_champions.py --lanes bottom
.venv/bin/python crawl_champions.py --lanes top,middle

# Single champion (PR threshold bypassed — works even for off-meta picks)
.venv/bin/python crawl_champions.py --only Swain --lanes bottom
.venv/bin/python crawl_champions.py --only Swain,Veigar --lanes bottom,middle

# Higher PR threshold (fewer champs, faster)
.venv/bin/python crawl_champions.py --min-pickrate 2

# Different rank bracket
.venv/bin/python crawl_champions.py --tier all
.venv/bin/python crawl_champions.py --tier challenger

# Stop after N successful scrapes
.venv/bin/python crawl_champions.py --limit 10
```

**Lane names:** `top`, `jungle`, `middle`, `bottom`, `support`
**Tiers:** `emerald_plus` (default), `all`, `platinum_plus`, `gold_plus`, `diamond_plus`, `master_plus`, `challenger`, `grandmaster`, `master`

## One-off scrape to a text file (no DB)

```bash
# Open browser visibly
.venv/bin/python scrape_lolalytics.py swain bottom
.venv/bin/python scrape_lolalytics.py garen top emerald_plus

# Headless (no browser window)
HEADLESS=1 .venv/bin/python scrape_lolalytics.py swain bottom
```

Output → `lolalytics_<champ>_<lane>.txt`.

## Inspect the database

```bash
# How much data do we have?
sqlite3 lolalytics.db "SELECT COUNT(*) FROM champion_stats;"
sqlite3 lolalytics.db "SELECT COUNT(*) FROM matchups;"
sqlite3 lolalytics.db "SELECT lane, COUNT(*) FROM champion_stats GROUP BY lane;"

# Crawl status summary
sqlite3 lolalytics.db "SELECT status, COUNT(*) FROM scrape_runs GROUP BY status;"

# Which combos failed?
sqlite3 lolalytics.db "SELECT champion_name, lane, status, note FROM scrape_runs WHERE status != 'ok';"

# Top winrate champs in a given lane
sqlite3 lolalytics.db "SELECT champion_name, winrate, pickrate, games FROM champion_stats WHERE lane='BOT' AND tier='emerald_plus' ORDER BY winrate DESC LIMIT 10;"

# Connect interactively
sqlite3 lolalytics.db
```

## Reset / recrawl

```bash
# Force re-crawl of a specific champion+lane (delete its run state)
sqlite3 lolalytics.db "DELETE FROM scrape_runs WHERE champion_name='Swain' AND lane='bottom';"

# Re-attempt all error/empty entries
sqlite3 lolalytics.db "DELETE FROM scrape_runs WHERE status IN ('error', 'empty');"

# Wipe everything and start over
rm lolalytics.db lolalytics.db-wal lolalytics.db-shm
.venv/bin/python crawl_champions.py  # rebuilds schema
```
