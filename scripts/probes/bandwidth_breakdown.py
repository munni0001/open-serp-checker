"""Phase 1 baseline — per-request bandwidth breakdown of the Google SERP fetch.

Instruments the production fetch path (stealth Chromium + Decodo rotating proxy
+ homepage warmup + search) with a CDP `Network.*` listener and records
`encodedDataLength` (post-compression wire bytes — the same number Decodo bills)
for every request. NO resource blocking. 10 fixed Techjury keywords, one fresh
browser per query.

Emits:
- scripts/probes/logs/bandwidth_breakdown_<ts>.json (per-request records)
- Console summary tables by resource_type and by host

Usage:
    source venv/bin/activate
    python scripts/probes/bandwidth_breakdown.py
    python scripts/probes/bandwidth_breakdown.py --n 3          # subset
    python scripts/probes/bandwidth_breakdown.py --no-proxy     # home IP
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

# Locked keyword set — same 10 across every phase for apples-to-apples comparison.
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

DB_PATH = Path(__file__).parent.parent.parent / "serp_scraper.db"


def load_decodo_rotating() -> Optional[dict]:
    """Decodo proxy on port 10001 (rotating). DB stores 10000 (sticky) — override."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1"
    ).fetchone()
    db.close()
    if not row:
        return None
    return {
        "server": f"http://{row['host']}:10001",
        "username": row["username"],
        "password": row["password"],
    }


def _short_host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except ValueError:
        return "other"
    # Collapse to eTLD+1-ish for the summary.
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
    request_id: str
    phase: str            # "warmup" | "search"
    url: str
    host: str
    resource_type: str    # from Network.requestWillBeSent
    status: Optional[int]
    mime_type: Optional[str]
    encoded_bytes: int    # wire bytes (compressed if server compressed)
    finished: bool
    failed: bool
    fail_reason: Optional[str] = None


@dataclass
class QueryResult:
    query: str
    final_url: str
    status: int
    h3_count: int
    enablejs: bool
    sorry: bool
    latency_ms: int
    total_warmup_bytes: int
    total_search_bytes: int
    total_bytes: int
    request_count: int
    error: Optional[str] = None
    requests: list[ReqRecord] = field(default_factory=list)


async def _instrument(cdp, phase_ref: dict, records: dict[str, ReqRecord]) -> None:
    """Wire CDP listeners. `phase_ref['current']` tags the phase of new requests."""

    def on_request(evt):
        rid = evt["requestId"]
        req = evt.get("request", {})
        url = req.get("url", "")
        records[rid] = ReqRecord(
            request_id=rid,
            phase=phase_ref["current"],
            url=url,
            host=_short_host(url),
            resource_type=evt.get("type", "Other"),
            status=None,
            mime_type=None,
            encoded_bytes=0,
            finished=False,
            failed=False,
        )

    def on_response(evt):
        rid = evt["requestId"]
        r = records.get(rid)
        if not r:
            return
        resp = evt.get("response", {})
        r.status = resp.get("status")
        r.mime_type = resp.get("mimeType")

    def on_finished(evt):
        rid = evt["requestId"]
        r = records.get(rid)
        if not r:
            return
        r.encoded_bytes = int(evt.get("encodedDataLength", 0) or 0)
        r.finished = True

    def on_failed(evt):
        rid = evt["requestId"]
        r = records.get(rid)
        if not r:
            return
        r.encoded_bytes = int(evt.get("encodedDataLength", 0) or 0)
        r.failed = True
        r.fail_reason = evt.get("errorText")

    cdp.on("Network.requestWillBeSent", on_request)
    cdp.on("Network.responseReceived", on_response)
    cdp.on("Network.loadingFinished", on_finished)
    cdp.on("Network.loadingFailed", on_failed)


async def run_query(pw, proxy: Optional[dict], query: str) -> QueryResult:
    t0 = time.perf_counter()
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        context = await browser.new_context(
            proxy=proxy,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        await context.add_init_script(STEALTH_INIT_JS)
        page = await context.new_page()

        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        phase_ref = {"current": "warmup"}
        records: dict[str, ReqRecord] = {}
        await _instrument(cdp, phase_ref, records)

        # Warmup — same as production fetch.
        await page.goto(
            "https://www.google.com/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )

        phase_ref["current"] = "search"
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        resp = await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_selector("h3", timeout=5_000)
        except Exception:
            pass

        html = await page.content()
        final_url = page.url

        # Small tail: give in-flight requests a beat to finish so encodedDataLength
        # is captured. Without this some Network.loadingFinished events drop.
        await asyncio.sleep(0.5)

        req_list = list(records.values())
        warmup_bytes = sum(r.encoded_bytes for r in req_list if r.phase == "warmup")
        search_bytes = sum(r.encoded_bytes for r in req_list if r.phase == "search")

        return QueryResult(
            query=query,
            final_url=final_url,
            status=resp.status if resp else 0,
            h3_count=len(re.findall(r"<h3", html, re.I)),
            enablejs="/httpservice/retry/enablejs" in html,
            sorry="/sorry/" in (final_url or "").lower(),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            total_warmup_bytes=warmup_bytes,
            total_search_bytes=search_bytes,
            total_bytes=warmup_bytes + search_bytes,
            request_count=len(req_list),
            requests=req_list,
        )
    except Exception as e:
        return QueryResult(
            query=query, final_url="", status=0, h3_count=0, enablejs=False,
            sorry=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            total_warmup_bytes=0, total_search_bytes=0, total_bytes=0,
            request_count=0,
            error=f"{type(e).__name__}: {e}"[:200],
        )
    finally:
        await browser.close()


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f}"


def print_summary(results: list[QueryResult]) -> None:
    ok = [r for r in results if r.error is None]
    if not ok:
        print("\n(!) All queries errored — no summary.")
        return

    print("\n=== Per-query totals ===")
    print(f"{'query':<38} {'h3':>3} {'reqs':>5} {'warmup':>8} {'search':>8} {'total_KB':>10} {'lat_s':>6}")
    for r in ok:
        note = " SORRY" if r.sorry else (" ENABLEJS" if r.enablejs else "")
        print(f"{r.query[:38]:<38} {r.h3_count:>3} {r.request_count:>5} "
              f"{_fmt_kb(r.total_warmup_bytes):>8} {_fmt_kb(r.total_search_bytes):>8} "
              f"{_fmt_kb(r.total_bytes):>10} {r.latency_ms/1000:>6.1f}{note}")

    n = len(ok)
    total = sum(r.total_bytes for r in ok)
    warm = sum(r.total_warmup_bytes for r in ok)
    search = sum(r.total_search_bytes for r in ok)
    reqs = sum(r.request_count for r in ok)
    print(f"\nOverall: n={n} avg_total={_fmt_kb(total // n)} KB "
          f"(warmup={_fmt_kb(warm // n)} KB, search={_fmt_kb(search // n)} KB) "
          f"avg_reqs={reqs // n} h3_pass_rate={sum(1 for r in ok if r.h3_count >= 5)}/{n}")

    # Aggregate by resource_type — production baseline is what the article prints.
    print("\n=== By resource_type (search phase only, aggregated across all queries) ===")
    by_rt = defaultdict(lambda: [0, 0])  # rt -> [n, bytes]
    for r in ok:
        for req in r.requests:
            if req.phase != "search":
                continue
            by_rt[req.resource_type][0] += 1
            by_rt[req.resource_type][1] += req.encoded_bytes
    total_search = sum(v[1] for v in by_rt.values()) or 1
    print(f"{'resource_type':<18} {'n':>6} {'total_KB':>10} {'avg_KB':>8} {'% of search':>12}")
    for rt, (cnt, byt) in sorted(by_rt.items(), key=lambda kv: -kv[1][1]):
        avg = (byt / cnt) if cnt else 0
        pct = 100 * byt / total_search
        print(f"{rt:<18} {cnt:>6} {_fmt_kb(byt):>10} {avg/1024:>8.1f} {pct:>11.1f}%")

    # Aggregate by host — cross-reference with Decodo's "top targets" screenshot.
    print("\n=== By host (search phase only) ===")
    by_host = defaultdict(lambda: [0, 0])
    for r in ok:
        for req in r.requests:
            if req.phase != "search":
                continue
            by_host[req.host][0] += 1
            by_host[req.host][1] += req.encoded_bytes
    print(f"{'host':<32} {'n':>6} {'total_KB':>10} {'avg_KB':>8} {'% of search':>12}")
    for host, (cnt, byt) in sorted(by_host.items(), key=lambda kv: -kv[1][1]):
        avg = (byt / cnt) if cnt else 0
        pct = 100 * byt / total_search
        print(f"{host:<32} {cnt:>6} {_fmt_kb(byt):>10} {avg/1024:>8.1f} {pct:>11.1f}%")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(KEYWORDS),
                    help="Number of keywords to run (default: all 10).")
    ap.add_argument("--no-proxy", action="store_true",
                    help="Run without Decodo — home IP.")
    args = ap.parse_args()

    keywords = KEYWORDS[: args.n]
    proxy = None if args.no_proxy else load_decodo_rotating()
    if not args.no_proxy and not proxy:
        print("(!) No Decodo row in DB — aborting. Use --no-proxy to run on home IP.",
              file=sys.stderr)
        return 2

    label = "home IP" if args.no_proxy else f"Decodo :10001 (user={proxy['username']})"
    print(f"Baseline (no blocking) — {label} — {len(keywords)} queries\n")

    results: list[QueryResult] = []
    async with async_playwright() as pw:
        for i, q in enumerate(keywords, 1):
            print(f"[{i}/{len(keywords)}] {q!r} ...", flush=True)
            r = await run_query(pw, proxy, q)
            if r.error:
                print(f"    ERR {r.error}")
            else:
                note = " SORRY" if r.sorry else (" enablejs" if r.enablejs else "")
                print(f"    h3={r.h3_count} reqs={r.request_count} "
                      f"total={_fmt_kb(r.total_bytes)} KB "
                      f"(warm={_fmt_kb(r.total_warmup_bytes)}, "
                      f"search={_fmt_kb(r.total_search_bytes)}) "
                      f"lat={r.latency_ms}ms{note}")
            results.append(r)

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "homeip" if args.no_proxy else "decodo"
    log_path = log_dir / f"bandwidth_breakdown_{tag}_{ts}.json"
    log_path.write_text(json.dumps([asdict(r) for r in results], indent=2))

    print_summary(results)
    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
