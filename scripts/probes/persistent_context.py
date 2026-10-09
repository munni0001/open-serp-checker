"""Experiment P — persistent browser context across queries.

Production `google.py` launches a fresh Chromium per fetch(). That means every
query pays cold-cache cost for ~800 KB of Google/gstatic JavaScript that a
real browser would serve from memory on the second query onward.

This probe measures the cost of keeping the browser + context alive across
queries: one browser, one context, warmup homepage once, then N searches
back-to-back on fresh pages (same context = same HTTP cache).

Expected shape:
- Query 1: cold cache, similar bytes to fresh-browser baseline.
- Queries 2-N: warm cache, JS refetches drop, per-query bytes plummet.

The delta between query 1 and queries 2-N is the persistence lever.

Usage:
    source venv/bin/activate
    python scripts/probes/persistent_context.py                  # Decodo, N=10
    python scripts/probes/persistent_context.py --no-proxy       # home IP
    python scripts/probes/persistent_context.py --n 5            # subset
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright

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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

STEALTH_INIT_JS = """
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

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]

DB_PATH = Path(__file__).parent.parent.parent / "open_serp_checker.db"


def load_decodo(port: int) -> Optional[dict]:
    """Build a Decodo proxy dict for the given endpoint port.

    Decodo's `gate.decodo.com` exposes ports 10001-10010 as SEPARATE sticky
    endpoints: each port holds one IP for a 10-minute window, independent of
    the others. Pinning all queries in a batch to one port = one sticky
    session for the batch. Port 10000 is a rotating backconnect (new IP per
    connection) and is a different beast entirely.
    """
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


def _short_host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except ValueError:
        return "other"
    if h.endswith(".google.com") or h == "google.com":
        return "google.com"
    if h.endswith(".gstatic.com"):
        return "gstatic.com"
    if h.endswith(".googleapis.com"):
        return "googleapis.com"
    if h.endswith(".googleusercontent.com"):
        return "googleusercontent.com"
    if h.endswith(".doubleclick.net"):
        return "doubleclick.net"
    return h or "other"


@dataclass
class ReqRecord:
    phase: str
    url: str
    host: str
    resource_type: str
    status: Optional[int]
    encoded_bytes: int
    from_cache: bool           # served from browser cache, no wire cost
    failed: bool


@dataclass
class QueryResult:
    idx: int                    # 1-based position in the sequential run
    query: str
    final_url: str
    status: int
    h3_count: int
    enablejs: bool
    sorry: bool
    latency_ms: int
    total_bytes: int            # sum of encodedDataLength for this phase's requests
    request_count: int
    cached_count: int
    error: Optional[str] = None
    requests: list[ReqRecord] = field(default_factory=list)


def _attach_cdp_listeners(cdp, phase_ref, records):
    """Wire CDP handlers that append into a shared records list, tagged by phase."""

    pending: dict[str, dict] = {}

    def on_request(evt):
        rid = evt["requestId"]
        req = evt.get("request", {})
        url = req.get("url", "")
        pending[rid] = {
            "phase": phase_ref["current"],
            "url": url,
            "host": _short_host(url),
            "resource_type": evt.get("type", "Other"),
            "status": None,
            "encoded_bytes": 0,
            "from_cache": False,
            "failed": False,
        }

    def on_response(evt):
        rid = evt["requestId"]
        p = pending.get(rid)
        if not p:
            return
        resp = evt.get("response", {})
        p["status"] = resp.get("status")
        # `fromDiskCache` / `fromPrefetchCache` / `fromServiceWorker` — any means zero wire.
        if resp.get("fromDiskCache") or resp.get("fromPrefetchCache") or resp.get("fromServiceWorker"):
            p["from_cache"] = True

    def on_finished(evt):
        rid = evt["requestId"]
        p = pending.pop(rid, None)
        if not p:
            return
        p["encoded_bytes"] = int(evt.get("encodedDataLength", 0) or 0)
        records.append(ReqRecord(**p))

    def on_failed(evt):
        rid = evt["requestId"]
        p = pending.pop(rid, None)
        if not p:
            return
        p["encoded_bytes"] = int(evt.get("encodedDataLength", 0) or 0)
        p["failed"] = True
        records.append(ReqRecord(**p))

    cdp.on("Network.requestWillBeSent", on_request)
    cdp.on("Network.responseReceived", on_response)
    cdp.on("Network.loadingFinished", on_finished)
    cdp.on("Network.loadingFailed", on_failed)


async def _capture_exit_ip(context, timeout_ms: int = 15_000) -> Optional[str]:
    """One shot at api.ipify.org — ~200 bytes. Used only to verify sticky
    behavior at session start and end (did the IP hold?). Not called
    per-query; that would inflate the measurement.
    """
    page = await context.new_page()
    try:
        r = await page.goto("https://api.ipify.org?format=json",
                            wait_until="domcontentloaded", timeout=timeout_ms)
        if r and r.status == 200:
            body = await page.content()
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
            if m:
                return m.group(1)
    except Exception:
        return None
    finally:
        await page.close()
    return None


async def run_sequential(proxy: Optional[dict], keywords: list[str],
                         pace_seconds: float = 0.0,
                         verify_sticky: bool = False) -> list[QueryResult]:
    results: list[QueryResult] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
        try:
            context = await browser.new_context(
                proxy=proxy,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
                user_agent=USER_AGENT,
            )
            await context.add_init_script(STEALTH_INIT_JS)

            # Optional sanity check: log the exit IP once at session start so
            # we can compare against end-of-session IP to verify sticky held.
            start_ip: Optional[str] = None
            if verify_sticky:
                start_ip = await _capture_exit_ip(context)
                print(f"Exit IP at session start: {start_ip}")

            # Warmup — ONCE at the start of the session, not per query. This is the
            # whole point of the experiment: pay the cold-cache cost once, amortize.
            warmup_page = await context.new_page()
            warm_cdp = await context.new_cdp_session(warmup_page)
            await warm_cdp.send("Network.enable")
            warm_phase = {"current": "warmup"}
            warm_records: list[ReqRecord] = []
            _attach_cdp_listeners(warm_cdp, warm_phase, warm_records)

            print("Warmup (once, homepage)...")
            t_warm = time.perf_counter()
            try:
                await warmup_page.goto(
                    "https://www.google.com/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await asyncio.sleep(0.5)
            finally:
                await warmup_page.close()
            warm_bytes = sum(r.encoded_bytes for r in warm_records)
            print(f"  warmup: {warm_bytes/1024:.1f} KB / {len(warm_records)} reqs / "
                  f"{int((time.perf_counter()-t_warm)*1000)}ms\n")

            # Sequential searches, fresh page per query but SAME context (=> same HTTP cache).
            for i, q in enumerate(keywords, 1):
                # Human-cadence pacing between queries. Applies BEFORE each search
                # except the first (which follows the warmup naturally).
                if pace_seconds > 0 and i > 1:
                    print(f"  ...pacing {pace_seconds:.0f}s")
                    await asyncio.sleep(pace_seconds)
                t0 = time.perf_counter()
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")
                phase = {"current": "search"}
                records: list[ReqRecord] = []
                _attach_cdp_listeners(cdp, phase, records)

                try:
                    search_url = f"https://www.google.com/search?q={q.replace(' ', '+')}"
                    resp = await page.goto(search_url, wait_until="domcontentloaded",
                                           timeout=30_000)
                    try:
                        await page.wait_for_selector("h3", timeout=5_000)
                    except Exception:
                        pass
                    html = await page.content()
                    final_url = page.url
                    await asyncio.sleep(0.5)  # let in-flight loadingFinished fire

                    total_b = sum(r.encoded_bytes for r in records)
                    cached = sum(1 for r in records if r.from_cache)

                    r = QueryResult(
                        idx=i, query=q, final_url=final_url,
                        status=resp.status if resp else 0,
                        h3_count=len(re.findall(r"<h3", html, re.I)),
                        enablejs="/httpservice/retry/enablejs" in html,
                        sorry="/sorry/" in (final_url or "").lower(),
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        total_bytes=total_b,
                        request_count=len(records),
                        cached_count=cached,
                        requests=records,
                    )
                    note = " SORRY" if r.sorry else (" enablejs" if r.enablejs else "")
                    print(f"  [q{i}] {q!r:<40} h3={r.h3_count} reqs={r.request_count} "
                          f"(cached={cached}) bytes={total_b/1024:.1f} KB "
                          f"lat={r.latency_ms}ms{note}")
                    results.append(r)
                except Exception as e:
                    results.append(QueryResult(
                        idx=i, query=q, final_url="", status=0, h3_count=0,
                        enablejs=False, sorry=False,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        total_bytes=0, request_count=0, cached_count=0,
                        error=f"{type(e).__name__}: {e}"[:200],
                    ))
                    print(f"  [q{i}] ERR: {e}")
                finally:
                    await page.close()

            if verify_sticky:
                end_ip = await _capture_exit_ip(context)
                verdict = "HELD" if (start_ip and end_ip and start_ip == end_ip) else "DRIFTED"
                print(f"\nExit IP at session end: {end_ip}  (sticky {verdict})")
        finally:
            await browser.close()
    return results


def print_summary(results: list[QueryResult]) -> None:
    ok = [r for r in results if r.error is None]
    if not ok:
        print("\n(!) All queries errored.")
        return

    print("\n=== Per-query totals (sequential in same context) ===")
    print(f"{'q':>2}  {'query':<40} {'h3':>3} {'reqs':>5} {'cached':>7} {'bytes_KB':>10} {'lat_s':>6}")
    for r in ok:
        note = " SORRY" if r.sorry else (" ENABLEJS" if r.enablejs else "")
        print(f"{r.idx:>2}  {r.query:<40} {r.h3_count:>3} {r.request_count:>5} "
              f"{r.cached_count:>7} {r.total_bytes/1024:>10.1f} "
              f"{r.latency_ms/1000:>6.1f}{note}")

    if len(ok) >= 2:
        cold = ok[0].total_bytes
        warm = [r.total_bytes for r in ok[1:]]
        warm_avg = sum(warm) / len(warm)
        print(f"\nCold q1: {cold/1024:.1f} KB")
        print(f"Warm q2-q{len(ok)} avg: {warm_avg/1024:.1f} KB  "
              f"(min={min(warm)/1024:.1f}, max={max(warm)/1024:.1f})")
        print(f"Warm-cache savings: {(cold - warm_avg)/1024:.1f} KB/query "
              f"({100*(1 - warm_avg/cold):.0f}% reduction)")

    # Aggregate by resource_type on WARM queries only — this is what production would pay.
    warm_results = ok[1:] if len(ok) >= 2 else []
    if warm_results:
        print("\n=== By resource_type (warm queries only — q2 onwards) ===")
        by_rt = defaultdict(lambda: [0, 0, 0])  # n, bytes, cached_count
        for r in warm_results:
            for req in r.requests:
                by_rt[req.resource_type][0] += 1
                by_rt[req.resource_type][1] += req.encoded_bytes
                if req.from_cache:
                    by_rt[req.resource_type][2] += 1
        total = sum(v[1] for v in by_rt.values()) or 1
        print(f"{'resource_type':<18} {'n':>6} {'cached':>7} {'total_KB':>10} {'avg_KB':>8} {'%':>6}")
        for rt, (cnt, byt, cch) in sorted(by_rt.items(), key=lambda kv: -kv[1][1]):
            avg = byt / cnt if cnt else 0
            pct = 100 * byt / total
            print(f"{rt:<18} {cnt:>6} {cch:>7} {byt/1024:>10.1f} {avg/1024:>8.1f} {pct:>5.1f}%")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(KEYWORDS))
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--port", type=int, default=10001,
                    help="Decodo sticky endpoint port (10001-10010). Ignored with --no-proxy.")
    ap.add_argument("--pace", type=float, default=0.0,
                    help="Seconds to sleep between queries. 0 = back-to-back. "
                         "35 = human search cadence.")
    ap.add_argument("--verify-sticky", action="store_true",
                    help="Hit api.ipify.org at session start and end to confirm the "
                         "sticky endpoint held the same IP for the whole batch.")
    args = ap.parse_args()

    keywords = KEYWORDS[: args.n]
    proxy = None if args.no_proxy else load_decodo(args.port)
    if not args.no_proxy and not proxy:
        print("(!) No Decodo row in DB; use --no-proxy to run on home IP.", file=sys.stderr)
        return 2

    label = "home IP" if args.no_proxy else f"Decodo :{args.port} (user={proxy['username']})"
    pace_note = f", pace={args.pace}s" if args.pace > 0 else ", back-to-back"
    print(f"Persistent context (no blocking) — {label} — {len(keywords)} queries{pace_note}\n")

    results = await run_sequential(proxy, keywords,
                                   pace_seconds=args.pace,
                                   verify_sticky=args.verify_sticky)

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "homeip" if args.no_proxy else f"decodo{args.port}"
    log_path = log_dir / f"persistent_context_{tag}_{ts}.json"
    log_path.write_text(json.dumps([asdict(r) for r in results], indent=2))

    print_summary(results)
    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
