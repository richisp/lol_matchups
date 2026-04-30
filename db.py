import sqlite3
from contextlib import contextmanager
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS champion_stats (
    champion_name TEXT NOT NULL,
    lane          TEXT NOT NULL,
    tier          TEXT NOT NULL,
    winrate       REAL,
    pickrate      REAL,
    banrate       REAL,
    games         INTEGER,
    tier_badge    TEXT,
    scraped_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_name, lane, tier)
);

CREATE INDEX IF NOT EXISTS idx_champion_stats_lane ON champion_stats(lane, tier);

CREATE TABLE IF NOT EXISTS matchups (
    champion_name  TEXT NOT NULL,
    champion_lane  TEXT NOT NULL,
    opponent_name  TEXT NOT NULL,
    opponent_lane  TEXT NOT NULL,
    matchup_type   TEXT NOT NULL,  -- 'counter' or 'synergy'
    tier           TEXT NOT NULL,
    winrate        REAL,
    pickrate       REAL,
    games          INTEGER,
    scraped_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (champion_name, champion_lane, opponent_name, opponent_lane, matchup_type, tier)
);

CREATE INDEX IF NOT EXISTS idx_matchups_focal     ON matchups(champion_name, champion_lane, tier);
CREATE INDEX IF NOT EXISTS idx_matchups_type_lane ON matchups(matchup_type, champion_lane, opponent_lane, tier);

CREATE TABLE IF NOT EXISTS scrape_runs (
    champion_name TEXT NOT NULL,
    lane          TEXT NOT NULL,
    tier          TEXT NOT NULL,
    status        TEXT NOT NULL,  -- 'ok', 'empty', 'error'
    last_attempt  TEXT NOT NULL DEFAULT (datetime('now')),
    note          TEXT,
    PRIMARY KEY (champion_name, lane, tier)
);
"""


def init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


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


def _parse_pct(text: str | None) -> float | None:
    if not text:
        return None
    t = text.replace("%", "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    t = text.replace(",", "").strip()
    try:
        return int(t)
    except ValueError:
        return None


def upsert_champion_stats(conn, champion: str, lane: str, tier: str, overall: dict) -> None:
    """`overall` keys: winrate, pickrate, banrate, games, tier (badge like 'A+')."""
    conn.execute(
        """
        INSERT INTO champion_stats
            (champion_name, lane, tier, winrate, pickrate, banrate, games, tier_badge, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(champion_name, lane, tier) DO UPDATE SET
            winrate    = excluded.winrate,
            pickrate   = excluded.pickrate,
            banrate    = excluded.banrate,
            games      = excluded.games,
            tier_badge = excluded.tier_badge,
            scraped_at = excluded.scraped_at
        """,
        (
            champion,
            lane,
            tier,
            _parse_pct(overall.get("winrate")),
            _parse_pct(overall.get("pickrate")),
            _parse_pct(overall.get("banrate")),
            _parse_int(overall.get("games")),
            # split("?")[0] strips lolalytics' tooltip "?" suffix that's
            # sometimes appended to the tier badge text (e.g. "S+?").
            (overall.get("tier") or "").strip().split("?")[0] or None,
        ),
    )


def upsert_matchup(
    conn,
    champion_name: str,
    champion_lane: str,
    opponent_name: str,
    opponent_lane: str,
    matchup_type: str,
    tier: str,
    winrate: float | None,
    pickrate: float | None,
    games: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO matchups
            (champion_name, champion_lane, opponent_name, opponent_lane,
             matchup_type, tier, winrate, pickrate, games, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(champion_name, champion_lane, opponent_name, opponent_lane, matchup_type, tier)
        DO UPDATE SET
            winrate    = excluded.winrate,
            pickrate   = excluded.pickrate,
            games      = excluded.games,
            scraped_at = excluded.scraped_at
        """,
        (champion_name, champion_lane, opponent_name, opponent_lane,
         matchup_type, tier, winrate, pickrate, games),
    )


def store_scrape_result(conn, data: dict) -> tuple[int, int]:
    """Persist one scrape_champion() result. Returns (matchup_count, has_overall)."""
    champ = data["champion"]
    lane = config.LANE_TO_POSITION.get(data["lane"].lower(), data["lane"].upper())
    tier = data["tier"]

    overall = data.get("overall") or {}
    if overall:
        upsert_champion_stats(conn, champ, lane, tier, overall)

    matchup_count = 0
    for matchup_type, key in [("counter", "strong_against"), ("synergy", "good_synergy")]:
        sections = data.get(key, [])
        # Only refresh this matchup_type when the scrape actually returned
        # data for it. An empty list means the scrape didn't see anything
        # (failed tab click, layout change, etc.) — in that case, don't wipe
        # what we already have on disk.
        if not sections:
            continue
        # Replace the previous batch wholesale so opponents that dropped out
        # of lolalytics' top-N for this (champ, lane, tier, type) are removed
        # rather than lingering at stale winrates/games forever.
        conn.execute(
            """
            DELETE FROM matchups
             WHERE champion_name=? AND champion_lane=? AND tier=? AND matchup_type=?
            """,
            (champ, lane, tier, matchup_type),
        )
        for row in sections:
            opp_lane = row["position"]
            for c in row["champs"]:
                stats = c.get("stats", [])
                # lolalytics order: WR, Delta1, Delta2, PR, Games
                wr = _parse_pct(stats[0]) if len(stats) > 0 else None
                pr = _parse_pct(stats[3]) if len(stats) > 3 else None
                games = _parse_int(stats[4]) if len(stats) > 4 else None
                upsert_matchup(
                    conn, champ, lane, c["name"], opp_lane,
                    matchup_type, tier, wr, pr, games,
                )
                matchup_count += 1
    return matchup_count, bool(overall)


def mark_scrape_run(conn, champion: str, lane: str, tier: str, status: str, note: str = "") -> None:
    conn.execute(
        """
        INSERT INTO scrape_runs (champion_name, lane, tier, status, last_attempt, note)
        VALUES (?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(champion_name, lane, tier) DO UPDATE SET
            status = excluded.status,
            last_attempt = excluded.last_attempt,
            note = excluded.note
        """,
        (champion, lane, tier, status, note),
    )


def already_scraped(conn, champion: str, lane: str, tier: str) -> bool:
    row = conn.execute(
        "SELECT status FROM scrape_runs WHERE champion_name=? AND lane=? AND tier=?",
        (champion, lane, tier),
    ).fetchone()
    return row is not None and row["status"] == "ok"


def stats(conn) -> dict:
    return {
        "champions_scraped": conn.execute("SELECT COUNT(*) FROM champion_stats").fetchone()[0],
        "matchups_total":    conn.execute("SELECT COUNT(*) FROM matchups").fetchone()[0],
        "scrape_ok":         conn.execute("SELECT COUNT(*) FROM scrape_runs WHERE status='ok'").fetchone()[0],
        "scrape_empty":      conn.execute("SELECT COUNT(*) FROM scrape_runs WHERE status='empty'").fetchone()[0],
        "scrape_error":      conn.execute("SELECT COUNT(*) FROM scrape_runs WHERE status='error'").fetchone()[0],
    }
