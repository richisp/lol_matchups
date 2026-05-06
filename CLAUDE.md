# LoL Draft Helper — Project Context

A Windows desktop app that helps League of Legends players pick champions in draft. Uses lolalytics scrape data; suggests picks based on counter/synergy matchups against the current champ-select state pulled live from the LoL client.

## Architecture

```
                ┌────────────────────────────────────────────┐
                │  GitHub Actions: crawl.yml (every 2 days)  │
                │  Playwright → lolalytics → lolalytics.db   │
                │  → uploads to "db-latest" release          │
                └────────────────┬───────────────────────────┘
                                 │ DB sync at startup
                                 ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Desktop app (PyInstaller .exe + Inno Setup installer)  │
  │                                                         │
  │  launcher.py → updater (auto-update) → sync (DB pull)   │
  │             → Flask (app.py) in thread                  │
  │             → pywebview window (WebView2 on Windows)    │
  │                                                         │
  │  Flask reads lolalytics.db (sqlite, next to .exe)       │
  │  + polls local LoL client via LCU API every 2s          │
  └─────────────────────────────────────────────────────────┘
```

Two release tracks on the same GitHub repo (`richisp/lol_matchups`):
- **`vX.Y.Z` tags** — app releases (the `.exe` and installer). The "Latest" release in the GitHub UI.
- **`db-latest` tag** — rolling DB snapshot from the crawler workflow. `make_latest: false` so it doesn't shadow app releases.

## File map

| File | Role |
|---|---|
| [launcher.py](launcher.py) | Entry point. Auto-update → DB sync → Flask thread → pywebview window. |
| [app.py](app.py) | Flask app. Routes: `/` (champion list), `/draft` (helper), `/champion/<name>`, `/api/lcu`. Holds the scoring math. |
| [config.py](config.py) | Constants: `POSITIONS`, `LANES`, `LANE_TO_POSITION`, `LCU_POSITION_MAP`, `COUNTER_WEIGHTS`, `SYNERGY_WEIGHTS`, `BLIND_PICK_BAD_WR_THRESHOLD`. PyInstaller-aware `DB_PATH`. |
| [db.py](db.py) | SQLite schema + upserts. Tables: `champion_stats`, `matchups`, `scrape_runs`. WAL mode. |
| [lcu.py](lcu.py) | LoL client integration. Reads `lockfile`, polls `/lol-champ-select/v1/session`, infers lanes for picks without `assignedPosition`. |
| [scrape_lolalytics.py](scrape_lolalytics.py) | Playwright scraper. `fetch_lane_pool()` for tier-list, `scrape_champion()` for matchups (clicks `data-type=strong_counter` and `good_synergy` tabs, scrolls carousels). |
| [crawl_champions.py](crawl_champions.py) | CLI driver around the scraper. Iterates lanes × popular champs, persists to DB via `db.store_scrape_result`. |
| [sync.py](sync.py) | Pulls newer `lolalytics.db` from `db-latest` release on startup. Silent on failure. Must run before any sqlite connection (Windows file-lock). |
| [updater.py](updater.py) | Auto-update. `/releases/latest` → download `lol-draft-helper.exe` → spawn detached PowerShell (`-EncodedCommand`) that waits for PID exit, swaps file, relaunches via `ShellExecute`. Only active when `sys.frozen`. |
| [version.py](version.py) | Single source of truth: `__version__`. Release workflow rewrites this from the git tag. |
| [lol_matchups.spec](lol_matchups.spec) | PyInstaller spec. Bundles `templates/` + `static/`; excludes `playwright`/`scrape_lolalytics`/`crawl_champions`. Generates `icon.ico` from `heimerdinger-emote.webp`. |
| [installer.iss](installer.iss) | Inno Setup installer script. |
| [templates/](templates/) | `base.html`, `index.html` (champ list), `draft.html` (main UI), `champion.html` (per-champ matchups). |
| [static/css/draft.css](static/css/draft.css), [static/js/draft.js](static/js/draft.js) | Draft page styling + AJAX-refresh / LCU-poll loop. |
| [.github/workflows/crawl.yml](.github/workflows/crawl.yml) | Cron: Playwright crawl on Linux runner, uploads to `db-latest`. |
| [.github/workflows/release.yml](.github/workflows/release.yml) | On `v*.*.*` tag: build `.exe` + installer on Windows, attach to release. |
| [.github/scripts/db_version.py](.github/scripts/db_version.py) | Generates `db-version.json` + `db-version-body.md` after a crawl. |

## Database (SQLite, `lolalytics.db`)

```sql
champion_stats (champion_name, lane, tier, winrate, pickrate, banrate, games, tier_badge, scraped_at)
   PK: (champion_name, lane, tier)

matchups (champion_name, champion_lane, opponent_name, opponent_lane,
          matchup_type, tier, winrate, pickrate, games, scraped_at)
   PK: (champion_name, champion_lane, opponent_name, opponent_lane, matchup_type, tier)
   matchup_type: 'counter' | 'synergy'

scrape_runs (champion_name, lane, tier, status, last_attempt, note)
   PK: (champion_name, lane, tier)
   status: 'ok' | 'empty' | 'error'
```

Lane keys are canonical positions: `TOP`, `JUNGLE`, `MID`, `BOT`, `SUPPORT`. Lolalytics URL slugs (`top`/`jungle`/`middle`/`bottom`/`support`) and LCU's `assignedPosition` (`utility` for support) are mapped via `config.LANE_TO_POSITION` / `config.LCU_POSITION_MAP`.

## Scoring math (in [app.py](app.py))

All scoring lives in `compute_draft_scores()` and `compute_pick_breakdown()`.

```
fit = 50.0
    + Σ (counter_winrate − 50) × COUNTER_WEIGHTS[my_lane][enemy_lane]/100
    + Σ (synergy_winrate − 50) × SYNERGY_WEIGHTS[my_lane][ally_lane]/100
```

- Base is **always 50** — individual champion winrate is deliberately excluded so rankings turn on counter/synergy contributions, not meta strength.
- Matchups with `games < 30` are treated as no-data (contrib 0).
- `compute_blind_risk()`: weighted sum of `pickrate` of bad-matchup opponents (counter winrate < `BLIND_PICK_BAD_WR_THRESHOLD = 48.0`), excluding lanes already filled by the enemy. Lower = safer blind pick.
- `COUNTER_WEIGHTS` / `SYNERGY_WEIGHTS` rows sum to 100. Synergy diagonal is 0 (no teammate in your own lane).

## LCU integration ([lcu.py](lcu.py))

- Reads `C:\Riot Games\League of Legends\lockfile` (override via `LEAGUE_INSTALL_PATH` env). Format: `name:pid:port:password:protocol`.
- Hits `https://127.0.0.1:<port>/lol-champ-select/v1/session` with HTTP basic auth `riot:<password>`, `verify=False`.
- `championPickIntent` (hover) AND `championId` (locked) both surface as picks.
- Picks without `assignedPosition` are placed by `best_lane_assignment()` — brute-force permutation maximizing summed pickrate over unoccupied lanes.
- Bans are read from `actions[]` (with `actorCellId` to split my/enemy) and fall back to structured `session["bans"]`.
- Frontend polls `/api/lcu?tier=...` every 2s; auto-fills slots only when the user hasn't typed an override.

## Sort param convention

URL `sort=` is always **signed**: `+winrate` (asc) or `-winrate` (desc). Bare `winrate` is normalized to its column's natural default and re-emitted as signed. This lets the JS distinguish "user toggled to ASC" from "no preference yet" — see comments in `parse_sort()` in [app.py](app.py).

## Common commands

Linux/WSL (crawling):
```bash
.venv/bin/python crawl_champions.py                    # full crawl
.venv/bin/python crawl_champions.py --only Swain --lanes bottom
.venv/bin/python app.py                                # dev UI on :5000
```

Windows (running/building):
```powershell
.venv\Scripts\python launcher.py                       # native window from source
.venv\Scripts\pyinstaller lol_matchups.spec            # build dist\lol-draft-helper.exe
```

Full reference: [COMMANDS.md](COMMANDS.md), [WINDOWS.md](WINDOWS.md).

## Gotchas

- **Don't open a sqlite connection before `sync.sync_db()`** — Windows can't replace an open `.db` file.
- **Frozen vs source paths**: `config.APP_DIR` resolves to `Path(sys.executable).parent` when `sys.frozen`, else the source dir. `app.py` similarly switches `template_folder` to `sys._MEIPASS` when frozen.
- **Cache-busting**: `app.py` sets `Cache-Control: no-store` on all responses; `launcher.py` appends `?_v=<epoch>` to the URL. Without these, WebView2 serves stale HTML across auto-updates.
- **Empty scrapes don't wipe data**: `db.store_scrape_result` only deletes existing matchup rows for a (champ, lane, tier, type) when the new scrape returned non-empty data for that type. Layout regressions on lolalytics won't nuke the DB.
- **`--only` bypasses pickrate filter** in the crawler so off-meta picks (e.g. Veigar bot) are crawlable.
- **Auto-updater testing knobs**: `LOL_MATCHUPS_FORCE_UPDATE=1` and `LOL_MATCHUPS_VERSION_OVERRIDE=0.0.0`. Both are stripped from the relaunched child env to prevent update loops.
