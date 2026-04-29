import httpx
from flask import Flask, render_template, request

import config
import db

app = Flask(__name__)

POSITIONS = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]

CHAMPION_SORT_KEYS = {
    "winrate":  ("winrate", True),
    "pickrate": ("pickrate", True),
    "banrate":  ("banrate", True),
    "games":    ("games", True),
    "name":     ("champion_name", False),
}

MATCHUP_SORT_KEYS = {
    "winrate": ("winrate", True),
    "games":   ("games", True),
}

_dd_version: str | None = None
_champion_lookup: dict[str, str] = {}


def get_dd_version() -> str:
    global _dd_version
    if _dd_version is None:
        r = httpx.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10)
        r.raise_for_status()
        _dd_version = r.json()[0]
    return _dd_version


def _norm(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


def _load_champion_lookup() -> dict[str, str]:
    """Map any reasonable form of a champion name to its Data Dragon id
    (the asset filename used in /img/champion/<id>.png)."""
    version = get_dd_version()
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    data = httpx.get(url, timeout=10).json()["data"]
    lookup: dict[str, str] = {}
    for dd_id, info in data.items():
        for variant in (dd_id, info["name"], _norm(dd_id), _norm(info["name"])):
            lookup[variant] = dd_id
    return lookup


def champ_id(name: str) -> str:
    global _champion_lookup
    if not _champion_lookup:
        _champion_lookup = _load_champion_lookup()
    return _champion_lookup.get(name) or _champion_lookup.get(_norm(name)) or name


app.jinja_env.globals["champ_id"] = champ_id


def get_available_tiers() -> list[str]:
    with db.connect(config.DB_PATH) as conn:
        return [r["tier"] for r in conn.execute(
            "SELECT DISTINCT tier FROM champion_stats ORDER BY tier"
        )]


def get_champion_list(lane: str, tier: str, sort_by: str):
    with db.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT champion_name, lane, tier, winrate, pickrate, banrate, games, tier_badge
              FROM champion_stats
             WHERE lane = ? AND tier = ?
            """,
            (lane, tier),
        ).fetchall()

    out = [dict(r) for r in rows]
    key, reverse = CHAMPION_SORT_KEYS[sort_by]
    if key == "champion_name":
        out.sort(key=lambda r: (r[key] or "").lower(), reverse=reverse)
    else:
        out.sort(key=lambda r: (r[key] is None, r[key] or 0), reverse=reverse)
    return out


def get_matchups(champion: str, lane: str, tier: str, matchup_type: str, min_games: int, sort_by: str):
    with db.connect(config.DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT opponent_name, opponent_lane, winrate, pickrate, games
              FROM matchups
             WHERE champion_name = ?
               AND champion_lane = ?
               AND tier = ?
               AND matchup_type = ?
               AND COALESCE(games, 0) >= ?
            """,
            (champion, lane, tier, matchup_type, min_games),
        ).fetchall()

    by_position: dict[str, list] = {p: [] for p in POSITIONS}
    for r in rows:
        pos = r["opponent_lane"]
        if pos in by_position:
            by_position[pos].append(dict(r))

    key, reverse = MATCHUP_SORT_KEYS[sort_by]
    for pos in by_position:
        by_position[pos].sort(key=lambda x, k=key: (x[k] is None, x[k] or 0), reverse=reverse)
    return by_position


@app.route("/")
def index():
    available_tiers = get_available_tiers()
    if not available_tiers:
        return render_template("index.html",
                               error="No data yet — run crawl_champions.py first.",
                               positions=POSITIONS)

    lane = (request.args.get("lane") or "BOT").upper()
    if lane not in POSITIONS:
        lane = "BOT"

    tier = request.args.get("tier") or available_tiers[0]
    if tier not in available_tiers:
        tier = available_tiers[0]

    sort_by = request.args.get("sort", "winrate")
    if sort_by not in CHAMPION_SORT_KEYS:
        sort_by = "winrate"

    champions = get_champion_list(lane, tier, sort_by)

    return render_template(
        "index.html",
        positions=POSITIONS,
        lane=lane,
        tier=tier,
        available_tiers=available_tiers,
        sort_by=sort_by,
        champions=champions,
        dd_version=get_dd_version(),
    )


@app.route("/champion/<champion_name>")
def champion_matchups(champion_name: str):
    available_tiers = get_available_tiers()
    if not available_tiers:
        return render_template("champion.html",
                               champion_name=champion_name,
                               error="No data yet — run crawl_champions.py first.",
                               positions=POSITIONS)

    lane = (request.args.get("lane") or "BOT").upper()
    if lane not in POSITIONS:
        lane = "BOT"

    tier = request.args.get("tier") or available_tiers[0]
    if tier not in available_tiers:
        tier = available_tiers[0]

    matchup_type = request.args.get("type", "counter")
    if matchup_type not in ("counter", "synergy"):
        matchup_type = "counter"

    try:
        min_games = max(0, int(request.args.get("min_games", 30)))
    except ValueError:
        min_games = 30

    sort_by = request.args.get("sort", "winrate")
    if sort_by not in MATCHUP_SORT_KEYS:
        sort_by = "winrate"

    matchups = get_matchups(champion_name, lane, tier, matchup_type, min_games, sort_by)

    with db.connect(config.DB_PATH) as conn:
        overall_row = conn.execute(
            "SELECT winrate, pickrate, banrate, games, tier_badge FROM champion_stats "
            "WHERE champion_name=? AND lane=? AND tier=?",
            (champion_name, lane, tier),
        ).fetchone()
    overall = dict(overall_row) if overall_row else None

    return render_template(
        "champion.html",
        champion_name=champion_name,
        lane=lane,
        tier=tier,
        positions=POSITIONS,
        available_tiers=available_tiers,
        matchup_type=matchup_type,
        min_games=min_games,
        sort_by=sort_by,
        matchups=matchups,
        overall=overall,
        dd_version=get_dd_version(),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
