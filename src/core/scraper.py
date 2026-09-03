from datetime import datetime
from typing import Optional
from urllib.parse import quote

from src.core.database import Database
from src.core.engines import bing
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

    async def scrape_keyword(self, keyword: Keyword, project: Project, proxy=None) -> KeywordResult:
        proxy_url = build_proxy_url(proxy)

        if keyword.engine == "bing":
            html = await bing.fetch(
                keyword.term,
                geo=keyword.geo,
                results_per_page=keyword.results_per_page,
                proxy=proxy_url,
                timeout=self.timeout,
            )
            match = bing.parse(html, project.domain or "", keyword.max_position)
        else:
            raise ValueError(f"Unknown engine: {keyword.engine!r}")

        result = KeywordResult(
            keyword_id=keyword.id,
            timestamp=datetime.utcnow(),
            position=match["position"],
            url=match["url"],
            found=match["found"],
        )
        result.id = self.db.create_keyword_result(result)
        self.db.touch_keyword_run(keyword.id, result.timestamp)
        return result
