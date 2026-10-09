"""SearXNG engine — Google results via a SearXNG instance's JSON API.

Point `SEARXNG_URL` at any public or self-hosted instance. Hits `/search` with
`format=json` and `engines=google` so the user gets Google's ranking without
Google's captcha. SearXNG itself may be rate-limited by its host; a self-hosted
instance removes that concern.

JSON return shape sidesteps the fetch/parse split used for Bing/Google — there's
no HTML to parse.
"""
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

from src.config import settings


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
    proxy: Optional[str] = None,
    timeout: float = 20.0,
) -> Dict:
    """Query SearXNG for `query`, return the first match on `domain` (or not found).

    `geo` is passed as `language` because SearXNG uses locale strings like `en-US`.
    """
    if not settings.searxng_url:
        raise RuntimeError(
            "SearXNG is not configured — set SEARXNG_URL in .env to a self-hosted "
            "or private instance (public instances universally rate-limit the JSON API). "
            "Quickstart: `docker run -p 8080:8080 searxng/searxng` then set "
            "SEARXNG_URL=http://localhost:8080"
        )

    locale = f"{language}-{geo.upper()}" if geo else language
    params = {
        "q": query,
        "format": "json",
        "engines": "google",
        "language": locale,
    }
    url = settings.searxng_url.rstrip("/") + "/search"
    async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()

    return parse(payload, domain, max_position)


def parse(payload: dict, domain: str, max_position: int) -> Dict:
    """Pure — extract first match on `domain` from a SearXNG `/search?format=json` payload."""
    target = _normalize_domain(domain)
    if not target:
        return {"position": None, "url": "", "found": False}

    for i, item in enumerate((payload.get("results") or [])[:max_position], 1):
        item_url = item.get("url") or ""
        host = urlparse(item_url).netloc.lower().removeprefix("www.")
        if target in host:
            return {"position": i, "url": item_url, "found": True}
    return {"position": None, "url": "", "found": False}
