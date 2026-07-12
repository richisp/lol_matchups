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
# Derived empirically by proximity_weights.py — the share of time each role
# spends physically near each other role, aggregated over 50 ranked matches
# (2026-06-25, 1200-unit radius). Each row sums to 100. Used by the blind-pick
# score and the champ-select helper.
COUNTER_WEIGHTS = {
    "TOP":     {"TOP": 61, "JUNGLE": 11, "MID":  9, "BOT": 10, "SUPPORT":  9},
    "JUNGLE":  {"TOP": 15, "JUNGLE": 26, "MID": 20, "BOT": 18, "SUPPORT": 21},
    "MID":     {"TOP":  9, "JUNGLE": 15, "MID": 52, "BOT": 12, "SUPPORT": 12},
    "BOT":     {"TOP":  8, "JUNGLE": 12, "MID": 11, "BOT": 36, "SUPPORT": 33},
    "SUPPORT": {"TOP":  8, "JUNGLE": 14, "MID": 11, "BOT": 34, "SUPPORT": 33},
}

# Same idea, for synergy with teammate in lane Y, from the same proximity run.
# Diagonal is 0 — you don't have a teammate in your own lane. Each row sums to 100.
SYNERGY_WEIGHTS = {
    "TOP":     {"TOP":  0, "JUNGLE": 28, "MID": 21, "BOT": 24, "SUPPORT": 27},
    "JUNGLE":  {"TOP": 20, "JUNGLE":  0, "MID": 26, "BOT": 25, "SUPPORT": 29},
    "MID":     {"TOP": 17, "JUNGLE": 29, "MID":  0, "BOT": 25, "SUPPORT": 29},
    "BOT":     {"TOP": 11, "JUNGLE": 16, "MID": 15, "BOT":  0, "SUPPORT": 58},
    "SUPPORT": {"TOP": 12, "JUNGLE": 18, "MID": 16, "BOT": 54, "SUPPORT":  0},
}

# A counter-matchup is "bad" if the focal champion's winrate against it is below
# this threshold. Used by the blind-pick score.
BLIND_PICK_BAD_WR_THRESHOLD = 48.0

# ---------------------------------------------------------------------------
# Team-comp classification (draws on champion_attributes; see app.py's
# compute_comp_fits / team_comp_profile).
#
# The five canonical comp archetypes. Champions get a soft 0-1 fit score for
# each — multi-membership, not exclusive assignment (Orianna legitimately
# fits three comps; Fiora is ~pure split).
TEAM_COMPS: tuple[str, ...] = ("f2b", "dive", "poke", "pick", "split")

COMP_LABELS: dict[str, str] = {
    "f2b":   "Front-to-back",
    "dive":  "Dive",
    "poke":  "Poke",
    "pick":  "Pick",
    "split": "Split",
}

def _c(f2b: float, dive: float, poke: float, pick: float, split: float) -> dict[str, float]:
    return {"f2b": f2b, "dive": dive, "poke": poke, "pick": pick, "split": split}


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
    #                f2b   dive  poke  pick  split
    "VANGUARD":   _c(0.3,  1.0,  0.0,  0.5,  0.0),
    "WARDEN":     _c(1.0,  0.0,  0.5,  0.0,  0.3),
    "ENCHANTER":  _c(1.0,  0.0,  0.5,  0.3,  0.0),
    "CATCHER":    _c(0.3,  0.3,  0.5,  1.0,  0.0),
    "ARTILLERY":  _c(0.0,  0.0,  1.0,  0.5,  0.0),
    "BURST":      _c(0.3,  0.5,  0.3,  1.0,  0.0),
    "BATTLEMAGE": _c(0.7,  0.7,  0.3,  0.0,  0.0),
    "MARKSMAN":   _c(1.0,  0.3,  0.5,  0.0,  0.3),
    "ASSASSIN":   _c(0.0,  0.7,  0.0,  1.0,  0.5),
    "SKIRMISHER": _c(0.3,  0.3,  0.0,  0.0,  1.0),
    "JUGGERNAUT": _c(0.5,  0.5,  0.0,  0.0,  0.5),
    "DIVER":      _c(0.0,  1.0,  0.0,  0.5,  0.3),
    "SPECIALIST": _c(0.3,  0.3,  0.3,  0.3,  0.3),
}

# Hybrid damping: a champion's subclass table values are multiplied by this,
# keyed by how many subclasses it has. A pure Marksman (Jinx) is the real
# f2b-hypercarry deal; an Assassin+Marksman+Specialist hybrid (Quinn) is a
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
