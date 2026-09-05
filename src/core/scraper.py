import random
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import httpx

from src.config import settings
from src.core.database import Database
from src.core.engines import bing, google
from src.models import Keyword, KeywordResult, Project


# Decodo sticky-port range. Their dashboard exposes 10001..10010 out of the box.
# Higher ports may exist on larger plans — extend when tested.
_DECODO_STICKY_PORT_BASE = 10001
_DECODO_STICKY_PORT_MAX = 10010


def build_proxy_url(proxy) -> Optional[str]:
    """Convert a Proxy object/dict/str into a proxy URL suitable for both
    httpx and Playwright.

    Provider-aware: for Decodo, `mode='sticky'` reshapes the URL to a sticky
    port and adds the `sessionduration` username fragment. For everything else,
    falls back to the naive host:port + basic-auth format the caller stored.
    """
    if proxy is None:
        return None
    if isinstance(proxy, str):
        return proxy
    # mode='json' ensures enum fields (ProxyProvider, ProxyType) serialize to
    # their string values — str(enum_member) gives 'ProxyProvider.DECODO', not 'decodo'.
    data = proxy.model_dump(mode='json') if hasattr(proxy, "model_dump") else proxy
    host = data.get("host")
    stored_port = data.get("port")
    if not host or not stored_port:
        return None

    provider = str(data.get("provider", "")).lower()
    mode = str(data.get("mode", "rotating")).lower()
    base_user = data.get("username") or ""
    password = data.get("password") or ""

    # Residential proxies are plain HTTP proxies (CONNECT tunneled). Not HTTPS.
    scheme = "http"

    port = stored_port
    user = base_user

    if provider == "decodo" and mode == "sticky":
        duration = int(data.get("sticky_duration_min") or 1)
        session_count = max(1, int(data.get("sticky_sessions") or 1))
        # Pick a sticky port from the available range. If user configured N
        # concurrent sticky sessions, we spread across ports 10001..10001+N-1
        # (bounded by the platform max we've seen).
        top = min(_DECODO_STICKY_PORT_BASE + session_count - 1, _DECODO_STICKY_PORT_MAX)
        port = random.randint(_DECODO_STICKY_PORT_BASE, top)
        if base_user:
            user = f"user-{base_user}-sessionduration-{duration}"

    if user and password:
        return f"{scheme}://{quote(user)}:{quote(password)}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


class SerpScraper:
    """Dispatches a keyword scrape to the right engine module."""

    def __init__(self, db: Database, timeout: float = 20.0):
        self.db = db
        self.timeout = timeout

    def _resolve_proxy(self, keyword: Keyword, project: Project, override):
        """Returns the Proxy object (or override, or a URL string, or None).

        Priority: explicit override → keyword.proxy_id → project.default_proxy_id → env → None.
        Callers use build_proxy_url() to shape it into a request URL, and read
        blocklist fields off the Proxy object directly.
        """
        if override is not None:
            return override
        if keyword.proxy_id:
            p = self.db.get_proxy(keyword.proxy_id)
            if p:
                return p
        if project.default_proxy_id:
            p = self.db.get_proxy(project.default_proxy_id)
            if p:
                return p
        return settings.serp_proxy_url  # str or None

    async def scrape_keyword(self, keyword: Keyword, project: Project, proxy=None) -> KeywordResult:
        proxy_obj = self._resolve_proxy(keyword, project, proxy)
        proxy_url = build_proxy_url(proxy_obj) if proxy_obj is not None else None
        html: str = ""

        # Blocklist support only applies when we have a Proxy record in the DB
        # (so we can log to proxy_ip_log) AND the user opted in.
        proxy_id = getattr(proxy_obj, "id", None)
        capture_ip = bool(
            proxy_id is not None
            and getattr(proxy_obj, "ip_blocklist_enabled", False)
        )
        ttl_days = int(getattr(proxy_obj, "ip_blocklist_ttl_days", 1) or 1)

        exit_ip: Optional[str] = None
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
                blocklist_check = None
                if capture_ip:
                    blocklist_check = lambda ip: self.db.is_ip_blocklisted(  # noqa: E731
                        proxy_id, ip, ttl_days
                    )
                html, final_url, exit_ip = await google.fetch(
                    keyword.term,
                    geo=keyword.geo,
                    results_per_page=keyword.results_per_page,
                    proxy=proxy_url,
                    timeout=self.timeout,
                    capture_exit_ip=capture_ip,
                    blocklist_check=blocklist_check,
                )
                match = google.parse(
                    html, project.domain or "", keyword.max_position,
                    final_url=final_url, status_code=200,
                )
            else:
                raise ValueError(f"Unknown engine: {keyword.engine!r}")
        except Exception as e:
            # Broadened from httpx.HTTPError to catch Playwright errors too
            # (playwright raises its own hierarchy without a shared base).
            match = {"position": None, "url": "", "found": False,
                     "status": "error", "error": f"{type(e).__name__}: {e}"[:200]}

        # Log the exit IP outcome when we captured one — this feeds both the
        # blocklist for future requests and long-term proxy reputation stats.
        if capture_ip and exit_ip and proxy_id is not None:
            was_blocked = match.get("status") in ("blocked", "error")
            self.db.log_proxy_ip(
                proxy_id=proxy_id, exit_ip=exit_ip,
                was_blocked=was_blocked, block_reason=match.get("error"),
                engine=keyword.engine,
            )

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
