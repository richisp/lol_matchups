import time

import httpx

DATA_DRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"


def fetch_recent_patches(count: int, timeout: float = 10.0) -> list[str]:
    """Return the N most recent distinct LoL patches (e.g. ['16.9', '16.8']).

    Data Dragon publishes the full version list with the newest first. Each
    patch may have multiple sub-versions (16.9.1, 16.9.2) — we collapse to
    'major.minor'.
    """
    r = httpx.get(DATA_DRAGON_VERSIONS_URL, timeout=timeout)
    r.raise_for_status()
    versions = r.json()
    seen: list[str] = []
    for v in versions:
        parts = v.split(".")
        if len(parts) < 2:
            continue
        patch = f"{parts[0]}.{parts[1]}"
        if patch not in seen:
            seen.append(patch)
            if len(seen) >= count:
                break
    return seen


class RiotClient:
    def __init__(
        self,
        api_key: str,
        regional_host: str,
        platform_host: str,
        min_interval_seconds: float = 1.3,
        timeout: float = 10.0,
    ):
        if not api_key:
            raise ValueError("RIOT_API_KEY is empty — set it in .env")
        self.regional_host = regional_host
        self.platform_host = platform_host
        self.min_interval = min_interval_seconds
        self._last_call = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Riot-Token": api_key},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._client.close()

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, params: dict | None = None):
        for attempt in range(5):
            self._throttle()
            r = self._client.get(url, params=params)
            self._last_call = time.monotonic()

            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                print(f"  [404] {url} params={params}")
                return None
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "1"))
                print(f"  [429] rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                backoff = 2**attempt
                print(f"  [{r.status_code}] server error, sleeping {backoff}s")
                time.sleep(backoff)
                continue
            print(f"  [{r.status_code}] {url} body={r.text[:200]}")
            r.raise_for_status()
        raise RuntimeError(f"giving up on {url} after 5 attempts")

    def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict | None:
        url = f"{self.regional_host}/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return self._get(url)

    def get_match_ids(
        self,
        puuid: str,
        queue: int,
        start: int = 0,
        count: int = 100,
        start_time: int | None = None,
    ) -> list[str] | None:
        url = f"{self.regional_host}/lol/match/v5/matches/by-puuid/{puuid}/ids"
        params: dict = {"queue": queue, "start": start, "count": count}
        if start_time is not None:
            params["startTime"] = start_time
        return self._get(url, params=params)

    def get_match(self, match_id: str) -> dict | None:
        url = f"{self.regional_host}/lol/match/v5/matches/{match_id}"
        return self._get(url)

    def get_solo_rank(self, puuid: str) -> dict | None:
        """Returns the player's RANKED_SOLO_5x5 entry, or None if unranked."""
        url = f"{self.platform_host}/lol/league/v4/entries/by-puuid/{puuid}"
        entries = self._get(url)
        if not entries:
            return None
        return next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
