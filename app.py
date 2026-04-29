import httpx
from flask import Flask, render_template, request

import config
import db

app = Flask(__name__)

POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

SORT_KEYS = {
    "winrate": ("winrate", True),
    "picks":   ("picks", True),
    "wins":    ("wins", True),
    "bans":    ("bans", True),
    "name":    ("champion_name", False),
}

MATCHUP_SORT_KEYS = {
    "winrate": ("winrate", True),
    "games":   ("games", True),
}

_dd_version: str | None = None


def get_dd_version() -> str:
    global _dd_version
    if _dd_version is None:
        r = httpx.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10)
        r.raise_for_status()
        _dd_version = r.json()[0]
    return _dd_version


def get_available_patches() -> list[str]:
    with db.connect(config.DB_PATH) as conn:
        return [
            row["patch"]
            for row in conn.execute(
                "SELECT DISTINCT patch FROM matches WHERE patch IS NOT NULL ORDER BY patch DESC"
            )
        ]


def get_champion_stats(position: str, patches: list[str], min_games: int):
    if not patches:
        return [], 0

    patch_qmarks = ",".join("?" * len(patches))

    with db.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            f"""
            SELECT
                p.champion_id,
                p.champion_name,
                COUNT(*)         AS picks,
                SUM(p.win)       AS wins
            FROM match_participants p
            JOIN matches m ON m.match_id = p.match_id
            WHERE p.team_position = ?
              AND m.patch IN ({patch_qmarks})
            GROUP BY p.champion_id, p.champion_name
            HAVING picks >= ?
            """,
            [position, *patches, min_games],
        ).fetchall()

        ban_rows = conn.execute(
            f"""
            SELECT b.champion_id, COUNT(*) AS bans
            FROM match_bans b
            JOIN matches m ON m.match_id = b.match_id
            WHERE m.patch IN ({patch_qmarks})
            GROUP BY b.champion_id
            """,
            patches,
        ).fetchall()
        bans = {r["champion_id"]: r["bans"] for r in ban_rows}

        total_matches = conn.execute(
            f"SELECT COUNT(*) FROM matches WHERE patch IN ({patch_qmarks})",
            patches,
        ).fetchone()[0]

    stats = []
    for r in rows:
        picks = r["picks"]
        wins = r["wins"] or 0
        stats.append({
            "champion_id": r["champion_id"],
            "champion_name": r["champion_name"],
            "picks": picks,
            "wins": wins,
            "losses": picks - wins,
            "winrate": wins / picks,
            "bans": bans.get(r["champion_id"], 0),
        })
    return stats, total_matches


def get_matchups(champion_name: str, my_position: str | None, patches: list[str], min_games: int):
    """For a given champion (optionally filtered by their position), return
    opposing-team champion stats grouped by opposing position.

    Returns (matchups_dict, total_games_for_selected_champion).
    """
    if not patches:
        return {p: [] for p in POSITIONS}, 0

    patch_qmarks = ",".join("?" * len(patches))

    pos_clause = ""
    pos_args: list = []
    if my_position:
        pos_clause = "AND a.team_position = ?"
        pos_args = [my_position]

    with db.connect(config.DB_PATH) as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) FROM match_participants a
            JOIN matches m ON m.match_id = a.match_id
            WHERE a.champion_name = ?
              {pos_clause}
              AND m.patch IN ({patch_qmarks})
            """,
            [champion_name, *pos_args, *patches],
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT
                b.champion_id,
                b.champion_name,
                b.team_position AS opp_pos,
                COUNT(*)  AS games,
                SUM(b.win) AS opp_wins
            FROM match_participants a
            JOIN match_participants b
                ON a.match_id = b.match_id
               AND a.team_id != b.team_id
            JOIN matches m ON m.match_id = a.match_id
            WHERE a.champion_name = ?
              {pos_clause}
              AND m.patch IN ({patch_qmarks})
              AND b.team_position IS NOT NULL
            GROUP BY b.champion_id, b.champion_name, b.team_position
            HAVING games >= ?
            """,
            [champion_name, *pos_args, *patches, min_games],
        ).fetchall()

    by_position: dict[str, list] = {p: [] for p in POSITIONS}
    for r in rows:
        pos = r["opp_pos"]
        if pos not in by_position:
            continue
        games = r["games"]
        opp_wins = r["opp_wins"] or 0
        by_position[pos].append({
            "champion_id": r["champion_id"],
            "champion_name": r["champion_name"],
            "games": games,
            "winrate": opp_wins / games,
        })

    return by_position, total


@app.route("/")
def index():
    available_patches = get_available_patches()
    if not available_patches:
        return render_template(
            "index.html",
            error="No matches in DB yet — run the crawler first.",
            positions=POSITIONS,
        )

    position = request.args.get("position", "MIDDLE")
    if position not in POSITIONS:
        position = "MIDDLE"

    selected_patches = request.args.getlist("patches")
    if not selected_patches:
        selected_patches = available_patches
    selected_patches = [p for p in selected_patches if p in available_patches]

    try:
        min_games = max(0, int(request.args.get("min_games", 5)))
    except ValueError:
        min_games = 5

    sort_by = request.args.get("sort", "winrate")
    if sort_by not in SORT_KEYS:
        sort_by = "winrate"

    stats, total = get_champion_stats(position, selected_patches, min_games)

    key, reverse = SORT_KEYS[sort_by]
    if key == "champion_name":
        stats.sort(key=lambda s: s[key].lower(), reverse=reverse)
    else:
        stats.sort(key=lambda s: s[key], reverse=reverse)

    return render_template(
        "index.html",
        positions=POSITIONS,
        position=position,
        available_patches=available_patches,
        selected_patches=selected_patches,
        min_games=min_games,
        sort_by=sort_by,
        stats=stats,
        total_matches=total,
        dd_version=get_dd_version(),
    )


@app.route("/champion/<champion_name>")
def champion_matchups(champion_name: str):
    available_patches = get_available_patches()
    if not available_patches:
        return render_template(
            "champion.html",
            champion_name=champion_name,
            error="No matches in DB yet — run the crawler first.",
            positions=POSITIONS,
        )

    my_position = request.args.get("position", "")
    if my_position and my_position not in POSITIONS:
        my_position = ""

    selected_patches = request.args.getlist("patches")
    if not selected_patches:
        selected_patches = available_patches
    selected_patches = [p for p in selected_patches if p in available_patches]

    try:
        min_games = max(1, int(request.args.get("min_games", 3)))
    except ValueError:
        min_games = 3

    sort_by = request.args.get("sort", "winrate")
    if sort_by not in MATCHUP_SORT_KEYS:
        sort_by = "winrate"

    matchups, total = get_matchups(champion_name, my_position or None, selected_patches, min_games)

    key, reverse = MATCHUP_SORT_KEYS[sort_by]
    for pos in matchups:
        matchups[pos].sort(key=lambda x, k=key: x[k], reverse=reverse)

    return render_template(
        "champion.html",
        champion_name=champion_name,
        my_position=my_position,
        positions=POSITIONS,
        available_patches=available_patches,
        selected_patches=selected_patches,
        min_games=min_games,
        sort_by=sort_by,
        matchups=matchups,
        total_matches=total,
        dd_version=get_dd_version(),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
