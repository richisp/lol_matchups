import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# When run as a PyInstaller-frozen exe, look for the DB next to the .exe
# (so the user can drop in / update lolalytics.db without rebuilding).
# Otherwise, look next to this source file.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

DB_PATH = APP_DIR / "lolalytics.db"

# User-editable runtime settings (League install path, etc.), persisted next to
# the .exe so they survive auto-updates. Kept separate from the DB so the
# sqlite file-lock ordering gotcha (sync before connect) doesn't apply.
SETTINGS_PATH = APP_DIR / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def get_setting(key: str, default=None):
    return load_settings().get(key, default)


def set_setting(key: str, value) -> None:
    """Persist a single setting. An empty/None value removes the key so it
    falls back to auto-detection."""
    settings = load_settings()
    if value in (None, ""):
        settings.pop(key, None)
    else:
        settings[key] = value
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass

DEFAULT_TIER = os.environ.get("LOLALYTICS_TIER", "emerald_plus")

# GitHub repo (owner/name) used for DB syncing and app auto-update. The
# crawler workflow pushes daily DB snapshots to the `db-latest` release on
# this repo; the app pulls from there. Public access is required on either
# the repo or its release assets — see WINDOWS.md for setup notes.
GITHUB_REPO: str = os.environ.get("LOL_MATCHUPS_REPO", "richisp/lol_matchups")

# Canonical position keys used everywhere the app refers to a role.
POSITIONS: tuple[str, ...] = ("TOP", "JUNGLE", "MID", "BOT", "SUPPORT")

# Lolalytics-style URL lane names (lowercase) — used by the crawler/scraper
# and as inputs to LANE_TO_POSITION below.
LANES: list[str] = ["top", "jungle", "middle", "bottom", "support"]

# Lolalytics URL lane → our canonical position.
LANE_TO_POSITION: dict[str, str] = {
    "top":     "TOP",
    "jungle":  "JUNGLE",
    "middle":  "MID",
    "bottom":  "BOT",
    "support": "SUPPORT",
}

# League Client (LCU) assignedPosition strings → our canonical position.
# Note "utility" — that's how Riot names the support slot.
LCU_POSITION_MAP: dict[str, str] = {
    "top":     "TOP",
    "jungle":  "JUNGLE",
    "middle":  "MID",
    "bottom":  "BOT",
    "utility": "SUPPORT",
}

# How much my pick choice in lane X depends on the enemy pick in lane Y.
# Derived empirically by proximity_weights.py — time each role spends physically
# near each other role, aggregated over 50 ranked matches (2026-07-19, 1200-unit
# radius). Global-max normalized: every cell in COUNTER_WEIGHTS and
# SYNERGY_WEIGHTS is divided by the single largest cell across both (the
# bot<->support duo, which anchors at 100 in SYNERGY_WEIGHTS), so one weight unit
# means the same shared time everywhere — counter vs synergy and lane vs lane.
# Rows are NOT forced to sum to 100: diffuse-proximity lanes (jungle) carry less
# total weight because their matchups matter less. Used by the blind-pick score
# and the champ-select helper.
COUNTER_WEIGHTS = {
    "TOP":     {"TOP": 59, "JUNGLE": 15, "MID": 13, "BOT": 10, "SUPPORT": 11},
    "JUNGLE":  {"TOP": 15, "JUNGLE": 22, "MID": 17, "BOT": 18, "SUPPORT": 19},
    "MID":     {"TOP": 13, "JUNGLE": 17, "MID": 55, "BOT": 14, "SUPPORT": 15},
    "BOT":     {"TOP": 10, "JUNGLE": 18, "MID": 14, "BOT": 44, "SUPPORT": 39},
    "SUPPORT": {"TOP": 11, "JUNGLE": 19, "MID": 15, "BOT": 39, "SUPPORT": 39},
}

# Same idea, for synergy with teammate in lane Y — same proximity run, same
# global-max scale, so a counter weight and a synergy weight of equal value mean
# equal shared time. Diagonal is 0 (no teammate in your own lane). The
# bot<->support cell is the global maximum, hence 100.
SYNERGY_WEIGHTS = {
    "TOP":     {"TOP":  0, "JUNGLE": 25, "MID": 21, "BOT": 21, "SUPPORT": 24},
    "JUNGLE":  {"TOP": 25, "JUNGLE":  0, "MID": 31, "BOT": 31, "SUPPORT": 35},
    "MID":     {"TOP": 21, "JUNGLE": 31, "MID":  0, "BOT": 25, "SUPPORT": 33},
    "BOT":     {"TOP": 21, "JUNGLE": 31, "MID": 25, "BOT":  0, "SUPPORT": 100},
    "SUPPORT": {"TOP": 24, "JUNGLE": 35, "MID": 33, "BOT": 100, "SUPPORT":  0},
}

# A counter-matchup is "bad" if the focal champion's winrate against it is below
# this threshold. Used by the blind-pick score.
BLIND_PICK_BAD_WR_THRESHOLD = 48.0

# Matchup sample-size handling in the fit score. Below MIN_MATCHUP_GAMES a
# matchup is no-data (contributes 0, winrate not shown). From there its
# contribution is scaled linearly by games/GAMES_FULL_CONFIDENCE, reaching
# full weight (×1.0) at GAMES_FULL_CONFIDENCE games — so a 35-game 60% WR
# row nudges the score at ×0.35 instead of swinging it as hard as a
# 10,000-game row.
MIN_MATCHUP_GAMES = 30
GAMES_FULL_CONFIDENCE = 100

# ---------------------------------------------------------------------------
# Team-comp classification (draws on champion_attributes; see app.py's
# compute_comp_fits / team_comp_profile).
#
# The three macro comp archetypes. Pick/dive/wombo are variations of forcing
# a fight (engage); split-push is a pressure variant of the same coin; and
# front-to-back / protect-the-carry / disengage are one family (keep the
# carry alive, win the extended fight). Champions get a soft 0-1 fit per
# archetype — multi-membership, not exclusive assignment.
TEAM_COMPS: tuple[str, ...] = ("engage", "poke", "protect")

COMP_LABELS: dict[str, str] = {
    "engage":  "Engage",
    "poke":    "Poke",
    "protect": "F2B",
}

def _c(engage: float, poke: float, protect: float) -> dict[str, float]:
    return {"engage": engage, "poke": poke, "protect": protect}


# Riot subclass tag -> base comp fit (0-1). Subclasses carry the signal
# attribute ratings can't: CC *direction*. A Vanguard (Malphite) and a Warden
# (Braum) both rate toughness 3 / control 3, but one initiates dives and the
# other peels for a front-to-back comp.
#
# Broad base classes (FIGHTER/MAGE/TANK/SUPPORT — Meraki mixes them into the
# same `roles` list) are deliberately NOT in this table: every champion has
# at least one subclass-level tag, and subclasses are strictly more accurate.
# Judgment lives in this one transparent, tweakable table instead of in
# hand-ratings of 170 champions.
SUBCLASS_COMP_FIT: dict[str, dict[str, float]] = {
    #                engage poke  protect
    "VANGUARD":   _c(1.0,   0.0,  0.3),
    "WARDEN":     _c(0.3,   0.3,  1.0),
    "ENCHANTER":  _c(0.0,   0.5,  1.0),
    "CATCHER":    _c(0.7,   0.5,  0.5),   # hooks initiate; their CC also peels
    "ARTILLERY":  _c(0.0,   1.0,  0.5),   # poke core; range also serves disengage
    "BURST":      _c(0.5,   0.5,  0.3),
    "BATTLEMAGE": _c(0.7,   0.3,  0.5),
    "MARKSMAN":   _c(0.3,   0.5,  1.0),   # the protect-comp centerpiece
    "ASSASSIN":   _c(0.7,   0.0,  0.0),
    "SKIRMISHER": _c(0.5,   0.0,  0.3),
    "JUGGERNAUT": _c(0.5,   0.0,  0.3),
    "DIVER":      _c(1.0,   0.0,  0.0),
    "SPECIALIST": _c(0.3,   0.5,  0.3),
}

# Hybrid damping: a champion's subclass table values are multiplied by this,
# keyed by how many subclasses it has. A pure Marksman (Jinx) is the real
# protect-comp hypercarry deal; an Assassin+Marksman+Specialist hybrid (Quinn) is a
# jack-of-all-trades and shouldn't get full points anywhere. (The wiki's
# subclass list is alphabetical — no primary/secondary ordering exists — so
# damping is uniform per champion rather than per-position.)
SUBCLASS_COUNT_DAMPING: dict[int, float] = {1: 1.0, 2: 0.7}
SUBCLASS_COUNT_DAMPING_MIN = 0.5  # 3 or more subclasses

# Subclasses that provide *initiation* — used by the "No engage" team warning.
ENGAGE_SUBCLASSES: frozenset[str] = frozenset({"VANGUARD", "DIVER", "CATCHER"})

# Subclasses whose kit protects a carry — used by the "No peel" team warning
# (a high-utility pick of any subclass also counts; see team_comp_profile).
PEEL_SUBCLASSES: frozenset[str] = frozenset({"WARDEN", "ENCHANTER"})
