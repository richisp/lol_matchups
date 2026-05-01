"""Best-effort DB sync from the latest GitHub release.

Run once at startup, before the Flask server opens any sqlite connections.
On Windows you can't replace an open .db file, so this must complete before
db.connect() is called.

All failures are silent — if GitHub is unreachable, rate-limited, or the
release is missing, we just keep the local DB. The crawler runs daily, so
a stale day or two is not a problem.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

import config

DB_RELEASE_TAG = "db-latest"
LOCAL_VERSION = config.APP_DIR / "db-version.json"

log = logging.getLogger(__name__)


def _release_url(repo: str, tag: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases/tags/{tag}"


def _read_local_version() -> dict | None:
    if not LOCAL_VERSION.exists():
        return None
    try:
        return json.loads(LOCAL_VERSION.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _fetch_remote(repo: str, tag: str, timeout: float) -> tuple[dict, str] | None:
    """Returns (manifest, db_download_url) or None on any failure."""
    try:
        r = httpx.get(_release_url(repo, tag), timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            log.info("DB sync: release lookup returned HTTP %d", r.status_code)
            return None
        rel = r.json()
        manifest_url = db_url = None
        for a in rel.get("assets", []):
            if a["name"] == "db-version.json":
                manifest_url = a["browser_download_url"]
            elif a["name"] == "lolalytics.db":
                db_url = a["browser_download_url"]
        if not manifest_url or not db_url:
            log.info("DB sync: required assets missing from release.")
            return None
        # browser_download_url 302-redirects to a signed CDN URL — must follow.
        manifest = httpx.get(manifest_url, timeout=timeout, follow_redirects=True).json()
        return manifest, db_url
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        log.info("DB sync: %s", e)
        return None


def _is_remote_newer(local: dict | None, remote: dict) -> bool:
    if local is None:
        return True
    # ISO-8601 lexicographically sorts correctly for these timestamps.
    return remote.get("updated_at", "") > local.get("updated_at", "")


def sync_db(repo: str | None = None, timeout: float = 5.0) -> bool:
    """Pull the latest DB snapshot if it's newer than the local copy.

    Returns True if a new DB was written, False otherwise.
    """
    repo = repo or config.GITHUB_REPO
    fetched = _fetch_remote(repo, DB_RELEASE_TAG, timeout)
    if not fetched:
        return False
    manifest, db_url = fetched

    local = _read_local_version()
    if not _is_remote_newer(local, manifest):
        log.info("DB sync: local DB is current (updated %s).",
                 (local or {}).get("updated_at", "?"))
        return False

    log.info("DB sync: downloading newer DB (remote=%s, local=%s).",
             manifest.get("updated_at"),
             (local or {}).get("updated_at", "<none>"))
    tmp = config.DB_PATH.with_suffix(".db.tmp")
    try:
        with httpx.stream("GET", db_url, timeout=60.0, follow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(64 * 1024):
                    f.write(chunk)
        tmp.replace(config.DB_PATH)
        LOCAL_VERSION.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
        )
        log.info("DB sync: complete.")
        return True
    except (httpx.HTTPError, OSError) as e:
        log.warning("DB sync: download failed — %s", e)
        # Clean up partial download if any.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False
