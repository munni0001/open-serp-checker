"""Google SERP engine: pure fetch + parse.

`fetch` is network-only; `parse` is pure and testable with an HTML fixture.

Design notes (see sessions/SESSION_2_7_google_engine.md):
- Anchor on <h3>. Position = DOM order of h3s in the main results container.
- <cite> holds the display domain (source of truth); hrefs are usually opaque
  /goto?url=CAES... wrappers we cannot decode without following them.
- Full clickable URL: scan the whole HTML for plaintext external URLs and
  match by netloc against the cite domain. Robust to Google's next class-name
  rename because it doesn't depend on DOM classes.
- Block detection is first-class. A blocked run must not corrupt rank history.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
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


async def fetch(
    query: str,
    geo: str = "us",
    results_per_page: int = 100,
    language: str = "en",
    proxy: Optional[str] = None,
    timeout: float = 20.0,
) -> Tuple[str, str]:
    """Fetch a Google SERP. Returns (html, final_url).

    `pws=0` disables personalization. `num=100` gets up to 100 results in one
    request. Consent cookie bypasses the EU interstitial for scrapers.
    """
    params = {
        "q": query,
        "num": max(10, min(results_per_page, 100)),
        "hl": language,
        "gl": geo.lower(),
        "pws": 0,
    }
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": f"{language}-{geo.upper()},{language};q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": CONSENT_COOKIE,
    }
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, proxy=proxy, headers=headers,
    ) as client:
        resp = await client.get(SEARCH_URL, params=params)
        # Do not raise_for_status: block detection needs to see the actual
        # response body/URL even on 429 or 3xx-to-sorry.
        return resp.text, str(resp.url)
