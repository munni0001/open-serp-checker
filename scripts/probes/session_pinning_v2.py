"""Probe 4b — proxy modes on Decodo, correct semantics.

Supersedes session_pinning.py, which was written with the port convention
inverted (had :10000 as sticky, :10001 as rotating). Dashboard screenshots
confirm the correct mapping:

  :10000                 → ROTATING. Bare username. New exit IP per TCP conn.
  :10001, :10002, ...    → STICKY. Each port = one exit IP for the sessionduration TTL.
                           Username: user-<base>-sessionduration-<minutes>
                           Min TTL = 1 min.

Modes tested (10 keywords each):

  A_rotating          :10000, bare user, fresh browser context per keyword.
                      Each keyword → new IP. Warm+search within a keyword share
                      the same IP (Playwright reuses conns within a context).

  B_sticky_batch      :10001, user-<base>-sessionduration-1, ONE shared context
                      for all 10 keywords. All keywords → same IP.

  C_sticky_per_kw     :10001-10010, user-<base>-sessionduration-1, fresh context
                      per keyword, port cycles across keywords. Each keyword →
                      unique-but-stable IP. Warm+search share IP AND IPs vary
                      across keywords (best-of-both theory).

Per-context ipify check logs the actual exit IP as ground truth.
"""
from __future__ import annotations

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
    proxy_port: int
    proxy_username: str
    exit_ip: Optional[str]
    status: int
    bytes: int
    h3: int
    is_sorry: bool
    is_enablejs_only: bool
    final_url: str
    latency_ms: int
    error: Optional[str] = None


async def _make_context(browser: Browser, proxy: dict) -> BrowserContext:
    ctx = await browser.new_context(
        proxy=proxy,
        locale="en-US",
        viewport={"width": 1280, "height": 800},
        user_agent=UA,
    )
    await ctx.add_init_script(STEALTH_INIT_JS)
    async def _route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()
    await ctx.route("**/*", _route)
    return ctx


async def _get_exit_ip(context: BrowserContext) -> Optional[str]:
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


async def _search(context: BrowserContext, query: str, mode: str,
                  port: int, username: str, exit_ip: Optional[str]) -> QResult:
    page = await context.new_page()
    t0 = time.perf_counter()
    try:
        await page.goto("https://www.google.com/", wait_until="domcontentloaded",
                        timeout=30_000)
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
            mode=mode, query=query, proxy_port=port, proxy_username=username,
            exit_ip=exit_ip,
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
            mode=mode, query=query, proxy_port=port, proxy_username=username,
            exit_ip=exit_ip, status=0, bytes=0, h3=0, is_sorry=False,
            is_enablejs_only=False, final_url="",
            latency_ms=int((time.perf_counter() - t0) * 1000),
            error=f"{type(e).__name__}: {e}"[:200],
        )
    finally:
        await page.close()


def _print_row(i: int, r: QResult) -> None:
    marker = "OK " if r.h3 > 0 else "XX "
    kind = "SERP" if r.h3 > 0 else ("sorry" if r.is_sorry else
                                    ("enablejs" if r.is_enablejs_only else "unknown"))
    print(f"  {marker} q{i:>2}: port={r.proxy_port} ip={r.exit_ip!s:<16} "
          f"status={r.status} bytes={r.bytes:>7} h3={r.h3:>2} "
          f"kind={kind:<8} lat={r.latency_ms}ms")


# -------- Mode A: true rotating --------

async def run_mode_a(pw, creds: dict) -> list[QResult]:
    print("\n=== MODE A: true rotating (:10000 bare user, fresh context per kw) ===")
    port = 10000
    username = creds["user"]
    proxy = {
        "server": f"http://{creds['host']}:{port}",
        "username": username,
        "password": creds["password"],
    }
    results: list[QResult] = []
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        for i, q in enumerate(QUERIES, 1):
            ctx = await _make_context(browser, proxy)
            exit_ip = await _get_exit_ip(ctx)
            r = await _search(ctx, q, "A_rotating", port, username, exit_ip)
            await ctx.close()
            results.append(r)
            _print_row(i, r)
    finally:
        await browser.close()
    return results


# -------- Mode B: sticky batch (all keywords on one sticky port) --------

async def run_mode_b(pw, creds: dict) -> list[QResult]:
    port = 10001
    username = f"user-{creds['user']}-sessionduration-1"
    print(f"\n=== MODE B: sticky batch ({port}, {username}, one shared context) ===")
    proxy = {
        "server": f"http://{creds['host']}:{port}",
        "username": username,
        "password": creds["password"],
    }
    results: list[QResult] = []
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        ctx = await _make_context(browser, proxy)
        exit_ip = await _get_exit_ip(ctx)
        print(f"  shared context exit IP: {exit_ip}")
        for i, q in enumerate(QUERIES, 1):
            r = await _search(ctx, q, "B_sticky_batch", port, username, exit_ip)
            results.append(r)
            _print_row(i, r)
        await ctx.close()
    finally:
        await browser.close()
    return results


# -------- Mode C: sticky per keyword (rotating ports, fresh context each) --------

async def run_mode_c(pw, creds: dict) -> list[QResult]:
    username = f"user-{creds['user']}-sessionduration-1"
    print(f"\n=== MODE C: sticky-per-kw ({{10001..10010}}, {username}, fresh ctx each) ===")
    results: list[QResult] = []
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        for i, q in enumerate(QUERIES, 1):
            port = 10001 + ((i - 1) % 10)
            proxy = {
                "server": f"http://{creds['host']}:{port}",
                "username": username,
                "password": creds["password"],
            }
            ctx = await _make_context(browser, proxy)
            exit_ip = await _get_exit_ip(ctx)
            r = await _search(ctx, q, "C_sticky_per_kw", port, username, exit_ip)
            await ctx.close()
            results.append(r)
            _print_row(i, r)
    finally:
        await browser.close()
    return results


async def main() -> int:
    creds = load_decodo()
    all_results: list[QResult] = []
    async with async_playwright() as pw:
        all_results.extend(await run_mode_a(pw, creds))
        all_results.extend(await run_mode_b(pw, creds))
        all_results.extend(await run_mode_c(pw, creds))

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = log_dir / f"session_pinning_v2_{ts}.json"
    log.write_text(json.dumps([asdict(r) for r in all_results], indent=2))

    print("\n=== Summary ===")
    print(f"{'mode':<20} {'pass':<7} {'unique_ips':<11} {'avg_lat_ms':<11} {'avg_bytes':<10}")
    for mode in ("A_rotating", "B_sticky_batch", "C_sticky_per_kw"):
        rows = [r for r in all_results if r.mode == mode]
        if not rows:
            continue
        passed = sum(1 for r in rows if r.h3 > 0)
        unique_ips = {r.exit_ip for r in rows if r.exit_ip}
        avg_lat = sum(r.latency_ms for r in rows) / len(rows)
        avg_bytes = sum(r.bytes for r in rows) / len(rows)
        print(f"  {mode:<18} {passed}/{len(rows):<5} {len(unique_ips):<11} "
              f"{int(avg_lat):<11} {int(avg_bytes):<10}")

    print(f"\nLog: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
