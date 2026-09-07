"""Session 3.2b — does retry-on-block lift pass rate, and is it the IP change?

A/B interleaved on the same Decodo pool.
  A = single attempt (current engine baseline, ~44% per Session 3.1)
  B = up to 2 attempts. On block (/sorry/ in final_url OR h3_count==0 on 200),
      close browser + open a fresh one, retry the search once.

Run twice: --port 10000 (rotating: retry gets a new IP) and --port 10001
(sticky: retry gets the same IP). Rotating tells us the lift; the delta
vs. sticky tells us how much of that lift is IP-rotation vs. session state.

Each attempt uses a fresh browser + fresh context — matches the production
engine's fetch() shape (Session 3.1 checkpoint). Only the orchestration differs.

Random 15-45s pacing between keywords, same on A and B, so pacing does not
confound the variant delta.
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

DB_PATH = Path(__file__).parent.parent.parent / "serp_scraper.db"

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
    n: int                   # 1 or 2
    exit_ip: Optional[str]
    final_url: str
    status: int
    h3_count: int
    sorry: bool
    enablejs: bool
    latency_ms: int
    total_bytes: int
    blocked: bool            # was this attempt considered blocked
    error: Optional[str] = None


@dataclass
class Result:
    idx: int
    variant: str             # "A" (no retry) or "B" (retry-on-block)
    query: str
    attempts: list = field(default_factory=list)
    final_ok: bool = False   # any attempt yielded a real SERP
    retried: bool = False    # B only: did we actually issue attempt 2
    ip_changed: Optional[bool] = None  # B only, if we retried: did IP change


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


async def one_attempt(pw, proxy: dict, query: str, n: int) -> Attempt:
    """One fetch through a fresh browser + fresh context. Mirrors production
    engine (Chromium + stealth + resource block + homepage warmup)."""
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

    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        context = await browser.new_context(
            proxy=proxy, locale="en-US",
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        await context.add_init_script(STEALTH_INIT_JS)

        async def route(r):
            if r.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await r.abort()
                return
            url = r.request.url.lower()
            # reCAPTCHA is only loaded by captcha pages — blanket-block it.
            # Saves ~500 KB per /sorry/ hit.
            if "recaptcha" in url:
                await r.abort()
                return
            # Block /sorry/ subresources but allow the top-level nav so we can
            # still detect the block via page.url. Aborting the nav itself
            # was tested and backfired — Google's soft-interstitial page
            # loads under /search before ever redirecting, so bytes were
            # already spent by the time /sorry/ was requested.
            if "/sorry/" in url:
                is_nav = (r.request.is_navigation_request()
                          if callable(r.request.is_navigation_request)
                          else r.request.is_navigation_request)
                if not is_nav:
                    await r.abort()
                    return
            await r.continue_()
        await context.route("**/*", route)

        exit_ip = await _get_ip(context)

        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto("https://www.google.com/",
                            wait_until="domcontentloaded", timeout=30_000)
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            # wait_until="commit" returns as soon as the response headers land
            # (before subresources download). For a /search that 302s to /sorry/,
            # page.url reflects the final /sorry/ URL at this point — so we can
            # detect the block and bail before reCAPTCHA JS pulls in ~500 KB.
            resp = await page.goto(search_url, wait_until="commit",
                                   timeout=30_000)
            final_url = page.url or ""
            status = resp.status if resp else 0

            if "/sorry/" in final_url.lower():
                # Blocked. Skip h3 wait, skip content read, close page to cancel
                # in-flight subresources.
                return Attempt(
                    n=n, exit_ip=exit_ip, final_url=final_url, status=status,
                    h3_count=0, sorry=True, enablejs=False,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    total_bytes=total_bytes, blocked=True,
                )

            # Not a /sorry/ redirect (yet) — proceed with normal DOM+h3 wait.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            # Re-check URL post-DCL: catches the soft-interstitial case where
            # /search returns 200 and a JS redirect to /sorry/ fires during DCL.
            final_url = page.url or final_url
            if "/sorry/" in final_url.lower():
                return Attempt(
                    n=n, exit_ip=exit_ip, final_url=final_url, status=status,
                    h3_count=0, sorry=True, enablejs=False,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    total_bytes=total_bytes, blocked=True,
                )
            try:
                await page.wait_for_selector("h3", timeout=3_000)
            except Exception:
                pass
            await asyncio.sleep(0.5)

            html = await page.content()
            final_url = page.url
            h3 = len(re.findall(r"<h3", html, re.I))
            sorry = "/sorry/" in (final_url or "").lower()  # covers late redirect
            # Block rule matches _is_blocked() in src/core/engines/google.py:
            # /sorry/, or 200 with zero h3s and a body.
            blocked = sorry or (status == 200 and h3 == 0 and "<body" in html.lower())
            return Attempt(
                n=n, exit_ip=exit_ip, final_url=final_url, status=status,
                h3_count=h3, sorry=sorry,
                enablejs="/httpservice/retry/enablejs" in html,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                total_bytes=total_bytes, blocked=blocked,
            )
        finally:
            await page.close()
            await context.close()
    finally:
        await browser.close()


async def run_variant(pw, proxy: dict, query: str, variant: str, idx: int) -> Result:
    r = Result(idx=idx, variant=variant, query=query)
    a1 = await one_attempt(pw, proxy, query, n=1)
    r.attempts.append(a1)

    if variant == "B" and a1.blocked:
        r.retried = True
        # Brief gap: matches the "cold retry" spirit — not so short it looks
        # scripted, not so long the pool state has shifted.
        await asyncio.sleep(random.uniform(2.0, 4.0))
        a2 = await one_attempt(pw, proxy, query, n=2)
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10000,
                    help="Decodo port. 10000=rotating, 10001+=sticky.")
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
    schedule = [("A" if i % 2 == 0 else "B", kw) for i, kw in enumerate(keywords)]

    print(f"Retry-on-block A/B — Decodo :{args.port} ({mode}) — "
          f"{len(schedule)} queries interleaved\n")

    results: list[Result] = []
    async with async_playwright() as pw:
        for i, (variant, kw) in enumerate(schedule, 1):
            if i > 1:
                gap = random.uniform(args.pace_min, args.pace_max)
                print(f"  ...pacing {gap:.1f}s")
                await asyncio.sleep(gap)

            try:
                r = await run_variant(pw, proxy, kw, variant, i)
                a1 = r.attempts[0]
                line = (f"  [{variant} q{i}] {kw!r:<28} "
                        f"a1 ip={a1.exit_ip or '?':<15} h3={a1.h3_count:>2} "
                        f"kb={a1.total_bytes/1024:>5.0f} "
                        f"lat={a1.latency_ms:>5}ms → {_tag(a1)}")
                if r.retried:
                    a2 = r.attempts[1]
                    ip_note = ("ip↔same" if r.ip_changed is False
                               else "ip→new" if r.ip_changed is True
                               else "ip=?")
                    line += (f"\n              retry: ip={a2.exit_ip or '?':<15} "
                             f"h3={a2.h3_count:>2} kb={a2.total_bytes/1024:>5.0f} "
                             f"lat={a2.latency_ms:>5}ms → "
                             f"{_tag(a2)} ({ip_note})")
                total_kb = sum(a.total_bytes for a in r.attempts) / 1024
                line += f"    final={'PASS' if r.final_ok else 'FAIL'} total={total_kb:.0f}KB"
                print(line)
                results.append(r)
            except Exception as e:
                print(f"  [{variant} q{i}] ERR: {e}")
                results.append(Result(
                    idx=i, variant=variant, query=kw,
                    attempts=[Attempt(
                        n=1, exit_ip=None, final_url="", status=0, h3_count=0,
                        sorry=False, enablejs=False, latency_ms=0, total_bytes=0,
                        blocked=True, error=f"{type(e).__name__}: {e}"[:200],
                    )],
                ))

    a_results = [r for r in results if r.variant == "A"]
    b_results = [r for r in results if r.variant == "B"]
    a_pass = sum(1 for r in a_results if r.final_ok)
    b_pass = sum(1 for r in b_results if r.final_ok)

    # B-only breakdown: how often did retry fire, and how often did it convert?
    b_retried = [r for r in b_results if r.retried]
    b_retry_converted = sum(1 for r in b_retried
                            if r.final_ok and r.attempts[0].blocked
                            and not r.attempts[1].blocked)
    b_ip_changed = sum(1 for r in b_retried if r.ip_changed is True)
    b_ip_same = sum(1 for r in b_retried if r.ip_changed is False)

    def _avg_kb(rs):
        if not rs:
            return 0.0
        total = sum(a.total_bytes for r in rs for a in r.attempts)
        return total / len(rs) / 1024

    a_kb = _avg_kb(a_results)
    b_kb = _avg_kb(b_results)
    b_pass_kb = _avg_kb([r for r in b_results if r.final_ok])
    b_fail_kb = _avg_kb([r for r in b_results if not r.final_ok])

    print(f"\n=== Retry-on-block summary — Decodo :{args.port} ({mode}) ===")
    print(f"  A (no retry):       {a_pass}/{len(a_results)} pass   avg={a_kb:.0f} KB/query")
    print(f"  B (retry on block): {b_pass}/{len(b_results)} pass   avg={b_kb:.0f} KB/query "
          f"(pass={b_pass_kb:.0f}KB, fail={b_fail_kb:.0f}KB)")
    delta = b_pass - a_pass
    verdict = ('retry helps' if delta > 0
               else 'retry hurts' if delta < 0
               else 'no effect')
    print(f"  Delta:              {'+' if delta >= 0 else ''}{delta} passes  ({verdict})")
    if b_retried:
        print(f"  Retry usage (B):    fired {len(b_retried)}/{len(b_results)} times, "
              f"converted {b_retry_converted}/{len(b_retried)}")
        print(f"  IP behavior (B):    new={b_ip_changed}  same={b_ip_same}  "
              f"unknown={len(b_retried) - b_ip_changed - b_ip_same}")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = log_dir / f"retry_on_block_decodo{args.port}_{ts}.json"
    p.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nLog: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
