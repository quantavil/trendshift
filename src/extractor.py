"""
RSC Stream Extractor for Trendshift
Parses Next.js React Flight stream payloads to retrieve initialData component props.
"""

import json
from typing import List, Dict, Any, Optional
from urllib.parse import urlencode

BASE_URL = "https://trendshift.io"

# Languages that trendshift.io serves a dedicated top-25 ranking for
SUPPORTED_LANGUAGES = [
    "C", "C#", "C++", "Dart", "Go", "Java", "JavaScript",
    "Kotlin", "PHP", "Python", "Ruby", "Rust", "Swift",
    "TypeScript", "Zig",
]


def ranking_url(path: str, language_filter: str = "all") -> str:
    path = path.split("?")[0] or "/"
    if not path.startswith("/"):
        path = "/" + path
    if language_filter == "all":
        return f"{BASE_URL}{path}"
    return f"{BASE_URL}{path}?{urlencode({'language': language_filter})}"


def slice_matches_language(items: List[Dict[str, Any]], language_filter: str) -> bool:
    if language_filter == "all":
        return True
    if not items:
        return False
    n = 0
    for item in items:
        lang = item.get("language") or item.get("repository_language") or ""
        if lang == language_filter:
            n += 1
    return n > len(items) / 2


def derive_period_key_from_item(item: Dict[str, Any]) -> Optional[str]:
    """
    Derives the actual period_key from fields inside the payload item.
    """
    year = item.get("year")
    if year:
        week = item.get("week")
        if week is not None:
            return f"{year}-W{int(week):02d}"

        month = item.get("month")
        if month is not None:
            return f"{year}-M{int(month):02d}"

        return str(year)

    date_str = item.get("date")
    if date_str and isinstance(date_str, str):
        return date_str[:10]

    return None


def derive_timeframe_from_item(item: Dict[str, Any]) -> str:
    if "week" in item:
        return "weekly"
    if "month" in item:
        return "monthly"
    if "year" in item:
        return "yearly"
    return "daily"


def derive_timeframe_from_path(path: str) -> str:
    clean = path.split("?")[0].strip("/")
    if not clean:
        return "daily"
    prefix = clean.split("/")[0].lower()
    return prefix if prefix in ("weekly", "monthly", "yearly") else "daily"


def extract_initial_data(rsc_text: str) -> Optional[List[Dict[str, Any]]]:
    """
    Extracts structured 'initialData' array from the React Flight stream payload.
    """
    pattern = '"initialData":'
    idx = rsc_text.find(pattern)
    if idx == -1:
        return None

    start_pos = idx + len(pattern)
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(rsc_text, start_pos)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    return None
