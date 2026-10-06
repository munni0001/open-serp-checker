"""Minted-identity Google fetching (Session 3.8).

The "minted identity + in-page fetch" mechanism ported from the probe
`scripts/probes/minted_identity_inpage_fetch.py` (arm B) into the engine:

1. MINT   — open a Camoufox browser, load google.com, do one real `/search`
   navigation so Google's JS challenge runs and the x5sec cookie is earned.
2. PARK   — navigate back to the homepage and leave the tab open. The tab
   holds ONE live TCP connection, which pins the exit IP (Decodo rotates
   per connection, not per request).
3. REPLAY — every query is an in-page `fetch()` of `/search` from inside the
   parked tab (`page.evaluate`). Same connection -> same IP -> same cookie ->
   one coherent identity Google never re-checks. No navigation, no resource
   load, no re-challenge.

The identity is recycled on block (close browser -> re-mint -> retry once)
and proactively after `query_limit` queries. Three consecutive failed mints
raise `PoolBurned` — the pool is dead right now, stop cleanly.

Evidence + economics: sessions/SESSION_3_7_minted_identity_inpage_fetch.md.
Do NOT extract cookies for httpx/curl_cffi replay (2.7.1 failed 30/30); the
browser owns the whole identity end to end.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable, List, Optional

from camoufox.async_api import AsyncCamoufox

HOME_URL = "https://www.google.com/"
SEARCH_URL = "https://www.google.com/search"
IPIFY_URL = "https://api.ipify.org?format=json"

# Mint limits: Google lets a cold browser through within a few tries; if the
# pool serves a 0/20 day even the mint itself is sorry/429.
MAX_MINTS = 3
DEFAULT_QUERY_LIMIT = 25

# Timeouts.
MINT_TIMEOUT_MS = 45_000
MINT_WAIT_H3_MS = 8_000
FETCH_TIMEOUT_MS = 20_000
IPIFY_TIMEOUT_MS = 8_000

# In-page fetch: same-origin, credentials included, hard timeout. Returns a
# structured object so evaluate() doesn't throw across the bridge.
_FETCH_JS = """async ({url, method, headers, body, timeoutMs}) => {
    try {
        const r = await fetch(url, {
            method: method || 'GET',
            credentials: 'include',
            headers: headers || {},
            body: body || undefined,
            signal: AbortSignal.timeout(timeoutMs),
        });
        return { ok: true, status: r.status, body: await r.text() };
    } catch (e) {
        return { ok: false, status: 0, body: '', error: String(e) };
    }
}"""

# Same minimal header set as the repo; the browser fills in sec-fetch-* and
# Origin automatically for same-origin fetch().
_FETCH_HEADERS = {"accept-language": "en-US,en;q=0.5"}

# IP echo needs NO cookies. Credentialed ('include') cross-origin fetches
# require Access-Control-Allow-Credentials, which ipify doesn't send —
# 'omit' makes it a plain cors request that ipify's ACAO:* satisfies.
_IP_JS = _FETCH_JS.replace("credentials: 'include'", "credentials: 'omit'")


class IdentityBlocked(Exception):
    """Mint failed to clear Google's challenge (sorry / enablejs / no h3)."""


class PoolBurned(Exception):
    """MAX_MINTS consecutive mints failed — the pool is dead right now."""


def classify(body: str, final_url: str = "") -> dict:
    """h3 count + block signals, mirroring the probe's classify()."""
    sorry = "/sorry/" in (final_url or "").lower() or (
        "unusual traffic" in body[:6000] and "unusual-traffic" in body[:6000]
    )
    enablejs = "/httpservice/retry/enablejs" in body
    h3 = len(re.findall(r"<h3", body, re.I))
    return {"h3": h3, "sorry": sorry, "enablejs": enablejs}


def blocked_reason(status: int, body: str, h3_count: int) -> Optional[str]:
    """Human-readable block reason for a serp() response, or None if OK.

    A real SERP has h3 > 0; anything else is a challenge/soft-block. Follows
    the probe's semantics (h3 presence wins over enablejs), so a body with
    both h3 and an enablejs script marker still counts as OK.
    """
    if status == 429:
        return "http 429 rate-limited"
    if "unusual traffic" in body[:6000] and "unusual-traffic" in body[:6000]:
        return "sorry"
    if "/httpservice/retry/enablejs" in body and h3_count == 0:
        return "enablejs"
    if h3_count == 0 and "<body" in body.lower():
        return "empty SERP (likely soft block)"
    return None


class MintedIdentity:
    """One minted browser identity: a parked google.com page that owns the
    JS challenge, the x5sec cookie, the live connection, and the exit IP.

    Camoufox lifecycle is manual (`__aenter__`/`__aexit__`), NOT `async
    with`, so the recycle loop can tear the browser down mid-run.
    """

    def __init__(self, proxy: Optional[dict] = None, geo: str = "us",
                 language: str = "en", timeout_ms: int = MINT_TIMEOUT_MS,
                 query_limit: int = DEFAULT_QUERY_LIMIT):
        self.proxy = proxy
        self.geo = geo
        self.language = language
        self.timeout_ms = timeout_ms
        self.query_limit = query_limit
        self._cam: Optional[AsyncCamoufox] = None
        self._browser = None
        self._parked = None
        self._wire_records: list = []
        self.minted = False
        self.exit_ip: Optional[str] = None
        self.queries_served = 0
        self.wire_bytes_total = 0

    # ---------- lifecycle ----------

    async def mint(self, capture_exit_ip: bool = True) -> None:
        """Homepage warmup + one real /search nav (the identity mint), then
        park back on the homepage. Raises IdentityBlocked if the challenge
        was not cleared."""
        if self.minted:
            return
        self._cam = AsyncCamoufox(
            headless=True,
            proxy=self.proxy,
            humanize=False,  # in-page fetch has no mouse path to humanize
            geoip=True,
            locale=f"{self.language}-{self.geo.upper()}",
        )
        self._browser = await self._cam.__aenter__()
        page = await self._browser.new_page()

        r = await page.goto(HOME_URL, wait_until="domcontentloaded",
                            timeout=self.timeout_ms)
        home_status = r.status if r else 0
        m = await page.goto(f"{SEARCH_URL}?q=serp+scrape",
                            wait_until="domcontentloaded",
                            timeout=self.timeout_ms)
        search_status = m.status if m else home_status
        try:
            await page.wait_for_selector("h3", timeout=MINT_WAIT_H3_MS)
        except Exception:
            body = await page.content()
            c = classify(body, page.url)
            await self._teardown()
            raise IdentityBlocked(
                f"mint: h3={c['h3']} sorry={c['sorry']} "
                f"enablejs={c['enablejs']} home={home_status} "
                f"search={search_status}"
            )

        # Park back on the light homepage; the parked tab now holds the
        # connection + cookies for every in-page fetch that follows.
        await page.goto(HOME_URL, wait_until="domcontentloaded",
                        timeout=self.timeout_ms)
        self._parked = page
        self._attach_wire_listener()
        if capture_exit_ip:
            self.exit_ip = await self._capture_exit_ip()
        self.minted = True

    async def _teardown(self) -> None:
        parked, cam = self._parked, self._cam
        self._parked, self._browser, self._cam = None, None, None
        self.minted = False
        if parked is not None:
            try:
                await parked.close()
            except Exception:
                pass
        if cam is not None:
            try:
                await cam.__aexit__(None, None, None)
            except Exception:
                pass

    async def close(self) -> None:
        """Tear down the browser. Safe to call more than once."""
        await self._teardown()

    # ---------- replay ----------

    async def serp(self, query: str) -> dict:
        """In-page fetch of /search from the parked tab.

        Returns {html, final_url, status, wire_bytes, request_count,
        body_bytes, h3_count, sorry, enablejs, blocked_reason, ok, error,
        latency_ms, exit_ip}. `blocked_reason` is None on a pass.
        """
        if not self.minted or self._parked is None:
            raise RuntimeError("identity not minted")
        url = f"{SEARCH_URL}?q={query.replace(' ', '+')}"
        base = len(self._wire_records)
        t0 = time.perf_counter()
        res = await self._parked.evaluate(
            _FETCH_JS,
            {"url": url, "method": "GET", "headers": _FETCH_HEADERS,
             "timeoutMs": FETCH_TIMEOUT_MS},
        ) or {}
        body = res.get("body") or ""
        status = int(res.get("status") or 0)

        wire = 0
        reqs = 0
        for (u, s, w) in self._wire_records[base:]:
            reqs += 1
            if "/search" in u:
                wire += w
        self.wire_bytes_total += wire
        self.queries_served += 1

        if not res.get("ok"):
            reason = str(res.get("error") or "fetch failed")[:120]
        else:
            h3 = len(re.findall(r"<h3", body, re.I))
            reason = blocked_reason(status, body, h3)

        return {
            "html": body,
            "final_url": url,
            "status": status,
            "wire_bytes": int(wire),
            "request_count": reqs,
            "body_bytes": len(body.encode("utf-8")),
            "h3_count": h3 if res.get("ok") else 0,
            "sorry": "/sorry/" in url.lower() or (
                "unusual traffic" in body[:6000]
                and "unusual-traffic" in body[:6000]
            ),
            "enablejs": "/httpservice/retry/enablejs" in body,
            "blocked_reason": reason,
            "ok": reason is None,
            "error": None if reason is None else reason,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "exit_ip": self.exit_ip,
        }

    # ---------- instrumentation ----------

    def _attach_wire_listener(self) -> None:
        """Per-request wire-byte accounting via request.sizes().

        request.sizes() returns {requestBodySize, requestHeadersSize,
        responseBodySize, responseHeadersSize} in WIRE bytes (post-gzip),
        which is what the proxy bills. In-page fetch() requests fire normal
        Playwright request events, so the parked page's /search fetches land
        here. Mint-time navigation is NOT captured (it's identity cost, not
        query cost) — the listener attaches after parking.
        """
        records = self._wire_records

        async def on_finished(request):
            try:
                resp = await request.response()
                status = resp.status if resp else None
                sizes = await request.sizes()
                wire = (
                    (sizes.get("responseBodySize") or 0)
                    + (sizes.get("responseHeadersSize") or 0)
                )
                records.append((request.url, status, int(wire)))
            except Exception:
                pass

        self._parked.on(
            "requestfinished", lambda r: asyncio.create_task(on_finished(r))
        )

    async def _capture_exit_ip(self) -> Optional[str]:
        """Observed exit IP via an in-page fetch to ipify on the parked page.

        NOTE: ipify is a different host than google.com, so on the rotating
        pool this opens a NEW connection and shows the pool IP at that
        moment — not necessarily the parked google.com connection's IP. It
        still proves the pool rotates, and is the best available reading.
        """
        try:
            res = await self._parked.evaluate(
                _IP_JS,
                {"url": IPIFY_URL, "method": "GET",
                 "headers": _FETCH_HEADERS, "timeoutMs": IPIFY_TIMEOUT_MS},
            ) or {}
            body = res.get("body") or ""
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
            return m.group(1) if m else None
        except Exception:
            return None


# ---------- mint / recycle helpers ----------

async def mint_identity(proxy: Optional[dict] = None, geo: str = "us",
                        language: str = "en",
                        timeout_ms: int = MINT_TIMEOUT_MS,
                        query_limit: int = DEFAULT_QUERY_LIMIT,
                        capture_exit_ip: bool = True,
                        blocklist_check: Optional[Callable[[str], bool]] = None,
                        ) -> MintedIdentity:
    """Mint an identity with up to MAX_MINTS attempts.

    A mint that fails the blocklist check (known-bad exit IP) counts as a
    failed mint — same recycle semantics. Raises PoolBurned if all attempts
    fail.
    """
    last_err: Optional[Exception] = None
    for _ in range(1, MAX_MINTS + 1):
        identity = MintedIdentity(proxy=proxy, geo=geo, language=language,
                                  timeout_ms=timeout_ms,
                                  query_limit=query_limit)
        try:
            await identity.mint(capture_exit_ip=capture_exit_ip)
            if (blocklist_check and identity.exit_ip
                    and blocklist_check(identity.exit_ip)):
                raise IdentityBlocked(
                    f"exit IP {identity.exit_ip} is blocklisted"
                )
            return identity
        except IdentityBlocked as e:
            last_err = e
            await identity.close()
    raise PoolBurned(
        f"pool_burned: {MAX_MINTS} consecutive mints failed "
        f"(last: {last_err})"
    )


async def fetch_identity(query: str, *, proxy: Optional[dict] = None,
                         geo: str = "us", language: str = "en",
                         timeout_ms: int = MINT_TIMEOUT_MS,
                         capture_exit_ip: bool = True,
                         blocklist_check: Optional[Callable[[str], bool]] = None,
                         ) -> dict:
    """Mint one identity, run one query, close. On a block: close the
    identity, mint fresh, and retry the query once on the fresh identity.
    Raises PoolBurned if mints keep failing."""
    identity = await mint_identity(
        proxy=proxy, geo=geo, language=language, timeout_ms=timeout_ms,
        capture_exit_ip=capture_exit_ip, blocklist_check=blocklist_check,
    )
    try:
        result = await identity.serp(query)
        if result["blocked_reason"]:
            await identity.close()
            identity = await mint_identity(
                proxy=proxy, geo=geo, language=language, timeout_ms=timeout_ms,
                capture_exit_ip=capture_exit_ip,
                blocklist_check=blocklist_check,
            )
            retry = await identity.serp(query)
            retry["retry_of_block"] = result["blocked_reason"]
            return retry
        return result
    finally:
        await identity.close()


async def run_identity_queries(
    keywords: List[str], *,
    on_query: Callable[[str, dict, bool, int], Awaitable[None]],
    proxy: Optional[dict] = None,
    geo: str = "us",
    language: str = "en",
    timeout_ms: int = MINT_TIMEOUT_MS,
    capture_exit_ip: bool = True,
    blocklist_check: Optional[Callable[[str], bool]] = None,
    query_limit: int = DEFAULT_QUERY_LIMIT,
) -> tuple[int, list[int]]:
    """Drive keywords across minted identities with the production recycle loop.

    - Proactive recycle after `query_limit` clean queries (fresh IP before the
      current one goes stale/hot).
    - On a block: close, mint fresh, retry the blocked query once on the fresh
      identity (`is_retry=True` in on_query).
    - 3 consecutive failed mints -> PoolBurned (propagates; caller marks the
      run error with reason pool_burned).

    on_query(query, result, is_retry, identity_index) is awaited for every
    final result. Returns (identities_used, queries_per_identity).
    """
    identities_used = 0
    queries_per_identity: list[int] = []
    identity: Optional[MintedIdentity] = None

    def _new() -> Awaitable[MintedIdentity]:
        return mint_identity(
            proxy=proxy, geo=geo, language=language, timeout_ms=timeout_ms,
            capture_exit_ip=capture_exit_ip, blocklist_check=blocklist_check,
        )

    try:
        for q in keywords:
            if identity is None or identity.queries_served >= identity.query_limit:
                if identity is not None:
                    queries_per_identity.append(identity.queries_served)
                    await identity.close()
                identity = await _new()
                identities_used += 1

            result = await identity.serp(q)
            if result["blocked_reason"]:
                queries_per_identity.append(identity.queries_served)
                await identity.close()
                identity = await _new()
                identities_used += 1
                retry = await identity.serp(q)
                retry["retry_of_block"] = result["blocked_reason"]
                await on_query(q, retry, True, identities_used)
            else:
                await on_query(q, result, False, identities_used)
    finally:
        if identity is not None:
            queries_per_identity.append(identity.queries_served)
            await identity.close()

    return identities_used, queries_per_identity