"""
Database Layer for Trendshift Pipeline
Handles SQLite connection, schema creation, and idempotent upserts.

language_filter distinguishes the overall ranking ("all") from
per-language rankings ("Python", "Rust", etc.).
"""

import sqlite3
import json
from datetime import datetime, timezone
from typing import Dict, Any

DB_FILE = "trendshift.db"


def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS repositories (
                full_name    TEXT PRIMARY KEY,
                description  TEXT,
                language     TEXT,
                created_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                timeframe             TEXT NOT NULL CHECK(timeframe IN ('daily', 'weekly', 'monthly', 'yearly')),
                period_key            TEXT NOT NULL,
                language_filter       TEXT NOT NULL DEFAULT 'all',
                repository_full_name  TEXT NOT NULL REFERENCES repositories(full_name) ON DELETE CASCADE,
                rank                  INTEGER NOT NULL,
                score                 INTEGER,
                language              TEXT,
                stars_total           INTEGER,
                stars_gained          INTEGER,
                forks_total           INTEGER,
                forks_gained          INTEGER,
                tags_json             TEXT,
                social_mentions_json  TEXT,
                fetched_at            TEXT NOT NULL,
                PRIMARY KEY (timeframe, period_key, language_filter, repository_full_name)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
            ON snapshots (timeframe, period_key, language_filter, rank);

            CREATE INDEX IF NOT EXISTS idx_snapshots_lang_filter
            ON snapshots (language_filter);

            CREATE INDEX IF NOT EXISTS idx_repos_lang
            ON repositories (language);
        """)


def upsert_snapshot(
    conn: sqlite3.Connection,
    item: Dict[str, Any],
    timeframe: str,
    period_key: str,
    language_filter: str = "all",
) -> None:
    full_name = item.get("full_name") or ""
    if not full_name:
        return

    description = item.get("repository_description") or ""
    language = item.get("language") or item.get("repository_language") or ""
    created_at = item.get("repository_created_at") or ""

    rank = item.get("rank", 0)
    score = item.get("score", 0)
    stars_total = item.get("repository_stars", 0)
    stars_gained = item.get("repository_stars_gained", 0)
    forks_total = item.get("repository_forks", 0)
    forks_gained = item.get("repository_forks_gained", 0)

    tags = [t.get("slug") or t.get("name") for t in item.get("tags", []) if isinstance(t, dict)]
    social_mentions = item.get("social_mention_platforms", [])

    fetched_at = datetime.now(timezone.utc).isoformat()

    with conn:
        conn.execute("""
            INSERT INTO repositories (full_name, description, language, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET
                description = COALESCE(NULLIF(excluded.description, ''), repositories.description),
                language = CASE WHEN excluded.language != '' THEN excluded.language ELSE repositories.language END,
                created_at = COALESCE(NULLIF(excluded.created_at, ''), repositories.created_at);
        """, (full_name, description, language, created_at))

        conn.execute("""
            INSERT INTO snapshots (
                timeframe, period_key, language_filter, repository_full_name,
                rank, score, language,
                stars_total, stars_gained, forks_total, forks_gained,
                tags_json, social_mentions_json, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(timeframe, period_key, language_filter, repository_full_name) DO UPDATE SET
                rank = excluded.rank,
                score = excluded.score,
                language = excluded.language,
                stars_total = excluded.stars_total,
                stars_gained = excluded.stars_gained,
                forks_total = excluded.forks_total,
                forks_gained = excluded.forks_gained,
                tags_json = excluded.tags_json,
                social_mentions_json = excluded.social_mentions_json,
                fetched_at = excluded.fetched_at;
        """, (
            timeframe, period_key, language_filter, full_name,
            rank, score, language,
            stars_total, stars_gained, forks_total, forks_gained,
            json.dumps(tags), json.dumps(social_mentions), fetched_at
        ))


def replace_snapshot_slice(
    conn: sqlite3.Connection,
    items: list[Dict[str, Any]],
    timeframe: str,
    period_key: str,
    language_filter: str = "all",
) -> None:
    """
    Replaces an entire snapshot slice for (timeframe, period_key, language_filter)
    with the new ranking list, evicting any old dropouts.
    """
    with conn:
        conn.execute(
            "DELETE FROM snapshots WHERE timeframe = ? AND period_key = ? AND language_filter = ?",
            (timeframe, period_key, language_filter),
        )
        for item in items:
            upsert_snapshot(conn, item, timeframe, period_key, language_filter)


def prune_ghost_dropouts(conn: sqlite3.Connection) -> int:
    """
    Prunes ghost dropouts across all DB slices where snapshot count exceeds 25.
    For each slice, keeps only the 25 records with the latest fetched_at timestamp.
    Returns total deleted rows.
    """
    slices = conn.execute("""
        SELECT timeframe, period_key, language_filter, COUNT(*) as cnt
        FROM snapshots
        GROUP BY timeframe, period_key, language_filter
        HAVING cnt > 25
    """).fetchall()

    deleted_total = 0
    with conn:
        for tf, pk, lf, cnt in slices:
            cur = conn.execute("""
                DELETE FROM snapshots
                WHERE timeframe = ? AND period_key = ? AND language_filter = ?
                  AND repository_full_name NOT IN (
                      SELECT repository_full_name
                      FROM snapshots
                      WHERE timeframe = ? AND period_key = ? AND language_filter = ?
                      ORDER BY fetched_at DESC, rank ASC
                      LIMIT 25
                  )
            """, (tf, pk, lf, tf, pk, lf))
            deleted_total += cur.rowcount

    return deleted_total
