"""Derive empirical COUNTER_WEIGHTS / SYNERGY_WEIGHTS from real matches.

Given Riot match + timeline payloads (match-v5), this measures how much time
each role spends physically near each other role, and turns those proximity
seconds into the same row-normalized weight tables used in config.py:

    fit(my_lane, other_lane) ∝ time my_lane spends near other_lane

Output rows use the canonical positions from config.POSITIONS and each row sums
to 100 (integers), so the printed tables are drop-in comparable with
config.COUNTER_WEIGHTS / config.SYNERGY_WEIGHTS.

Usage:
    # From local JSON files (match + timeline as returned by match-v5):
    python proximity_weights.py --match match.json --timeline timeline.json

    # Or fetch straight from the Riot API (needs RIOT_API_KEY in env):
    python proximity_weights.py --riot-id "Richis#EUW" --region europe --count 50
"""

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict

import config

# Riot match-v5 `teamPosition` strings → our canonical positions (config.POSITIONS).
TEAM_POSITION_TO_ROLE = {
    "TOP": "TOP",
    "JUNGLE": "JUNGLE",
    "MIDDLE": "MID",
    "BOTTOM": "BOT",
    "UTILITY": "SUPPORT",
}

# Valid regional routing values for the match-v5 endpoints.
REGIONAL_ROUTES = {"americas", "asia", "europe", "sea"}


class ProximityAnalyzer:
    """Measures per-role proximity seconds over a single match timeline."""

    def __init__(self, match, timeline, threshold=1200):
        self.match = match
        self.timeline = timeline
        self.threshold = threshold

    def analyze(self):
        synergy_seconds = new_seconds_table()
        counter_seconds = new_seconds_table()
        self.accumulate_into(synergy_seconds, counter_seconds)
        return weights_from_seconds(synergy_seconds, counter_seconds)

    def accumulate_into(self, synergy_seconds, counter_seconds):
        """Add this match's proximity seconds into shared accumulators.

        Mutates the two passed-in nested dicts (see new_seconds_table) so the
        same tables can be summed across many matches before normalizing.
        """
        participants = self._build_participants()
        frames = self.timeline.get("info", {}).get("frames", [])

        for frame, next_frame in zip(frames, frames[1:]):
            duration_seconds = (
                next_frame["timestamp"] - frame["timestamp"]
            ) / 1000.0

            participant_frames = frame.get("participantFrames", {})

            for source_id, source in participants.items():
                for target_id, target in participants.items():
                    if source_id == target_id:
                        continue

                    source_pos = participant_frames.get(str(source_id), {}).get("position")
                    target_pos = participant_frames.get(str(target_id), {}).get("position")

                    if not source_pos or not target_pos:
                        continue

                    if not self._near(source_pos, target_pos):
                        continue

                    bucket = (
                        synergy_seconds
                        if source["team_id"] == target["team_id"]
                        else counter_seconds
                    )

                    bucket[source["role"]][target["role"]] += duration_seconds

    def _build_participants(self):
        participants = {}

        for participant in self.match.get("info", {}).get("participants", []):
            role = TEAM_POSITION_TO_ROLE.get(participant.get("teamPosition"))

            if not role:
                continue

            participants[participant["participantId"]] = {
                "role": role,
                "team_id": participant["teamId"],
                "champion": participant.get("championName"),
                "summoner": participant.get("summonerName") or participant.get("riotIdGameName"),
            }

        return participants

    def _near(self, pos1, pos2):
        dx = pos1["x"] - pos2["x"]
        dy = pos1["y"] - pos2["y"]

        return math.sqrt((dx * dx) + (dy * dy)) <= self.threshold


def new_seconds_table():
    """A {source_role: {target_role: seconds}} accumulator."""
    return defaultdict(lambda: defaultdict(float))


def weights_from_seconds(synergy_seconds, counter_seconds):
    """Turn accumulated proximity seconds into config-shaped weight tables."""
    return {
        "synergy": _normalize(synergy_seconds, include_diagonal=False),
        "counter": _normalize(counter_seconds, include_diagonal=True),
        "raw_seconds": {
            "synergy": _round_seconds_table(synergy_seconds),
            "counter": _round_seconds_table(counter_seconds),
        },
    }


def _normalize(seconds_by_role, *, include_diagonal):
    """Row-normalize proximity seconds to integer weights summing to 100.

    Produces a full row for every position in config.POSITIONS (missing
    roles get 0) so the output is shaped like config.COUNTER_WEIGHTS /
    config.SYNERGY_WEIGHTS. Synergy excludes the diagonal (no teammate in
    your own lane); counter keeps it (your direct lane opponent).
    """
    result = {}

    for source_role in config.POSITIONS:
        target_seconds = seconds_by_role.get(source_role, {})

        row_seconds = {
            target_role: target_seconds.get(target_role, 0.0)
            for target_role in config.POSITIONS
            if include_diagonal or target_role != source_role
        }

        result[source_role] = _to_int_weights(row_seconds)

    return result


def _to_int_weights(row_seconds):
    """Scale a {role: seconds} row to integer percentages summing to 100.

    Uses largest-remainder rounding so the row total is exactly 100 (or all
    zeros when there was no proximity data for the source role).
    """
    total = sum(row_seconds.values())

    if total == 0:
        return {role: 0 for role in row_seconds}

    exact = {role: (seconds / total) * 100 for role, seconds in row_seconds.items()}
    floored = {role: int(value) for role, value in exact.items()}
    remainder = 100 - sum(floored.values())

    # Hand the leftover points to the roles with the largest fractional parts.
    order = sorted(
        exact,
        key=lambda role: exact[role] - floored[role],
        reverse=True,
    )
    for role in order[:remainder]:
        floored[role] += 1

    return floored


def _round_seconds_table(nested):
    """Convert a nested defaultdict of seconds to plain dicts, rounded to 0.1s."""
    return {
        source_role: {t: round(s, 1) for t, s in target_roles.items()}
        for source_role, target_roles in nested.items()
    }


def _format_table(name, table):
    """Render a weight table as a pasteable Python dict literal."""
    lines = [f"{name} = {{"]
    for source_role in config.POSITIONS:
        row = table[source_role]
        cells = ", ".join(
            f'"{target}": {row.get(target, 0):>2}'
            for target in config.POSITIONS
            if target in row
        )
        lines.append(f'    "{source_role}":{" " * (8 - len(source_role))}{{{cells}}},')
    lines.append("}")
    return "\n".join(lines)


def _check_region(region):
    if region not in REGIONAL_ROUTES:
        raise SystemExit(
            f"--region must be one of {sorted(REGIONAL_ROUTES)} (regional routing), got {region!r}"
        )


def _api_get(client, url, params=None):
    """GET a Riot endpoint, transparently waiting out 429 rate limits."""
    while True:
        resp = client.get(url, params=params)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"# rate limited, waiting {wait}s ...", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()


def resolve_match_ids(client, riot_id, region, count, *, match_type="ranked"):
    """Riot ID (GameName#TagLine) → PUUID → most recent `count` match ids.

    match_type filters the match list: "ranked" covers both solo/duo (420)
    and flex (440); "all" applies no filter (includes normals/ARAM/etc.).
    """
    if "#" not in riot_id:
        raise SystemExit("--riot-id must look like GameName#TagLine, e.g. Richis#EUW")
    game_name, tag_line = riot_id.rsplit("#", 1)

    acct = _api_get(
        client,
        f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
    )
    puuid = acct["puuid"]

    params = {"start": 0, "count": count}
    if match_type != "all":
        params["type"] = match_type
    match_ids = _api_get(
        client,
        f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids",
        params=params,
    )

    if not match_ids:
        raise SystemExit(f"no recent matches found for {riot_id}")
    return match_ids


def fetch_match_and_timeline(client, match_id, region):
    """Fetch a match + its timeline from the Riot match-v5 API."""
    base = f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return _api_get(client, base), _api_get(client, f"{base}/timeline")


def aggregate_weights(match_timeline_pairs, threshold):
    """Sum proximity seconds across many (match, timeline) pairs, then normalize."""
    synergy_seconds = new_seconds_table()
    counter_seconds = new_seconds_table()
    for match, timeline in match_timeline_pairs:
        ProximityAnalyzer(match, timeline, threshold).accumulate_into(
            synergy_seconds, counter_seconds
        )
    return weights_from_seconds(synergy_seconds, counter_seconds)


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match", help="path to a match-v5 match JSON file")
    parser.add_argument("--timeline", help="path to a match-v5 timeline JSON file")
    parser.add_argument("--match-id", help="match id to fetch from the Riot API, e.g. EUW1_123")
    parser.add_argument(
        "--riot-id",
        help="Riot ID (GameName#TagLine) — fetches this player's most recent match",
    )
    parser.add_argument(
        "--region",
        default="europe",
        help="regional routing for --match-id/--riot-id (americas/asia/europe/sea)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="with --riot-id, how many recent matches to average over (default 1)",
    )
    parser.add_argument(
        "--type",
        dest="match_type",
        default="ranked",
        choices=["ranked", "normal", "tourney", "all"],
        help="with --riot-id, which match types to include "
        "(default 'ranked' = solo/duo + flex; 'all' = no filter)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=1200,
        help="proximity radius in map units (default 1200, ~lane-trading distance)",
    )
    parser.add_argument(
        "--raw", action="store_true", help="also print raw proximity seconds"
    )
    args = parser.parse_args(argv)

    if args.match_id or args.riot_id:
        api_key = os.environ.get("RIOT_API_KEY")
        if not api_key:
            raise SystemExit("RIOT_API_KEY env var is required when fetching from the API")
        _check_region(args.region)

        import httpx

        with httpx.Client(headers={"X-Riot-Token": api_key}, timeout=30.0) as client:
            if args.match_id:
                match_ids = [args.match_id]
            else:
                match_ids = resolve_match_ids(
                    client, args.riot_id, args.region, args.count,
                    match_type=args.match_type,
                )
            pairs = []
            for i, match_id in enumerate(match_ids, 1):
                print(f"# fetching {i}/{len(match_ids)}: {match_id}", file=sys.stderr)
                pairs.append(fetch_match_and_timeline(client, match_id, args.region))
    elif args.match and args.timeline:
        pairs = [(_load_json(args.match), _load_json(args.timeline))]
    else:
        raise SystemExit("provide --riot-id, --match-id, or both --match and --timeline")

    weights = aggregate_weights(pairs, args.threshold)

    print(f"# aggregated over {len(pairs)} match(es)")
    print(_format_table("COUNTER_WEIGHTS", weights["counter"]))
    print()
    print(_format_table("SYNERGY_WEIGHTS", weights["synergy"]))

    if args.raw:
        print("\n# raw proximity seconds:")
        print(json.dumps(weights["raw_seconds"], indent=2))


if __name__ == "__main__":
    main()
