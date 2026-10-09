"""Serper engine — Google results via serper.dev.

POST to `https://google.serper.dev/search` with `X-API-KEY`. Returns parsed JSON
`organic` array. Cheaper than DataForSEO per query at low volume, same tradeoff
(no proxy/captcha handling our side).

Credentials via `SERPER_API_KEY` in `.env`.
"""
from typing import Dict
from urllib.parse import urlparse

import httpx

from src.config import settings

API_URL = "https://google.serper.dev/search"


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
    timeout: float = 30.0,
) -> Dict:
    if not settings.serper_api_key:
        raise RuntimeError(
            "Serper is not configured — set SERPER_API_KEY in .env"
        )

    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "gl": (geo or "us").lower(),
        "hl": language,
        "num": max(10, min(max_position, 100)),
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.post(API_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    return parse(data, domain, max_position)


def parse(data: dict, domain: str, max_position: int) -> Dict:
    """Pure — extract first `domain` match from a serper.dev response."""
    target = _normalize_domain(domain)
    if not target:
        return {"position": None, "url": "", "found": False}

    for item in (data.get("organic") or [])[:max_position]:
        item_url = item.get("link") or ""
        rank = item.get("position")
        if not item_url or rank is None:
            continue
        host = urlparse(item_url).netloc.lower().removeprefix("www.")
        if target in host:
            return {"position": int(rank), "url": item_url, "found": True}
    return {"position": None, "url": "", "found": False}
