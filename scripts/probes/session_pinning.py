"""Probe 4 — session-pinning vs pure rotating, Decodo residential.

Tests whether pinning Decodo's exit IP via `-session-<id>` in the proxy
username fixes the low pass rate seen in Probe 3.

Modes:
  A  Pure rotating: gate.decodo.com:10001, no session param.
     Fresh browser context per keyword (matches current architecture).
     Expected: warm-up GET / lands on IP X, GET /search lands on IP Y —
     warm-up cookies useless, Google sees us as a cold visitor each time.

  B  Sticky-session: gate.decodo.com:10000, username carries a session ID.
     ONE browser context reused for all keywords.
     Expected: same exit IP for the whole run, warm-up applies once and
     benefits every subsequent query.

Also hits api.ipify.org once per browser context to log the actual exit
IP — proves session pinning is doing what we think.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext

QUERIES = [
    "best proxy",
    "top proxy",
    "decodo review",
    "oxylabs review",
    "smartproxy review",
    "residential proxy",
    "rotating proxy service",
    "seo rank tracker",
    "seo software review",
    "best serp api",
]

DB_PATH = Path(__file__).parent.parent.parent / "serp_scraper.db"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check", "--no-first-run",
]
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


def load_decodo() -> dict:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1"
    ).fetchone()
    db.close()
    return {"user": row["username"], "password": row["password"], "host": row["host"]}


@dataclass
class QResult:
    mode: str
    query: str
    exit_ip_at_context_start: Optional[str]
    status: int
    bytes: int
    h3: int
    is_sorry: bool
    is_enablejs_only: bool
    final_url: str
    latency_ms: int
    error: Optional[str] = None


async def _apply_stealth(context: BrowserContext) -> None:
    await context.add_init_script(STEALTH_INIT_JS)


async def _make_context(browser: Browser, proxy: dict) -> BrowserContext:
    ctx = await browser.new_context(
        proxy=proxy,
        locale="en-US",
        viewport={"width": 1280, "height": 800},
        user_agent=UA,
    )
    await _apply_stealth(ctx)
    # Block heavy resources but NOT stylesheets (see Probe 3 findings).
    async def _route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()
    await ctx.route("**/*", _route)
    return ctx


async def _get_exit_ip(context: BrowserContext) -> Optional[str]:
    """Hit api.ipify.org via the proxied context to learn our exit IP."""
    page = await context.new_page()
    try:
        r = await page.goto("https://api.ipify.org?format=json",
                            wait_until="domcontentloaded", timeout=20_000)
        if r and r.status == 200:
            body = await page.content()
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
            return m.group(1) if m else None
    except Exception:
        return None
    finally:
        await page.close()
    return None


async def _search_one(context: BrowserContext, query: str, mode: str,
                      exit_ip: Optional[str]) -> QResult:
    page = await context.new_page()
    t0 = time.perf_counter()
    try:
        # Warm-up: GET / (Google issues session cookies here).
        await page.goto("https://www.google.com/", wait_until="domcontentloaded",
                        timeout=30_000)
        # Then the actual search.
        resp = await page.goto(
            f"https://www.google.com/search?q={query.replace(' ', '+')}",
            wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_selector("h3", timeout=5_000)
        except Exception:
            pass
        html = await page.content()
        final = page.url
        h3 = len(re.findall(r"<h3", html, re.I))
        return QResult(
            mode=mode, query=query, exit_ip_at_context_start=exit_ip,
            status=resp.status if resp else 0,
            bytes=len(html), h3=h3,
            is_sorry="/sorry/" in final.lower(),
            is_enablejs_only=(h3 == 0 and "/httpservice/retry/enablejs" in html
                              and "unusual traffic" not in html.lower()),
            final_url=final,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:
        return QResult(
            mode=mode, query=query, exit_ip_at_context_start=exit_ip,
            status=0, bytes=0, h3=0, is_sorry=False, is_enablejs_only=False,
            final_url="", latency_ms=int((time.perf_counter() - t0) * 1000),
            error=f"{type(e).__name__}: {e}"[:200],
        )
    finally:
        await page.close()


async def run_mode_a(pw, creds: dict) -> list[QResult]:
    """Mode A: pure rotating :10001, fresh browser context per keyword."""
    print("\n=== MODE A: pure rotating (:10001), fresh context per keyword ===")
    proxy = {
        "server": f"http://{creds['host']}:10001",
        "username": creds["user"],
        "password": creds["password"],
    }
    results: list[QResult] = []
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        for i, q in enumerate(QUERIES, 1):
            ctx = await _make_context(browser, proxy)
            exit_ip = await _get_exit_ip(ctx)
            r = await _search_one(ctx, q, "A_rotating", exit_ip)
            await ctx.close()
            results.append(r)
            _print_row(i, r)
    finally:
        await browser.close()
    return results


async def run_mode_b(pw, creds: dict) -> list[QResult]:
    """Mode B: sticky-session :10000, ONE context for all keywords."""
    session_id = secrets.token_hex(8)
    print(f"\n=== MODE B: sticky-session (:10000, session={session_id}), shared context ===")
    proxy = {
        "server": f"http://{creds['host']}:10000",
        "username": f"user-{creds['user']}-session-{session_id}",
        "password": creds["password"],
    }
    results: list[QResult] = []
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        ctx = await _make_context(browser, proxy)
        exit_ip = await _get_exit_ip(ctx)
        print(f"  exit IP at context start: {exit_ip}")
        for i, q in enumerate(QUERIES, 1):
            r = await _search_one(ctx, q, "B_sticky_session", exit_ip)
            results.append(r)
            _print_row(i, r)
        await ctx.close()
    finally:
        await browser.close()
    return results


def _print_row(i: int, r: QResult) -> None:
    marker = "OK " if r.h3 > 0 else "XX "
    kind = "SERP" if r.h3 > 0 else ("sorry" if r.is_sorry else
                                    ("enablejs" if r.is_enablejs_only else "unknown"))
    print(f"  {marker} q{i:>2}: ip={r.exit_ip_at_context_start!s:<16} "
          f"status={r.status} bytes={r.bytes:>7} h3={r.h3:>2} kind={kind:<8} "
          f"lat={r.latency_ms}ms")


async def main() -> int:
    creds = load_decodo()
    all_results: list[QResult] = []
    async with async_playwright() as pw:
        all_results.extend(await run_mode_a(pw, creds))
        all_results.extend(await run_mode_b(pw, creds))

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = log_dir / f"session_pinning_{ts}.json"
    log.write_text(json.dumps([asdict(r) for r in all_results], indent=2))

    print("\n=== Summary ===")
    for mode in ("A_rotating", "B_sticky_session"):
        rows = [r for r in all_results if r.mode == mode]
        passed = sum(1 for r in rows if r.h3 > 0)
        unique_ips = {r.exit_ip_at_context_start for r in rows if r.exit_ip_at_context_start}
        avg_lat = sum(r.latency_ms for r in rows) / max(len(rows), 1)
        print(f"  {mode:<18}: {passed}/{len(rows)} passed  "
              f"unique_ips={len(unique_ips)}  avg_lat={int(avg_lat)}ms")

    print(f"\nLog: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
