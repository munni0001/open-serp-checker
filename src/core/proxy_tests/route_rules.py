"""Bandwidth-tier route handlers for proxy_tests (session 4.10 Phase 2b).

Ported verbatim from `google-scrape/root-test.py` so cross-project numbers
are directly comparable. Async form for our Playwright usage.

Six tiers, cheapest → most permissive on the top, most-blocked on the
bottom:

  default   Serp-scrape historical rule: abort image/media/font +
            recaptcha, allow top-level /sorry navigation (so page.url
            reflects a block). Preserved so pre-4.10 rows stay
            meaningful.

  fat       No blocking. Full google.com load. Baseline for the
            bandwidth story — "what does an unfiltered fetch cost?"

  moderate  Block tracking/ads hosts (fonts.gstatic, doubleclick,
            googletagmanager, analytics, ogs, googlesyndication). Cuts
            ~30% for zero risk to results.

  skinny    Moderate + CDN/embed hosts (gstatic thumbs, YouTube, gvid)
            + block by static asset extension (png/jpg/mp4/woff...).

  ultra     Skinny + block `/xjs/*` — the dynamic-content injection
            endpoint that's ~80% of google.com cost. Results still
            render because the async /search XHR remains allowed.

  doconly   Abort every non-document request. Only the top-level
            document and `/search` sub-paths pass. Pure HTML floor —
            ~200KB brotli-wire in the google-scrape data.

Route rules are engine-agnostic (chromium and camoufox both see the
same Playwright route API). Callers register once per context:
    handler = make_route_handler(tier)
    await context.route("**/*", handler)
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# --- Tier constants ----------------------------------------------------

TIER_DEFAULT = "default"
TIER_FAT = "fat"
TIER_MODERATE = "moderate"
TIER_SKINNY = "skinny"
TIER_ULTRA = "ultra"
TIER_DOCONLY = "doconly"

TIERS = (TIER_DEFAULT, TIER_FAT, TIER_MODERATE, TIER_SKINNY, TIER_ULTRA,
         TIER_DOCONLY)

# --- Blocklists (verbatim from google-scrape/root-test.py) -------------

MODERATE_HOSTS = {
    "fonts.gstatic.com", "maps.googleapis.com", "adservice.google.com",
    "doubleclick.net", "www.googletagmanager.com", "analytics.google.com",
    "i.ytimg.com", "www.google-analytics.com", "ogs.google.com",
    "pagead2.googlesyndication.com", "googlesyndication.com",
}

SKINNY_EXTRA_HOSTS = {
    "www.gstatic.com", "encrypted-tbn0.gstatic.com", "yt3.ggpht.com",
    "googleusercontent.com", "www.youtube.com", "googlevideo.com",
    "ytimg.com", "youtube-nocookie.com",
}

ASSET_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".ico",
              ".svg", ".woff", ".woff2", ".ttf", ".ttc",
              ".mp4", ".webm", ".mp3", ".ogg", ".m4a")

# Serp-scrape's historical block set (matches google.py _BLOCKED_RESOURCE_TYPES).
_DEFAULT_BLOCKED_TYPES = {"image", "media", "font"}


# --- Handlers ----------------------------------------------------------

def _host_blocked(host: str, hosts: set) -> bool:
    return any(host == b or host.endswith("." + b) for b in hosts)


def _is_nav(request) -> bool:
    """Playwright exposes is_navigation_request as method or attr depending
    on version. Handle both."""
    v = request.is_navigation_request
    return v() if callable(v) else v


def make_route_handler(tier: str, doconly_allow: Optional[list[str]] = None):
    """Return an async Playwright route handler for the given tier.

    doconly_allow (doconly only): list of allowlist specs, matched greedily
    so elements can be added back one at a time. Ignored for other tiers.
      - '/path'   → path-prefix match
      - 'host.com' → hostname match (exact or subdomain)
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")

    doconly_allow = doconly_allow or []

    if tier == TIER_FAT:
        async def fat_handler(route):
            await route.continue_()
        return fat_handler

    if tier == TIER_DEFAULT:
        # Historical open-serp-checker rule. Kept verbatim so runs pre- and
        # post-4.10 with this tier are directly comparable.
        async def default_handler(route):
            if route.request.resource_type in _DEFAULT_BLOCKED_TYPES:
                await route.abort()
                return
            url = route.request.url.lower()
            if "recaptcha" in url:
                await route.abort()
                return
            if "/sorry/" in url and not _is_nav(route.request):
                await route.abort()
                return
            await route.continue_()
        return default_handler

    if tier == TIER_DOCONLY:
        async def doconly_handler(route):
            req = route.request
            try:
                parsed = urlparse(req.url)
                host = parsed.netloc
                path = parsed.path.lower()
            except Exception:
                host, path = "", ""
            rtype = req.resource_type

            if rtype == "document" or path.startswith("/search"):
                await route.continue_()
                return

            for spec in doconly_allow:
                if not spec:
                    continue
                if spec.startswith("/"):
                    if path.startswith(spec):
                        await route.continue_()
                        return
                elif _host_blocked(host, {spec}):
                    await route.continue_()
                    return

            await route.abort()
        return doconly_handler

    # moderate / skinny / ultra — layered from a shared skeleton.
    hosts = set(MODERATE_HOSTS)
    block_assets = False
    block_xjs = False
    if tier == TIER_SKINNY:
        hosts |= SKINNY_EXTRA_HOSTS
        block_assets = True
    elif tier == TIER_ULTRA:
        hosts |= SKINNY_EXTRA_HOSTS
        block_assets = True
        block_xjs = True

    async def tiered_handler(route):
        req = route.request
        try:
            parsed = urlparse(req.url)
            host = parsed.netloc
            path = parsed.path.lower()
        except Exception:
            host, path = "", ""

        if _host_blocked(host, hosts):
            await route.abort()
            return
        if block_xjs and path.startswith("/xjs"):
            await route.abort()
            return
        if block_assets and path.endswith(ASSET_EXTS):
            await route.abort()
            return
        await route.continue_()
    return tiered_handler
