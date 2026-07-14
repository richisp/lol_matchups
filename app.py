import os
import sqlite3
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

# Maps URL sort key → (row key, default-descending) for the champion index
# (attributes + comp fits view). The URL accepts a leading "-" to flip
# direction (e.g. "-name" sorts name descending).
CHAMPION_SORT_KEYS: dict[str, tuple[str, bool]] = {
    "name":      ("champion_name", False),
    "class":     ("subclass_label", False),
    "damage":    ("damage", True),
    "toughness": ("toughness", True),
    "control":   ("control", True),
    "mobility":  ("mobility", True),
    "utility":   ("utility", True),
    "f2b":       ("fit_f2b", True),
    "dive":      ("fit_dive", True),
    "poke":      ("fit_poke", True),
    "pick":      ("fit_pick", True),
    "split":     ("fit_split", True),
}
DEFAULT_SORT = "name"

# Same shape, but for the draft recs table. Column names map to candidate-dict
# keys (compute_draft_scores's output, not raw DB columns). The second block
# serves the Attributes tab, which shares the same signed sort param.
DRAFT_SORT_KEYS: dict[str, tuple[str, bool]] = {
    "name":    ("champion_name", False),
    "fit":     ("fit", True),
    "winrate": ("winrate", True),
    "counter": ("counter_total", True),
    "synergy": ("synergy_total", True),
    "risk":    ("blind_risk", False),
    "roles":   ("roles", True),
    "lane_share": ("lane_pr_share", True),
    "comp":    ("comp_align", True),
    "class":     ("subclass_label", False),
    "damage":    ("damage", True),
    "toughness": ("toughness", True),
    "control":   ("control", True),
    "mobility":  ("mobility", True),
    "utility":   ("utility", True),
    "f2b":       ("fit_f2b", True),
    "dive":      ("fit_dive", True),
    "poke":      ("fit_poke", True),
    "pick":      ("fit_pick", True),
    "split":     ("fit_split", True),
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


# ---------------------------------------------------------------------------
# Team-comp classification. Champion attributes (Riot's class/subclass tags +
# 0-3 attribute ratings, fetched by fetch_attributes.py) are turned into a
# soft 0-1 fit per comp archetype (config.TEAM_COMPS). Display-only for now —
# comp fits do NOT feed the fit score.

def compute_comp_fits(a: dict) -> dict[str, float]:
    """Comp fits for one champion_attributes row (expects a['subclasses'],
    the roles filtered to SUBCLASS_COMP_FIT tags). Base = max over the
    champ's subclasses, damped by how many it has — a single-subclass
    specialist gets the table at full strength, hybrids are diluted
    (see config.SUBCLASS_COUNT_DAMPING). Then small attribute nudges,
    clamped 0-1."""
    subs = a["subclasses"]
    damping = config.SUBCLASS_COUNT_DAMPING.get(
        len(subs), config.SUBCLASS_COUNT_DAMPING_MIN)
    fits = {
        comp: damping * max(
            (config.SUBCLASS_COMP_FIT[r][comp] for r in subs),
            default=0.0,
        )
        for comp in config.TEAM_COMPS
    }
    mobility = a["mobility"] or 0
    control = a["control"] or 0
    # Nudges: mobility+CC helps reach/lock the backline (dive), mobility is
    # the split-pusher's escape, CC is what converts a catch (pick).
    fits["dive"] += 0.05 * mobility + 0.05 * control
    fits["pick"] += 0.05 * control
    fits["split"] += 0.10 * mobility
    return {c: min(1.0, max(0.0, v)) for c, v in fits.items()}


_champion_attrs_cache: dict[str, dict] | None = None


def get_champion_attributes(conn) -> dict[str, dict]:
    """champion_name -> attributes row (+ derived comp_fits / display strings).
    Cached for the process lifetime, like available tiers. Returns {} when the
    table is missing (DB snapshot from before attributes existed)."""
    global _champion_attrs_cache
    if _champion_attrs_cache is None:
        try:
            rows = conn.execute("SELECT * FROM champion_attributes").fetchall()
        except sqlite3.OperationalError:
            return {}
        attrs: dict[str, dict] = {}
        for r in rows:
            a = dict(r)
            a["roles"] = [x for x in (a["roles"] or "").split(",") if x]
            # Base classes (FIGHTER/MAGE/TANK/SUPPORT) are dropped everywhere
            # — subclasses are strictly more accurate, and the hybrid damping
            # in compute_comp_fits keys off the *subclass* count.
            a["subclasses"] = [x for x in a["roles"] if x in config.SUBCLASS_COMP_FIT]
            a["comp_fits"] = compute_comp_fits(a)
            a["subclass_label"] = " · ".join(x.capitalize() for x in a["subclasses"])
            top = sorted(a["comp_fits"].items(), key=lambda kv: kv[1], reverse=True)
            a["comp_top"] = " · ".join(
                f"{config.COMP_LABELS[c]} {v:.1f}" for c, v in top[:2] if v >= 0.4
            )
            attrs[a["champion_name"]] = a
        if not attrs:
            # Don't cache an empty table — if it gets populated later (e.g. a
            # manual fetch_attributes.py run after a failed startup fetch),
            # the next request picks it up without an app restart.
            return {}
        _champion_attrs_cache = attrs
    return _champion_attrs_cache


def team_comp_profile(team: dict[str, str], attrs: dict[str, dict]) -> dict:
    """Aggregate a team's picks into a comp profile: mean comp fit per
    archetype (0-1), the leading comp(s), and the team attribute bars.
    With no attributed picks yet, returns a zeroed profile (bars at 0,
    values dashed) so the panels are always visible."""
    picked = [attrs[name] for name in team.values() if name in attrs]
    n = len(picked)
    comps = {
        c: (sum(a["comp_fits"][c] for a in picked) / n if n else 0.0)
        for c in config.TEAM_COMPS
    }
    best = max(comps.values())
    leading = [c for c, v in comps.items() if best > 0 and v >= best - 0.05]

    # Team attribute bars — the composition-gap dimensions rendered as
    # always-visible progress bars (not chips that only appear when broken).
    # `warn` turns a bar red; armed only from 3 picks up, since earlier two
    # more picks can still fill any gap.
    armed = n >= 3

    def avg(key: str) -> float:
        return sum((a[key] or 0) for a in picked) / n if n else 0.0

    def rating(key: str) -> str:
        return f"{avg(key):.1f}" if n else "—"

    engage_n = sum(
        1 for a in picked if set(a["subclasses"]) & config.ENGAGE_SUBCLASSES)
    # Peel = a kit that protects the carry: Warden/Enchanter, or high utility.
    peel_n = sum(
        1 for a in picked
        if set(a["subclasses"]) & config.PEEL_SUBCLASSES
        or (a["utility"] or 0) >= 2
    )
    has_marksman = any("MARKSMAN" in a["subclasses"] for a in picked)
    bars = [
        {"label": "Damage", "fill": avg("damage") / 3, "value": rating("damage"),
         "hint": "Average damage rating (0–3). Red: nobody hits damage 3 — no kill threat.",
         "warn": armed and not any((a["damage"] or 0) >= 3 for a in picked)},
        {"label": "Frontline", "fill": avg("toughness") / 3, "value": rating("toughness"),
         "hint": "Average toughness (0–3). Red: no toughness-3 pick — nobody to soak.",
         "warn": armed and not any((a["toughness"] or 0) >= 3 for a in picked)},
        {"label": "CC", "fill": avg("control") / 3, "value": rating("control"),
         "hint": "Average crowd control (0–3). Red: team average below 1.4.",
         "warn": armed and avg("control") < 1.4},
        {"label": "Engage", "fill": min(engage_n, 2) / 2, "value": str(engage_n) if n else "—",
         "hint": "Picks that can start a fight (Vanguard/Diver/Catcher). Red: none.",
         "warn": armed and engage_n == 0},
        {"label": "Peel", "fill": min(peel_n, 2) / 2, "value": str(peel_n) if n else "—",
         "hint": "Picks that protect a carry (Warden/Enchanter or utility ≥ 2). "
                 "Red: you have a marksman and nobody to peel for it.",
         "warn": armed and has_marksman and peel_n == 0},
    ]
    # Damage profile over the actual dealers (damage rating >= 2). Always
    # present so the AD/AP row renders (empty bar) before any dealer is picked.
    # A healthy comp wants at least 2 dealers of EACH type — 0 or 1 of either
    # side means the enemy can stack one resistance and blunt most of your
    # damage, so the row goes red.
    ad = sum(1 for a in picked
             if (a["damage"] or 0) >= 2 and a["adaptive_type"] == "PHYSICAL_DAMAGE")
    ap = sum(1 for a in picked
             if (a["damage"] or 0) >= 2 and a["adaptive_type"] == "MAGIC_DAMAGE")
    dmg_split = {"ad": ad, "ap": ap,
                 "warn": armed and (ad < 2 or ap < 2)}
    return {"comps": comps, "count": n, "leading": leading,
            "bars": bars, "dmg_split": dmg_split}


def comp_alignment(cand_fits: dict[str, float], profile: dict | None) -> float | None:
    """How well a candidate reinforces the comp direction `profile`'s team is
    already drafting toward: mean of the candidate's fit over the team's
    *leading* comp(s). 0-1; None without team context.

    Deliberately NOT a dot product over all five comps — that's a weighted
    average, so a flat-high generalist (the wiki tags most mobile marksmen
    ASSASSIN+MARKSMAN, giving them ~1.0 in four comps via max()) would top the
    list for every team. Scoring only the leading comp(s) makes the ranking
    actually change with the team's direction."""
    if not profile or not profile.get("leading"):
        return None
    return sum(cand_fits[c] for c in profile["leading"]) / len(profile["leading"])


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


def get_role_counts(conn, tier: str) -> dict[str, int]:
    """Map champion_name → number of distinct lanes it has `champion_stats`
    records in for `tier` (how many roles it's played as)."""
    rows = conn.execute(
        "SELECT champion_name, COUNT(DISTINCT lane) AS roles "
        "FROM champion_stats WHERE tier = ? GROUP BY champion_name",
        (tier,),
    ).fetchall()
    return {r["champion_name"]: r["roles"] for r in rows}


def get_total_pickrate(conn, tier: str) -> dict[str, float]:
    """Map champion_name → summed pickrate across all its lanes for `tier`.
    Denominator for each row's lane-share (lane PR / total PR)."""
    rows = conn.execute(
        "SELECT champion_name, COALESCE(SUM(pickrate), 0) AS total_pr "
        "FROM champion_stats WHERE tier = ? GROUP BY champion_name",
        (tier,),
    ).fetchall()
    return {r["champion_name"]: r["total_pr"] for r in rows}


def lane_pr_share(pickrate, total_pr) -> float | None:
    """Share (%) of a champion's total pick rate that comes from one lane.
    100 = only played in this lane. None when no pickrate data."""
    if not total_pr or pickrate is None:
        return None
    return pickrate / total_pr * 100.0


def get_champion_lanes(conn, champion_name: str, tier: str) -> list[str]:
    """Lanes (in canonical POSITIONS order) `champion_name` has records in for
    `tier`. Drives the role tabs on the champion page."""
    have = {
        r["lane"] for r in conn.execute(
            "SELECT DISTINCT lane FROM champion_stats WHERE champion_name = ? AND tier = ?",
            (champion_name, tier),
        )
    }
    return [p for p in POSITIONS if p in have]


_ATTR_LIST_KEYS = ("subclass_label", "damage", "toughness", "control",
                   "mobility", "utility", "comp_top")


def get_champion_list(lane: str, tier: str, sort_param: str):
    """Champion index rows: one per champion (attributes are champion-
    intrinsic, no lane/tier dimension). `lane` filters to champions with
    champion_stats records there; ALL also includes attribute-only champs.
    Champions without attribute data (brand-new releases) render as dashes
    and sort last."""
    with db.connect(config.DB_PATH) as conn:
        attrs = get_champion_attributes(conn)
        if lane == LANE_ALL:
            names = {r["champion_name"] for r in conn.execute(
                "SELECT DISTINCT champion_name FROM champion_stats WHERE tier = ?",
                (tier,),
            )} | set(attrs)
        else:
            names = {r["champion_name"] for r in conn.execute(
                "SELECT DISTINCT champion_name FROM champion_stats WHERE lane = ? AND tier = ?",
                (lane, tier),
            )}

    out = []
    for name in names:
        a = attrs.get(name)
        row = {"champion_name": name}
        if a:
            row.update({k: a[k] for k in _ATTR_LIST_KEYS})
            for comp in config.TEAM_COMPS:
                row[f"fit_{comp}"] = a["comp_fits"][comp]
            row["leading_comps"] = {
                c for c, v in a["comp_fits"].items()
                if v >= max(a["comp_fits"].values()) - 0.05
            }
        else:
            row.update(dict.fromkeys(_ATTR_LIST_KEYS))
            row.update({f"fit_{comp}": None for comp in config.TEAM_COMPS})
            row["leading_comps"] = set()
        out.append(row)

    _, col, desc = parse_sort(sort_param)
    # Alphabetical first, then the (stable) primary sort — ties inside equal
    # primary values stay alphabetical.
    out.sort(key=lambda r: r["champion_name"].lower())
    if col in ("champion_name", "subclass_label"):
        # None-flag flips with direction so no-data rows sort last either way.
        out.sort(key=lambda r: ((r[col] is None) != desc, (r[col] or "").lower()),
                 reverse=desc)
    else:
        out.sort(key=lambda r: ((r[col] is None) != desc, r[col] or 0), reverse=desc)
    return out


def _invert_winrate(winrate, matchup_type: str):
    """Winrate of the reverse matchup. lolalytics filters low-pickrate champs
    off an opponent's counter list, so `A vs B` can be missing while `B vs A`
    exists. A counter matchup is ~zero-sum, so A's WR vs B ≈ 100 − (B's WR vs A).
    Synergy is symmetric (same game outcome from either ally's perspective), so
    it carries over unchanged."""
    if winrate is None:
        return None
    return (100.0 - winrate) if matchup_type == "counter" else winrate


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
        # Reverse-direction rows (this champ as the *opponent*), to fill in
        # matchups missing from the direct list — see _invert_winrate.
        inv_rows = conn.execute(
            """
            SELECT champion_name AS opponent_name, champion_lane AS opponent_lane,
                   winrate, pickrate, games
              FROM matchups
             WHERE opponent_name = ?
               AND opponent_lane = ?
               AND tier = ?
               AND matchup_type = ?
               AND COALESCE(games, 0) >= ?
            """,
            (champion, lane, tier, matchup_type, min_games),
        ).fetchall()

    by_position: dict[str, list] = {p: [] for p in POSITIONS}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        pos = r["opponent_lane"]
        if pos in by_position:
            by_position[pos].append(dict(r))
            seen.add((r["opponent_name"], pos))
    for r in inv_rows:
        pos = r["opponent_lane"]
        if pos not in by_position or (r["opponent_name"], pos) in seen:
            continue  # prefer the direct row when both exist
        by_position[pos].append({
            "opponent_name": r["opponent_name"],
            "opponent_lane": pos,
            "winrate": _invert_winrate(r["winrate"], matchup_type),
            "pickrate": r["pickrate"],  # opponent's own PR; unused in the view
            "games": r["games"],
            "inferred": True,
        })

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
        comp_labels=config.COMP_LABELS,
        team_comps=config.TEAM_COMPS,
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

        # Reverse direction: picked opponent (as champion) vs candidate. Fills
        # pairs missing from the direct rows — e.g. a low-pickrate enemy that
        # doesn't appear on the candidate's own counter list. Winrate inverted
        # for counters; synergy is symmetric. See _invert_winrate.
        inv_rows = conn.execute(
            f"""
            SELECT champion_name AS opp_name, champion_lane AS opp_lane,
                   opponent_name AS cand_name, winrate, games
              FROM matchups
             WHERE tier = ?
               AND opponent_lane = ?
               AND matchup_type = ?
               AND champion_name IN ({name_ph})
               AND champion_lane IN ({lane_ph})
               AND opponent_name IN ({cand_ph})
            """,
            (tier, lane, matchup_type, *opp_names, *opp_lanes, *candidate_names),
        ).fetchall()
        for r in inv_rows:
            if opponents.get(r["opp_lane"]) != r["opp_name"]:
                continue
            key = (r["cand_name"], lane, r["opp_name"], r["opp_lane"], matchup_type)
            if key in matchup_lookup:
                continue  # prefer the direct row
            matchup_lookup[key] = {
                "champion_name": r["cand_name"],
                "opponent_name": r["opp_name"],
                "opponent_lane": r["opp_lane"],
                "winrate": _invert_winrate(r["winrate"], matchup_type),
                "games": r["games"],
                "inferred": True,
            }

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
                    "inferred": mu.get("inferred", False),
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
                    "inferred": mu.get("inferred", False),
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
        row = conn.execute(
            "SELECT winrate, games FROM matchups "
            "WHERE champion_name=? AND champion_lane=? "
            "AND opponent_name=? AND opponent_lane=? "
            "AND matchup_type=? AND tier=?",
            (champ, lane, opponent, opp_lane, kind, tier),
        ).fetchone()
        if row is not None:
            return dict(row)
        # Fall back to the reverse matchup (opponent vs this champ). See
        # _invert_winrate.
        inv = conn.execute(
            "SELECT winrate, games FROM matchups "
            "WHERE champion_name=? AND champion_lane=? "
            "AND opponent_name=? AND opponent_lane=? "
            "AND matchup_type=? AND tier=?",
            (opponent, opp_lane, champ, lane, kind, tier),
        ).fetchone()
        if inv is None:
            return None
        return {"winrate": _invert_winrate(inv["winrate"], kind),
                "games": inv["games"], "inferred": True}

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
                "inferred": bool(mu and mu.get("inferred")),
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
                "inferred": bool(mu and mu.get("inferred")),
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


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Get/set user runtime settings. Currently just `league_path` — the folder
    (or direct lockfile path) the LCU integration should read for the League
    client. POST a JSON body {"league_path": "..."} to update; empty clears it
    and reverts to auto-detection. Always returns the current lockfile status so
    the UI can tell the user whether the client was found."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        config.set_setting("league_path", (data.get("league_path") or "").strip())
    lockfile = lcu.find_lockfile()
    return jsonify({
        "league_path": config.get_setting("league_path", ""),
        "lockfile_found": lockfile is not None,
        "lockfile_path": str(lockfile) if lockfile else None,
        "default_paths": [str(p) for p in lcu.LOCKFILE_PATHS],
    })


def team_avg_score(breakdowns: dict) -> float | None:
    """Average fit score across a team's filled slots. None when the team has no
    picks. `breakdowns` is the {pos: pick_breakdown} dict, whose values each
    carry the champion's `fit` (base 50 + counter/synergy contribs)."""
    scores = [b["fit"] for b in breakdowns.values() if b.get("fit") is not None]
    return sum(scores) / len(scores) if scores else None


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
    # Which team the active slot belongs to. "enemy" turns the recs pane into
    # a scout: candidates for that enemy lane, countered by MY picks and
    # synergizing with THEIR picks.
    active_side = (request.args.get("active_side") or "my").lower()
    if active_side not in ("my", "enemy"):
        active_side = "my"

    my_team = parse_team("my")
    enemy_team = parse_team("enemy")
    # Manual comp target for the Comp column: pick flexible champs early with
    # a comp in mind instead of waiting for the allies' leading comp to
    # emerge. Empty = auto (score against picked allies' leading comp).
    comp_choice = (request.args.get("comp") or "").lower()
    if comp_choice not in config.TEAM_COMPS:
        comp_choice = ""
    # Recs pane tab: matchup-based table (winrates) or the attribute/comp-fit
    # view of the same candidates (attributes).
    view = request.args.get("view") or "winrates"
    if view not in ("winrates", "attributes"):
        view = "winrates"
    # LCU-detected bans split by team — drives the visual icon row under each
    # team; their union also drives scoring (excluded from candidates).
    my_bans_list = [b.strip() for b in (request.args.get("my_bans") or "").split(",") if b.strip()]
    enemy_bans_list = [b.strip() for b in (request.args.get("enemy_bans") or "").split(",") if b.strip()]
    bans = set(my_bans_list) | set(enemy_bans_list)

    # The drafting side's allies/opponents; the active slot is stripped from
    # the allies for scoring (it's the one being filled).
    drafting_allies = enemy_team if active_side == "enemy" else my_team
    drafting_opponents = my_team if active_side == "enemy" else enemy_team
    allies_for_scoring = {k: v for k, v in drafting_allies.items() if k != active}

    with db.connect(config.DB_PATH) as conn:
        candidates = compute_draft_scores(
            conn, active, tier,
            allies_for_scoring, drafting_opponents, bans,
        )
        role_counts = get_role_counts(conn, tier)
        total_pr = get_total_pickrate(conn, tier)
        attrs = get_champion_attributes(conn)
        # Comp alignment for the rec column: the manually selected comp when
        # one is chosen, otherwise scored against the drafting side's
        # *already-picked allies* (active slot excluded).
        align_profile = team_comp_profile(allies_for_scoring, attrs)
        for c in candidates:
            c["roles"] = role_counts.get(c["champion_name"], 0)
            c["lane_pr_share"] = lane_pr_share(c.get("pickrate"), total_pr.get(c["champion_name"]))
            a = attrs.get(c["champion_name"])
            if a is None:
                c["comp_align"] = None
            elif comp_choice:
                c["comp_align"] = a["comp_fits"][comp_choice]
            else:
                c["comp_align"] = comp_alignment(a["comp_fits"], align_profile)
            c["subclass_label"] = a["subclass_label"] if a else None
            c["comp_top"] = a["comp_top"] if a else None
            # Attribute ratings + per-comp fits for the Attributes tab.
            for key in ("damage", "toughness", "control", "mobility", "utility"):
                c[key] = a[key] if a else None
            for comp in config.TEAM_COMPS:
                c[f"fit_{comp}"] = a["comp_fits"][comp] if a else None
            c["leading_comps"] = {
                comp for comp, v in a["comp_fits"].items()
                if v >= max(a["comp_fits"].values()) - 0.05
            } if a else set()
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

    my_avg_score = team_avg_score(my_pick_breakdowns)
    enemy_avg_score = team_avg_score(enemy_pick_breakdowns)

    # Comp panels (per team, full picks including the active slot) + the
    # attribute line in every picked champion's hover tooltip.
    my_comp_profile = team_comp_profile(my_team, attrs)
    enemy_comp_profile = team_comp_profile(enemy_team, attrs)
    for breakdowns in (my_pick_breakdowns, enemy_pick_breakdowns):
        for bk in breakdowns.values():
            a = attrs.get(bk["champion_name"])
            bk["subclass_label"] = a["subclass_label"] if a else None
            bk["comp_top"] = a["comp_top"] if a else None

    # How each other picked champ moves the *active-slot* pick's score, so
    # the board can show — next to every other champ — the same contribution
    # the hover tooltip lists. `ally_impact` renders on MY team's slots,
    # `enemy_impact` on the enemy's; which breakdown feeds which flips with
    # the active side (its counters point at the opposing team). Both maps
    # are empty until the active slot is filled.
    active_bk = (enemy_pick_breakdowns if active_side == "enemy"
                 else my_pick_breakdowns).get(active)
    active_champ = drafting_allies.get(active)
    enemy_impact = {}
    ally_impact = {}
    if active_bk:
        counter_target = ally_impact if active_side == "enemy" else enemy_impact
        synergy_target = enemy_impact if active_side == "enemy" else ally_impact
        for it in active_bk["counter_breakdown"]:
            counter_target[it["lane"]] = it
        for it in active_bk["synergy_breakdown"]:
            synergy_target[it["lane"]] = it

    raw_sort = request.args.get("sort") or DRAFT_DEFAULT_SORT
    sort_key, sort_col, sort_desc = parse_sort(
        raw_sort, DRAFT_SORT_KEYS, DRAFT_DEFAULT_SORT,
    )
    if sort_col in ("champion_name", "subclass_label"):
        candidates.sort(
            key=lambda c: ((c[sort_col] is None) != sort_desc, (c[sort_col] or "").lower()),
            reverse=sort_desc,
        )
    else:
        # No-data rows must sort last in BOTH directions: the None flag has to
        # flip with sort_desc, since reverse=True would otherwise put it first.
        candidates.sort(
            key=lambda c: ((c[sort_col] is None) != sort_desc, c[sort_col] or 0),
            reverse=sort_desc,
        )
    # Always signed — see comment in `index()` route.
    sort_by = ("-" if sort_desc else "+") + sort_key

    return render_template(
        "draft.html",
        positions=POSITIONS,
        active=active,
        active_side=active_side,
        tier=tier,
        available_tiers=available_tiers,
        my_team=my_team,
        enemy_team=enemy_team,
        my_avg_score=my_avg_score,
        enemy_avg_score=enemy_avg_score,
        my_comp_profile=my_comp_profile,
        enemy_comp_profile=enemy_comp_profile,
        comp_choice=comp_choice,
        view=view,
        comp_labels=config.COMP_LABELS,
        team_comps=config.TEAM_COMPS,
        my_pick_breakdowns=my_pick_breakdowns,
        enemy_pick_breakdowns=enemy_pick_breakdowns,
        active_champ=active_champ,
        enemy_impact=enemy_impact,
        ally_impact=ally_impact,
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

    tier = request.args.get("tier") or available_tiers[0]
    if tier not in available_tiers:
        tier = available_tiers[0]

    with db.connect(config.DB_PATH) as conn:
        champion_lanes = get_champion_lanes(conn, champion_name, tier)

    # Default to the requested lane if the champ has records there; otherwise
    # fall back to its first recorded lane (BOT if it has none at all).
    requested_lane = (request.args.get("lane") or "").upper()
    if requested_lane in champion_lanes:
        lane = requested_lane
    elif champion_lanes:
        lane = champion_lanes[0]
    else:
        lane = "BOT"

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
        champion_lanes=champion_lanes,
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
