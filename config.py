import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).parent / "lolalytics.db"

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
