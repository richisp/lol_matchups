import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "").strip()

REGIONAL_HOST = "https://europe.api.riotgames.com"
PLATFORM_HOST = "https://euw1.api.riotgames.com"

RANKED_SOLO_QUEUE_ID = 420

# How many recent patches to keep (current + N-1 previous).
PATCH_WINDOW = 2

# Force a specific patch set, bypassing Data Dragon. Useful for testing or if
# Data Dragon is down. Format: {"16.9", "16.8"}. Leave None to auto-detect.
ALLOWED_PATCHES_OVERRIDE: set[str] | None = None

# Match-list pre-filter: skip match IDs older than this. Coarser than the
# patch filter — used to avoid fetching matches we'll throw away. Set
# generously to cover the patch window with buffer (~2 patches ≈ 4 weeks).
RECENT_MATCHES_DAYS = 35

DB_PATH = Path(__file__).parent / "matches.db"

# Spacing between Riot API requests. Dev key allows 100 req / 2 min, so 1.3s
# keeps us safely under (~46 req/min). Lower for production keys.
MIN_REQUEST_INTERVAL_SECONDS = 1.3
