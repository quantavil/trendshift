"""
Daily Sync Script for Trendshift Pipeline
Fetches current Daily, Weekly, Monthly, and Yearly endpoints —
both overall and per-language rankings — upserts into trendshift.db,
and regenerates the JSON exports.
"""

import sys
import os
import asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
import time
from datetime import datetime, timezone
import httpx
from db import get_connection, init_db, replace_snapshot_slice, prune_ghost_dropouts
from extractor import (
    SUPPORTED_LANGUAGES,
    extract_initial_data,
    derive_period_key_from_item,
    derive_timeframe_from_item,
    derive_timeframe_from_path,
    ranking_url,
)
from export_json import export_snapshots

CORE_ENDPOINTS = ["/", "/weekly", "/monthly", "/yearly"]


async def fetch_and_upsert(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    conn: "sqlite3.Connection",
    path: str,
    language_filter: str = "all",
) -> int:
    async with sem:
        url = ranking_url(path, language_filter)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "RSC": "1"}

        try:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()

            data = extract_initial_data(resp.text)
            if not data:
                print(f"[WARN] No data: {path}", file=sys.stderr)
                return 0

            first = data[0]
            timeframe = derive_timeframe_from_item(first)
            period_key = derive_period_key_from_item(first)

            if period_key is None:
                timeframe = derive_timeframe_from_path(path)
                period_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            replace_snapshot_slice(conn, data, timeframe, period_key, language_filter)
            print(f"[OK] {language_filter:12s} {path:15s} -> {len(data)} ({timeframe}:{period_key})", file=sys.stderr)
            return len(data)

        except Exception as e:
            print(f"[ERROR] {path}: {e}", file=sys.stderr)
            return 0


async def main():
    print("=== Daily Trendshift Sync (overall + 15 languages) ===", file=sys.stderr)
    start = time.time()

    conn = get_connection()
    init_db(conn)

    # 4 endpoints × (1 overall + 15 languages) = 64 requests
    targets = [(ep, "all") for ep in CORE_ENDPOINTS]
    targets.extend((ep, lang) for ep in CORE_ENDPOINTS for lang in SUPPORTED_LANGUAGES)

    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(follow_redirects=True, transport=httpx.AsyncHTTPTransport(retries=3)) as client:
        tasks = [fetch_and_upsert(client, sem, conn, path, lf) for path, lf in targets]
        results = await asyncio.gather(*tasks)

    pruned = prune_ghost_dropouts(conn)
    if pruned > 0:
        print(f"Pruned {pruned} ghost dropouts from DB.", file=sys.stderr)

    conn.close()

    ok = sum(1 for n in results if n > 0)
    if ok == 0 or ok * 2 < len(results):
        print(f"[FAIL] {ok}/{len(results)} endpoints succeeded — aborting.", file=sys.stderr)
        sys.exit(1)

    total = sum(results)
    elapsed = time.time() - start
    print(f"Sync done in {elapsed:.1f}s — {total} snapshots.", file=sys.stderr)

    print("\nRegenerating JSON...", file=sys.stderr)
    counts = export_snapshots()
    print(f"Updated {len(counts)} JSON files.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
