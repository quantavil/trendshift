"""
JSON Exporter for Trendshift Pipeline
Queries trendshift.db and exports:
  - data/{timeframe}/{timeframe}-all.json      (overall top-25, language_filter='all')
  - data/{timeframe}/{timeframe}-{lang}.json    (per-language top-25, language_filter=lang)
"""

import json
import os
import shutil
import sqlite3
import sys
from typing import Any, Dict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_connection, DB_FILE, BANNED_REPOSITORIES, is_banned_repository

OUTPUT_DIR = "data"


def sanitize_filename(name: str) -> str:
    s = name.lower().strip()
    s = s.replace("c++", "cpp").replace("c#", "csharp")
    s = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in s)
    s = "-".join(filter(None, s.split("-")))
    return s or "unknown"


def _safe_json_loads(raw: Any) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def format_row(r: sqlite3.Row) -> Dict[str, Any]:
    tags = _safe_json_loads(r["tags_json"])
    socials = _safe_json_loads(r["social_mentions_json"])

    return {
        "period_key": r["period_key"],
        "rank": r["rank"],
        "score": r["score"],
        "full_name": r["full_name"],
        "github_url": f"https://github.com/{r['full_name']}",
        "description": r["description"],
        "language": r["language"] or "Unknown",
        "stars_total": r["stars_total"],
        "stars_gained": r["stars_gained"],
        "forks_total": r["forks_total"],
        "forks_gained": r["forks_gained"],
        "created_at": r["created_at"],
        "tags": tags,
        "social_mention_platforms": socials,
        "timeframe": r["timeframe"],
        "language_filter": r["language_filter"],
    }


def write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def _swap_into_place(tmp_dir: str, out_dir: str) -> None:
    """Rename around .old so a crash never leaves data/ missing."""
    old_dir = out_dir + ".old"
    if os.path.exists(old_dir):
        shutil.rmtree(old_dir)
    if os.path.exists(out_dir):
        os.rename(out_dir, old_dir)
    try:
        os.rename(tmp_dir, out_dir)
    except Exception:
        if not os.path.exists(out_dir) and os.path.exists(old_dir):
            os.rename(old_dir, out_dir)
        raise
    if os.path.exists(old_dir):
        shutil.rmtree(old_dir)


def export_snapshots(db_path: str = DB_FILE, out_dir: str = OUTPUT_DIR) -> Dict[str, int]:
    tmp_dir = out_dir + ".tmp"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    conn = get_connection(db_path)
    try:
        counts = {}

        # Ensure all standard timeframe directories exist and initialize default -all counts
        for tf in ("daily", "weekly", "monthly", "yearly"):
            os.makedirs(os.path.join(tmp_dir, tf), exist_ok=True)
            counts[f"{tf}-all"] = 0

        # Query all snapshots in a single index-backed pass, excluding banned repositories
        banned_list = [b.lower() for b in BANNED_REPOSITORIES]
        placeholders = ",".join("?" for _ in banned_list) if banned_list else "''"
        query = f"""
            SELECT
                s.rank, s.score, s.language, s.language_filter,
                s.stars_total, s.stars_gained, s.forks_total, s.forks_gained,
                s.tags_json, s.social_mentions_json, s.period_key, s.timeframe,
                r.full_name, r.description, r.created_at
            FROM snapshots s
            JOIN repositories r ON s.repository_full_name = r.full_name
            WHERE LOWER(r.full_name) NOT IN ({placeholders})
            ORDER BY s.timeframe, s.language_filter, s.period_key DESC, s.rank ASC
        """
        params = banned_list

        grouped: Dict[tuple[str, str], list] = {}
        for row in conn.execute(query, params):
            if is_banned_repository(row["full_name"]):
                continue
            key = (row["timeframe"], row["language_filter"])
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(format_row(row))

        # Export all 4 standard timeframes in logical order: all ranking first, then per-language rankings
        for tf in ("daily", "weekly", "monthly", "yearly"):
            items = grouped.pop((tf, "all"), [])
            write_json(os.path.join(tmp_dir, tf, f"{tf}-all.json"), items)
            counts[f"{tf}-all"] = len(items)

            # Per-language ranking JSON files for this timeframe
            tf_langs = [lf for (t, lf) in list(grouped.keys()) if t == tf]
            for lf in tf_langs:
                items = grouped.pop((tf, lf), [])
                slug = sanitize_filename(lf)
                write_json(os.path.join(tmp_dir, tf, f"{tf}-{slug}.json"), items)
                counts[f"{tf}-{slug}"] = len(items)

        # Write any remaining timeframes if present
        for (tf, lang_filter), items in list(grouped.items()):
            os.makedirs(os.path.join(tmp_dir, tf), exist_ok=True)
            slug = sanitize_filename(lang_filter)
            write_json(os.path.join(tmp_dir, tf, f"{tf}-{slug}.json"), items)
            counts[f"{tf}-{slug}"] = len(items)


        write_json(os.path.join(tmp_dir, "index.json"), {
            "schema_version": 1,
            "files": counts,
        })

        _swap_into_place(tmp_dir, out_dir)
        return counts
    finally:
        conn.close()
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    exported = export_snapshots()
    print(f"Exported {len(exported)} JSON files.")
