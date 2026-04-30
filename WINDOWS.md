# Windows: run from source & build the .exe

## Prerequisites

- Windows 10 or 11 (Edge WebView2 is preinstalled — no separate runtime needed).
- **Python 3.12** specifically (3.13 mostly works; **avoid 3.14** — pythonnet doesn't have wheels for it yet, and pip will try to source-build and fail). Get it from https://www.python.org/downloads/release/python-3128/ and tick **"Add Python to PATH"**.
- Git for Windows (optional, only if cloning the repo from there).

## One-time setup

Open **PowerShell** in the project folder.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -e .[build]
```

That installs Flask, pywebview, pyinstaller, etc. (Playwright is a heavy dependency and only needed for crawling — see below.)

## Run from source

You need a populated `lolalytics.db` in the project root. If you've been crawling on WSL, copy that file over to the Windows project folder.

```powershell
.venv\Scripts\python launcher.py
```

A native window opens with the draft helper.

(For the old champion-list page, `.venv\Scripts\python app.py` also works and serves at http://localhost:5000.)

## Build the .exe

```powershell
.venv\Scripts\pyinstaller lol_matchups.spec
```

Output: `dist\lol-draft-helper.exe` (single file, ~30–50 MB).

To use it, place `lolalytics.db` in the same folder as the .exe and double-click.

## Crawling on Windows (optional)

The crawler is heavy — it downloads Chromium (~150 MB) and pulls live data from lolalytics. Most users will just receive a pre-built `lolalytics.db`. If you want to crawl on Windows:

```powershell
.venv\Scripts\pip install playwright
.venv\Scripts\playwright install chromium
.venv\Scripts\python crawl_champions.py
```

Same flags as on Linux — see `COMMANDS.md`.

## LCU auto-fill

When the LoL client is running on the same machine, the draft helper auto-detects champ select state every 2 seconds:

- Reads `C:\Riot Games\League of Legends\lockfile` (override path with the `LEAGUE_INSTALL_PATH` env var if you installed elsewhere).
- Polls the local LCU API for the current champ select session.
- Auto-fills picks, bans, and your lane. The "active" slot follows your local cell.

The status badge in the top-right shows one of:

- **LCU: client not running** — no lockfile found.
- **LCU: not in champ select** — client up but you're in lobby/game/menu.
- **LCU: in champ select** — live, syncing every 2s.

Uncheck **auto-sync** to freeze the form for manual experimentation while still seeing the badge update.

## Troubleshooting

- **Blank window / WebView2 missing**: install Microsoft's Edge WebView2 Evergreen Runtime. (Win10 ≥ 21H2 and all of Win11 already include it.)
- **`No data yet — run crawl_champions.py first`**: `lolalytics.db` is missing or empty next to the `.exe` (or next to `launcher.py` when running from source).
- **PyInstaller build fails on a missing module**: add it to the `hiddenimports` list in `lol_matchups.spec` and rebuild.
