"""
Historical Backfill Script for Trendshift (2024 to Present)
Fetches all weekly, monthly, and yearly endpoints — both overall and per-language
rankings — concurrently using httpx + asyncio.

Total requests: ~2,768  (173 endpoints × 16 filters: 1 overall + 15 languages)
Optional: python3 src/backfill.py 'C#' 'C++'  # one language filter, no overall
"""

import sys
import os
import asyncio
import re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
from datetime import datetime, timezone
import httpx
from db import get_connection, init_db, replace_snapshot_slice
from extractor import (
    BASE_URL,
    SUPPORTED_LANGUAGES,
    extract_initial_data,
    derive_period_key_from_item,
    derive_timeframe_from_item,
    derive_timeframe_from_path,
    ranking_url,
)
from export_json import export_snapshots

SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
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


def build_fetch_targets(
    endpoints: list[str],
    languages: list[str] | None = None,
) -> list[tuple[str, str]]:
    """(path, language_filter) pairs. Default: overall + every supported language."""
    filters = languages if languages is not None else ["all", *SUPPORTED_LANGUAGES]
    return [(path, lang) for path in endpoints for lang in filters]


async def process_target(
    client: httpx.AsyncClient,
    url_path: str,
    language_filter: str,
    sem: asyncio.Semaphore,
    conn,
) -> tuple[str, str, int]:
    url = ranking_url(url_path, language_filter)
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

            print(
                f"[OK] {language_filter:12s} {url_path:30s} -> {len(data)} ({timeframe}:{period_key})",
                file=sys.stderr,
            )
            return (url_path, language_filter, len(data))

        except Exception as e:
            print(f"[ERROR] {url_path}: {e}", file=sys.stderr)
            return (url_path, language_filter, 0)


def _parse_languages(argv: list[str]) -> list[str] | None:
    if not argv:
        return None
    allowed = set(SUPPORTED_LANGUAGES) | {"all"}
    unknown = [lang for lang in argv if lang not in allowed]
    if unknown:
        print(f"Unknown language filter(s): {', '.join(unknown)}", file=sys.stderr)
        sys.exit(2)
    return argv


async def main():
    languages = _parse_languages(sys.argv[1:])
    label = ", ".join(languages) if languages else "overall + 15 languages"
    print(f"=== Trendshift Backfill ({label}) ===", file=sys.stderr)
    start = time.time()

    conn = get_connection()
    init_db(conn)

    async with httpx.AsyncClient(follow_redirects=True, transport=httpx.AsyncHTTPTransport(retries=3)) as client:
        endpoints = await fetch_sitemap_urls(client)
        targets = build_fetch_targets(endpoints, languages)
        print(f"{len(endpoints)} endpoints × {len(targets) // max(len(endpoints), 1)} filters = {len(targets)} requests", file=sys.stderr)

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            process_target(client, path, lang_filter, sem, conn)
            for path, lang_filter in targets
        ]
        results = await asyncio.gather(*tasks)

    conn.close()

    ok = sum(1 for _, _, c in results if c > 0)
    if ok == 0 or ok * 2 < len(results):
        print(f"[FAIL] {ok}/{len(results)} endpoints succeeded — aborting.", file=sys.stderr)
        sys.exit(1)

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
