"""Ports the github-script-for-test "minted identity + in-page fetch" mechanism.

The repo mints ONE identity (a Camoufox browser that clears Google's JS
challenge once), then replays every query as an in-page `fetch()` from a
parked google.com page instead of a fresh navigation. The fetch inherits the
page's cookies, x5sec clearance and TLS fingerprint, and skips the entire
page-chrome/resource load — the query cost collapses to the SERP body alone.

This probe A/B's that against our current approach (same browser, same
context, but a full `goto /search` per query) on the same Decodo pool,
interleaved per keyword so pool drift can't masquerade as a mechanism win.

  ARM A  current   new page -> goto /search -> wait h3   (full nav, all resources)
  ARM B  repo      parked homepage page -> evaluate(fetch(searchUrl)) -> read body

Minting here is warmup-only (homepage + one search navigation) — the repo
mints with a CapSolver captcha extension which we don't have. If arm B fails
with botguard, that gap is the likely cause, not the mechanism.

Usage:
    source venv/bin/activate
    python scripts/probes/minted_identity_inpage_fetch.py
    python scripts/probes/minted_identity_inpage_fetch.py --port 10000
    python scripts/probes/minted_identity_inpage_fetch.py --no-proxy
    python scripts/probes/minted_identity_inpage_fetch.py --n 5 --arm a
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from camoufox.async_api import AsyncCamoufox

KEYWORDS = [
    "foxy ai promo code",
    "heygen promo code",
    "apify promo code",
    "webshare coupon code",
    "gamestop promo code",
    "midjourney promo code",
    "twilio promo code",
    "bnesim coupon",
    "retroid discount code",
    "paymore discount code",
]

DB_PATH = Path(__file__).parent.parent.parent / "serp_scraper.db"
SEARCH_URL = "https://www.google.com/search"
HOME_URL = "https://www.google.com/"

# The repo's in-page fetch: same-origin, credentials included, hard timeout.
# Returns a structured object instead of throwing across the evaluate bridge.
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
_FETCH_TIMEOUT_MS = 20_000

# IP echo needs NO cookies, and credentialed ('include') cross-origin fetches
# require Access-Control-Allow-Credentials which ipify doesn't send — 'omit'
# makes it a plain cors request that ipify's ACAO:* satisfies.
_IP_JS = _FETCH_JS.replace("credentials: 'include'", "credentials: 'omit'")


@dataclass
class QueryResult:
    arm: str
    idx: int
    query: str
    ok: bool
    h3_count: int
    sorry: bool
    enablejs: bool
    status: int
    latency_ms: int
    body_bytes: int
    ip: Optional[str] = None
    error: Optional[str] = None


def load_decodo(port: int) -> Optional[dict]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1"
    ).fetchone()
    db.close()
    if not row:
        return None
    return {
        "server": f"http://{row['host']}:{port}",
        "username": row["username"],
        "password": row["password"],
    }


def classify(body: str, final_url: str = "") -> dict:
    sorry = "/sorry/" in (final_url or "").lower() or (
        "unusual traffic" in body[:6000] and "unusual-traffic" in body[:6000]
    )
    enablejs = "/httpservice/retry/enablejs" in body
    h3 = len(re.findall(r"<h3", body, re.I))
    return {"h3": h3, "sorry": sorry, "enablejs": enablejs}


async def _exit_ip(page) -> Optional[str]:
    """Observed exit IP via an in-page fetch to ipify on the given page.

    NOTE: ipify is a different host than google.com, so on the rotating pool
    this opens a NEW connection and shows the pool IP at that moment — not
    necessarily the parked google.com connection's IP. It still proves the
    pool is rotating, and if SERPs pass while these keep changing, that's a
    signal Google tolerates the cookie across fresh connections.
    """
    try:
        res = await page.evaluate(
            _IP_JS,
            {"url": "https://api.ipify.org?format=json", "method": "GET",
             "headers": _FETCH_HEADERS, "timeoutMs": 8_000},
        ) or {}
        body = res.get("body") or ""
        m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
        return m.group(1) if m else None
    except Exception:
        return None


async def run_arm_a(browser, q: str, t0: float) -> QueryResult:
    """Current approach: new page, full goto /search."""
    page = await browser.new_page()
    try:
        url = f"{SEARCH_URL}?q={q.replace(' ', '+')}"
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.wait_for_selector("h3", timeout=5_000)
        except Exception:
            pass
        body = await page.content()
        c = classify(body, page.url)
        ip = await _exit_ip(page)
        return QueryResult(
            arm="A", idx=0, query=q, ok=c["h3"] > 0, h3_count=c["h3"],
            sorry=c["sorry"], enablejs=c["enablejs"],
            status=resp.status if resp else 0,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            body_bytes=len(body.encode()), ip=ip,
        )
    except Exception as e:
        return QueryResult(
            arm="A", idx=0, query=q, ok=False, h3_count=0, sorry=False,
            enablejs=False, status=0,
            latency_ms=int((time.perf_counter() - t0) * 1000), body_bytes=0,
            error=f"{type(e).__name__}: {e}"[:200],
        )
    finally:
        await page.close()


async def run_arm_b(parked, q: str, t0: float) -> QueryResult:
    """Repo approach: in-page fetch of /search from the parked homepage."""
    url = f"{SEARCH_URL}?q={q.replace(' ', '+')}"
    res = await parked.evaluate(
        _FETCH_JS,
        {"url": url, "method": "GET", "headers": _FETCH_HEADERS,
         "timeoutMs": _FETCH_TIMEOUT_MS},
    ) or {}
    body = res.get("body") or ""
    c = classify(body)
    ip = await _exit_ip(parked)
    return QueryResult(
        arm="B", idx=0, query=q, ok=c["h3"] > 0, h3_count=c["h3"],
        sorry=c["sorry"], enablejs=c["enablejs"],
        status=int(res.get("status") or 0),
        latency_ms=int((time.perf_counter() - t0) * 1000),
        body_bytes=len(body.encode()), ip=ip,
        error=None if c["h3"] > 0 or res.get("ok") else
        (res.get("error") or "fetch failed")[:200],
    )


async def mint(browser):
    """Homepage warmup + one real search navigation (the identity mint).

    Returns the parked homepage page, ready for in-page fetches.
    """
    page = await browser.new_page()
    r = await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
    print(f"  mint: homepage status={r.status if r else None}")
    m = await page.goto(
        f"{SEARCH_URL}?q=serp scrape", wait_until="domcontentloaded",
        timeout=45_000,
    )
    try:
        await page.wait_for_selector("h3", timeout=8_000)
        print(f"  mint: first search h3=present (identity cleared)")
    except Exception:
        body = await page.content()
        c = classify(body, page.url)
        print(f"  mint: first search h3={c['h3']} "
              f"sorry={c['sorry']} enablejs={c['enablejs']} "
              f"(identity NOT cleared)")
    # Park back on the light homepage for the in-page fetches.
    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
    return page


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(KEYWORDS))
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--port", type=int, default=10001,
                    help="Decodo endpoint. 10000=rotating, 10001-10010=sticky.")
    ap.add_argument("--arm", choices=("a", "b", "both"), default="both")
    ap.add_argument("--pace", type=float, default=0.0,
                    help="Seconds between keyword pairs.")
    args = ap.parse_args()

    keywords = KEYWORDS[: args.n]
    proxy = None if args.no_proxy else load_decodo(args.port)
    if not args.no_proxy and not proxy:
        print("(!) No Decodo row in DB; use --no-proxy to run on home IP.",
              file=sys.stderr)
        return 2

    label = "home IP" if args.no_proxy else f"Decodo :{args.port} (user={proxy['username']})"
    print(f"Minted identity + in-page fetch A/B — {label} — "
          f"{len(keywords)} keywords, arms={args.arm}\n")

    results: list[QueryResult] = []
    try:
        cam = AsyncCamoufox(
            headless=True,
            proxy=proxy,
            humanize=False,   # kept off: in-page fetch has no mouse path to humanize
            geoip=True,
            locale="en-US",
        )
    except Exception as e:
        print(f"camoufox construct failed: {type(e).__name__}: {e}")
        return 2

    async with cam as browser:
        parked = await mint(browser)
        arms = ("A", "B") if args.arm == "both" else (args.arm.upper(),)
        for i, q in enumerate(keywords, 1):
            if i > 1 and args.pace > 0:
                print(f"  ...pacing {args.pace:.0f}s")
                await asyncio.sleep(args.pace)
            for arm in arms:
                t0 = time.perf_counter()
                r = (await run_arm_a(browser, q, t0)
                     if arm == "A"
                     else await run_arm_b(parked, q, t0))
                r.idx = i
                tag = "OK" if r.ok else ("SORRY" if r.sorry
                                         else ("ENABLEJS" if r.enablejs
                                               else "FAIL"))
                print(f"  [arm {r.arm} q{i}] {q!r:<28} h3={r.h3_count} "
                      f"status={r.status} bytes={r.body_bytes/1024:.1f} KB "
                      f"ip={r.ip or '?'} lat={r.latency_ms}ms -> {tag}"
                      + (f"  err={r.error}" if r.error else ""))
                results.append(r)
        await parked.close()

    print("\n=== Summary (interleaved, same pool) ===")
    for arm in ("A", "B"):
        rs = [r for r in results if r.arm == arm]
        if not rs:
            continue
        oks = sum(1 for r in rs if r.ok)
        bytes_ok = [r.body_bytes for r in rs if r.ok]
        lat_ok = [r.latency_ms for r in rs if r.ok]
        print(f"  arm {arm}: {oks}/{len(rs)} OK  "
              f"bytes/OK={sum(bytes_ok)/len(bytes_ok)/1024:.1f} KB avg "
              f"(n={len(bytes_ok)})  "
              f"lat/OK={sum(lat_ok)/len(lat_ok):.0f}ms avg (n={len(lat_ok)})")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "homeip" if args.no_proxy else f"decodo{args.port}"
    log_path = log_dir / f"minted_identity_inpage_fetch_{tag}_{ts}.json"
    log_path.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))