"""Probe 3 — Playwright proof-of-life against Google.

Hits the same 5 queries as Probe 1, once unproxied and once via Decodo
rotating (:10001). Success = h3_count > 0. Blocks images/CSS/fonts to
cut per-request bandwidth (the SERP HTML is all we need for parsing).

Reads Decodo creds from the project's SQLite DB (per stored preference:
creds live in DB, not .env).

Usage:
    source venv/bin/activate
    python scripts/probes/playwright_google.py
    python scripts/probes/playwright_google.py --unproxied-only
    python scripts/probes/playwright_google.py --proxy-only
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

from playwright.async_api import async_playwright, Route

QUERIES = [
    "best crm for small business",
    "semrush review",
    "how to make sourdough starter",
    "python asyncio semaphore example",
    "seo audit checklist 2026",
]

# Note: NOT blocking stylesheet — Google detects that pattern and captchas.
# Only images/media/fonts, which don't affect the SERP HTML we parse.
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# Stealth init script — patches Playwright's tells that Google flags:
# navigator.webdriver, missing window.chrome, empty plugins/languages,
# and permissions.query returning 'default' for notifications.
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
    """Return Decodo proxy dict shaped for Playwright, on port 10001 (rotating).

    DB stores port 10000 (sticky) by default — we override to 10001 here.
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
        "server": f"http://{row['host']}:10001",
        "username": row["username"],
        "password": row["password"],
    }


@dataclass
class Result:
    label: str
    query: str
    status: int
    bytes: int
    h3_count: int
    final_url: str
    enablejs: bool
    latency_ms: int
    error: Optional[str] = None


async def _block_heavy(route: Route) -> None:
    if route.request.resource_type in BLOCK_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


async def run_query(context, query: str, label: str) -> Result:
    t0 = time.perf_counter()
    page = await context.new_page()
    await page.route("**/*", _block_heavy)
    try:
        resp = await page.goto(
            f"https://www.google.com/search?q={query.replace(' ', '+')}",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        # Give the enablejs challenge (if any) time to complete and land us
        # on real SERPs. 3s is a comfortable ceiling for that redirect chain.
        try:
            await page.wait_for_selector("h3", timeout=5_000)
        except Exception:
            pass
        html = await page.content()
        final_url = page.url
        return Result(
            label=label,
            query=query,
            status=resp.status if resp else 0,
            bytes=len(html),
            h3_count=len(re.findall(r"<h3", html, re.I)),
            final_url=final_url,
            enablejs="/httpservice/retry/enablejs" in html,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:
        return Result(
            label=label, query=query, status=0, bytes=0, h3_count=0,
            final_url="", enablejs=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            error=f"{type(e).__name__}: {e}"[:200],
        )
    finally:
        await page.close()


async def run_pass(pw, label: str, proxy: Optional[dict]) -> list[Result]:
    print(f"\n=== {label} ===")
    if proxy:
        print(f"  proxy: {proxy['server']} (user={proxy['username']})")
    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    context = await browser.new_context(
        proxy=proxy,
        locale="en-US",
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    await context.add_init_script(STEALTH_INIT_JS)
    results = []
    try:
        for i, q in enumerate(QUERIES, 1):
            r = await run_query(context, q, label)
            results.append(r)
            marker = "OK " if r.h3_count > 0 else "XX "
            print(f"  {marker} q{i}: status={r.status} bytes={r.bytes:>6} "
                  f"h3={r.h3_count:>2} enablejs={r.enablejs} "
                  f"lat={r.latency_ms}ms {'err='+r.error if r.error else ''}")
    finally:
        await context.close()
        await browser.close()
    return results


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unproxied-only", action="store_true")
    ap.add_argument("--proxy-only", action="store_true")
    args = ap.parse_args()

    all_results: list[Result] = []
    async with async_playwright() as pw:
        if not args.proxy_only:
            all_results.extend(await run_pass(pw, "unproxied (home IP)", proxy=None))
        if not args.unproxied_only:
            proxy = load_decodo_rotating()
            if not proxy:
                print("\n(!) No Decodo row in DB; skipping proxied pass.")
            else:
                all_results.extend(await run_pass(pw, "Decodo rotating (:10001)", proxy=proxy))

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"playwright_google_{ts}.json"
    log_path.write_text(json.dumps([asdict(r) for r in all_results], indent=2))

    print("\n=== Summary ===")
    for label in ("unproxied (home IP)", "Decodo rotating (:10001)"):
        rows = [r for r in all_results if r.label == label]
        if not rows:
            continue
        passed = sum(1 for r in rows if r.h3_count > 0)
        avg_lat = sum(r.latency_ms for r in rows) / len(rows)
        avg_bytes = sum(r.bytes for r in rows) / len(rows)
        print(f"  {label}: {passed}/{len(rows)} passed  "
              f"avg_lat={int(avg_lat)}ms avg_bytes={int(avg_bytes)}")

    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
