"""Fetch Riot-authored champion attributes into the champion_attributes table.

Sources (both static JSON, no browser needed):
  - Riot Data Dragon: canonical champion list (id -> display name). Same
    source fetch_champion_list() uses, so names are guaranteed to match the
    champion_name values in champion_stats.
  - Meraki Analytics (cdn.merakianalytics.com): a maintained JSON mirror of
    the LoL wiki's champion data. Provides Riot's class/subclass tags
    (VANGUARD, ENCHANTER, ARTILLERY, ...) and the champ-select attribute
    ratings (damage/toughness/control/mobility/utility, 0-3, plus
    abilityReliance 0-100).
  - CommunityDragon (raw.communitydragon.org): fallback for champions Meraki
    doesn't have yet (the wiki is human-curated and trails new releases by
    days/weeks). Ships Riot's own champ-select data straight from the game
    client, so it's never stale — but its `roles` are base classes only
    (assassin/fighter/mage/...), no subclasses, so comp fits for these champs
    lean on whatever tags overlap the subclass table (ASSASSIN, MARKSMAN)
    until the wiki catches up and Meraki takes over again.

Upsert-only: a failed fetch leaves the existing rows untouched, so staleness
is harmless and the crawl workflow runs this with continue-on-error.

Usage:
    python fetch_attributes.py
"""

import json
import logging
import sqlite3
import sys
import urllib.request

import config
import db

log = logging.getLogger(__name__)

MERAKI_URL = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions.json"
CDRAGON_URL = ("https://raw.communitydragon.org/latest/plugins/"
               "rcp-be-lol-game-data/global/default/v1/champions/{key}.json")

# CommunityDragon tacticalInfo.damageType → Meraki-style adaptive type.
# kMixed maps to None (no single adaptive profile).
_CD_DAMAGE_TYPE = {"kPhysical": "PHYSICAL_DAMAGE", "kMagic": "MAGIC_DAMAGE"}

# Structural sanity floor: Data Dragon lists ~170 champions; if we matched far
# fewer, one of the sources changed shape — bail without writing.
MIN_EXPECTED = 150


def _fetch_json(url: str, timeout: int = 60):
    # CommunityDragon 403s the default Python-urllib user agent.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _row_from_cdragon(dd_id: str, info: dict) -> dict | None:
    """Build an attribute row from CommunityDragon's per-champion JSON
    (playstyleInfo maps 1:1 onto Meraki's attributeRatings; roles are
    base classes uppercased). None on any failure."""
    try:
        d = _fetch_json(CDRAGON_URL.format(key=info["key"]), timeout=30)
    except Exception:  # noqa: BLE001 — caller treats as still-unmatched
        return None
    ps = d.get("playstyleInfo") or {}
    ti = d.get("tacticalInfo") or {}
    if not ps:
        return None
    style = ti.get("style")  # 0-10, spell-reliance; Meraki's scale is 0-100
    return {
        "champion_name": info["name"],
        "riot_id": dd_id,
        "roles": ",".join(r.upper() for r in d.get("roles") or []),
        "damage": ps.get("damage"),
        "toughness": ps.get("durability"),
        "control": ps.get("crowdControl"),
        "mobility": ps.get("mobility"),
        "utility": ps.get("utility"),
        "ability_reliance": style * 10 if isinstance(style, int) else None,
        "difficulty": ti.get("difficulty"),
        "adaptive_type": _CD_DAMAGE_TYPE.get(ti.get("damageType")),
    }


def build_rows() -> tuple[list[dict], list[str], list[str]]:
    """Join Data Dragon (canonical names) with Meraki (ratings, roles),
    falling back to CommunityDragon for champions Meraki lacks.
    Returns (rows, cdragon_fallback_ids, unmatched_dd_ids)."""
    version = _fetch_json("https://ddragon.leagueoflegends.com/api/versions.json")[0]
    dd = _fetch_json(
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    )["data"]
    meraki = _fetch_json(MERAKI_URL)

    rows: list[dict] = []
    fallback: list[str] = []
    unmatched: list[str] = []
    for dd_id, info in dd.items():
        m = meraki.get(dd_id)  # Meraki is keyed by the same Riot ids
        if not m:
            row = _row_from_cdragon(dd_id, info)
            if row:
                rows.append(row)
                fallback.append(dd_id)
            else:
                unmatched.append(dd_id)
            continue
        ratings = m.get("attributeRatings") or {}
        rows.append({
            "champion_name": info["name"],
            "riot_id": dd_id,
            "roles": ",".join(m.get("roles") or []),
            "damage": ratings.get("damage"),
            "toughness": ratings.get("toughness"),
            "control": ratings.get("control"),
            "mobility": ratings.get("mobility"),
            "utility": ratings.get("utility"),
            "ability_reliance": ratings.get("abilityReliance"),
            "difficulty": ratings.get("difficulty"),
            "adaptive_type": m.get("adaptiveType"),
        })
    return rows, fallback, unmatched


def has_attributes() -> bool:
    try:
        with db.connect(config.DB_PATH) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM champion_attributes"
            ).fetchone()[0] > 0
    except sqlite3.OperationalError:  # table doesn't exist
        return False


def ensure_attributes() -> None:
    """Self-heal for the app: populate champion_attributes when the synced DB
    lacks it. The crawler bakes attributes into the db-latest snapshot, but a
    snapshot from before that (or a crawl whose attribute step flaked) would
    otherwise leave the comp UI empty until the next crawl. Call after
    sync_db() and before Flask opens connections. Best-effort — failures only
    log; the app degrades to no comp data."""
    if has_attributes():
        return
    log.info("champion_attributes missing from DB — fetching.")
    rc = main()
    log.info("attribute fetch %s", "ok" if rc == 0 else f"failed (rc={rc})")


def main() -> int:
    try:
        rows, fallback, unmatched = build_rows()
    except Exception as e:  # network / schema failure — keep the old rows
        print(f"attribute fetch failed: {e}", file=sys.stderr)
        return 1

    if fallback:
        print(f"cdragon fallback (no Meraki data yet): {', '.join(sorted(fallback))}")
    if unmatched:
        print(f"warning: no data from any source for: {', '.join(sorted(unmatched))}")
    if len(rows) < MIN_EXPECTED:
        print(f"only {len(rows)} champions matched (< {MIN_EXPECTED}) — refusing to write",
              file=sys.stderr)
        return 1

    db.init_db(config.DB_PATH)
    with db.connect(config.DB_PATH) as conn:
        for row in rows:
            db.upsert_champion_attributes(conn, row)
    print(f"stored attributes for {len(rows)} champions -> {config.DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
