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
from typing import Optional
import httpx
from db import get_connection, init_db, replace_snapshot_slice, wal_checkpoint
from extractor import (
    SUPPORTED_LANGUAGES,
    extract_initial_data,
    derive_period_key_from_item,
    derive_timeframe_from_item,
    derive_timeframe_from_path,
    fallback_period_key,
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
    db_lock: Optional[asyncio.Lock] = None,
) -> int:
    async with sem:
        url = ranking_url(path, language_filter)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "RSC": "1"}

        try:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()

            data = extract_initial_data(resp.text)
            if not data:
                print(f"[WARN] No data: {language_filter} {path}", file=sys.stderr)
                return 0

            first = data[0]
            path_tf = derive_timeframe_from_path(path)
            item_tf = derive_timeframe_from_item(first)
            timeframe = path_tf if path_tf != "daily" else item_tf
            period_key = derive_period_key_from_item(first)

            if (
                period_key is None
                or (timeframe == "weekly" and "-W" not in period_key)
                or (timeframe == "monthly" and "-M" not in period_key)
                or (timeframe == "yearly" and not (len(period_key) == 4 and period_key.isdigit()))
            ):
                period_key = fallback_period_key(timeframe)


            # Perform synchronous DB slice replacement in worker thread to prevent event loop blocking
            if db_lock is not None:
                async with db_lock:
                    await asyncio.to_thread(
                        replace_snapshot_slice, conn, data, timeframe, period_key, language_filter
                    )
            else:
                await asyncio.to_thread(
                    replace_snapshot_slice, conn, data, timeframe, period_key, language_filter
                )

            print(f"[OK] {language_filter:12s} {path:15s} -> {len(data)} ({timeframe}:{period_key})", file=sys.stderr)
            return len(data)

        except Exception as e:
            print(f"[ERROR] {language_filter} {path}: {e}", file=sys.stderr)
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
    db_lock = asyncio.Lock()
    try:
        async with httpx.AsyncClient(follow_redirects=True, transport=httpx.AsyncHTTPTransport(retries=3)) as client:
            tasks = [fetch_and_upsert(client, sem, conn, path, lf, db_lock) for path, lf in targets]
            results = await asyncio.gather(*tasks)
    finally:
        wal_checkpoint(conn)
        conn.close()

    ok = sum(1 for n in results if n > 0)
    if ok < len(results):
        print(f"[FAIL] Only {ok}/{len(results)} endpoints succeeded — aborting.", file=sys.stderr)
        sys.exit(1)

    total = sum(results)
    elapsed = time.time() - start
    print(f"Sync done in {elapsed:.1f}s — {total} snapshots.", file=sys.stderr)

    print("\nRegenerating JSON...", file=sys.stderr)
    counts = export_snapshots()
    print(f"Updated {len(counts)} JSON files.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
