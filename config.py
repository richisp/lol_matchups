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

DEFAULT_TIER = os.environ.get("LOLALYTICS_TIER", "emerald_plus")

LANES = ["top", "jungle", "middle", "bottom", "support"]

# Map URL lane → display position (used in DB)
LANE_TO_POSITION = {
    "top":     "TOP",
    "jungle":  "JUNGLE",
    "middle":  "MID",
    "bottom":  "BOT",
    "support": "SUPPORT",
}

# How much my pick choice in lane X depends on the enemy pick in lane Y.
# Concentrated on direct lane interactions — see chat history for reasoning.
# Each row sums to 100. Used by the blind-pick score and the future champ-select helper.
COUNTER_WEIGHTS = {
    "TOP":     {"TOP": 85, "JUNGLE":  8, "MID":  3, "BOT":  2, "SUPPORT":  2},
    "JUNGLE":  {"TOP": 15, "JUNGLE": 40, "MID": 18, "BOT": 15, "SUPPORT": 12},
    "MID":     {"TOP":  3, "JUNGLE": 10, "MID": 80, "BOT":  4, "SUPPORT":  3},
    "BOT":     {"TOP":  2, "JUNGLE":  5, "MID":  3, "BOT": 50, "SUPPORT": 40},
    "SUPPORT": {"TOP":  2, "JUNGLE":  5, "MID":  3, "BOT": 45, "SUPPORT": 45},
}

# Same idea, for synergy with teammate in lane Y. Diagonal is 0 — you don't
# have a teammate in your own lane. Each row sums to 100.
SYNERGY_WEIGHTS = {
    "TOP":     {"TOP":  0, "JUNGLE": 40, "MID": 25, "BOT": 20, "SUPPORT": 15},
    "JUNGLE":  {"TOP": 22, "JUNGLE":  0, "MID": 35, "BOT": 22, "SUPPORT": 21},
    "MID":     {"TOP": 20, "JUNGLE": 50, "MID":  0, "BOT": 10, "SUPPORT": 20},
    "BOT":     {"TOP":  5, "JUNGLE": 15, "MID": 10, "BOT":  0, "SUPPORT": 70},
    "SUPPORT": {"TOP":  5, "JUNGLE": 15, "MID": 10, "BOT": 70, "SUPPORT":  0},
}

# A counter-matchup is "bad" if the focal champion's winrate against it is below
# this threshold. Used by the blind-pick score.
BLIND_PICK_BAD_WR_THRESHOLD = 48.0
