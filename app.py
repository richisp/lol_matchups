import os
import sys

import httpx
from flask import Flask, jsonify, render_template, request

import config
import db
import lcu
from version import __version__ as APP_VERSION

# When frozen by PyInstaller, templates + static both extract to sys._MEIPASS.
if getattr(sys, "frozen", False):
    _template_folder = os.path.join(sys._MEIPASS, "templates")  # type: ignore[attr-defined]
    _static_folder = os.path.join(sys._MEIPASS, "static")  # type: ignore[attr-defined]
else:
    _template_folder = "templates"
    _static_folder = "static"

app = Flask(
    __name__,
    template_folder=_template_folder,
    static_folder=_static_folder,
)


@app.after_request
def _no_cache(response):
    """Don't let the embedded WebView2 cache HTML/JS responses across app
    relaunches. Without this, an auto-update can swap the .exe but the
    relaunched webview still shows the previous version's page from cache."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

POSITIONS = list(config.POSITIONS)

LANE_ALL = "ALL"
LANE_SORT_ORDER = {"TOP": 0, "JUNGLE": 1, "MID": 2, "BOT": 3, "SUPPORT": 4}

# Maps URL sort key → (db column, default-descending). The URL accepts a
# leading "-" to flip direction (e.g. "-name" sorts name descending).
CHAMPION_SORT_KEYS: dict[str, tuple[str, bool]] = {
    "winrate":     ("winrate", True),
    "pickrate":    ("pickrate", True),
    "banrate":     ("banrate", True),
    "games":       ("games", True),
    "blind_risk":  ("blind_risk", False),
    "name":        ("champion_name", False),
    "role":        ("lane", False),
}
DEFAULT_SORT = "winrate"  # canonical default; rendered as "winrate" (descending via natural default)

# Same shape, but for the draft recs table. Column names map to candidate-dict
# keys (compute_draft_scores's output, not raw DB columns).
DRAFT_SORT_KEYS: dict[str, tuple[str, bool]] = {
    "name":    ("champion_name", False),
    "fit":     ("fit", True),
    "winrate": ("winrate", True),
    "counter": ("counter_total", True),
    "synergy": ("synergy_total", True),
    "risk":    ("blind_risk", False),
}
DRAFT_DEFAULT_SORT = "fit"

MATCHUP_SORT_KEYS = {
    "winrate": ("winrate", True),
    "games":   ("games", True),
}

_dd_version: str | None = None
_champion_lookup: dict[str, str] = {}
_available_tiers_cache: list[str] | None = None


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


# Our canonical lane keys → CommunityDragon URL slugs for the position-icon
# SVGs. Only the five real roles; the "All" button doesn't render an icon.
_ROLE_SLUG = {
    "TOP": "top", "JUNGLE": "jungle", "MID": "middle",
    "BOT": "bottom", "SUPPORT": "utility",
}


def role_icon_slug(role: str) -> str:
    return _ROLE_SLUG.get((role or "").upper(), "top")


app.jinja_env.globals["role_icon_slug"] = role_icon_slug
app.jinja_env.globals["app_version"] = APP_VERSION


def tier_label(tier: str) -> str:
    """'emerald_plus' → 'Emerald+'; 'master' → 'Master'; 'all' → 'All'."""
    if not tier or tier == "all":
        return "All"
    if tier.endswith("_plus"):
        return tier[:-5].capitalize() + "+"
    return tier.capitalize()


app.jinja_env.globals["tier_label"] = tier_label


def get_available_tiers() -> list[str]:
    """Tiers present in the DB. Cached for the process lifetime — recrawling
    new tiers requires restarting the app, which is fine for a desktop tool."""
    global _available_tiers_cache
    if _available_tiers_cache is None:
        with db.connect(config.DB_PATH) as conn:
            _available_tiers_cache = [r["tier"] for r in conn.execute(
                "SELECT DISTINCT tier FROM champion_stats ORDER BY tier"
            )]
    return _available_tiers_cache


def compute_blind_risk(
    conn,
    lane: str,
    tier: str,
    known_enemy_lanes: set[str] | None = None,
    known_ally_lanes: set[str] | None = None,
) -> dict[str, float]:
    """For each champion in `lane × tier`, compute a blind-pick risk score.

    Risk is popularity-weighted exposure to bad matchups (focal WR < threshold),
    summed over two sources:

      counter exposure (enemy lanes) — the enemy picks after you and can target
      your blind pick:
        bad_pr_L = SUM(pickrate) over counter matchups vs enemy lane L
        contribution = bad_pr_L * (COUNTER_WEIGHTS[lane][L] / 100)
      synergy exposure (ally lanes) — a blind pick may end up paired with a
      popular ally it works poorly with:
        bad_pr_L = SUM(pickrate) over synergy matchups with ally lane L
        contribution = bad_pr_L * (SYNERGY_WEIGHTS[lane][L] / 100)

    score = SUM(contribution) over the 5 enemy lanes + the 5 ally lanes.

    Lower = safer blind pick. Lanes already filled by the enemy are not "blind"
    — the matchup is known and reflected in the `vs` column; pass them via
    `known_enemy_lanes`. Likewise allies already locked are known partners
    (`known_ally_lanes`). Each known lane is excluded from its respective term.
    """
    if lane not in config.COUNTER_WEIGHTS:
        return {}
    threshold = config.BLIND_PICK_BAD_WR_THRESHOLD
    known_enemy = known_enemy_lanes or set()
    known_ally = known_ally_lanes or set()
    counter_w = config.COUNTER_WEIGHTS[lane]
    synergy_w = config.SYNERGY_WEIGHTS.get(lane, {})

    rows = conn.execute(
        """
        SELECT champion_name,
               matchup_type,
               opponent_lane,
               COALESCE(SUM(pickrate), 0) AS bad_pr
          FROM matchups
         WHERE champion_lane = ?
           AND tier = ?
           AND matchup_type IN ('counter', 'synergy')
           AND winrate < ?
         GROUP BY champion_name, matchup_type, opponent_lane
        """,
        (lane, tier, threshold),
    ).fetchall()

    scores: dict[str, float] = {}
    for r in rows:
        opp_lane = r["opponent_lane"]
        if r["matchup_type"] == "counter":
            if opp_lane in known_enemy:
                continue
            weight = counter_w.get(opp_lane, 0) / 100.0
        else:  # synergy — SYNERGY_WEIGHTS diagonal is 0, so own-lane drops out
            if opp_lane in known_ally:
                continue
            weight = synergy_w.get(opp_lane, 0) / 100.0
        if weight == 0:
            continue
        scores[r["champion_name"]] = scores.get(r["champion_name"], 0.0) + r["bad_pr"] * weight
    # Halve so the combined counter+synergy score stays on roughly the same
    # scale as the original counter-only metric (keeps the UI color bands valid).
    return {name: v / 2.0 for name, v in scores.items()}


def parse_sort(
    sort_param: str,
    sort_keys: dict[str, tuple[str, bool]] = CHAMPION_SORT_KEYS,
    default: str = DEFAULT_SORT,
) -> tuple[str, str, bool]:
    """Parse a `sort` URL param like 'winrate' or '-name' into
    (canonical_key, db_column, descending). A leading `-` forces descending,
    `+` forces ascending; absence falls back to the natural default for the
    column."""
    explicit_desc = sort_param.startswith("-")
    explicit_asc = sort_param.startswith("+")
    base = sort_param.lstrip("+-")
    if base not in sort_keys:
        base = default
    db_col, default_desc = sort_keys[base]
    if explicit_desc:
        desc = True
    elif explicit_asc:
        desc = False
    else:
        desc = default_desc
    return base, db_col, desc


def get_champion_list(lane: str, tier: str, sort_param: str):
    with db.connect(config.DB_PATH) as conn:
        if lane == LANE_ALL:
            rows = conn.execute(
                """
                SELECT champion_name, lane, tier, winrate, pickrate, banrate, games, tier_badge
                  FROM champion_stats
                 WHERE tier = ?
                """,
                (tier,),
            ).fetchall()
            risk_by_lane = {l: compute_blind_risk(conn, l, tier) for l in POSITIONS}
        else:
            rows = conn.execute(
                """
                SELECT champion_name, lane, tier, winrate, pickrate, banrate, games, tier_badge
                  FROM champion_stats
                 WHERE lane = ? AND tier = ?
                """,
                (lane, tier),
            ).fetchall()
            risk_by_lane = {lane: compute_blind_risk(conn, lane, tier)}

    out = [dict(r) for r in rows]
    for d in out:
        d["blind_risk"] = risk_by_lane.get(d["lane"], {}).get(d["champion_name"], 0.0)

    _, db_col, desc = parse_sort(sort_param)
    if db_col == "champion_name":
        out.sort(key=lambda r: (r[db_col] or "").lower(), reverse=desc)
    elif db_col == "lane":
        # Sort by canonical lane order, with winrate desc as a stable secondary.
        out.sort(key=lambda r: (
            -(r["winrate"] or 0),
        ), reverse=False)
        out.sort(
            key=lambda r: LANE_SORT_ORDER.get(r["lane"], 99),
            reverse=desc,
        )
    else:
        out.sort(key=lambda r: (r[db_col] is None, r[db_col] or 0), reverse=desc)
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

    lane = (request.args.get("lane") or LANE_ALL).upper()
    if lane != LANE_ALL and lane not in POSITIONS:
        lane = LANE_ALL

    tier = request.args.get("tier") or available_tiers[0]
    if tier not in available_tiers:
        tier = available_tiers[0]

    raw_sort = request.args.get("sort") or DEFAULT_SORT
    sort_key, _, sort_desc = parse_sort(raw_sort)
    # Always emit the sign so JS can tell ASC from "no preference" — without
    # this, "fit" round-trips as "fit" and a user-clicked toggle to ASC gets
    # served back as the natural-default DESC.
    sort_by = ("-" if sort_desc else "+") + sort_key

    champions = get_champion_list(lane, tier, sort_by)

    lane_label = "All roles" if lane == LANE_ALL else lane

    return render_template(
        "index.html",
        positions=POSITIONS,
        lane=lane,
        lane_label=lane_label,
        lane_all=LANE_ALL,
        tier=tier,
        available_tiers=available_tiers,
        sort_by=sort_by,
        sort_key=sort_key,
        sort_desc=sort_desc,
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


def compute_draft_scores(
    conn,
    lane: str,
    tier: str,
    my_team: dict[str, str],
    enemy_team: dict[str, str],
    bans: set[str],
) -> list[dict]:
    """Return all candidates for `lane × tier` with fit scores.

    fit = 50.0 + Σ counter_contrib + Σ synergy_contrib

    Base is always 50.0 — we deliberately ignore each champion's individual
    tier winrate so rankings turn purely on counter/synergy contributions
    instead of meta strength.
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

    # Bulk-fetch all relevant matchup rows in two queries (one per matchup_type).
    # Previously we ran ~candidates × picked-team queries (~1000+ per request);
    # batching cuts that to two regardless of team size.
    matchup_lookup: dict[tuple, dict] = {}
    candidate_names = [c["champion_name"] for c in candidates]

    def _fetch_matchups(opponents: dict[str, str], matchup_type: str) -> None:
        if not opponents:
            return
        opp_names = list(opponents.values())
        opp_lanes = list(opponents.keys())
        cand_ph = ",".join("?" * len(candidate_names))
        name_ph = ",".join("?" * len(opp_names))
        lane_ph = ",".join("?" * len(opp_lanes))
        rows = conn.execute(
            f"""
            SELECT champion_name, opponent_name, opponent_lane, winrate, games
              FROM matchups
             WHERE tier = ?
               AND champion_lane = ?
               AND matchup_type = ?
               AND champion_name IN ({cand_ph})
               AND opponent_name IN ({name_ph})
               AND opponent_lane IN ({lane_ph})
            """,
            (tier, lane, matchup_type, *candidate_names, *opp_names, *opp_lanes),
        ).fetchall()
        # The IN-clause filter is over-broad: it admits any opponent_name in
        # any opponent_lane, even pairs that don't actually exist in our team.
        # Keep only rows where (opponent_name, opponent_lane) corresponds to an
        # actually-picked opponent.
        for r in rows:
            if opponents.get(r["opponent_lane"]) != r["opponent_name"]:
                continue
            key = (r["champion_name"], lane, r["opponent_name"], r["opponent_lane"], matchup_type)
            matchup_lookup[key] = dict(r)

    _fetch_matchups(enemy_team, "counter")
    _fetch_matchups(my_team, "synergy")

    # Lanes the enemy has already filled aren't "blind" — exclude them from
    # the risk score so it shrinks toward 0 as the enemy team locks in.
    risk_scores = compute_blind_risk(
        conn, lane, tier,
        known_enemy_lanes=set(enemy_team.keys()),
        known_ally_lanes=set(my_team.keys()),
    )

    out = []
    for c in candidates:
        name = c["champion_name"]
        base = 50.0

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
            "blind_risk": risk_scores.get(name, 0.0),
            "counter_breakdown": counter_breakdown,
            "synergy_breakdown": synergy_breakdown,
        })

    out.sort(key=lambda r: r["fit"], reverse=True)
    return out


def compute_pick_breakdown(
    conn,
    champ: str,
    lane: str,
    tier: str,
    opposing_team: dict[str, str],
    allies_team: dict[str, str],
) -> dict:
    """Per-pick breakdown for the hover tooltip on a slot. Same shape as the
    candidate dicts compute_draft_scores produces, so the template's
    breakdown_section macro renders both identically.
    """
    counter_w = config.COUNTER_WEIGHTS.get(lane, {})
    synergy_w = config.SYNERGY_WEIGHTS.get(lane, {})

    base = 50.0
    stats_row = conn.execute(
        "SELECT winrate FROM champion_stats "
        "WHERE champion_name=? AND lane=? AND tier=?",
        (champ, lane, tier),
    ).fetchone()
    winrate = stats_row["winrate"] if stats_row else None

    def _matchup(opponent: str, opp_lane: str, kind: str) -> dict | None:
        return conn.execute(
            "SELECT winrate, games FROM matchups "
            "WHERE champion_name=? AND champion_lane=? "
            "AND opponent_name=? AND opponent_lane=? "
            "AND matchup_type=? AND tier=?",
            (champ, lane, opponent, opp_lane, kind, tier),
        ).fetchone()

    counter_total = 0.0
    counter_breakdown = []
    for opp_lane, opp_name in opposing_team.items():
        mu = _matchup(opp_name, opp_lane, "counter")
        weight = counter_w.get(opp_lane, 0) / 100.0
        wr = mu["winrate"] if mu else None
        games = mu["games"] if mu else None
        if wr is not None and (games or 0) >= 30:
            contrib = (wr - 50.0) * weight
            counter_total += contrib
            counter_breakdown.append({
                "opponent": opp_name, "lane": opp_lane,
                "winrate": wr, "games": games,
                "weight": weight, "contrib": contrib,
            })
        else:
            counter_breakdown.append({
                "opponent": opp_name, "lane": opp_lane,
                "winrate": None, "games": games,
                "weight": weight, "contrib": 0.0,
            })

    synergy_total = 0.0
    synergy_breakdown = []
    for ally_lane, ally_name in allies_team.items():
        if ally_lane == lane:
            continue  # the champ itself
        mu = _matchup(ally_name, ally_lane, "synergy")
        weight = synergy_w.get(ally_lane, 0) / 100.0
        wr = mu["winrate"] if mu else None
        games = mu["games"] if mu else None
        if wr is not None and (games or 0) >= 30:
            contrib = (wr - 50.0) * weight
            synergy_total += contrib
            synergy_breakdown.append({
                "ally": ally_name, "lane": ally_lane,
                "winrate": wr, "games": games,
                "weight": weight, "contrib": contrib,
            })
        else:
            synergy_breakdown.append({
                "ally": ally_name, "lane": ally_lane,
                "winrate": None, "games": games,
                "weight": weight, "contrib": 0.0,
            })

    risk_map = compute_blind_risk(
        conn, lane, tier,
        known_enemy_lanes=set(opposing_team.keys()),
        known_ally_lanes=set(allies_team.keys()),
    )
    return {
        "champion_name": champ,
        "lane": lane,
        "base": base,
        "winrate": winrate,
        "counter_total": counter_total,
        "synergy_total": synergy_total,
        "fit": base + counter_total + synergy_total,
        "blind_risk": risk_map.get(champ, 0.0),
        "counter_breakdown": counter_breakdown,
        "synergy_breakdown": synergy_breakdown,
    }


@app.route("/api/lcu")
def api_lcu():
    """Snapshot of current champ select state from the local LoL client.
    Accepts ?tier= so role inference (for picks without assignedPosition) uses
    the tier the user is currently viewing."""
    available = get_available_tiers()
    tier = request.args.get("tier") or (available[0] if available else None)
    if tier and tier not in available:
        tier = available[0] if available else None
    if tier:
        with db.connect(config.DB_PATH) as conn:
            return jsonify(lcu.get_state(get_dd_version(), tier=tier, conn=conn))
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
    # LCU-detected bans split by team — drives the visual icon row under each
    # team; their union also drives scoring (excluded from candidates).
    my_bans_list = [b.strip() for b in (request.args.get("my_bans") or "").split(",") if b.strip()]
    enemy_bans_list = [b.strip() for b in (request.args.get("enemy_bans") or "").split(",") if b.strip()]
    bans = set(my_bans_list) | set(enemy_bans_list)

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
        # Hover-tooltip breakdowns for already-picked champions, keyed by
        # the slot's position. Mirrors what compute_draft_scores does for
        # rec candidates, so the template can render both with the same
        # macro.
        my_pick_breakdowns = {
            pos: compute_pick_breakdown(
                conn, name, pos, tier,
                opposing_team=enemy_team,
                allies_team=my_team,
            )
            for pos, name in my_team.items()
        }
        enemy_pick_breakdowns = {
            pos: compute_pick_breakdown(
                conn, name, pos, tier,
                opposing_team=my_team,
                allies_team=enemy_team,
            )
            for pos, name in enemy_team.items()
        }

    raw_sort = request.args.get("sort") or DRAFT_DEFAULT_SORT
    sort_key, sort_col, sort_desc = parse_sort(
        raw_sort, DRAFT_SORT_KEYS, DRAFT_DEFAULT_SORT,
    )
    if sort_col == "champion_name":
        candidates.sort(key=lambda c: (c[sort_col] or "").lower(), reverse=sort_desc)
    else:
        candidates.sort(
            key=lambda c: (c[sort_col] is None, c[sort_col] or 0),
            reverse=sort_desc,
        )
    # Always signed — see comment in `index()` route.
    sort_by = ("-" if sort_desc else "+") + sort_key

    return render_template(
        "draft.html",
        positions=POSITIONS,
        active=active,
        tier=tier,
        available_tiers=available_tiers,
        my_team=my_team,
        enemy_team=enemy_team,
        my_pick_breakdowns=my_pick_breakdowns,
        enemy_pick_breakdowns=enemy_pick_breakdowns,
        my_bans=my_bans_list,
        enemy_bans=enemy_bans_list,
        my_bans_str=",".join(my_bans_list),
        enemy_bans_str=",".join(enemy_bans_list),
        ban_slots=range(5),
        candidates=candidates,
        champ_names=champ_names,
        dd_version=get_dd_version(),
        sort_by=sort_by,
        sort_key=sort_key,
        sort_desc=sort_desc,
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

    sort_by = request.args.get("sort", "games")
    if sort_by not in MATCHUP_SORT_KEYS:
        sort_by = "games"

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
