"""One-shot diagnostic: what is Playwright actually stuck on when Google hangs?

Uses wait_until="commit" (fires on the FIRST response byte, not lifecycle
events) + a 5s post-commit dwell, then dumps: response status, page URL,
document title, HTML length, first 500 chars. That's enough to identify
consent interstitials, /sorry/ redirects, or JS challenge loops.

Ignores CLI args — hardcoded to Decodo sticky :10003.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

DB = Path(__file__).parent.parent.parent / "serp_scraper.db"


async def main() -> int:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    r = db.execute(
        "SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1"
    ).fetchone()
    db.close()
    proxy = {
        "server": f"http://{r['host']}:10003",
        "username": r["username"],
        "password": r["password"],
    }

    async with async_playwright() as pw:
        b = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--disable-quic",
                "--disable-features=UsePreferredIntervalForRAF,Http2Grease",
            ],
        )
        ctx = await b.new_context(
            proxy=proxy,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()
        try:
            resp = await page.goto("https://www.google.com/",
                                   wait_until="commit", timeout=15_000)
            print(f"COMMITTED  status={resp.status if resp else None}  "
                  f"url_at_commit={page.url}")
            await asyncio.sleep(5)
            print(f"5s later   url={page.url}")
            try:
                title = await page.title()
                print(f"title: {title!r}")
            except Exception as e:
                print(f"title fetch failed: {e}")
            html = await page.content()
            print(f"html_len={len(html)}")
            print(f"first 500 chars:\n{html[:500]}")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
        finally:
            await b.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
