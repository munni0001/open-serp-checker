"""Session 3.1 phase 2 — persistent context with Firefox on Decodo residential.

Isolate probe proved Firefox passes 3/3 where Chromium hangs 0/3 on the same
Decodo pool. This script measures whether the bandwidth-diet win from Session
2.7.5 (177 KB/query warm avg with Chromium on home IP) survives the browser
swap AND the proxy path.

Same shape as persistent_context.py — one browser, one context, warmup once,
N sequential searches in the same HTTP cache. Two forced changes:

1. pw.firefox.launch() instead of pw.chromium.launch(). No stealth args,
   no --disable-blink-features (Firefox flags are different and it does
   not expose navigator.webdriver in the same way).
2. Byte accounting via Playwright's request events + request.sizes()
   instead of CDP (Firefox does not speak CDP). Cache-hit detection is
   NOT available cross-browser through Playwright — we infer the warm-cache
   win from the total-bytes drop across the sequence, same as the Chromium
   run just without the per-request fromDiskCache flag.

Usage:
    source venv/bin/activate
    python scripts/probes/persistent_context_firefox.py                       # Decodo :10001 sticky
    python scripts/probes/persistent_context_firefox.py --port 10000          # rotating
    python scripts/probes/persistent_context_firefox.py --no-proxy            # home IP baseline
    python scripts/probes/persistent_context_firefox.py --pace 35 --verify-sticky
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

# Stock Firefox 122 desktop UA. Matches the ClientHello shape Playwright
# ships when you `pw.firefox.launch()` — do NOT override to a Chrome UA
# (that would mismatch the TLS fingerprint, defeating the whole point).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) "
    "Gecko/20100101 Firefox/122.0"
)

DB_PATH = Path(__file__).parent.parent.parent / "open_serp_checker.db"


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
    failed: bool


@dataclass
class QueryResult:
    idx: int
    query: str
    final_url: str
    status: int
    h3_count: int
    enablejs: bool
    sorry: bool
    latency_ms: int
    total_bytes: int
    request_count: int
    error: Optional[str] = None
    requests: list[ReqRecord] = field(default_factory=list)


def _attach_page_listeners(page, phase_ref, records):
    """Cross-browser byte accounting via Playwright request events.

    Firefox does not expose CDP, so we cannot use Network.loadingFinished's
    encodedDataLength. Instead we use `request.sizes()` which Playwright
    provides for both engines and returns dict {requestBodySize,
    requestHeadersSize, responseBodySize, responseHeadersSize} in wire bytes.
    """

    async def on_finished(request):
        try:
            resp = await request.response()
            status = resp.status if resp else None
            sizes = await request.sizes()
            wire = (
                (sizes.get("responseBodySize") or 0)
                + (sizes.get("responseHeadersSize") or 0)
            )
            records.append(ReqRecord(
                phase=phase_ref["current"],
                url=request.url,
                host=_short_host(request.url),
                resource_type=request.resource_type,
                status=status,
                encoded_bytes=int(wire),
                failed=False,
            ))
        except Exception:
            # Page may have closed mid-flight; drop silently.
            pass

    async def on_failed(request):
        records.append(ReqRecord(
            phase=phase_ref["current"],
            url=request.url,
            host=_short_host(request.url),
            resource_type=request.resource_type,
            status=None,
            encoded_bytes=0,
            failed=True,
        ))

    page.on("requestfinished", lambda r: asyncio.create_task(on_finished(r)))
    page.on("requestfailed",   lambda r: asyncio.create_task(on_failed(r)))


async def _capture_exit_ip(context, timeout_ms: int = 15_000) -> Optional[str]:
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
        browser = await pw.firefox.launch(headless=True)
        try:
            context = await browser.new_context(
                proxy=proxy,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
                user_agent=USER_AGENT,
            )

            start_ip: Optional[str] = None
            if verify_sticky:
                start_ip = await _capture_exit_ip(context)
                print(f"Exit IP at session start: {start_ip}")

            warmup_page = await context.new_page()
            warm_phase = {"current": "warmup"}
            warm_records: list[ReqRecord] = []
            _attach_page_listeners(warmup_page, warm_phase, warm_records)

            print("Warmup (once, homepage)...")
            t_warm = time.perf_counter()
            try:
                await warmup_page.goto(
                    "https://www.google.com/",
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                await asyncio.sleep(0.8)
            finally:
                await warmup_page.close()
            warm_bytes = sum(r.encoded_bytes for r in warm_records)
            print(f"  warmup: {warm_bytes/1024:.1f} KB / {len(warm_records)} reqs / "
                  f"{int((time.perf_counter()-t_warm)*1000)}ms\n")

            for i, q in enumerate(keywords, 1):
                if pace_seconds > 0 and i > 1:
                    print(f"  ...pacing {pace_seconds:.0f}s")
                    await asyncio.sleep(pace_seconds)
                t0 = time.perf_counter()
                page = await context.new_page()
                phase = {"current": "search"}
                records: list[ReqRecord] = []
                _attach_page_listeners(page, phase, records)

                try:
                    search_url = f"https://www.google.com/search?q={q.replace(' ', '+')}"
                    resp = await page.goto(search_url, wait_until="domcontentloaded",
                                           timeout=45_000)
                    try:
                        await page.wait_for_selector("h3", timeout=5_000)
                    except Exception:
                        pass
                    html = await page.content()
                    final_url = page.url
                    await asyncio.sleep(0.8)

                    total_b = sum(r.encoded_bytes for r in records)

                    r = QueryResult(
                        idx=i, query=q, final_url=final_url,
                        status=resp.status if resp else 0,
                        h3_count=len(re.findall(r"<h3", html, re.I)),
                        enablejs="/httpservice/retry/enablejs" in html,
                        sorry="/sorry/" in (final_url or "").lower(),
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        total_bytes=total_b,
                        request_count=len(records),
                        requests=records,
                    )
                    note = " SORRY" if r.sorry else (" enablejs" if r.enablejs else "")
                    print(f"  [q{i}] {q!r:<40} h3={r.h3_count} reqs={r.request_count} "
                          f"bytes={total_b/1024:.1f} KB "
                          f"lat={r.latency_ms}ms{note}")
                    results.append(r)
                except Exception as e:
                    results.append(QueryResult(
                        idx=i, query=q, final_url="", status=0, h3_count=0,
                        enablejs=False, sorry=False,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        total_bytes=0, request_count=0,
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
    print(f"{'q':>2}  {'query':<40} {'h3':>3} {'reqs':>5} {'bytes_KB':>10} {'lat_s':>6}")
    for r in ok:
        note = " SORRY" if r.sorry else (" ENABLEJS" if r.enablejs else "")
        print(f"{r.idx:>2}  {r.query:<40} {r.h3_count:>3} {r.request_count:>5} "
              f"{r.total_bytes/1024:>10.1f} "
              f"{r.latency_ms/1000:>6.1f}{note}")

    if len(ok) >= 2:
        cold = ok[0].total_bytes
        warm = [r.total_bytes for r in ok[1:]]
        warm_avg = sum(warm) / len(warm)
        print(f"\nCold q1: {cold/1024:.1f} KB")
        print(f"Warm q2-q{len(ok)} avg: {warm_avg/1024:.1f} KB  "
              f"(min={min(warm)/1024:.1f}, max={max(warm)/1024:.1f})")
        if cold > 0:
            print(f"Warm-cache savings: {(cold - warm_avg)/1024:.1f} KB/query "
                  f"({100*(1 - warm_avg/cold):.0f}% reduction)")

    warm_results = ok[1:] if len(ok) >= 2 else []
    if warm_results:
        print("\n=== By resource_type (warm queries only — q2 onwards) ===")
        by_rt = defaultdict(lambda: [0, 0])  # n, bytes
        for r in warm_results:
            for req in r.requests:
                by_rt[req.resource_type][0] += 1
                by_rt[req.resource_type][1] += req.encoded_bytes
        total = sum(v[1] for v in by_rt.values()) or 1
        print(f"{'resource_type':<18} {'n':>6} {'total_KB':>10} {'avg_KB':>8} {'%':>6}")
        for rt, (cnt, byt) in sorted(by_rt.items(), key=lambda kv: -kv[1][1]):
            avg = byt / cnt if cnt else 0
            pct = 100 * byt / total
            print(f"{rt:<18} {cnt:>6} {byt/1024:>10.1f} {avg/1024:>8.1f} {pct:>5.1f}%")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(KEYWORDS))
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--port", type=int, default=10001,
                    help="Decodo endpoint port. 10000=rotating, 10001-10010=sticky.")
    ap.add_argument("--pace", type=float, default=0.0,
                    help="Seconds between queries. 35 = human search cadence.")
    ap.add_argument("--verify-sticky", action="store_true",
                    help="Log exit IP at session start and end.")
    args = ap.parse_args()

    keywords = KEYWORDS[: args.n]
    proxy = None if args.no_proxy else load_decodo(args.port)
    if not args.no_proxy and not proxy:
        print("(!) No Decodo row in DB; use --no-proxy to run on home IP.", file=sys.stderr)
        return 2

    label = "home IP" if args.no_proxy else f"Decodo :{args.port} (user={proxy['username']})"
    pace_note = f", pace={args.pace}s" if args.pace > 0 else ", back-to-back"
    print(f"Firefox persistent context — {label} — {len(keywords)} queries{pace_note}\n")

    results = await run_sequential(proxy, keywords,
                                   pace_seconds=args.pace,
                                   verify_sticky=args.verify_sticky)

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "homeip" if args.no_proxy else f"decodo{args.port}"
    log_path = log_dir / f"persistent_context_firefox_{tag}_{ts}.json"
    log_path.write_text(json.dumps([asdict(r) for r in results], indent=2))

    print_summary(results)
    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
