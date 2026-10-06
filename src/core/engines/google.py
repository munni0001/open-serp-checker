"""Google SERP engine: Playwright fetch + pure parse.

`fetch` drives a headless Chromium via Playwright — pure HTTP clients cannot
beat Google's JS-challenge (`/httpservice/retry/enablejs`) regardless of TLS
or header fingerprint. See sessions/SESSION_2_7_1_google_bot_signals.md
and the probes under scripts/probes/ for the evidence.

`parse` remains pure and fixture-testable.

Design notes:
- Anchor on <h3>. Position = DOM order of h3s in the main results container.
- <cite> holds the display domain (source of truth); hrefs are usually opaque
  /goto?url=CAES... wrappers we cannot decode without following them.
- Full clickable URL: scan the whole HTML for plaintext external URLs and
  match by netloc against the cite domain.
- Block detection is first-class. A blocked run must not corrupt rank history.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SEARCH_URL = "https://www.google.com/search"

# Consent cookie that tells Google "user has already accepted consent" for the
# EU/UK interstitial. Cheap; skipped by non-EU IPs but harmless.
CONSENT_COOKIE = "CONSENT=YES+cb.20210328-17-p0.en+FX+000"

# Hosts to exclude when scanning the HTML for external result URLs.
_INTERNAL_HOSTS = (
    "google.com", "google.de", "google.co.uk", "google.mk", "google.fr",
    "google.es", "google.it", "google.nl", "google.pl", "google.ca",
    "google.com.au", "google.co.jp",
    "gstatic.com", "googleapis.com", "googleusercontent.com",
    "youtube.com", "schema.org", "w3.org", "ggpht.com",
    "googletagmanager.com", "googleadservices.com", "doubleclick.net",
    "g.co", "goo.gl", "play.google.com",
)


# ---------- Block detection ----------

def _is_blocked(html: str, final_url: str, status_code: int, organic_count: int) -> Optional[str]:
    """Return a human-readable block reason, or None if the page looks OK."""
    if status_code == 429:
        return "http 429 rate-limited"
    if final_url:
        low = final_url.lower()
        if "consent.google." in low:
            return "consent interstitial"
        if "/sorry/" in low:
            return "captcha / sorry page"
    # Title-based: the interstitial page has exactly this title. The modal-
    # overlay variant has the normal query title, so this check is precise.
    m = re.search(r"<title[^>]*>([^<]*)</title>", html, re.IGNORECASE)
    if m and m.group(1).strip().lower() == "before you continue to google":
        return "consent interstitial"
    if status_code == 200 and organic_count == 0 and "<body" in html.lower():
        return "empty SERP (likely soft block)"
    return None


# ---------- URL utilities ----------

def _normalize_domain(domain: str) -> str:
    d = (domain or "").lower().strip()
    if not d:
        return ""
    if "://" not in d:
        d = f"//{d}"
    netloc = urlparse(d).netloc or d.replace("//", "")
    return netloc.removeprefix("www.")


def _cite_domain(cite_text: str) -> str:
    """Extract the domain shown in a <cite>. Google cite format examples:
    'https://digitalnomadlifestyle.com › semrush-review-2'
    'https://www.g2.com › ... › Semrush › Semrush Reviews'
    '60+ comments  ·  2 years ago'            <- discussion block, no domain
    """
    if not cite_text:
        return ""
    # Take the first token before the breadcrumb separator (Google uses ' › ').
    head = re.split(r"\s[›·]\s", cite_text.strip(), maxsplit=1)[0].strip()
    if not head.lower().startswith(("http://", "https://")):
        return ""
    return _normalize_domain(head)


def _extract_url_pool(html: str) -> List[str]:
    """All plaintext external URLs in the HTML, dedup-preserving-order.
    Used to look up the full clickable URL by cite domain when the anchor
    href is an opaque /goto?url= wrapper.
    """
    raw = re.findall(r"https?://[^\s\"'<>\\]+", html)
    seen = set()
    out: List[str] = []
    for u in raw:
        # Trim trailing punctuation that isn't a legal URL char.
        u = u.rstrip(").,;")
        try:
            netloc = urlparse(u).netloc.lower()
        except ValueError:
            continue
        if not netloc or any(h in netloc for h in _INTERNAL_HOSTS):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _find_url_for_domain(pool: List[str], domain: str) -> str:
    """Prefer a URL with a real path (not just the bare domain landing page)."""
    if not domain:
        return ""
    matches = [
        u for u in pool
        if urlparse(u).netloc.lower().removeprefix("www.") == domain
    ]
    if not matches:
        return ""
    with_path = [u for u in matches if urlparse(u).path not in ("", "/")]
    return (with_path or matches)[0]


# ---------- Organic extraction ----------

def _nearest_cite(h3) -> Optional[str]:
    """Walk up from an h3 to find the nearest <cite> in its result container."""
    node = h3
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        cite = parent.find("cite")
        if cite:
            return cite.get_text(" ", strip=True)
        node = parent
    return None


def _extract_organic(html: str, max_position: int) -> List[Dict]:
    """Return [{position, domain, url, title}] for blue-link organic results.

    Position is DOM order of <h3>. Results whose <cite> yields no domain
    (discussion blocks, image packs, etc.) are dropped from candidate list
    but do NOT shift downstream positions — Google's DOM already renders
    them in place, so the observed rank of subsequent blue links matches
    what a human sees.
    """
    soup = BeautifulSoup(html, "html.parser")
    pool = _extract_url_pool(html)
    out: List[Dict] = []
    for idx, h3 in enumerate(soup.find_all("h3"), start=1):
        if idx > max_position:
            break
        cite_text = _nearest_cite(h3) or ""
        domain = _cite_domain(cite_text)
        if not domain:
            # Not a matchable blue link (discussion, image pack, ...). Skip.
            continue
        out.append({
            "position": idx,
            "domain": domain,
            "url": _find_url_for_domain(pool, domain),
            "title": h3.get_text(strip=True),
        })
    return out


# ---------- Public API ----------

def parse(html: str, domain: str, max_position: int,
          final_url: str = "", status_code: int = 200) -> Dict:
    """Find the first organic result matching `domain`.
    Returns {position, url, found, status, error}.
    """
    target = _normalize_domain(domain)
    organics = _extract_organic(html, max_position)

    reason = _is_blocked(html, final_url, status_code, len(organics))
    if reason:
        return {"position": None, "url": "", "found": False,
                "status": "blocked", "error": reason}

    if not target:
        return {"position": None, "url": "", "found": False,
                "status": "ok", "error": None}

    for item in organics:
        if target == item["domain"] or item["domain"].endswith("." + target):
            return {"position": item["position"], "url": item["url"],
                    "found": True, "status": "ok", "error": None}

    return {"position": None, "url": "", "found": False,
            "status": "ok", "error": None}


# ---------- Playwright fetch ----------

# Patches the Playwright/CDP fingerprint tells Google captures on. Kept minimal:
# every prop here fires on a real Chrome page. Adding more risks false-positives.
_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p)
  );
}
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]

# Blocking stylesheets triggers a /sorry/ redirect. Blocking these is safe.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def _proxy_url_to_dict(proxy: Optional[str]) -> Optional[dict]:
    """Convert 'http://user:pass@host:port' → Playwright proxy dict."""
    if not proxy:
        return None
    p = urlparse(proxy)
    d = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        d["username"] = unquote(p.username)
    if p.password:
        d["password"] = unquote(p.password)
    return d


async def _open_context_with_ip(browser, proxy_dict, geo, language, page_timeout_ms,
                                capture_exit_ip: bool):
    """Open a stealth+resource-blocked browser context. If capture_exit_ip,
    also hit ipify to learn the assigned exit IP. Returns (context, exit_ip_or_none).
    """
    context = await browser.new_context(
        proxy=proxy_dict,
        locale=f"{language}-{geo.upper()}",
        viewport={"width": 1280, "height": 800},
        user_agent=DEFAULT_USER_AGENT,
    )
    await context.add_init_script(_STEALTH_INIT_JS)

    async def _route(route):
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    await context.route("**/*", _route)

    exit_ip: Optional[str] = None
    if capture_exit_ip:
        page = await context.new_page()
        try:
            r = await page.goto("https://api.ipify.org?format=json",
                                wait_until="domcontentloaded", timeout=page_timeout_ms)
            if r and r.status == 200:
                body = await page.content()
                m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
                if m:
                    exit_ip = m.group(1)
        except Exception:
            pass
        finally:
            await page.close()
    return context, exit_ip


async def fetch(
    query: str,
    geo: str = "us",
    results_per_page: int = 10,
    language: str = "en",
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    capture_exit_ip: bool = False,
    blocklist_check: Optional[Callable[[str], bool]] = None,
    max_ip_retries: int = 1,
    mode: str = "identity",
) -> Tuple[str, str, Optional[str]]:
    """Fetch a Google SERP. Returns (html, final_url, exit_ip).

    Two modes (session 3.8):
    - "identity" (default) — the minted-identity + in-page fetch mechanism.
      One Camoufox browser earns the JS challenge once and parks a homepage
      tab; every query is an in-page `fetch()` of /search from that tab, so
      the same connection / IP / cookie are held for the whole identity.
      Recycles (close + re-mint + retry once) on block. Requires a rotating
      proxy; callers on sticky/direct should pass mode="navigation".
      See src/core/engines/google_identity.py.
    - "navigation" — the pre-3.8 path below: stealth-patched headless
      Chromium, homepage warm, full `goto /search` per query. Kept for
      regression and the no-proxy baseline. Byte-identical to pre-3.8.

    When identity mode raises, the only error that escapes is PoolBurned
    (3 consecutive mints failed) — callers mark the run error with reason
    pool_burned.

    Blocklist support applies to both modes:
    - `capture_exit_ip=True` hits ipify to learn the exit IP.
    - `blocklist_check(ip) -> bool` — returning True means "this IP is
      known-bad, get me a new one." In identity mode this is evaluated at
      mint time (a blocklisted exit IP recycles the identity); in navigation
      mode the context is closed and a fresh one opened up to
      `max_ip_retries` times.
    - Returned `exit_ip` is the IP we actually used (may be None if
      capture_exit_ip=False or ipify failed).

    `results_per_page` is accepted for signature stability but ignored —
    Google returns ~10/page now that `num` is dead; pagination is separate.
    """
    from src.core.engines import google_identity as gi

    proxy_dict = _proxy_url_to_dict(proxy)
    timeout_ms = int(timeout * 1000)

    if mode == "identity":
        result = await gi.fetch_identity(
            query,
            proxy=proxy_dict,
            geo=geo,
            language=language,
            timeout_ms=timeout_ms,
            capture_exit_ip=capture_exit_ip,
            blocklist_check=blocklist_check,
        )
        return result["html"], result["final_url"], result.get("exit_ip")

    if mode != "navigation":
        raise ValueError(f"unknown google fetch mode {mode!r}; "
                         "expected 'identity' or 'navigation'")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_STEALTH_ARGS)
        context = None
        exit_ip: Optional[str] = None
        try:
            attempts = 0
            max_attempts = max(1, max_ip_retries + 1) if (capture_exit_ip and blocklist_check) else 1
            while attempts < max_attempts:
                context, exit_ip = await _open_context_with_ip(
                    browser, proxy_dict, geo, language, timeout_ms, capture_exit_ip,
                )
                if not (capture_exit_ip and blocklist_check and exit_ip):
                    break
                if not blocklist_check(exit_ip):
                    break
                # IP is known-bad; drop this context and try for a fresh one.
                await context.close()
                context = None
                attempts += 1

            if context is None:
                # Every attempt got a blocked IP. Open one final context and
                # scrape anyway — better a probably-blocked attempt than nothing.
                context, exit_ip = await _open_context_with_ip(
                    browser, proxy_dict, geo, language, timeout_ms, capture_exit_ip,
                )

            page = await context.new_page()
            await page.goto("https://www.google.com/", wait_until="domcontentloaded",
                            timeout=timeout_ms)

            search_url = f"{SEARCH_URL}?q={query.replace(' ', '+')}"
            resp = await page.goto(search_url, wait_until="domcontentloaded",
                                   timeout=timeout_ms)
            try:
                await page.wait_for_selector("h3", timeout=5_000)
            except Exception:
                pass

            html = await page.content()
            final_url = page.url
            return html, final_url, exit_ip
        finally:
            await browser.close()
