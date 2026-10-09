"""DataForSEO engine — Google organic results via their SERP API.

POST to `/v3/serp/google/organic/live/regular` with Basic auth. Returns parsed
JSON — no HTML, no captcha handling, no proxy needed on our side. DataForSEO
runs the browser/proxy stack internally and bills per query.

Credentials via `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` in `.env`.
"""
from typing import Dict
from urllib.parse import urlparse

import httpx

from src.config import settings

API_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"


def _normalize_domain(domain: str) -> str:
    d = (domain or "").lower().strip()
    if not d:
        return ""
    if "://" not in d:
        d = f"//{d}"
    netloc = urlparse(d).netloc or d.replace("//", "")
    return netloc.removeprefix("www.")


async def search(
    query: str,
    domain: str,
    geo: str = "us",
    max_position: int = 100,
    language: str = "en",
    timeout: float = 60.0,
) -> Dict:
    """Submit a live SERP task to DataForSEO, return the first match on `domain`.

    Raises if credentials aren't configured — the error flows up to SerpScraper's
    exception handler and lands in the `error` field of the KeywordResult.
    """
    if not (settings.dataforseo_login and settings.dataforseo_password):
        raise RuntimeError(
            "DataForSEO is not configured — set DATAFORSEO_LOGIN and "
            "DATAFORSEO_PASSWORD in .env"
        )

    payload = [{
        "language_code": language,
        "location_code": _location_code(geo),
        "keyword": query,
        "depth": max(10, min(max_position, 100)),
    }]
    auth = (settings.dataforseo_login, settings.dataforseo_password)
    async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
        resp = await client.post(API_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    return parse(data, domain, max_position)


def parse(data: dict, domain: str, max_position: int) -> Dict:
    """Pure — extract first `domain` match from a DataForSEO SERP response."""
    tasks = data.get("tasks") or []
    if not tasks or not tasks[0].get("result"):
        return {"position": None, "url": "", "found": False}
    items = tasks[0]["result"][0].get("items") or []

    target = _normalize_domain(domain)
    if not target:
        return {"position": None, "url": "", "found": False}

    for item in items[:max_position]:
        if item.get("type") != "organic":
            continue
        rank = item.get("rank_absolute")
        item_url = item.get("url") or ""
        if not item_url or rank is None:
            continue
        host = urlparse(item_url).netloc.lower().removeprefix("www.")
        if target in host:
            return {"position": int(rank), "url": item_url, "found": True}
    return {"position": None, "url": "", "found": False}


# DataForSEO uses numeric location codes, not ISO country codes. These are the
# common ones — see https://docs.dataforseo.com/v3/serp/google/locations/ for
# the full list. Extend as users ask for new geos.
_LOCATION_CODES = {
    "us": 2840, "uk": 2826, "gb": 2826, "ca": 2124, "au": 2036,
    "in": 2356, "de": 2276, "fr": 2250, "es": 2724, "it": 2380,
}


def _location_code(geo: str) -> int:
    return _LOCATION_CODES.get((geo or "us").lower(), 2840)
