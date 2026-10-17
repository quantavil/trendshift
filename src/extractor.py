"""
RSC Stream Extractor for Trendshift
Parses Next.js React Flight stream payloads to retrieve initialData component props.
"""

import json
import re
from datetime import datetime, timezone
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
    path = path.split("?")[0].strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if language_filter == "all":
        return f"{BASE_URL}{path}"
    return f"{BASE_URL}{path}?{urlencode({'language': language_filter})}"


def slice_matches_language(items: List[Dict[str, Any]], language_filter: str) -> bool:
    if language_filter == "all":
        return True
    if not items:
        return False

    target = language_filter.lower().strip()
    target_aliases = {target}
    if target in ("c++", "cpp"):
        target_aliases.update({"c++", "cpp"})
    elif target in ("c#", "csharp"):
        target_aliases.update({"c#", "csharp"})

    n = 0
    for item in items:
        lang = (item.get("language") or item.get("repository_language") or "").lower().strip()
        if lang and lang in target_aliases:
            n += 1
        elif not lang:
            tags = item.get("tags") or []
            matched = False
            for t in tags:
                if isinstance(t, dict):
                    slug = (t.get("slug") or "").lower().strip()
                    name = (t.get("name") or "").lower().strip()
                    if slug in target_aliases or name in target_aliases:
                        matched = True
                        break
                elif isinstance(t, str):
                    if t.lower().strip() in target_aliases:
                        matched = True
                        break
            if matched:
                n += 1

    # On small slices (<= 2 items), 1 matching item is sufficient to avoid false-rejection crashes
    if len(items) <= 2:
        return n >= 1

    return n >= len(items) / 2



def fallback_period_key(timeframe: str, now: Optional[datetime] = None) -> str:
    """
    Returns a standard period key formatted for the given timeframe:
      - daily:   YYYY-MM-DD
      - weekly:  YYYY-Www
      - monthly: YYYY-Mmm
      - yearly:  YYYY
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if timeframe == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    elif timeframe == "monthly":
        return f"{now.year}-M{now.month:02d}"
    elif timeframe == "yearly":
        return str(now.year)
    else:
        return now.strftime("%Y-%m-%d")


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
    if item.get("week") is not None:
        return "weekly"
    if item.get("month") is not None:
        return "monthly"
    if item.get("year") is not None:
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
    Iterates over matches to tolerate whitespace, multiple occurrences, and escaped JSON.
    """
    if not rsc_text:
        return None

    decoder = json.JSONDecoder()

    # Search for unescaped patterns, e.g. "initialData": or "initialData" : [
    for match in re.finditer(r'"initialData"\s*:\s*', rsc_text):
        start_pos = match.end()
        try:
            data, _ = decoder.raw_decode(rsc_text, start_pos)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            continue

    # Also check if wrapped in flight line string literals, e.g. 0:"{\"initialData\": ...}"
    for line in rsc_text.splitlines():
        if ":" in line:
            parts = line.split(":", 1)
            try:
                val = json.loads(parts[1].strip())
                if isinstance(val, str):
                    res = extract_initial_data(val)
                    if res:
                        return res
                elif isinstance(val, dict) and isinstance(val.get("initialData"), list):
                    return val["initialData"]
            except Exception:
                pass

    return None
