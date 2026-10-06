"""
Database Layer for Trendshift Pipeline
Handles SQLite connection, schema creation, and idempotent upserts.

language_filter distinguishes the overall ranking ("all") from
per-language rankings ("Python", "Rust", etc.).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
import json
from datetime import datetime, timezone
from typing import Dict, Any

try:
    from extractor import slice_matches_language
except ImportError:
    from src.extractor import slice_matches_language


DB_FILE = "trendshift.db"

# Banned spam/malware repositories evicted from DB and exports
BANNED_REPOSITORIES = {
    "postlayerrespect26/fps-booster-for-wiindows",
    "primedrobulwark/discord-server-booster",
    "wavebureaucrat/fps-booster",
    "daggerconsole/metatrader-4-boost",
    "liquidgiraffe8/metatrader-5-plus-edge",
    "driftpremierplay/pia-vpn-boost",
    "galaxydirectorcrack/indesign-setup",
}


def is_banned_repository(full_name: str) -> bool:
    if not full_name:
        return False
    return full_name.lower().strip() in BANNED_REPOSITORIES


def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def wal_checkpoint(conn: sqlite3.Connection) -> None:
    """Checkpoints WAL log into the main database file and truncates the WAL file."""
    try:
        conn.commit()
    except Exception:
        pass
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")


def evict_banned_repositories(conn: sqlite3.Connection) -> int:
    """
    Deletes all banned/malware repositories and their associated snapshots.
    Returns total deleted snapshot rows.
    """
    deleted_snapshots = 0
    with conn:
        for banned in BANNED_REPOSITORIES:
            cur = conn.execute(
                "DELETE FROM snapshots WHERE LOWER(repository_full_name) = ?",
                (banned,),
            )
            deleted_snapshots += cur.rowcount
            conn.execute(
                "DELETE FROM repositories WHERE LOWER(full_name) = ?",
                (banned,),
            )
    return deleted_snapshots


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

            DROP INDEX IF EXISTS idx_snapshots_lookup;
            DROP INDEX IF EXISTS idx_snapshots_lang_filter;

            CREATE INDEX IF NOT EXISTS idx_snapshots_export
            ON snapshots (timeframe, language_filter, period_key DESC, rank ASC);

            CREATE INDEX IF NOT EXISTS idx_repos_lang
            ON repositories (language);
        """)
    evict_banned_repositories(conn)
    wal_checkpoint(conn)


def upsert_snapshot(
    conn: sqlite3.Connection,
    item: Dict[str, Any],
    timeframe: str,
    period_key: str,
    language_filter: str = "all",
) -> None:
    full_name = item.get("full_name") or ""
    if not full_name or is_banned_repository(full_name):
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

    raw_tags = item.get("tags") or []
    tags = [t.get("slug") or t.get("name") for t in raw_tags if isinstance(t, dict)]
    raw_socials = item.get("social_mention_platforms") or []
    social_mentions = [s for s in raw_socials if s is not None]

    fetched_at = datetime.now(timezone.utc).isoformat()

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
    if not slice_matches_language(items, language_filter):
        raise ValueError(
            f"slice does not match language_filter={language_filter!r}"
        )
    with conn:
        conn.execute(
            "DELETE FROM snapshots WHERE timeframe = ? AND period_key = ? AND language_filter = ?",
            (timeframe, period_key, language_filter),
        )
        for item in items:
            upsert_snapshot(conn, item, timeframe, period_key, language_filter)
