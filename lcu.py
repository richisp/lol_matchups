"""LCU (League Client Update) API client.

Reads the local LoL client's lockfile to get auth credentials, then queries
champ select state. Only works while the client is running on the same
machine. Returns None gracefully when the client isn't running or the user
isn't in champ select.
"""

import os
from itertools import permutations
from pathlib import Path
from typing import Any

import httpx

import config


# Default lockfile locations. The LEAGUE_INSTALL_PATH env var overrides these
# (point it at the directory containing `lockfile`).
LOCKFILE_PATHS = [
    Path("C:/Riot Games/League of Legends/lockfile"),
    Path("/Applications/League of Legends.app/Contents/LoL/lockfile"),
    Path("/mnt/c/Riot Games/League of Legends/lockfile"),  # WSL → Windows
]


LCU_POSITION_MAP = config.LCU_POSITION_MAP


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


def best_lane_assignment(
    picks: list[tuple[int, str]],
    tier: str,
    conn,
    occupied: set[str] | None = None,
) -> dict[str, str]:
    """Place each picked champion into a distinct unoccupied lane such that the
    summed pickrate (in the given tier) is maximized. This is the assignment
    problem; with ≤5 picks × ≤5 lanes a brute-force over permutations is fine
    (≤120 candidates) and avoids pulling in scipy.

    `picks` are (cell_id, champion_name) tuples — cell_id is used only for
    deterministic tie-break ordering. `occupied` is the set of lanes already
    filled by teammates whose `assignedPosition` was provided by Riot; those
    lanes are excluded from inference so we never overwrite known data.

    Returns POSITION → champion_name for the placements made.
    """
    occupied_set = set(occupied or ())
    available = [r for r in config.POSITIONS if r not in occupied_set]
    if not picks or not available:
        return {}

    picks = sorted(picks, key=lambda p: p[0])
    names = [name for _, name in picks]

    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""
        SELECT champion_name, lane, COALESCE(pickrate, 0) AS pr
          FROM champion_stats
         WHERE tier = ?
           AND champion_name IN ({placeholders})
        """,
        (tier, *names),
    ).fetchall()

    pr: dict[tuple[str, str], float] = {}
    for r in rows:
        pr[(r["champion_name"], r["lane"])] = r["pr"] or 0.0

    n = min(len(picks), len(available))
    best_score = -1.0
    best: dict[str, str] = {}
    for combo in permutations(available, n):
        s = sum(pr.get((names[i], combo[i]), 0.0) for i in range(n))
        if s > best_score:
            best_score = s
            best = {combo[i]: names[i] for i in range(n)}
    return best


def _build_team(
    players: list[dict],
    dd_version: str,
    tier: str | None,
    conn,
) -> dict[str, str]:
    """Build {POSITION: champion_name} for one team. Honors `assignedPosition`
    when Riot provides it, and infers the most-probable composition for the
    remaining picks via `best_lane_assignment`."""
    assigned: dict[str, str] = {}
    unassigned: list[tuple[int, str]] = []

    for player in players or []:
        # championPickIntent = hover (live preview); championId = locked pick.
        cid = player.get("championId") or player.get("championPickIntent") or 0
        if not cid:
            continue
        name = champion_name_by_key(cid, dd_version)
        if not name:
            continue
        pos = LCU_POSITION_MAP.get(player.get("assignedPosition") or "")
        if pos:
            assigned[pos] = name
        else:
            unassigned.append((player.get("cellId", 0), name))

    if unassigned and tier and conn is not None:
        inferred = best_lane_assignment(
            unassigned, tier, conn, occupied=set(assigned.keys()),
        )
        assigned.update(inferred)

    return assigned


def normalize_session(
    session: dict,
    dd_version: str,
    tier: str | None = None,
    conn=None,
) -> dict[str, Any]:
    """Convert raw LCU session JSON into the shape the UI expects:
        { connected, in_champ_select, my_lane, my_team, enemy_team, bans }

    When `tier` and `conn` are provided, picks lacking `assignedPosition`
    (common in Blind/ARAM, and frequent during the hover phase even in
    role-assigned queues) are placed into lanes via the most-probable
    composition. Without those args, such picks are dropped — preserving
    the original conservative behavior.
    """
    local_cell = session.get("localPlayerCellId", -1)
    my_team_players = session.get("myTeam", []) or []
    enemy_team_players = session.get("theirTeam", []) or []

    my_team = _build_team(my_team_players, dd_version, tier, conn)
    enemy_team = _build_team(enemy_team_players, dd_version, tier, conn)

    # Determine my_lane: prefer Riot's assignedPosition for the local cell;
    # if absent (e.g. blind pick), look up our own champion in the inferred
    # composition.
    my_lane = ""
    for player in my_team_players:
        if player.get("cellId") != local_cell:
            continue
        pos = LCU_POSITION_MAP.get(player.get("assignedPosition") or "")
        if pos:
            my_lane = pos
        else:
            cid = player.get("championId") or player.get("championPickIntent") or 0
            name = champion_name_by_key(cid, dd_version) if cid else None
            if name:
                for p, n in my_team.items():
                    if n == name:
                        my_lane = p
                        break
        break

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


def get_state(dd_version: str, tier: str | None = None, conn=None) -> dict[str, Any]:
    """One-shot: returns a fully-normalized state dict suitable for the API.
    Always returns a dict (never None) so the front-end has something to render.
    """
    if not find_lockfile():
        return {"connected": False, "reason": "no_client"}
    raw = get_champ_select_session()
    if raw is None:
        return {"connected": True, "in_champ_select": False}
    return normalize_session(raw, dd_version, tier=tier, conn=conn)
