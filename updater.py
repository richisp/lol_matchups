"""Auto-update the .exe from the latest GitHub release.

Strategy:
  1. On startup the app calls `check_and_apply()` (best-effort, short timeout).
  2. We hit the GitHub Releases API for the most recent app release (tags
     matching `vX.Y.Z`, ignoring the `db-latest` tag the crawler uses).
  3. If the release version is newer than the embedded one, we download the
     new .exe to a temp file alongside the current .exe.
  4. Windows can't overwrite a running .exe, so we spawn a small detached
     "swap" command (a .bat) that waits for our PID to exit, swaps the file,
     and relaunches. We then exit ourselves.

If anything fails we silently keep running the current version.

Only active when running as a frozen PyInstaller .exe (`sys.frozen`); when
running from source we skip the check entirely.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

import config
from version import __version__

log = logging.getLogger(__name__)

VERSION_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _list_app_releases(repo: str, timeout: float) -> list[dict]:
    """Return all releases tagged like vX.Y.Z, newest first."""
    r = httpx.get(
        f"https://api.github.com/repos/{repo}/releases",
        timeout=timeout,
        params={"per_page": 30},
        follow_redirects=True,
    )
    r.raise_for_status()
    return [rel for rel in r.json() if VERSION_RE.match(rel.get("tag_name", ""))]


PORTABLE_EXE_NAME = "lol-draft-helper.exe"


def _find_exe_asset(release: dict) -> dict | None:
    """Locate the portable .exe asset on the release.

    Each release has two .exe assets: the portable PyInstaller binary
    (`lol-draft-helper.exe`) and the Inno Setup installer
    (`lol-draft-helper-setup-X.Y.Z.exe`). The auto-updater wants the former
    — swapping in the installer would replace the running app with a setup
    wizard and break things. Match by exact name, not extension.
    """
    for a in release.get("assets", []):
        if a.get("name", "").lower() == PORTABLE_EXE_NAME:
            return a
    return None


def _download(url: str, dest: Path, timeout: float) -> None:
    # GitHub release-asset URLs 302-redirect to a signed CDN URL — follow_redirects
    # must be on or we'd just receive the redirect response and try to write that.
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(64 * 1024):
                f.write(chunk)


def _spawn_swap(current_exe: Path, new_exe: Path) -> None:
    """Spawn a detached cmd that waits for us to exit, swaps in the new .exe,
    and relaunches. We then exit ourselves.

    The swap can race with antivirus / Windows Search holding a transient file
    lock on the just-exited .exe, so the script retries move several times and
    logs to .update.log alongside the .exe — that file is invaluable when an
    update silently fails.
    """
    pid = os.getpid()
    bat = current_exe.parent / ".update.bat"
    # NB: `if errorlevel N` is "errorlevel >= N", so `not errorlevel 1` means
    #     errorlevel == 0 (= command succeeded / find matched).
    bat.write_text(
        f"""@echo off
setlocal
set "NEW_EXE={new_exe}"
set "CUR_EXE={current_exe}"
set "LOG=%~dp0.update.log"
set MAX_RETRIES=30

(echo [%date% %time%] update.bat start, waiting for PID {pid}) >> "%LOG%"

:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
(echo [%date% %time%] PID gone, attempting swap) >> "%LOG%"

set RETRIES=0
:try_move
move /Y "%NEW_EXE%" "%CUR_EXE%" >> "%LOG%" 2>&1
if not errorlevel 1 goto move_done
set /a RETRIES+=1
if %RETRIES% GEQ %MAX_RETRIES% (
    (echo [%date% %time%] giving up after %MAX_RETRIES% retries) >> "%LOG%"
    exit /b 1
)
(echo [%date% %time%] move failed, retry %RETRIES%) >> "%LOG%"
timeout /t 1 /nobreak >nul
goto try_move

:move_done
(echo [%date% %time%] swap done, refreshing icon cache + relaunching) >> "%LOG%"
ie4uinit.exe -show >nul 2>&1
start "" "%CUR_EXE%"
del "%~f0"
""",
        encoding="utf-8",
    )
    # CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS = 0x00000200 | 0x00000008
    DETACHED = 0x00000008
    NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=DETACHED | NEW_PROCESS_GROUP,
        close_fds=True,
    )


def check_and_apply(repo: str | None = None, timeout: float = 5.0) -> bool:
    """Check for a newer app release and, if found, swap-and-relaunch.

    Returns True if an update is in progress (the caller should exit). Returns
    False if no update is available, the check failed, or we're not frozen.
    """
    if not _is_frozen():
        log.debug("updater: not running as frozen .exe — skipping check.")
        return False

    repo = repo or config.GITHUB_REPO
    try:
        releases = _list_app_releases(repo, timeout)
    except (httpx.HTTPError, ValueError) as e:
        log.info("updater: release lookup failed — %s", e)
        return False
    if not releases:
        log.info("updater: no app releases tagged vX.Y.Z found.")
        return False

    latest = releases[0]
    tag = latest["tag_name"]
    m = VERSION_RE.match(tag)
    if not m:
        return False
    remote_v = m.group(1)
    if _parse(remote_v) <= _parse(__version__):
        log.info("updater: already on latest (%s).", __version__)
        return False

    asset = _find_exe_asset(latest)
    if not asset:
        log.info("updater: no .exe asset on release %s.", tag)
        return False

    current_exe = Path(sys.executable)
    new_exe = current_exe.with_name(current_exe.stem + f".{remote_v}.new.exe")
    log.info("updater: downloading %s → %s", asset["name"], new_exe)
    try:
        _download(asset["browser_download_url"], new_exe, timeout=120.0)
    except (httpx.HTTPError, OSError) as e:
        log.warning("updater: download failed — %s", e)
        try:
            if new_exe.exists():
                new_exe.unlink()
        except OSError:
            pass
        return False

    log.info("updater: scheduling swap to %s and relaunch.", remote_v)
    try:
        _spawn_swap(current_exe, new_exe)
    except OSError as e:
        log.warning("updater: failed to spawn swap script — %s", e)
        return False

    # Give the spawn a moment to start before we exit.
    time.sleep(0.2)
    return True
