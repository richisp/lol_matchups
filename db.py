import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid TEXT PRIMARY KEY,
    game_name TEXT,
    tag_line TEXT,
    crawl_status TEXT NOT NULL DEFAULT 'pending',
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    crawled_at TEXT,
    tier TEXT,
    division TEXT,
    lp INTEGER,
    rank_fetched_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_players_status ON players(crawl_status);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    queue_id INTEGER NOT NULL,
    game_version TEXT,
    patch TEXT,
    game_creation INTEGER,
    game_duration INTEGER,
    winning_team INTEGER,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_matches_queue ON matches(queue_id);

CREATE TABLE IF NOT EXISTS match_participants (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    champion_id INTEGER NOT NULL,
    champion_name TEXT NOT NULL,
    team_position TEXT,
    win INTEGER NOT NULL,
    PRIMARY KEY (match_id, puuid),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_participants_match ON match_participants(match_id);
CREATE INDEX IF NOT EXISTS idx_participants_champion ON match_participants(champion_id);
CREATE INDEX IF NOT EXISTS idx_participants_position ON match_participants(team_position);

CREATE TABLE IF NOT EXISTS match_bans (
    match_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    champion_id INTEGER NOT NULL,
    pick_turn INTEGER NOT NULL,
    PRIMARY KEY (match_id, team_id, pick_turn),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_bans_champion ON match_bans(champion_id);
"""


def extract_patch(game_version: str | None) -> str | None:
    if not game_version:
        return None
    parts = game_version.split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def _add_column_if_missing(conn, table: str, column: str, decl: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn) -> None:
    _add_column_if_missing(conn, "players", "tier", "TEXT")
    _add_column_if_missing(conn, "players", "division", "TEXT")
    _add_column_if_missing(conn, "players", "lp", "INTEGER")
    _add_column_if_missing(conn, "players", "rank_fetched_at", "TEXT")
    _add_column_if_missing(conn, "matches", "patch", "TEXT")

    rows = conn.execute(
        "SELECT match_id, game_version FROM matches WHERE patch IS NULL AND game_version IS NOT NULL"
    ).fetchall()
    for row in rows:
        patch = extract_patch(row[1])
        if patch:
            conn.execute("UPDATE matches SET patch = ? WHERE match_id = ?", (patch, row[0]))

    conn.execute("CREATE INDEX IF NOT EXISTS idx_players_tier ON players(tier)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_patch ON matches(patch)")


def init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def cleanup_outside_patches(conn, allowed_patches: set[str]) -> int:
    placeholders = ",".join("?" * len(allowed_patches))
    args = list(allowed_patches)
    doomed = [
        row[0]
        for row in conn.execute(
            f"SELECT match_id FROM matches WHERE patch IS NULL OR patch NOT IN ({placeholders})",
            args,
        )
    ]
    if not doomed:
        return 0
    qmarks = ",".join("?" * len(doomed))
    conn.execute(f"DELETE FROM match_participants WHERE match_id IN ({qmarks})", doomed)
    conn.execute(f"DELETE FROM match_bans WHERE match_id IN ({qmarks})", doomed)
    conn.execute(f"DELETE FROM matches WHERE match_id IN ({qmarks})", doomed)
    return len(doomed)


@contextmanager
def connect(path: Path):
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_player(conn, puuid: str, game_name: str | None = None, tag_line: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO players (puuid, game_name, tag_line)
        VALUES (?, ?, ?)
        ON CONFLICT(puuid) DO UPDATE SET
            game_name = COALESCE(excluded.game_name, players.game_name),
            tag_line  = COALESCE(excluded.tag_line,  players.tag_line)
        """,
        (puuid, game_name, tag_line),
    )


def mark_player(conn, puuid: str, status: str) -> None:
    conn.execute(
        "UPDATE players SET crawl_status = ?, crawled_at = datetime('now') WHERE puuid = ?",
        (status, puuid),
    )


def update_player_rank(conn, puuid: str, entry: dict | None) -> None:
    if entry is None:
        conn.execute(
            """
            UPDATE players
               SET tier = 'UNRANKED', division = NULL, lp = NULL,
                   rank_fetched_at = datetime('now')
             WHERE puuid = ?
            """,
            (puuid,),
        )
    else:
        conn.execute(
            """
            UPDATE players
               SET tier = ?, division = ?, lp = ?,
                   rank_fetched_at = datetime('now')
             WHERE puuid = ?
            """,
            (entry["tier"], entry["rank"], entry["leaguePoints"], puuid),
        )


def next_pending_player(conn) -> str | None:
    row = conn.execute(
        "SELECT puuid FROM players WHERE crawl_status = 'pending' ORDER BY discovered_at LIMIT 1"
    ).fetchone()
    return row["puuid"] if row else None


def match_exists(conn, match_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    return row is not None


def insert_match(conn, match: dict, patch: str) -> None:
    info = match["info"]
    metadata = match["metadata"]
    match_id = metadata["matchId"]

    winning_team = next((t["teamId"] for t in info["teams"] if t["win"]), None)

    conn.execute(
        """
        INSERT OR IGNORE INTO matches
            (match_id, queue_id, game_version, patch, game_creation, game_duration, winning_team, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            info["queueId"],
            info.get("gameVersion"),
            patch,
            info.get("gameCreation"),
            info.get("gameDuration"),
            winning_team,
            json.dumps(match),
        ),
    )

    for p in info["participants"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO match_participants
                (match_id, puuid, team_id, champion_id, champion_name, team_position, win)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                p["puuid"],
                p["teamId"],
                p["championId"],
                p["championName"],
                p.get("teamPosition") or None,
                int(p["win"]),
            ),
        )

    for team in info["teams"]:
        for ban in team.get("bans", []):
            if ban["championId"] == -1:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO match_bans
                    (match_id, team_id, champion_id, pick_turn)
                VALUES (?, ?, ?, ?)
                """,
                (match_id, team["teamId"], ban["championId"], ban["pickTurn"]),
            )


def stats(conn) -> dict:
    return {
        "players_total": conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        "players_pending": conn.execute(
            "SELECT COUNT(*) FROM players WHERE crawl_status = 'pending'"
        ).fetchone()[0],
        "players_done": conn.execute(
            "SELECT COUNT(*) FROM players WHERE crawl_status = 'done'"
        ).fetchone()[0],
        "matches_total": conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
    }
