"""LCU (League Client Update) API client.

Reads the local LoL client's lockfile to get auth credentials, then queries
champ select state. Only works while the client is running on the same
machine. Returns None gracefully when the client isn't running or the user
isn't in champ select.
"""

import os
from pathlib import Path
from typing import Any

import httpx


# Default lockfile locations. The LEAGUE_INSTALL_PATH env var overrides these
# (point it at the directory containing `lockfile`).
LOCKFILE_PATHS = [
    Path("C:/Riot Games/League of Legends/lockfile"),
    Path("/Applications/League of Legends.app/Contents/LoL/lockfile"),
    Path("/mnt/c/Riot Games/League of Legends/lockfile"),  # WSL → Windows
]


# LCU's `assignedPosition` strings → our internal position keys.
LCU_POSITION_MAP = {
    "top":     "TOP",
    "jungle":  "JUNGLE",
    "middle":  "MID",
    "bottom":  "BOT",
    "utility": "SUPPORT",
}


# Cache: numeric champion key → display name (from Data Dragon).
_champion_by_key: dict[int, str] | None = None


def find_lockfile() -> Path | None:
    custom = os.environ.get("LEAGUE_INSTALL_PATH")
    if custom:
        p = Path(custom) / "lockfile"
        if p.exists():
            return p
    for p in LOCKFILE_PATHS:
        if p.exists():
            return p
    return None


def read_credentials() -> dict | None:
    p = find_lockfile()
    if not p:
        return None
    try:
        # Format: name:pid:port:password:protocol
        parts = p.read_text().strip().split(":")
    except OSError:
        return None
    if len(parts) < 5:
        return None
    return {
        "host": "127.0.0.1",
        "port": int(parts[2]),
        "password": parts[3],
        "protocol": parts[4],
    }


def get_champ_select_session() -> dict | None:
    """Fetch the current champ select session from the local LoL client.
    Returns the raw LCU JSON, or None if:
      - the client isn't running (no lockfile),
      - the user isn't currently in champ select (LCU 404),
      - LCU is unreachable (timeout, SSL, etc.).
    """
    creds = read_credentials()
    if not creds:
        return None
    url = f"{creds['protocol']}://{creds['host']}:{creds['port']}/lol-champ-select/v1/session"
    try:
        r = httpx.get(
            url,
            auth=("riot", creds["password"]),
            verify=False,
            timeout=2.0,
        )
        if r.status_code == 200:
            return r.json()
        return None
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        return None


def _load_champion_keys(dd_version: str) -> None:
    global _champion_by_key
    url = f"https://ddragon.leagueoflegends.com/cdn/{dd_version}/data/en_US/champion.json"
    data = httpx.get(url, timeout=10).json()["data"]
    _champion_by_key = {int(info["key"]): info["name"] for info in data.values()}


def champion_name_by_key(key: int, dd_version: str) -> str | None:
    if _champion_by_key is None:
        _load_champion_keys(dd_version)
    if not key:
        return None
    return _champion_by_key.get(int(key))  # type: ignore[union-attr]


def normalize_session(session: dict, dd_version: str) -> dict[str, Any]:
    """Convert raw LCU session JSON into the shape the UI expects:
        { connected, in_champ_select, my_lane, my_team, enemy_team, bans }
    """
    local_cell = session.get("localPlayerCellId", -1)
    my_lane = ""
    my_team: dict[str, str] = {}
    enemy_team: dict[str, str] = {}

    for player in session.get("myTeam", []) or []:
        # championPickIntent = hover (live preview); championId = locked pick.
        cid = player.get("championId") or player.get("championPickIntent") or 0
        pos = LCU_POSITION_MAP.get(player.get("assignedPosition") or "")
        if not pos:
            continue
        if cid:
            name = champion_name_by_key(cid, dd_version)
            if name:
                my_team[pos] = name
        if player.get("cellId") == local_cell:
            my_lane = pos

    for player in session.get("theirTeam", []) or []:
        cid = player.get("championId") or player.get("championPickIntent") or 0
        pos = LCU_POSITION_MAP.get(player.get("assignedPosition") or "")
        if not pos or not cid:
            continue
        name = champion_name_by_key(cid, dd_version)
        if name:
            enemy_team[pos] = name

    bans: list[str] = []
    for action_group in session.get("actions") or []:
        for action in action_group:
            if action.get("type") == "ban" and action.get("completed"):
                cid = action.get("championId")
                if cid:
                    name = champion_name_by_key(cid, dd_version)
                    if name:
                        bans.append(name)

    return {
        "connected": True,
        "in_champ_select": True,
        "local_cell_id": local_cell,
        "my_lane": my_lane,
        "my_team": my_team,
        "enemy_team": enemy_team,
        "bans": bans,
    }


def get_state(dd_version: str) -> dict[str, Any]:
    """One-shot: returns a fully-normalized state dict suitable for the API.
    Always returns a dict (never None) so the front-end has something to render.
    """
    if not find_lockfile():
        return {"connected": False, "reason": "no_client"}
    raw = get_champ_select_session()
    if raw is None:
        return {"connected": True, "in_champ_select": False}
    return normalize_session(raw, dd_version)
