"""Session 3.2c — sticky cold vs warm start. Bandwidth question:

Does one browser + one context across N=10 queries on a Decodo sticky IP
(:10001) land avg bytes/query meaningfully below the ~500 KB cold-rotating
floor from 3.2b, without pass rate collapsing from parking on one IP for
the whole run?

Three variants, interleaved A B C A B C A B C A on :10001:

  A = cold sticky:   fresh browser + fresh context + homepage warmup per
                     query. No retry. Sticky-equivalent of 3.2b's baseline.
  B = warm sticky:   one browser + one context for the whole run. Homepage
                     warmup happens only on query 1 (bytes attributed to
                     query 1's total_bytes to match A's accounting). No retry.
  C = warm sticky +  one browser + one context; warmup once. On block: close
      retry:         context + reopen fresh context on same browser (sticky
                     port holds the IP inside the sessionduration=1 window),
                     re-warmup, retry once.

The fetch primitive (route abort, wait_until="commit", URL bail, post-DCL
re-check, h3 timeout 3s) is carried verbatim from 3.2b's run-3 shipped path.
Only the browser lifecycle around it changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

DB_PATH = Path(__file__).parent.parent.parent / "open_serp_checker.db"

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

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]

STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def load_decodo(port: int, sticky_duration_min: int = 1) -> Optional[dict]:
    """Return Playwright proxy dict. For sticky ports (>=10001), embed
    Decodo's `user-<u>-sessionduration-<n>` fragment so the IP actually holds."""
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    r = db.execute("SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1").fetchone()
    db.close()
    if not r:
        return None
    user = r["username"]
    if port >= 10001:
        user = f"user-{user}-sessionduration-{sticky_duration_min}"
    return {"server": f"http://{r['host']}:{port}",
            "username": user, "password": r["password"]}


@dataclass
class Attempt:
    n: int                   # attempt index within a query (1 or 2)
    exit_ip: Optional[str]
    final_url: str
    status: int
    h3_count: int
    sorry: bool
    enablejs: bool
    latency_ms: int
    total_bytes: int
    blocked: bool
    browser_reused: bool = False
    context_reused: bool = False
    did_warmup: bool = False
    error: Optional[str] = None


@dataclass
class Result:
    idx: int
    variant: str             # "A", "B", or "C"
    query: str
    attempts: list = field(default_factory=list)
    final_ok: bool = False
    retried: bool = False    # C only: did we cycle context and retry
    ip_changed: Optional[bool] = None  # C only, if retried: did sticky IP hold


async def _get_ip(context) -> Optional[str]:
    page = await context.new_page()
    try:
        r = await page.goto("https://api.ipify.org?format=json",
                            wait_until="domcontentloaded", timeout=10_000)
        if r and r.status == 200:
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', await page.content())
            return m.group(1) if m else None
    except Exception:
        return None
    finally:
        await page.close()


async def _route(r):
    """Verbatim from Session 3.2b's shipped path (run-3):
      - abort image/media/font
      - blanket-abort recaptcha (saves ~500 KB per SORRY hit)
      - abort /sorry/ subresources but allow /sorry/ top-level nav (needed
        so page.url reflects the block; aborting the nav backfired in run 4).
    """
    if r.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await r.abort()
        return
    url = r.request.url.lower()
    if "recaptcha" in url:
        await r.abort()
        return
    if "/sorry/" in url:
        is_nav = (r.request.is_navigation_request()
                  if callable(r.request.is_navigation_request)
                  else r.request.is_navigation_request)
        if not is_nav:
            await r.abort()
            return
    await r.continue_()


async def _open_ctx(pw, proxy: dict):
    """Launch a fresh browser+context wired with proxy, stealth, and the
    3.2b-run-3 route rules. Returns (browser, context)."""
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    context = await browser.new_context(
        proxy=proxy, locale="en-US",
        viewport={"width": 1280, "height": 800},
        user_agent=USER_AGENT,
    )
    await context.add_init_script(STEALTH_INIT_JS)
    await context.route("**/*", _route)
    return browser, context


async def _open_ctx_on(browser, proxy: dict):
    """Same as _open_ctx but on an already-open browser. Used by variant C's
    context-cycle-on-block path (keep browser alive, swap context)."""
    context = await browser.new_context(
        proxy=proxy, locale="en-US",
        viewport={"width": 1280, "height": 800},
        user_agent=USER_AGENT,
    )
    await context.add_init_script(STEALTH_INIT_JS)
    await context.route("**/*", _route)
    return context


async def _do_search(context, query: str, n: int, do_warmup: bool,
                     browser_reused: bool, context_reused: bool) -> Attempt:
    """Search one query on the given context. Fetch path is verbatim from
    3.2b run-3: warmup (optional) + /search with wait_until='commit', URL bail
    on /sorry/, post-DCL re-check, h3 timeout 3s. Bytes are page-level
    content-length sum — includes warmup when do_warmup=True, matching the
    3.2b baseline's accounting so cross-run comparison stays clean."""
    t0 = time.perf_counter()
    total_bytes = 0

    def on_response(resp):
        nonlocal total_bytes
        try:
            cl = resp.headers.get("content-length")
            if cl:
                total_bytes += int(cl)
        except Exception:
            pass

    exit_ip = await _get_ip(context)

    page = await context.new_page()
    page.on("response", on_response)
    try:
        if do_warmup:
            await page.goto("https://www.google.com/",
                            wait_until="domcontentloaded", timeout=30_000)
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        resp = await page.goto(search_url, wait_until="commit", timeout=30_000)
        final_url = page.url or ""
        status = resp.status if resp else 0

        if "/sorry/" in final_url.lower():
            return Attempt(
                n=n, exit_ip=exit_ip, final_url=final_url, status=status,
                h3_count=0, sorry=True, enablejs=False,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                total_bytes=total_bytes, blocked=True,
                browser_reused=browser_reused, context_reused=context_reused,
                did_warmup=do_warmup,
            )

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        final_url = page.url or final_url
        if "/sorry/" in final_url.lower():
            return Attempt(
                n=n, exit_ip=exit_ip, final_url=final_url, status=status,
                h3_count=0, sorry=True, enablejs=False,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                total_bytes=total_bytes, blocked=True,
                browser_reused=browser_reused, context_reused=context_reused,
                did_warmup=do_warmup,
            )
        try:
            await page.wait_for_selector("h3", timeout=3_000)
        except Exception:
            pass
        await asyncio.sleep(0.5)

        html = await page.content()
        final_url = page.url
        h3 = len(re.findall(r"<h3", html, re.I))
        sorry = "/sorry/" in (final_url or "").lower()
        blocked = sorry or (status == 200 and h3 == 0 and "<body" in html.lower())
        return Attempt(
            n=n, exit_ip=exit_ip, final_url=final_url, status=status,
            h3_count=h3, sorry=sorry,
            enablejs="/httpservice/retry/enablejs" in html,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            total_bytes=total_bytes, blocked=blocked,
            browser_reused=browser_reused, context_reused=context_reused,
            did_warmup=do_warmup,
        )
    finally:
        await page.close()


async def run_A(pw, proxy: dict, query: str, idx: int) -> Result:
    """Cold sticky: fresh browser+context+warmup per query. No retry."""
    r = Result(idx=idx, variant="A", query=query)
    browser, context = await _open_ctx(pw, proxy)
    try:
        att = await _do_search(context, query, n=1, do_warmup=True,
                               browser_reused=False, context_reused=False)
        r.attempts.append(att)
    finally:
        await context.close()
        await browser.close()
    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


async def run_B(context, query: str, idx: int, first: bool) -> Result:
    """Warm sticky: reuse the passed-in context. Warmup only on first query."""
    r = Result(idx=idx, variant="B", query=query)
    att = await _do_search(context, query, n=1, do_warmup=first,
                           browser_reused=True, context_reused=not first)
    r.attempts.append(att)
    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


async def run_C(browser, context_holder: dict, proxy: dict, query: str,
                idx: int, first: bool) -> Result:
    """Warm sticky + retry: reuse browser+context. Warmup on first query.
    On block, close context + reopen (same browser, same sticky proxy URL —
    IP should hold inside sessionduration window), re-warmup, retry once.
    `context_holder` is a mutable dict {"ctx": <context>} so a swap is
    visible to the caller for the next query.
    """
    r = Result(idx=idx, variant="C", query=query)
    context = context_holder["ctx"]
    a1 = await _do_search(context, query, n=1, do_warmup=first,
                          browser_reused=True, context_reused=not first)
    r.attempts.append(a1)

    if a1.blocked:
        r.retried = True
        # Cycle context: close, reopen fresh on same browser+proxy.
        await context.close()
        new_context = await _open_ctx_on(browser, proxy)
        context_holder["ctx"] = new_context
        # Brief gap — matches 3.2b retry timing spirit.
        await asyncio.sleep(random.uniform(2.0, 4.0))
        a2 = await _do_search(new_context, query, n=2, do_warmup=True,
                              browser_reused=True, context_reused=False)
        r.attempts.append(a2)
        if a1.exit_ip and a2.exit_ip:
            r.ip_changed = (a1.exit_ip != a2.exit_ip)

    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


def _tag(a: Attempt) -> str:
    if a.sorry:
        return "SORRY"
    if a.enablejs:
        return "ENABLEJS"
    if a.h3_count > 0:
        return "OK"
    return "NO_H3"


def _print_attempt(prefix: str, a: Attempt) -> str:
    return (f"{prefix} ip={a.exit_ip or '?':<15} h3={a.h3_count:>2} "
            f"kb={a.total_bytes/1024:>5.0f} lat={a.latency_ms:>5}ms → {_tag(a)}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10001,
                    help="Decodo port. 10001+=sticky (this session assumes sticky).")
    ap.add_argument("--n", type=int, default=len(KEYWORDS))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pace-min", type=float, default=15.0)
    ap.add_argument("--pace-max", type=float, default=45.0)
    args = ap.parse_args()

    random.seed(args.seed)

    proxy = load_decodo(args.port)
    if not proxy:
        print("(!) No Decodo row in DB.", file=sys.stderr)
        return 2

    mode = "rotating" if args.port == 10000 else "sticky"
    keywords = KEYWORDS[: args.n]
    # Interleave A B C A B C A B C A — 10 items: 4 A, 3 B, 3 C.
    variants_cycle = ["A", "B", "C"]
    schedule = [(variants_cycle[i % 3], kw) for i, kw in enumerate(keywords)]

    print(f"Sticky cold-vs-warm — Decodo :{args.port} ({mode}) — "
          f"{len(schedule)} queries interleaved (A B C ...)\n")

    results: list[Result] = []

    # Warm-B state: opened lazily on first B query.
    b_state = {"browser": None, "context": None, "first_done": False}
    # Warm-C state: opened lazily on first C query. Context is a holder so
    # run_C can swap it after a retry-driven cycle.
    c_state = {"browser": None, "context_holder": {"ctx": None},
               "first_done": False}

    async with async_playwright() as pw:
        try:
            for i, (variant, kw) in enumerate(schedule, 1):
                if i > 1:
                    gap = random.uniform(args.pace_min, args.pace_max)
                    print(f"  ...pacing {gap:.1f}s")
                    await asyncio.sleep(gap)

                try:
                    if variant == "A":
                        r = await run_A(pw, proxy, kw, i)
                    elif variant == "B":
                        if b_state["browser"] is None:
                            b_state["browser"], b_state["context"] = \
                                await _open_ctx(pw, proxy)
                        first = not b_state["first_done"]
                        r = await run_B(b_state["context"], kw, i, first=first)
                        b_state["first_done"] = True
                    else:  # C
                        if c_state["browser"] is None:
                            br, ctx = await _open_ctx(pw, proxy)
                            c_state["browser"] = br
                            c_state["context_holder"]["ctx"] = ctx
                        first = not c_state["first_done"]
                        r = await run_C(c_state["browser"],
                                        c_state["context_holder"],
                                        proxy, kw, i, first=first)
                        c_state["first_done"] = True

                    a1 = r.attempts[0]
                    line = _print_attempt(
                        f"  [{variant} q{i}] {kw!r:<28} a1", a1)
                    if r.retried:
                        a2 = r.attempts[1]
                        ip_note = ("ip↔same" if r.ip_changed is False
                                   else "ip→new" if r.ip_changed is True
                                   else "ip=?")
                        line += "\n              " + _print_attempt(
                            "retry:", a2) + f" ({ip_note})"
                    total_kb = sum(a.total_bytes for a in r.attempts) / 1024
                    line += (f"    final={'PASS' if r.final_ok else 'FAIL'}"
                             f" total={total_kb:.0f}KB")
                    print(line)
                    results.append(r)
                except Exception as e:
                    print(f"  [{variant} q{i}] ERR: {e}")
                    results.append(Result(
                        idx=i, variant=variant, query=kw,
                        attempts=[Attempt(
                            n=1, exit_ip=None, final_url="", status=0,
                            h3_count=0, sorry=False, enablejs=False,
                            latency_ms=0, total_bytes=0, blocked=True,
                            error=f"{type(e).__name__}: {e}"[:200],
                        )],
                    ))
        finally:
            # Tear down warm-variant browsers.
            for st in (b_state, c_state):
                br = st.get("browser")
                if br is not None:
                    try:
                        await br.close()
                    except Exception:
                        pass

    # --- Summary ---
    def _avg_kb(rs):
        if not rs:
            return 0.0
        total = sum(a.total_bytes for r in rs for a in r.attempts)
        return total / len(rs) / 1024

    a_rs = [r for r in results if r.variant == "A"]
    b_rs = [r for r in results if r.variant == "B"]
    c_rs = [r for r in results if r.variant == "C"]

    a_pass = sum(1 for r in a_rs if r.final_ok)
    b_pass = sum(1 for r in b_rs if r.final_ok)
    c_pass = sum(1 for r in c_rs if r.final_ok)

    a_kb = _avg_kb(a_rs)
    b_kb = _avg_kb(b_rs)
    c_kb = _avg_kb(c_rs)

    # For B: first-query bytes vs avg of subsequent (the amortization signal).
    b_first_kb = (sum(a.total_bytes for a in b_rs[0].attempts) / 1024
                  if b_rs else 0.0)
    b_rest_kb = _avg_kb(b_rs[1:]) if len(b_rs) > 1 else 0.0

    # For C: retry usage + conversion + IP stickiness.
    c_retried = [r for r in c_rs if r.retried]
    c_retry_conv = sum(1 for r in c_retried
                       if r.final_ok and r.attempts[0].blocked
                       and not r.attempts[1].blocked)
    c_ip_same = sum(1 for r in c_retried if r.ip_changed is False)
    c_ip_new = sum(1 for r in c_retried if r.ip_changed is True)

    # IP stickiness diagnostic for B and C: how many distinct exit IPs?
    def _unique_ips(rs):
        ips = {a.exit_ip for r in rs for a in r.attempts if a.exit_ip}
        return sorted(ips)
    b_ips = _unique_ips(b_rs)
    c_ips = _unique_ips(c_rs)

    print(f"\n=== Sticky cold-vs-warm summary — Decodo :{args.port} ({mode}) ===")
    print(f"  A (cold sticky):        {a_pass}/{len(a_rs)} pass   avg={a_kb:.0f} KB/query")
    print(f"  B (warm sticky):        {b_pass}/{len(b_rs)} pass   avg={b_kb:.0f} KB/query")
    if b_rs:
        print(f"      first-query:        {b_first_kb:.0f} KB  "
              f"subsequent avg: {b_rest_kb:.0f} KB")
        print(f"      unique exit IPs:    {len(b_ips)}  {b_ips}")
    print(f"  C (warm + retry):       {c_pass}/{len(c_rs)} pass   avg={c_kb:.0f} KB/query")
    if c_retried:
        print(f"      retry fired:        {len(c_retried)}/{len(c_rs)}, "
              f"converted {c_retry_conv}/{len(c_retried)}")
        print(f"      sticky held on retry: same={c_ip_same}  new={c_ip_new}  "
              f"unknown={len(c_retried) - c_ip_same - c_ip_new}")
    if c_rs:
        print(f"      unique exit IPs:    {len(c_ips)}  {c_ips}")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = log_dir / f"sticky_cold_vs_warm_decodo{args.port}_{ts}.json"
    p.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nLog: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
