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
| [launcher.py](launcher.py) | Entry point. Auto-update → DB sync → attribute self-heal (`fetch_attributes.ensure_attributes`) → Flask thread → pywebview window. |
| [app.py](app.py) | Flask app. Routes: `/` (champion attribute browser: classes, 0-3 ratings, comp fits — one row per champion; lane tabs filter by champion_stats presence), `/draft` (helper), `/champion/<name>`, `/api/lcu`, `/api/settings` (get/set League install path), `/api/overrides` (persist per-champion attribute overrides). Holds the scoring math + `team_avg_score` (mean fit across a team's picks). |
| [config.py](config.py) | Constants: `POSITIONS`, `LANES`, `LANE_TO_POSITION`, `LCU_POSITION_MAP`, `COUNTER_WEIGHTS`, `SYNERGY_WEIGHTS`, `BLIND_PICK_BAD_WR_THRESHOLD`, `TEAM_COMPS`/`COMP_LABELS`/`SUBCLASS_COMP_FIT`/`ENGAGE_SUBCLASSES` (team-comp classification). PyInstaller-aware `DB_PATH`. Also user-settings persistence (`SETTINGS_PATH`/`settings.json` next to the .exe, `get_setting`/`set_setting`). |
| [db.py](db.py) | SQLite schema + upserts. Tables: `champion_stats`, `matchups`, `champion_attributes`, `scrape_runs`. WAL mode. |
| [fetch_attributes.py](fetch_attributes.py) | Fetches Riot-authored champion attributes into `champion_attributes`: subclass tags + 0-3 ratings from Meraki Analytics (LoL-wiki JSON mirror), canonical names from Data Dragon, CommunityDragon fallback for champs Meraki lacks (new releases). Upsert-only — a failed fetch leaves old rows; runs in crawl.yml with `continue-on-error`. Exits 1 if <150 champs matched (structural breakage guard). `ensure_attributes()` is the launcher's self-heal: no-op when the table is populated, full fetch otherwise. |
| [lcu.py](lcu.py) | LoL client integration. Reads `lockfile`, polls `/lol-champ-select/v1/session`, infers lanes for picks without `assignedPosition`. `find_lockfile()` precedence: `LEAGUE_INSTALL_PATH` env → saved `league_path` setting → default install paths. `get_gameflow_phase()` powers the "keep board through the game" UI. |
| [scrape_lolalytics.py](scrape_lolalytics.py) | Playwright scraper for champion build pages (clicks `data-type=strong_counter` and `good_synergy` tabs, scrolls carousels). `scrape_champion_on_page` reuses an existing `Page`; `scrape_champion` is a CLI wrapper that owns its own browser. `fetch_champion_list()` pulls every champion's display name + lolalytics URL slug from Riot's Data Dragon — the crawler uses this instead of lolalytics' tier list, so a tier-list layout regression can no longer silently drop champs. With `min_pickrate_for_matchups>0`, the scraper extracts overall stats first and short-circuits before the (slow) tab work if pickrate is below threshold (`_skipped_low_pr=True` flag in result). |
| [crawl_champions.py](crawl_champions.py) | CLI driver. Pulls the champion list from Data Dragon once, then runs the 5 lanes in parallel `ThreadPoolExecutor` workers (one Chromium each, page reused across all candidates). For each (champ, lane): if pickrate < `--min-pickrate` (default 0.1), no DB write and no `scrape_runs` row (next full crawl re-checks); otherwise persist + mark `ok`/`empty`. Retries transient navigation timeouts with 2s/8s backoff. Exits non-zero if Data Dragon fetch failed or any lane scrapes <`LANE_OK_FLOOR` (20) champs, so the workflow's upload step is skipped on structural breakage. |
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

champion_attributes (champion_name, riot_id, roles, damage, toughness, control,
                     mobility, utility, ability_reliance, difficulty,
                     adaptive_type, fetched_at)
   PK: champion_name  — no lane/tier dimension (champion-intrinsic; from fetch_attributes.py)
   roles: comma-joined Riot class/subclass tags, e.g. 'FIGHTER,JUGGERNAUT,TANK'
   (db.init_db carries additive/subtractive column migrations — e.g. the
    attack_type/attack_range columns are actively DROPped from older DBs)

scrape_runs (champion_name, lane, tier, status, last_attempt, note)
   PK: (champion_name, lane, tier)
   status: 'ok' | 'empty' | 'error'
```

Lane keys are canonical positions: `TOP`, `JUNGLE`, `MID`, `BOT`, `SUPPORT`. Lolalytics URL slugs (`top`/`jungle`/`middle`/`bottom`/`support`) and LCU's `assignedPosition` (`utility` for support) are mapped via `config.LANE_TO_POSITION` / `config.LCU_POSITION_MAP`.

## Scoring math (in [app.py](app.py))

All scoring lives in `compute_draft_scores()` and `compute_pick_breakdown()`.

```
fit = 50.0
    + Σ (counter_winrate − 50) × COUNTER_WEIGHTS[my_lane][enemy_lane]/100 × conf
    + Σ (synergy_winrate − 50) × SYNERGY_WEIGHTS[my_lane][ally_lane]/100 × conf

conf = 0 if games < 30 else min(1, games / 100)   # sample-size confidence
```

- Base is **always 50** — individual champion winrate is deliberately excluded so rankings turn on counter/synergy contributions, not meta strength. (An expected-value term for unpicked lanes was tried and reverted 2026-07: even with sample-size damping and a pickrate trust prior it made early-draft scores track champion/meta strength more than the user wanted.)
- **Sample-size confidence** (`_games_confidence`): matchups with `games < MIN_MATCHUP_GAMES` (30) are no-data (contrib 0); above the floor each contribution is scaled by `min(1, games / GAMES_FULL_CONFIDENCE)` (100) — linear ramp, so a 35-game row counts at ×0.35 and 100+ games at full weight. Both constants live in [config.py](config.py). Damped rows show an amber `×0.NN` next to the games count in breakdown tooltips.
- **Reverse-matchup fallback** (`_invert_winrate`): lolalytics filters low-pickrate champs off an opponent's counter list, so `A vs B` can be missing while `B vs A` exists. When the direct row is absent, lookups fall back to the reverse row — counters use `100 − winrate` (zero-sum), synergy carries over unchanged (symmetric). Applied in `compute_draft_scores._fetch_matchups`, `compute_pick_breakdown._matchup`, and `get_matchups`; such values carry an `inferred` flag and render with a `~` prefix. **Not** applied in `compute_blind_risk` (it's pickrate-weighted over popular matchups; the only missing rows are low-pickrate, hence negligible, and the reverse row's pickrate is the wrong direction).
- `compute_blind_risk()`: popularity-weighted exposure to bad matchups (winrate < `BLIND_PICK_BAD_WR_THRESHOLD = 48.0`), summed over two sources — **counter** exposure across enemy lanes (weighted by `COUNTER_WEIGHTS`, excluding lanes already filled by the enemy via `known_enemy_lanes`) **and** bad-**synergy** exposure across ally lanes (weighted by `SYNERGY_WEIGHTS`, excluding locked allies via `known_ally_lanes`). The summed score is then halved. UI color bands (in `draft.html`): green `<10`, red `>40` — recalibrated to the quartiles of the score distribution after `COUNTER_WEIGHTS`/`SYNERGY_WEIGHTS` moved to proximity-derived global-max normalization (the old `<5`/`>10` bands, set for a counter-only metric, painted ~72% of champs red). Lower = safer blind pick. Risk is display-only — it does not feed the fit score. The general champion list passes no team context, so it reflects raw all-lanes exposure.
- `COUNTER_WEIGHTS` / `SYNERGY_WEIGHTS` rows sum to 100. Synergy diagonal is 0 (no teammate in your own lane).

## Team-comp classification (in [app.py](app.py), display-only)

Champions get a soft 0–1 fit per comp archetype (`engage`, `poke`, `protect` — three macro comps; dive/pick/wombo are engage variations, f2b/peel/disengage are the protect family) from `compute_comp_fits()`: base = **max** over the champ's Riot **subclass** tags in `config.SUBCLASS_COMP_FIT`, multiplied by hybrid damping (`SUBCLASS_COUNT_DAMPING`: 1 subclass ×1.0, 2 ×0.7, 3+ ×0.5 — a pure Marksman like Jinx is the real protect-comp hypercarry; a triple hybrid like Quinn is a jack-of-all-trades and gets diluted), plus small attribute nudges (mobility/control → engage, utility → protect), clamped to [0,1]. Base classes (FIGHTER/MAGE/TANK/SUPPORT) are dropped everywhere — every champion has ≥1 subclass tag and subclasses are strictly more accurate. Damping is uniform per champion because the wiki's subclass list is alphabetical (no primary/secondary ordering exists). Subclasses carry what ratings can't: CC *direction* (Vanguard engages, Warden peels).

**User attribute overrides**: runes/items can flip a champ's effective profile (tank vs full-AP Gragas), so every filled slot shows its attribute line (subclass label + AD/AP icon + five wiki rating icons each with 3 dots — icons bundled in `static/img/` from the LoL wiki via Fandom's CDN; ✎+amber label/dots when overridden) with a ✎ editor. Empty slots render the same row as a dimmed placeholder so slot height never changes when a champ fills in — subclass, the five ratings, AD/AP. Apply POSTs `{champion, override}` to `/api/overrides`, which persists to settings.json (`attr_overrides`) — overrides survive games and app restarts (deliberately NOT wiped by `clearDraftBoard()`); Reset (`override: null`) removes one. `apply_attr_overrides()` patches a copy of the cached attrs and re-derives comp fits; applied in both the draft route and the champion index.

- `team_comp_profile()` — mean comp fit per archetype across a team's picks, leading comp(s), and **team attribute bars** (`bars` + `dmg_split`): Damage/Frontline/CC (average rating ÷ 3), Engage/Peel (provider count, full at 2), AD/AP (dealer split over damage-rating-≥2 picks, gold/violet segments). Bars are always visible; each carries a `warn` flag that colors it red when the gap condition fires ("no damage-3 pick", "no toughness-3 pick", "mean control < 1.4", "no `ENGAGE_SUBCLASSES` member", "Marksman with no `PEEL_SUBCLASSES`/utility-≥2 pick", "fewer than 2 dealers of either adaptive type") — armed only from 3 picks up. Rendered under the comp rows in each team's panel; both teams get them.
- `comp_alignment()` — the draft table's sortable **Comp** column: the candidate's mean fit over the *already-picked allies'* leading comp(s) (active slot excluded). `—` until an ally is picked. Deliberately **not** a dot product over all comps: that's a weighted average, and flat-high generalists (the wiki tags most mobile marksmen ASSASSIN+MARKSMAN → high fit almost everywhere via max()) would top the list for every team regardless of its direction (the "Quinn always #1" bug). The toolbar's **Comp picker** (`?comp=` param) overrides auto-detection: the column becomes the candidate's fit for the selected comp — works with zero picks (draft flexible champs early toward a planned comp), the target row is highlighted amber (◎) in your team's panel, and the column header shows `Comp · <label>`.
- Comp fits do **not** feed the fit score — purely informational for now.
- The draft page's recs pane has two tabs (`?view=` param): **Winrates** (matchup table: Score/WR/PR/vs/w//Comp/Risk/…) and **Attributes** (same candidates as subclass tags + attribute dots + all three comp fits + the draft Score). Both share the same signed `sort` param.
- **Enemy scout mode** (`?active_side=enemy`): every slot on both teams has a "pick here" strip; selecting an enemy slot flips the recs to that side's perspective (ally/opponent swap into `compute_draft_scores`, Comp column vs *their* allies, impact badges swap teams). Enemy active slot glows red; `clearDraftBoard()` resets the side to `my` on a new champ select.
- The champion index (`/`) is the same attribute browse view over *all* champions — but the top nav was removed (the draft page is the app now), so it's only reachable by URL. One row per champion; lane tabs filter by champion_stats presence.
- `get_champion_attributes()` is cached for the process lifetime (like available tiers) and returns `{}` when the `champion_attributes` table is missing (DB snapshot predating attributes), so all comp UI degrades to hidden/`—`.

## LCU integration ([lcu.py](lcu.py))

- Reads `C:\Riot Games\League of Legends\lockfile`. Format: `name:pid:port:password:protocol`. Lookup precedence: `LEAGUE_INSTALL_PATH` env → user-saved `league_path` (settings.json, set via the ⚙ panel in the draft toolbar → `/api/settings`) → default install paths. `league_path` accepts either the install folder or a direct path to the `lockfile`.
- Hits `https://127.0.0.1:<port>/lol-champ-select/v1/session` with HTTP basic auth `riot:<password>`, `verify=False`.
- `championPickIntent` (hover) AND `championId` (locked) both surface as picks.
- Picks without `assignedPosition` are placed by `best_lane_assignment()` — brute-force permutation maximizing summed pickrate over unoccupied lanes.
- Bans are read from `actions[]` (with `actorCellId` to split my/enemy) and fall back to structured `session["bans"]`.
- Frontend polls `/api/lcu?tier=...` every 2s; auto-fills slots only when the user hasn't typed an override. `/api/lcu` also returns the client `gameflow phase`.
- **Board persists through the game**: the draft board is *not* wiped when champ select ends. It stays visible (so you can still see who counters your team) across `InProgress`/post-game phases and is cleared only on the **rising edge** of the *next* champ select (`draft.js` tracks `wasInChampSelect`; the wipe is guarded by the auto-sync toggle). This is a change from the old "reset on leaving champ select" behavior.

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
- **`--only` bypasses the pickrate filter** in the crawler so off-meta picks (e.g. Veigar bot) are crawlable on demand. Without `--only`, the default `--min-pickrate=0.1` decides per (champ, lane) whether to bother extracting matchups.
- **Champion list is from Data Dragon, not lolalytics**: `scrape_lolalytics.fetch_champion_list()` hits Riot's `ddragon.leagueoflegends.com` for the canonical (name, slug) list. Riot id → lolalytics URL slug is `id.lower()` with one override (`MonkeyKing` → `wukong`); add to `LOLALYTICS_SLUG_OVERRIDES` if other mismatches appear. Data Dragon failure at crawl start is fatal (exit 1), preserving the live DB.
- **Stale rows can linger**: when a champion drops below `--min-pickrate` in a lane, the new crawl skips writing — but pre-existing `champion_stats`/`matchups` rows are not deleted. Acceptable for now since the app naturally weights by recency/games; revisit if the DB starts accumulating dead entries.
- **Crawler exits non-zero on structural breakage**: a fatal worker, Data Dragon fetch failure, or any lane with fewer than `LANE_OK_FLOOR` (20) successful scrapes makes `crawl_champions.py` exit 1. The workflow's `Generate version metadata` and `Upload to db-latest release` steps are then skipped (default GH Actions behavior on previous-step failure), so the broken DB never replaces the released one. The floor is bypassed when `--only` or `--skip-failed` is set since both legitimately produce small batches.
- **`scrape_runs` status split**: `ok` = data stored; `empty` = page loaded but no matchups (the default crawl re-attempts these every run, `--skip-failed` skips them); `error` = transient/structural failure, including the case where `overall` stats failed to extract (the page didn't load properly). Combos that scored below `--min-pickrate` are intentionally **not** recorded so every full crawl re-checks them (PR may climb back above threshold). The workflow's `failures` mode `DELETE`s rows where `status IN ('error','empty')` so the next crawl re-attempts them.
- **Auto-updater testing knobs**: `LOL_MATCHUPS_FORCE_UPDATE=1` and `LOL_MATCHUPS_VERSION_OVERRIDE=0.0.0`. Both are stripped from the relaunched child env to prevent update loops.
- **Brand-new champions lag on Meraki**: Data Dragon lists a champ at release, but the wiki/Meraki mirror can trail by days/weeks. `fetch_attributes.py` falls back to CommunityDragon (Riot's own client data, never stale) for those — full attribute ratings, but roles are base classes only, so comp fits rely on the tags that overlap the subclass table (ASSASSIN, MARKSMAN). Once Meraki adds the champ, its richer subclass data takes over on the next fetch. CommunityDragon 403s the default urllib user agent — `_fetch_json` sends a browser UA.
- **DB sync replaces the whole file** — anything written only to the local DB (e.g. a manual `fetch_attributes.py` run) is lost the next time `db-latest` is newer. Champion attributes survive because (a) the crawl workflow bakes them into the snapshot and (b) the launcher's `ensure_attributes()` self-heals when they're missing anyway.
- **Descending sorts must keep no-data rows last**: sort keys use `((val is None) != desc, val or 0)` — a plain `(val is None, val)` key puts Nones *first* under `reverse=True` (bug originally hit the Comp column for champs without attribute rows).
