import os
import sys

import httpx
from flask import Flask, jsonify, render_template, request

import config
import db

# When frozen by PyInstaller, templates extract to sys._MEIPASS/templates.
if getattr(sys, "frozen", False):
    _template_folder = os.path.join(sys._MEIPASS, "templates")  # type: ignore[attr-defined]
else:
    _template_folder = "templates"

app = Flask(__name__, template_folder=_template_folder)

POSITIONS = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]

CHAMPION_SORT_KEYS = {
    "winrate":     ("winrate", True),
    "pickrate":    ("pickrate", True),
    "banrate":     ("banrate", True),
    "games":       ("games", True),
    "blind_risk":  ("blind_risk", False),  # ascending — lower risk is better
    "name":        ("champion_name", False),
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


def compute_blind_risk(conn, lane: str, tier: str) -> dict[str, float]:
    """For each champion in `lane × tier`, compute a blind-pick risk score.

    For each enemy lane L:
        bad_pr_L = SUM(pickrate) over counter matchups where focal.WR < threshold
        contribution = bad_pr_L * (counter_weight[lane][L] / 100)
    score = SUM(contribution) over the 5 enemy lanes.

    Lower = safer blind pick (less popular bad-matchup exposure, weighted by
    how much each enemy lane impacts your pick choice).
    """
    if lane not in config.COUNTER_WEIGHTS:
        return {}
    weights = config.COUNTER_WEIGHTS[lane]
    threshold = config.BLIND_PICK_BAD_WR_THRESHOLD

    rows = conn.execute(
        """
        SELECT champion_name,
               opponent_lane,
               COALESCE(SUM(pickrate), 0) AS bad_pr
          FROM matchups
         WHERE champion_lane = ?
           AND tier = ?
           AND matchup_type = 'counter'
           AND winrate < ?
         GROUP BY champion_name, opponent_lane
        """,
        (lane, tier, threshold),
    ).fetchall()

    scores: dict[str, float] = {}
    for r in rows:
        weight = weights.get(r["opponent_lane"], 0) / 100.0
        scores[r["champion_name"]] = scores.get(r["champion_name"], 0.0) + r["bad_pr"] * weight
    return scores


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
        risk = compute_blind_risk(conn, lane, tier)

    out = [dict(r) for r in rows]
    for d in out:
        d["blind_risk"] = risk.get(d["champion_name"], 0.0)

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


def parse_team(prefix: str) -> dict[str, str]:
    """Read 5 lane-keyed champion picks from query params, e.g. my_TOP=Garen.
    Returns dict mapping POSITION → champion_name (skipping empty)."""
    out: dict[str, str] = {}
    for pos in POSITIONS:
        v = (request.args.get(f"{prefix}_{pos}") or "").strip()
        if v:
            out[pos] = v
    return out


def parse_bans() -> set[str]:
    raw = (request.args.get("bans") or "").strip()
    return {b.strip() for b in raw.split(",") if b.strip()}


def compute_draft_scores(
    conn,
    lane: str,
    tier: str,
    my_team: dict[str, str],
    enemy_team: dict[str, str],
    bans: set[str],
) -> list[dict]:
    """Return all candidates for `lane × tier` with fit scores.

    fit = base_winrate + Σ counter_contrib + Σ synergy_contrib
    """
    counter_w = config.COUNTER_WEIGHTS.get(lane, {})
    synergy_w = config.SYNERGY_WEIGHTS.get(lane, {})

    # Candidates = champions with stats in this lane × tier, minus bans/already-picked.
    excluded = bans | set(my_team.values()) | set(enemy_team.values())
    rows = conn.execute(
        """
        SELECT champion_name, winrate, pickrate, banrate, games, tier_badge
          FROM champion_stats
         WHERE lane = ? AND tier = ?
        """,
        (lane, tier),
    ).fetchall()
    candidates = [dict(r) for r in rows if r["champion_name"] not in excluded]
    if not candidates:
        return []

    # Bulk-fetch all matchup rows for these candidates against the picked enemies/teammates.
    enemy_keys = [(c["champion_name"], lane, e_name, e_lane, "counter")
                  for c in candidates for e_lane, e_name in enemy_team.items()]
    synergy_keys = [(c["champion_name"], lane, t_name, t_lane, "synergy")
                    for c in candidates for t_lane, t_name in my_team.items()]

    matchup_lookup: dict[tuple, dict] = {}
    for keyset in (enemy_keys, synergy_keys):
        if not keyset:
            continue
        # OR over composite keys via UNION of equality groups (kept simple — N is small)
        for k in keyset:
            row = conn.execute(
                """
                SELECT winrate, games FROM matchups
                 WHERE champion_name=? AND champion_lane=? AND opponent_name=?
                   AND opponent_lane=? AND matchup_type=? AND tier=?
                """,
                (*k, tier),
            ).fetchone()
            if row:
                matchup_lookup[k] = dict(row)

    out = []
    for c in candidates:
        name = c["champion_name"]
        base = c["winrate"] or 50.0

        counter_contribs = []
        counter_breakdown = []
        for e_lane, e_name in enemy_team.items():
            mu = matchup_lookup.get((name, lane, e_name, e_lane, "counter"))
            weight = counter_w.get(e_lane, 0) / 100.0
            if mu and mu.get("winrate") is not None and (mu.get("games") or 0) >= 30:
                contrib = (mu["winrate"] - 50.0) * weight
                counter_contribs.append(contrib)
                counter_breakdown.append({
                    "opponent": e_name, "lane": e_lane,
                    "winrate": mu["winrate"], "games": mu["games"],
                    "weight": weight, "contrib": contrib,
                })
            else:
                counter_breakdown.append({
                    "opponent": e_name, "lane": e_lane,
                    "winrate": None, "games": (mu or {}).get("games"),
                    "weight": weight, "contrib": 0.0,
                })

        synergy_contribs = []
        synergy_breakdown = []
        for t_lane, t_name in my_team.items():
            if t_lane == lane:
                continue  # the active slot itself
            mu = matchup_lookup.get((name, lane, t_name, t_lane, "synergy"))
            weight = synergy_w.get(t_lane, 0) / 100.0
            if mu and mu.get("winrate") is not None and (mu.get("games") or 0) >= 30:
                contrib = (mu["winrate"] - 50.0) * weight
                synergy_contribs.append(contrib)
                synergy_breakdown.append({
                    "ally": t_name, "lane": t_lane,
                    "winrate": mu["winrate"], "games": mu["games"],
                    "weight": weight, "contrib": contrib,
                })
            else:
                synergy_breakdown.append({
                    "ally": t_name, "lane": t_lane,
                    "winrate": None, "games": (mu or {}).get("games"),
                    "weight": weight, "contrib": 0.0,
                })

        counter_total = sum(counter_contribs)
        synergy_total = sum(synergy_contribs)
        fit = base + counter_total + synergy_total
        out.append({
            **c,
            "base": base,
            "counter_total": counter_total,
            "synergy_total": synergy_total,
            "fit": fit,
            "counter_breakdown": counter_breakdown,
            "synergy_breakdown": synergy_breakdown,
        })

    out.sort(key=lambda r: r["fit"], reverse=True)
    return out


@app.route("/api/lcu")
def api_lcu():
    """Snapshot of current champ select state from the local LoL client."""
    import lcu
    return jsonify(lcu.get_state(get_dd_version()))


@app.route("/draft")
def draft():
    available_tiers = get_available_tiers()
    if not available_tiers:
        return render_template("draft.html",
                               error="No data yet — run crawl_champions.py first.",
                               positions=POSITIONS)

    tier = request.args.get("tier") or available_tiers[0]
    if tier not in available_tiers:
        tier = available_tiers[0]

    active = (request.args.get("active") or "BOT").upper()
    if active not in POSITIONS:
        active = "BOT"

    my_team = parse_team("my")
    enemy_team = parse_team("enemy")
    bans = parse_bans()

    # Strip the active slot from my_team for scoring (it's the one we're filling).
    my_team_for_scoring = {k: v for k, v in my_team.items() if k != active}

    with db.connect(config.DB_PATH) as conn:
        candidates = compute_draft_scores(
            conn, active, tier,
            my_team_for_scoring, enemy_team, bans,
        )
        # All champion display names (for the autocomplete datalist).
        champ_names = sorted({
            r["champion_name"] for r in conn.execute(
                "SELECT DISTINCT champion_name FROM champion_stats"
            )
        })

    return render_template(
        "draft.html",
        positions=POSITIONS,
        active=active,
        tier=tier,
        available_tiers=available_tiers,
        my_team=my_team,
        enemy_team=enemy_team,
        bans=sorted(bans),
        bans_str=",".join(sorted(bans)),
        candidates=candidates,
        champ_names=champ_names,
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
