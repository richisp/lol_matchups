"""debug_lcu.py — dump LCU state across game phases for analysis.

Run it, then play (or queue) a game. On every gameflow-phase transition it
writes a JSON snapshot to ./debug_dumps/ containing:
  - gameflow session (post-champ-select has both teams' puuids)
  - champ-select session (pre-game)
  - per-puuid probes of /lol-ranked/v1/ranked-stats and
    /lol-champion-mastery to see which endpoints actually return data
    for teammates vs. enemies.

Console prints a short summary per probe. JSON files have the full bodies
for offline inspection.
"""

import json
import time
import warnings
from datetime import datetime
from pathlib import Path

import httpx

import lcu


DUMP_DIR = Path("debug_dumps")
POLL_INTERVAL_S = 2.0


def lcu_get(creds: dict, path: str):
    url = f"{creds['protocol']}://{creds['host']}:{creds['port']}{path}"
    try:
        r = httpx.get(
            url,
            auth=("riot", creds["password"]),
            verify=False,
            timeout=3.0,
        )
        body = None
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body
    except Exception as e:
        return -1, {"error": str(e)}


def collect_players(gf_session: dict | None, cs_session: dict | None) -> list[dict]:
    """Pull every (puuid, championId, team) we can find across both session
    shapes. Dedup by puuid."""
    out: list[dict] = []

    game_data = (gf_session or {}).get("gameData") or {}
    for team_key in ("teamOne", "teamTwo"):
        for p in game_data.get(team_key, []) or []:
            out.append({
                "source": f"gameflow.{team_key}",
                "puuid": p.get("puuid"),
                "summonerId": p.get("summonerId"),
                "summonerName": p.get("summonerName") or p.get("displayName"),
                "championId": p.get("championId"),
                "selectedRole": p.get("selectedRole"),
            })

    for team_key in ("myTeam", "theirTeam"):
        for p in (cs_session or {}).get(team_key, []) or []:
            out.append({
                "source": f"champselect.{team_key}",
                "puuid": p.get("puuid"),
                "summonerId": p.get("summonerId"),
                "championId": p.get("championId") or p.get("championPickIntent"),
                "assignedPosition": p.get("assignedPosition"),
            })

    seen, unique = set(), []
    for p in out:
        key = p.get("puuid")
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def probe_player(creds: dict, player: dict) -> dict:
    puuid = player["puuid"]
    cid = player.get("championId") or 0
    rank_status, rank_body = lcu_get(creds, f"/lol-ranked/v1/ranked-stats/{puuid}")
    out = {
        "player": player,
        "ranked": {"status": rank_status, "body": rank_body},
    }
    if cid:
        m_status, m_body = lcu_get(
            creds, f"/lol-champion-mastery/v1/{puuid}/champion-mastery/{cid}"
        )
        out["mastery"] = {"status": m_status, "body": m_body}
    return out


def summarize(probe: dict) -> str:
    p = probe["player"]
    src = p.get("source", "?")
    name = p.get("summonerName") or p.get("puuid", "?")[:8]
    cid = p.get("championId")
    rank = probe.get("ranked", {})
    rank_tier = ""
    if rank.get("status") == 200 and isinstance(rank.get("body"), dict):
        q = rank["body"].get("queueMap", {}).get("RANKED_SOLO_5x5") or {}
        rank_tier = f"{q.get('tier','?')} {q.get('division','?')}"
    rank_str = f"rank={rank.get('status')}" + (f" ({rank_tier})" if rank_tier else "")
    mastery_str = ""
    if "mastery" in probe:
        m = probe["mastery"]
        m_lvl = ""
        if m.get("status") == 200 and isinstance(m.get("body"), dict):
            m_lvl = f" lvl={m['body'].get('championLevel')}"
        mastery_str = f", mastery={m.get('status')}{m_lvl}"
    return f"  [{src}] {name} (champId={cid}) → {rank_str}{mastery_str}"


def main():
    warnings.filterwarnings("ignore")
    DUMP_DIR.mkdir(exist_ok=True)

    creds = lcu.read_credentials()
    if not creds:
        print("No lockfile found. Start the LoL client first.")
        return
    print(f"Connected to LCU on port {creds['port']}.")
    print(f"Dumping to {DUMP_DIR.resolve()}")

    last_phase = None
    while True:
        try:
            _, phase = lcu_get(creds, "/lol-gameflow/v1/gameflow-phase")
            phase_str = phase if isinstance(phase, str) else "Unknown"

            if phase_str != last_phase:
                print(f"\n[{datetime.now():%H:%M:%S}] Phase: {last_phase!r} -> {phase_str!r}")

                _, gf_session = lcu_get(creds, "/lol-gameflow/v1/session")
                _, cs_session = lcu_get(creds, "/lol-champ-select/v1/session")
                _, current_summoner = lcu_get(creds, "/lol-summoner/v1/current-summoner")

                players = collect_players(
                    gf_session if isinstance(gf_session, dict) else None,
                    cs_session if isinstance(cs_session, dict) else None,
                )
                probes = [probe_player(creds, p) for p in players]

                snap = {
                    "ts": datetime.now().isoformat(),
                    "phase": phase_str,
                    "current_summoner": current_summoner,
                    "gameflow_session": gf_session,
                    "champ_select_session": cs_session,
                    "probes": probes,
                }

                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                path = DUMP_DIR / f"{ts}-{phase_str.lower() or 'unknown'}.json"
                path.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
                print(f"  -> {path.name} ({len(probes)} puuids probed)")
                for pr in probes:
                    print(summarize(pr))

                last_phase = phase_str

            time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


if __name__ == "__main__":
    main()
