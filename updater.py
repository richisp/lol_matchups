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

import base64
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

# Testing knobs. These let you verify the auto-update pipeline end-to-end
# without re-tagging / re-publishing each time:
#   LOL_MATCHUPS_VERSION_OVERRIDE=0.0.0   — make the running app *report* this
#                                            version, so the updater treats
#                                            the current GitHub latest as a
#                                            real upgrade.
#   LOL_MATCHUPS_FORCE_UPDATE=1           — bypass the "is remote newer?" check
#                                            entirely. The updater will fetch
#                                            and apply the current latest even
#                                            when versions match.
# Both are read fresh on each check_and_apply() call.
_VERSION_OVERRIDE_ENV = "LOL_MATCHUPS_VERSION_OVERRIDE"
_FORCE_UPDATE_ENV = "LOL_MATCHUPS_FORCE_UPDATE"


def _local_version() -> str:
    return os.environ.get(_VERSION_OVERRIDE_ENV) or __version__


def _force_update() -> bool:
    return os.environ.get(_FORCE_UPDATE_ENV, "").lower() in ("1", "true", "yes")

log = logging.getLogger(__name__)

VERSION_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _latest_app_release(repo: str, timeout: float) -> dict | None:
    """Return the release GitHub marks as 'latest'.

    We deliberately use the dedicated /releases/latest endpoint rather than
    listing /releases and taking [0]: the list endpoint is eventually
    consistent and has been observed to omit a just-published release for
    several minutes, while /releases/latest is updated immediately when a
    workflow sets `make_latest: true`.
    """
    r = httpx.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        timeout=timeout,
        follow_redirects=True,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


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
    """Spawn a detached PowerShell that waits for us to exit, swaps in the new
    .exe, and relaunches. We then exit ourselves.

    Important detail: we pass the script inline via -EncodedCommand rather
    than -File. PowerShell's execution-policy checks only block loading a
    .ps1 file from disk — they don't apply to inline commands — and on
    locked-down machines (group policy, AllSigned, etc.) -ExecutionPolicy
    Bypass on the command line is itself overridden. -EncodedCommand
    sidesteps the whole issue.
    """
    pid = os.getpid()
    exe_stem = current_exe.stem  # "lol-draft-helper" (no extension)
    ps1 = current_exe.parent / ".update.ps1"
    self_path = ps1  # what the script should delete on exit (its own visible copy)
    script = f"""$ErrorActionPreference = 'Continue'
$pidToWait = {pid}
$exeName   = '{exe_stem}'
$cur       = '{current_exe}'
$new       = '{new_exe}'
$selfPath  = '{self_path}'
$log       = (Split-Path -Parent $cur) + '\\.update.log'

# Marker before any function/loop definitions so we know PS reached this
# script even if a later parse error kills it.
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] update invoked, ps_pid=$PID, target_pid=$pidToWait" | Out-File -Append -FilePath $log -Encoding utf8

function Log($msg) {{
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $log -Value $line -Encoding utf8
}}

Log "update start, waiting for PID $pidToWait / $exeName"

# Wait for the launcher to exit. Get-Process resolves by PID *and* we verify
# the process name — if Windows recycles the PID for an unrelated process,
# the name check rules it out instead of looping forever.
$waited = 0
while ($waited -lt 60) {{
    $p = Get-Process -Id $pidToWait -ErrorAction SilentlyContinue
    if (-not $p -or $p.ProcessName -ne $exeName) {{ break }}
    Start-Sleep -Seconds 1
    $waited++
}}
Log "proceeding to swap after $waited s wait"

# Retry the move — antivirus / Windows Search can hold transient file locks
# on the just-exited .exe.
$retries = 0
$swapped = $false
while ($retries -lt 30) {{
    try {{
        Move-Item -Force -LiteralPath $new -Destination $cur -ErrorAction Stop
        $swapped = $true
        break
    }} catch {{
        Log "move attempt $retries failed: $($_.Exception.Message)"
        Start-Sleep -Seconds 1
        $retries++
    }}
}}

if (-not $swapped) {{
    Log "giving up after $retries retries; leaving $new in place"
    Remove-Item -Force -LiteralPath $selfPath -ErrorAction SilentlyContinue
    exit 1
}}

Log "swap done, refreshing icon cache + relaunching"

# Refresh Explorer icon cache so the new icon takes effect immediately.
try {{
    Add-Type -Namespace W -Name S -MemberDefinition '[System.Runtime.InteropServices.DllImport("shell32.dll")] public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);'
    [W.S]::SHChangeNotify(0x08000000, 0, [System.IntPtr]::Zero, [System.IntPtr]::Zero)
}} catch {{}}

Start-Process -FilePath $cur
Remove-Item -Force -LiteralPath $selfPath -ErrorAction SilentlyContinue
"""

    # Save the script to disk for debug visibility (the user can inspect it
    # if anything goes wrong). The actual execution is via -EncodedCommand,
    # so it doesn't matter that this file might be blocked from running
    # directly.
    ps1.write_text(script, encoding="utf-8")

    # PowerShell -EncodedCommand expects a base64-encoded UTF-16-LE string.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    # Capture stdout/stderr to files so silent failures can't disappear.
    out_fh = open(current_exe.parent / ".update-stdout.log", "ab")
    err_fh = open(current_exe.parent / ".update-stderr.log", "ab")
    DETACHED = 0x00000008
    NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-EncodedCommand", encoded,
        ],
        stdout=out_fh,
        stderr=err_fh,
        creationflags=DETACHED | NEW_PROCESS_GROUP,
        close_fds=False,
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
    local_v = _local_version()
    forced = _force_update()
    if local_v != __version__:
        log.info("updater: VERSION_OVERRIDE=%s (real=%s).", local_v, __version__)
    if forced:
        log.info("updater: FORCE_UPDATE=1 — bypassing newer-than check.")

    try:
        latest = _latest_app_release(repo, timeout)
    except (httpx.HTTPError, ValueError) as e:
        log.info("updater: release lookup failed — %s", e)
        return False
    if not latest:
        log.info("updater: no /releases/latest available.")
        return False

    tag = latest.get("tag_name", "")
    m = VERSION_RE.match(tag)
    if not m:
        log.info("updater: latest tag %r isn't vX.Y.Z — skipping.", tag)
        return False
    remote_v = m.group(1)
    if not forced and _parse(remote_v) <= _parse(local_v):
        log.info("updater: already on latest (%s).", local_v)
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
