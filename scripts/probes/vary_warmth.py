"""Session 3.2d — vary the warmth: which piece of a 'warm' browser context
is doing the work?

Four variants, interleaved W0 W1 W2 W3 across 10 keywords × 4 = 40 queries
on Decodo :10001 with sessionduration=10 (so the IP holds the full run):

  W0 (fully cold):    fresh browser + fresh context + warmup per query.
  W1 (cache-only):    one browser + one context for the run; warmup once at
                      run start; context.clear_cookies() BEFORE each search.
                      HTTP cache + TLS ticket persist; cookies wiped.
  W2 (cookies-only):  one browser + a long-lived DONOR context that never
                      runs searches. Warmup once on donor; snapshot cookies.
                      Per query: fresh context on same browser + preload
                      donor cookies + search + close context. Refresh donor
                      cookies every 5 W2 queries (revisit google.com).
  W3 (fully warm):    one browser + one context for the run; warmup once;
                      no clearing between queries. (= 3.2c's B.)

Fixes carried from 3.2c's post-mortem:
  1. sticky_duration_min=10 so the IP holds the whole ~25-30 min run.
  2. Homepage warmup retries once on timeout (5s backoff).
  3. Warmup-fail = tear down + rebuild on next query (don't fake warm).
     Track warmup_success per attempt.

Fetch primitive (route abort, wait_until="commit", URL bail, post-DCL
re-check, h3 timeout 3s) is carried verbatim from 3.2b's run-3 shipped path.
Only the warmth-state setup around it changes.

Interpretation matrix (see session doc):
  W1 ≈ W0  and  W2 ≈ W3  → cookies win (ship cookie-preload)
  W1 ≈ W3  and  W2 ≈ W0  → cache wins  (need full context reuse)
  W1 ≈ W3  and  W2 ≈ W3  → both work independently (ship cookies — cheaper)
  W1 ≈ W0  and  W2 ≈ W0  → neither alone; both required OR 3.2c was noise
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

W2_REFRESH_EVERY = 5  # re-warm donor cookies every N W2 queries


def load_decodo(port: int, sticky_duration_min: int = 10) -> Optional[dict]:
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
    n: int
    exit_ip: Optional[str]
    final_url: str
    status: int
    h3_count: int
    sorry: bool
    enablejs: bool
    latency_ms: int
    total_bytes: int              # SERP page bytes only
    blocked: bool
    browser_reused: bool = False
    context_reused: bool = False
    cookies_state: str = ""       # "empty" | "wiped" | "preloaded" | "preserved"
    cache_state: str = ""         # "empty" | "warm"
    warmup_this_call: bool = False
    warmup_success: bool = True   # True if no warmup needed OR warmup succeeded
    warmup_bytes: int = 0         # bytes spent on the warmup that ran for THIS query
    cookies_before_clear: Optional[int] = None  # W1 only
    cookies_after_clear: Optional[int] = None   # W1 only
    donor_cookies_loaded: Optional[int] = None  # W2 only
    error: Optional[str] = None


@dataclass
class Result:
    idx: int
    variant: str
    query: str
    attempts: list = field(default_factory=list)
    final_ok: bool = False


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
    """Verbatim from 3.2b run-3."""
    if r.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await r.abort(); return
    url = r.request.url.lower()
    if "recaptcha" in url:
        await r.abort(); return
    if "/sorry/" in url:
        is_nav = (r.request.is_navigation_request()
                  if callable(r.request.is_navigation_request)
                  else r.request.is_navigation_request)
        if not is_nav:
            await r.abort(); return
    await r.continue_()


async def _open_ctx(pw, proxy: dict):
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
    context = await browser.new_context(
        proxy=proxy, locale="en-US",
        viewport={"width": 1280, "height": 800},
        user_agent=USER_AGENT,
    )
    await context.add_init_script(STEALTH_INIT_JS)
    await context.route("**/*", _route)
    return context


async def _do_warmup(context, retries: int = 2) -> tuple[bool, int]:
    """Visit https://www.google.com/ on a dedicated page. Retry once on timeout.
    Returns (success, total_bytes_across_all_attempts)."""
    warmup_bytes = 0
    def on_response(resp):
        nonlocal warmup_bytes
        try:
            cl = resp.headers.get("content-length")
            if cl:
                warmup_bytes += int(cl)
        except Exception:
            pass
    for attempt in range(retries):
        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto("https://www.google.com/",
                            wait_until="domcontentloaded", timeout=30_000)
            await page.close()
            return True, warmup_bytes
        except Exception:
            try: await page.close()
            except Exception: pass
            if attempt < retries - 1:
                await asyncio.sleep(5.0)
    return False, warmup_bytes


async def _do_search_only(context, query: str, n: int, *,
                          browser_reused: bool, context_reused: bool,
                          cookies_state: str, cache_state: str) -> Attempt:
    """SERP fetch only — no warmup. Verbatim 3.2b run-3 primitive.
    total_bytes is SERP page bytes only (warmup bytes are separate)."""
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
                cookies_state=cookies_state, cache_state=cache_state,
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
                cookies_state=cookies_state, cache_state=cache_state,
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
            cookies_state=cookies_state, cache_state=cache_state,
        )
    finally:
        await page.close()


def _warmup_failed_attempt(*, browser_reused: bool, context_reused: bool,
                            cookies_state: str, cache_state: str,
                            warmup_bytes: int) -> Attempt:
    return Attempt(
        n=1, exit_ip=None, final_url="", status=0, h3_count=0,
        sorry=False, enablejs=False, latency_ms=0, total_bytes=0, blocked=True,
        browser_reused=browser_reused, context_reused=context_reused,
        cookies_state=cookies_state, cache_state=cache_state,
        warmup_this_call=True, warmup_success=False,
        warmup_bytes=warmup_bytes,
        error="warmup failed after retry",
    )


# --- Variant runners --------------------------------------------------------

async def run_W0(pw, proxy: dict, query: str, idx: int) -> Result:
    """Fully cold: fresh browser + context + warmup per query."""
    r = Result(idx=idx, variant="W0", query=query)
    browser, context = await _open_ctx(pw, proxy)
    try:
        ok, wb = await _do_warmup(context)
        if not ok:
            att = _warmup_failed_attempt(
                browser_reused=False, context_reused=False,
                cookies_state="empty", cache_state="empty", warmup_bytes=wb)
            r.attempts.append(att)
        else:
            att = await _do_search_only(
                context, query, n=1,
                browser_reused=False, context_reused=False,
                cookies_state="empty", cache_state="empty")
            att.warmup_this_call = True
            att.warmup_success = True
            att.warmup_bytes = wb
            r.attempts.append(att)
    finally:
        try: await context.close()
        except Exception: pass
        try: await browser.close()
        except Exception: pass
    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


async def _ensure_ctx(pw, proxy: dict, state: dict) -> tuple[bool, bool, int]:
    """For W1/W3: ensure state["context"] is live and warmed.
    Returns (warmup_this_call, warmup_success, warmup_bytes)."""
    if state.get("context") is not None and state.get("warmup_ok"):
        return False, True, 0
    # (Re)build browser+context.
    if state.get("browser") is not None:
        try: await state["browser"].close()
        except Exception: pass
    state["browser"], state["context"] = await _open_ctx(pw, proxy)
    ok, wb = await _do_warmup(state["context"])
    state["warmup_ok"] = ok
    if not ok:
        # Tear down; next call will rebuild.
        try: await state["context"].close()
        except Exception: pass
        try: await state["browser"].close()
        except Exception: pass
        state["browser"] = None
        state["context"] = None
    return True, ok, wb


async def run_W1(pw, proxy: dict, state: dict, query: str, idx: int) -> Result:
    """Cache-only: one browser+context; warmup once; clear_cookies before search."""
    r = Result(idx=idx, variant="W1", query=query)
    warmup_this_call, warmup_success, warmup_bytes = \
        await _ensure_ctx(pw, proxy, state)
    if not warmup_success:
        r.attempts.append(_warmup_failed_attempt(
            browser_reused=(not warmup_this_call), context_reused=(not warmup_this_call),
            cookies_state="empty", cache_state="empty", warmup_bytes=warmup_bytes))
        return r

    context = state["context"]
    cookies_before = await context.cookies()
    await context.clear_cookies()
    cookies_after = await context.cookies()

    att = await _do_search_only(
        context, query, n=1,
        browser_reused=True, context_reused=(not warmup_this_call),
        cookies_state="wiped", cache_state="warm")
    att.warmup_this_call = warmup_this_call
    att.warmup_success = True
    att.warmup_bytes = warmup_bytes
    att.cookies_before_clear = len(cookies_before)
    att.cookies_after_clear = len(cookies_after)
    r.attempts.append(att)
    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


async def run_W3(pw, proxy: dict, state: dict, query: str, idx: int) -> Result:
    """Fully warm: one browser+context; warmup once; nothing cleared."""
    r = Result(idx=idx, variant="W3", query=query)
    warmup_this_call, warmup_success, warmup_bytes = \
        await _ensure_ctx(pw, proxy, state)
    if not warmup_success:
        r.attempts.append(_warmup_failed_attempt(
            browser_reused=(not warmup_this_call), context_reused=(not warmup_this_call),
            cookies_state="empty", cache_state="empty", warmup_bytes=warmup_bytes))
        return r

    context = state["context"]
    cookies_state = "empty" if warmup_this_call else "preserved"
    # After warmup the context has the warmup cookies; those count as "preserved"
    # for post-first queries. On the first query itself, cookies present are
    # from the warmup, so "preserved" is more honest than "empty".
    if warmup_this_call:
        cookies_state = "preserved"  # warmup set google's initial cookies

    att = await _do_search_only(
        context, query, n=1,
        browser_reused=True, context_reused=(not warmup_this_call),
        cookies_state=cookies_state, cache_state="warm")
    att.warmup_this_call = warmup_this_call
    att.warmup_success = True
    att.warmup_bytes = warmup_bytes
    r.attempts.append(att)
    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


async def _ensure_donor(pw, proxy: dict, state: dict) -> tuple[bool, bool, int]:
    """For W2: ensure donor context exists and holds a snapshot of google cookies.
    Returns (warmup_this_call, warmup_success, warmup_bytes)."""
    if state.get("donor_ctx") is not None and state.get("warmup_ok"):
        return False, True, 0
    if state.get("browser") is not None:
        try: await state["browser"].close()
        except Exception: pass
    state["browser"], state["donor_ctx"] = await _open_ctx(pw, proxy)
    ok, wb = await _do_warmup(state["donor_ctx"])
    state["warmup_ok"] = ok
    if not ok:
        try: await state["donor_ctx"].close()
        except Exception: pass
        try: await state["browser"].close()
        except Exception: pass
        state["browser"] = None
        state["donor_ctx"] = None
        state["donor_cookies"] = []
        state["queries_since_refresh"] = 0
        return True, False, wb
    state["donor_cookies"] = await state["donor_ctx"].cookies()
    state["queries_since_refresh"] = 0
    return True, True, wb


async def _refresh_donor(state: dict) -> tuple[bool, int]:
    """Re-warm the existing donor context to refresh cookies.
    Returns (success, warmup_bytes)."""
    ok, wb = await _do_warmup(state["donor_ctx"])
    if ok:
        state["donor_cookies"] = await state["donor_ctx"].cookies()
        state["queries_since_refresh"] = 0
    return ok, wb


async def run_W2(pw, proxy: dict, state: dict, query: str, idx: int) -> Result:
    """Cookies-only: fresh context per query on shared browser, preload cookies
    dumped from a persistent donor context."""
    r = Result(idx=idx, variant="W2", query=query)
    warmup_this_call, warmup_success, warmup_bytes = \
        await _ensure_donor(pw, proxy, state)
    if not warmup_success:
        r.attempts.append(_warmup_failed_attempt(
            browser_reused=(not warmup_this_call), context_reused=False,
            cookies_state="empty", cache_state="empty", warmup_bytes=warmup_bytes))
        return r

    # Optional donor refresh.
    if (not warmup_this_call
            and state.get("queries_since_refresh", 0) >= W2_REFRESH_EVERY):
        ok, extra_wb = await _refresh_donor(state)
        if ok:
            warmup_this_call = True
            warmup_bytes += extra_wb
        # If refresh fails, we keep going with stale cookies — better than
        # tearing down mid-run.

    # Fresh context per query, seeded with donor cookies.
    context = await _open_ctx_on(state["browser"], proxy)
    try:
        cookies_to_load = state.get("donor_cookies") or []
        if cookies_to_load:
            await context.add_cookies(cookies_to_load)

        att = await _do_search_only(
            context, query, n=1,
            browser_reused=True, context_reused=False,
            cookies_state="preloaded", cache_state="empty")
        att.warmup_this_call = warmup_this_call
        att.warmup_success = True
        att.warmup_bytes = warmup_bytes
        att.donor_cookies_loaded = len(cookies_to_load)
        r.attempts.append(att)
    finally:
        try: await context.close()
        except Exception: pass

    state["queries_since_refresh"] = state.get("queries_since_refresh", 0) + 1
    r.final_ok = any(not a.blocked and a.h3_count > 0 for a in r.attempts)
    return r


# --- Main -------------------------------------------------------------------

def _tag(a: Attempt) -> str:
    if a.error:
        return "ERR"
    if a.sorry:
        return "SORRY"
    if a.enablejs:
        return "ENABLEJS"
    if a.h3_count > 0:
        return "OK"
    return "NO_H3"


def _print_attempt(prefix: str, a: Attempt) -> str:
    kb = (a.total_bytes + a.warmup_bytes) / 1024
    wm = f" +wm{a.warmup_bytes/1024:.0f}" if a.warmup_bytes else ""
    return (f"{prefix} ip={a.exit_ip or '?':<15} h3={a.h3_count:>2} "
            f"kb={kb:>5.0f}{wm} lat={a.latency_ms:>5}ms → {_tag(a)}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10001)
    ap.add_argument("--sticky-min", type=int, default=10)
    ap.add_argument("--n-keywords", type=int, default=len(KEYWORDS))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pace-min", type=float, default=15.0)
    ap.add_argument("--pace-max", type=float, default=45.0)
    args = ap.parse_args()

    random.seed(args.seed)

    proxy = load_decodo(args.port, sticky_duration_min=args.sticky_min)
    if not proxy:
        print("(!) No Decodo row in DB.", file=sys.stderr)
        return 2

    mode = "rotating" if args.port == 10000 else "sticky"
    keywords = KEYWORDS[: args.n_keywords]
    variants = ["W0", "W1", "W2", "W3"]
    # For each keyword, run all four variants in order — same difficulty across variants.
    schedule = [(v, kw) for kw in keywords for v in variants]

    print(f"Vary-warmth — Decodo :{args.port} ({mode}, sticky_min={args.sticky_min}) — "
          f"{len(schedule)} queries ({len(keywords)} kw × {len(variants)} variants)\n")

    results: list[Result] = []
    w1_state: dict = {"browser": None, "context": None, "warmup_ok": False}
    w3_state: dict = {"browser": None, "context": None, "warmup_ok": False}
    w2_state: dict = {"browser": None, "donor_ctx": None, "warmup_ok": False,
                      "donor_cookies": [], "queries_since_refresh": 0}

    async with async_playwright() as pw:
        try:
            for i, (variant, kw) in enumerate(schedule, 1):
                if i > 1:
                    gap = random.uniform(args.pace_min, args.pace_max)
                    print(f"  ...pacing {gap:.1f}s")
                    await asyncio.sleep(gap)

                try:
                    if variant == "W0":
                        r = await run_W0(pw, proxy, kw, i)
                    elif variant == "W1":
                        r = await run_W1(pw, proxy, w1_state, kw, i)
                    elif variant == "W2":
                        r = await run_W2(pw, proxy, w2_state, kw, i)
                    else:  # W3
                        r = await run_W3(pw, proxy, w3_state, kw, i)

                    a1 = r.attempts[0]
                    line = _print_attempt(
                        f"  [{variant} q{i:>2}] {kw!r:<28}", a1)
                    line += f"  final={'PASS' if r.final_ok else 'FAIL'}"
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
            for st in (w1_state, w3_state, w2_state):
                br = st.get("browser")
                if br is not None:
                    try: await br.close()
                    except Exception: pass

    # --- Summary ---
    def _rs(v):
        return [r for r in results if r.variant == v]

    def _bytes(a: Attempt) -> int:
        return a.total_bytes + a.warmup_bytes

    def _avg_kb(rs, filt=None):
        atts = [a for r in rs for a in r.attempts if (filt is None or filt(a))]
        if not atts:
            return 0.0
        return sum(_bytes(a) for a in atts) / len(atts) / 1024

    def _unique_ips(rs):
        ips = {a.exit_ip for r in rs for a in r.attempts if a.exit_ip}
        return sorted(ips)

    print(f"\n=== Vary-warmth summary — Decodo :{args.port} ({mode}, sticky_min={args.sticky_min}) ===")
    for v in variants:
        rs = _rs(v)
        if not rs:
            continue
        n = len(rs)
        passes = sum(1 for r in rs if r.final_ok)
        avg_all = _avg_kb(rs)
        avg_pass = _avg_kb(rs, filt=lambda a: not a.blocked and a.h3_count > 0)
        avg_blocked = _avg_kb(rs, filt=lambda a: a.blocked)
        warmup_ok = sum(1 for r in rs for a in r.attempts if a.warmup_this_call and a.warmup_success)
        warmup_fail = sum(1 for r in rs for a in r.attempts if a.warmup_this_call and not a.warmup_success)
        first_kb = (_bytes(rs[0].attempts[0]) / 1024) if rs else 0.0
        rest_kb = _avg_kb(rs[1:]) if len(rs) > 1 else 0.0
        ips = _unique_ips(rs)
        print(f"  {v}: {passes}/{n} pass   "
              f"avg={avg_all:.0f} KB   pass_avg={avg_pass:.0f}   blocked_avg={avg_blocked:.0f}")
        print(f"      first_query={first_kb:.0f} KB   subsequent_avg={rest_kb:.0f} KB")
        print(f"      warmup: {warmup_ok} ok, {warmup_fail} fail   "
              f"unique_ips={len(ips)}")

    # Per-keyword breakdown — any keyword that fails on all 4 variants is a
    # bad-IP / bad-query signal, not a warmth signal.
    print(f"\n  Per-keyword pass rate across variants (0-4):")
    for kw in keywords:
        by_v = {r.variant: r.final_ok for r in results if r.query == kw}
        passes = sum(1 for v in variants if by_v.get(v, False))
        tag = "" if passes > 0 else "  ← failed all 4 (burned/hard)"
        detail = " ".join(f"{v}={'✓' if by_v.get(v) else '✗'}" for v in variants)
        print(f"    {kw:<32} {passes}/4  [{detail}]{tag}")

    all_ips = _unique_ips(results)
    print(f"\n  Total unique exit IPs across run: {len(all_ips)}")
    if len(all_ips) > 2:
        print(f"    (!) >2 IPs — sessionduration={args.sticky_min} did not hold. IPs: {all_ips}")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = log_dir / f"vary_warmth_decodo{args.port}_{ts}.json"
    p.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nLog: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
