from datetime import datetime
from typing import Optional
from urllib.parse import quote

import httpx

from src.config import settings
from src.core.database import Database
from src.core.engines import bing, google
from src.models import Keyword, KeywordResult, Project


def build_proxy_url(proxy) -> Optional[str]:
    """Convert a Proxy object/dict/str into an httpx proxy URL."""
    if proxy is None:
        return None
    if isinstance(proxy, str):
        return proxy
    data = proxy.model_dump() if hasattr(proxy, "model_dump") else proxy
    host, port = data.get("host"), data.get("port")
    if not host or not port:
        return None
    scheme = "https" if str(data.get("type", "")).lower() == "paid" else "http"
    if data.get("username") and data.get("password"):
        return f"{scheme}://{quote(data['username'])}:{quote(data['password'])}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


class SerpScraper:
    """Dispatches a keyword scrape to the right engine module."""

    def __init__(self, db: Database, timeout: float = 20.0):
        self.db = db
        self.timeout = timeout

    def _resolve_proxy(self, keyword: Keyword, project: Project, override) -> Optional[str]:
        """Priority: explicit override → keyword.proxy_id → project.default_proxy_id → env → None."""
        if override is not None:
            return build_proxy_url(override)
        if keyword.proxy_id:
            p = self.db.get_proxy(keyword.proxy_id)
            if p:
                return build_proxy_url(p)
        if project.default_proxy_id:
            p = self.db.get_proxy(project.default_proxy_id)
            if p:
                return build_proxy_url(p)
        return settings.serp_proxy_url

    async def scrape_keyword(self, keyword: Keyword, project: Project, proxy=None) -> KeywordResult:
        proxy_url = self._resolve_proxy(keyword, project, proxy)
        html: str = ""

        try:
            if keyword.engine == "bing":
                html = await bing.fetch(
                    keyword.term,
                    geo=keyword.geo,
                    results_per_page=keyword.results_per_page,
                    proxy=proxy_url,
                    timeout=self.timeout,
                )
                match = bing.parse(html, project.domain or "", keyword.max_position)
                match.setdefault("status", "ok")
                match.setdefault("error", None)
            elif keyword.engine == "google":
                html, final_url = await google.fetch(
                    keyword.term,
                    geo=keyword.geo,
                    results_per_page=keyword.results_per_page,
                    proxy=proxy_url,
                    timeout=self.timeout,
                )
                match = google.parse(
                    html, project.domain or "", keyword.max_position,
                    final_url=final_url, status_code=200,
                )
            else:
                raise ValueError(f"Unknown engine: {keyword.engine!r}")
        except httpx.HTTPError as e:
            match = {"position": None, "url": "", "found": False,
                     "status": "error", "error": f"{type(e).__name__}: {e}"[:200]}

        result = KeywordResult(
            keyword_id=keyword.id,
            timestamp=datetime.utcnow(),
            position=match["position"],
            url=match["url"],
            found=match["found"],
            status=match["status"],
            error=match["error"],
            bytes_downloaded=len(html.encode("utf-8")) if html else 0,
        )
        result.id = self.db.create_keyword_result(result)
        self.db.touch_keyword_run(keyword.id, result.timestamp)
        return result
