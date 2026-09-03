"""Bing SERP engine: pure fetch + parse.

`fetch` is network-only; `parse` is pure and testable with an HTML fixture.
"""
import base64
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SEARCH_URL = "https://www.bing.com/search"


def _resolve_redirect(href: str) -> str:
    """Decode Bing's /ck/a wrapper URLs back to the real destination."""
    if "bing.com/ck/a" not in href:
        return href
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", href)
    if not m:
        return href
    b64 = m.group(1).rstrip("%3D").rstrip("=")
    padded = b64 + "=" * (-len(b64) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(padded).decode("utf-8", "ignore")
        except Exception:
            continue
    return href


async def fetch(
    query: str,
    geo: str = "us",
    results_per_page: int = 10,
    language: str = "en",
    proxy: Optional[str] = None,
    timeout: float = 20.0,
) -> str:
    params = {"q": query, "count": min(max(results_per_page, 10), 50)}
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": f"{language}-{geo.upper()},{language};q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, proxy=proxy, headers=headers,
    ) as client:
        resp = await client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        return resp.text


def _extract_results(html: str, max_position: int) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for li in soup.select("li.b_algo"):
        h2 = li.select_one("h2 a")
        if not h2:
            continue
        url = _resolve_redirect(h2.get("href", "").strip())
        if not url:
            continue
        results.append({
            "position": len(results) + 1,
            "url": url,
            "title": h2.get_text(strip=True),
        })
        if len(results) >= max_position:
            break
    return results


def _normalize_domain(domain: str) -> str:
    d = (domain or "").lower().strip()
    if not d:
        return ""
    if "://" not in d:
        d = f"//{d}"
    netloc = urlparse(d).netloc or d.replace("//", "")
    return netloc.removeprefix("www.")


def parse(html: str, domain: str, max_position: int) -> Dict:
    """Find the first result matching `domain`. Returns {position, url, found}."""
    target = _normalize_domain(domain)
    if not target:
        return {"position": None, "url": "", "found": False}

    for item in _extract_results(html, max_position):
        host = urlparse(item["url"]).netloc.lower().removeprefix("www.")
        if target in host:
            return {"position": item["position"], "url": item["url"], "found": True}
    return {"position": None, "url": "", "found": False}
