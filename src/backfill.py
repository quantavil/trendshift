"""
Historical Backfill Script for Trendshift (2024 to Present)
Fetches all weekly, monthly, and yearly endpoints — both overall and per-language
rankings — concurrently using httpx + asyncio.

Total requests: ~2,595  (173 endpoints × 15 variants: 1 overall + 14 languages)
Total snapshots: ~64,875 (2,595 × 25 repos each)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
from datetime import datetime, timezone
import httpx
from db import get_connection, init_db, replace_snapshot_slice
from extractor import (
    SUPPORTED_LANGUAGES,
    extract_initial_data,
    derive_period_key_from_item,
    derive_timeframe_from_item,
    derive_timeframe_from_path,
)
from export_json import export_snapshots

SITEMAP_URL = "https://trendshift.io/sitemap.xml"
BASE_URL = "https://trendshift.io"
CONCURRENCY = 10


async def fetch_sitemap_urls(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(
        SITEMAP_URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
        timeout=15.0,
    )
    resp.raise_for_status()

    locs = re.findall(r"<loc>(.*?)</loc>", resp.text)
    skip = (
        "/live-mentions", "/advertise", "/tos", "/privacy-policy",
        "/stats", "/signal", "/topics", "/trending", "/insights",
        "/github-trending-repositories", "/repositories",
    )
    endpoints = []
    for loc in locs:
        path = loc.replace(BASE_URL, "") or "/"
        if any(path.startswith(s) for s in skip):
            continue
        endpoints.append(path)

    for core in ["/", "/weekly", "/monthly", "/yearly"]:
        if core not in endpoints:
            endpoints.append(core)

    return sorted(set(endpoints))


def build_fetch_targets(endpoints: list[str]) -> list[tuple[str, str]]:
    """
    For each endpoint, generate (url_path_with_query, language_filter) pairs.
    Returns overall ("all") + one per supported language.
    """
    targets = []
    for path in endpoints:
        targets.append((path, "all"))
        for lang in SUPPORTED_LANGUAGES:
            sep = "&" if "?" in path else "?"
            targets.append((f"{path}{sep}language={lang}", lang))
    return targets


async def process_target(
    client: httpx.AsyncClient,
    url_path: str,
    language_filter: str,
    sem: asyncio.Semaphore,
    conn,
) -> tuple[str, str, int]:
    url = f"{BASE_URL}{url_path}"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "RSC": "1"}

    async with sem:
        try:
            resp = await client.get(url, headers=headers, timeout=20.0)
            if resp.status_code != 200:
                print(f"[FAIL {resp.status_code}] {url_path}", file=sys.stderr)
                return (url_path, language_filter, 0)

            data = extract_initial_data(resp.text)
            if not data:
                print(f"[WARN] No data: {url_path}", file=sys.stderr)
                return (url_path, language_filter, 0)

            first = data[0]
            timeframe = derive_timeframe_from_item(first)
            period_key = derive_period_key_from_item(first)

            if period_key is None:
                timeframe = derive_timeframe_from_path(url_path)
                period_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            replace_snapshot_slice(conn, data, timeframe, period_key, language_filter)

            label = f"{language_filter:12s}" if language_filter != "all" else "all         "
            print(
                f"[OK] {label} {url_path.split('?')[0]:30s} -> {len(data)} ({timeframe}:{period_key})",
                file=sys.stderr,
            )
            return (url_path, language_filter, len(data))

        except Exception as e:
            print(f"[ERROR] {url_path}: {e}", file=sys.stderr)
            return (url_path, language_filter, 0)


async def main():
    print("=== Trendshift Full Backfill (overall + 15 languages) ===", file=sys.stderr)
    start = time.time()

    conn = get_connection()
    init_db(conn)

    async with httpx.AsyncClient(follow_redirects=True, transport=httpx.AsyncHTTPTransport(retries=3)) as client:
        endpoints = await fetch_sitemap_urls(client)
        targets = build_fetch_targets(endpoints)
        print(f"{len(endpoints)} endpoints × 15 variants = {len(targets)} requests", file=sys.stderr)

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            process_target(client, path, lang_filter, sem, conn)
            for path, lang_filter in targets
        ]
        results = await asyncio.gather(*tasks)

    conn.close()

    ok = sum(1 for _, _, c in results if c > 0)
    total = sum(c for _, _, c in results)
    elapsed = time.time() - start
    print(
        f"\nBackfill done in {elapsed:.1f}s — {ok}/{len(results)} OK, {total} snapshots.",
        file=sys.stderr,
    )

    print("\nExporting JSON...", file=sys.stderr)
    counts = export_snapshots()
    print(f"Exported {len(counts)} JSON files.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
