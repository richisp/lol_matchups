import time

import config
import db
from riot_client import RiotClient, fetch_recent_patches


def resolve_allowed_patches() -> set[str]:
    if config.ALLOWED_PATCHES_OVERRIDE:
        return config.ALLOWED_PATCHES_OVERRIDE
    return set(fetch_recent_patches(count=config.PATCH_WINDOW))


def seed_player(riot: RiotClient, conn, riot_id: str) -> str:
    if "#" not in riot_id:
        raise ValueError(f"riot id must be 'GameName#TAG', got: {riot_id!r}")
    name, tag = riot_id.rsplit("#", 1)
    account = riot.get_account_by_riot_id(name, tag)
    if not account:
        raise ValueError(f"account not found: {riot_id}")
    db.upsert_player(conn, account["puuid"], name, tag)
    conn.commit()
    return account["puuid"]


def crawl_player(
    riot: RiotClient,
    conn,
    puuid: str,
    matches_per_player: int,
    allowed_patches: set[str],
) -> tuple[int, int]:
    start_time = int(time.time()) - config.RECENT_MATCHES_DAYS * 86400

    try:
        match_ids = riot.get_match_ids(
            puuid,
            queue=config.RANKED_SOLO_QUEUE_ID,
            count=matches_per_player,
            start_time=start_time,
        )
    except Exception as e:
        print(f"  failed match-ids: {e}")
        db.mark_player(conn, puuid, "failed")
        conn.commit()
        return 0, 0

    if match_ids is None:
        db.mark_player(conn, puuid, "failed")
        conn.commit()
        return 0, 0

    new_count = 0
    skipped = 0
    for mid in match_ids:
        if db.match_exists(conn, mid):
            continue
        try:
            match = riot.get_match(mid)
        except Exception as e:
            print(f"  failed match {mid}: {e}")
            continue
        if match is None:
            continue

        patch = db.extract_patch(match["info"].get("gameVersion"))
        if patch not in allowed_patches:
            skipped += 1
            continue

        db.insert_match(conn, match, patch)
        for p in match["info"]["participants"]:
            db.upsert_player(conn, p["puuid"])
        conn.commit()
        new_count += 1

    try:
        rank = riot.get_solo_rank(puuid)
        db.update_player_rank(conn, puuid, rank)
    except Exception as e:
        print(f"  failed rank fetch: {e}")

    db.mark_player(conn, puuid, "done")
    conn.commit()
    return new_count, skipped


def run_crawl(seed_riot_id: str, max_players: int, matches_per_player: int) -> None:
    db.init_db(config.DB_PATH)

    allowed_patches = resolve_allowed_patches()
    print(f"allowed patches: {sorted(allowed_patches, reverse=True)}")

    with db.connect(config.DB_PATH) as conn:
        purged = db.cleanup_outside_patches(conn, allowed_patches)
        if purged:
            print(f"purged {purged} matches outside allowed patches")

    with (
        RiotClient(
            config.RIOT_API_KEY,
            regional_host=config.REGIONAL_HOST,
            platform_host=config.PLATFORM_HOST,
            min_interval_seconds=config.MIN_REQUEST_INTERVAL_SECONDS,
        ) as riot,
        db.connect(config.DB_PATH) as conn,
    ):
        seed_puuid = seed_player(riot, conn, seed_riot_id)
        print(f"seeded {seed_riot_id} -> {seed_puuid[:16]}...")

        crawled = 0
        while crawled < max_players:
            puuid = db.next_pending_player(conn)
            if puuid is None:
                print("no more pending players")
                break
            crawled += 1
            print(f"[{crawled}/{max_players}] {puuid[:16]}...")
            new, skipped = crawl_player(riot, conn, puuid, matches_per_player, allowed_patches)
            extra = f" ({skipped} skipped: outside patches)" if skipped else ""
            print(f"  +{new} new matches{extra}")

        s = db.stats(conn)
        print(
            f"\ndone. players: {s['players_total']} "
            f"(done={s['players_done']}, pending={s['players_pending']}) | "
            f"matches: {s['matches_total']}"
        )
