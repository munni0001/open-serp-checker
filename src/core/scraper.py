import asyncio
import base64
import re
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.models import ScraperSettings, ScrapingResult
from src.core.database import Database

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SerpScraper:
    def __init__(self, db: Database, timeout: float = 20.0):
        self.db = db
        self.timeout = timeout

    @staticmethod
    def _build_proxy_url(proxy) -> Optional[str]:
        """Convert a Proxy object/dict into an httpx proxy URL string."""
        if proxy is None:
            return None
        if isinstance(proxy, str):
            return proxy
        data = proxy.model_dump() if hasattr(proxy, "model_dump") else proxy
        host = data.get("host")
        port = data.get("port")
        if not host or not port:
            return None
        scheme = "https" if str(data.get("type", "")).lower() == "paid" else "http"
        url = f"{scheme}://{host}:{port}"
        if data.get("username") and data.get("password"):
            from urllib.parse import quote
            url = f"{scheme}://{quote(data['username'])}:{quote(data['password'])}@{host}:{port}"
        return url

    @staticmethod
    def _resolve_url(href: str) -> str:
        """Decode Bing /ck/a redirect URLs to the real destination."""
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

    def _parse_results(self, html: str, max_position: int) -> List[Dict]:
        """Parse organic results from a Bing SERP HTML page."""
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for li in soup.select("li.b_algo"):
            h2 = li.select_one("h2 a")
            if not h2:
                continue
            url = self._resolve_url(h2.get("href", "").strip())
            if not url:
                continue
            title = h2.get_text(strip=True)
            results.append({
                "position": len(results) + 1,
                "url": url,
                "title": title,
            })
            if len(results) >= max_position:
                break
        return results

    async def fetch_serp(self, keyword: str, geo: str = "us",
                         max_position: int = 100,
                         proxy=None, language: str = "en") -> List[Dict]:
        """Fetch and parse a SERP for a keyword using httpx, optionally via a proxy."""
        params = {"q": keyword, "count": min(max_position, 50)}
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": f"{language}-{geo.upper()},{language};q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        proxy_url = self._build_proxy_url(proxy)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, proxy=proxy_url, headers=headers
        ) as client:
            resp = await client.get("https://www.bing.com/search", params=params)
            resp.raise_for_status()
            return self._parse_results(resp.text, max_position)

    async def scrape_serp(self, settings: ScraperSettings,
                          proxy=None) -> ScrapingResult:
        """Scrape all search terms for a scraper and save a ScrapingResult."""
        rankings: Dict[str, Dict] = {}
        domain = (settings.domain or "").lower().strip()
        if domain and "://" not in domain:
            domain = f"//{domain}"
        parsed_domain = urlparse(domain).netloc or domain.replace("//", "")

        for term in settings.search_terms:
            try:
                items = await self.fetch_serp(
                    term, geo=settings.geo,
                    max_position=settings.max_position,
                    proxy=proxy, language=settings.language
                )
            except Exception as exc:
                items = []
                rankings[term] = {
                    "found": False,
                    "position": None,
                    "url": "",
                    "title": f"Error: {exc}",
                    "results": [],
                }
                continue

            matched = None
            for item in items:
                host = urlparse(item["url"]).netloc.lower()
                if parsed_domain and parsed_domain.removeprefix("www.") in host.removeprefix("www."):
                    matched = item
                    break

            rankings[term] = {
                "found": bool(matched),
                "position": matched["position"] if matched else None,
                "url": matched["url"] if matched else "",
                "title": matched["title"] if matched else "",
                "results": items,
            }

        result = ScrapingResult(
            scraper_id=settings.id,
            timestamp=datetime.now(),
            rankings=rankings,
        )
        result.id = self.db.create_scraping_result(result)
        return result

    async def run_scraper_batch(self, settings: ScraperSettings,
                                proxy=None) -> ScrapingResult:
        """Run a scrape for all search terms in the given settings."""
        return await self.scrape_serp(settings, proxy=proxy)