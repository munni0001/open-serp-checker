"""Second diagnostic: proxy at LAUNCH level (native Chromium path), no stealth.

Isolates two variables: (1) proxy config path — launch args vs context, and
(2) stealth patches — do they cause the hang. Baseline: pure defaults.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DB = Path(__file__).parent.parent.parent / "open_serp_checker.db"


async def try_launch_proxy():
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    r = db.execute("SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1").fetchone()
    db.close()
    proxy = {
        "server": f"http://{r['host']}:10003",
        "username": r["username"],
        "password": r["password"],
    }

    async with async_playwright() as pw:
        print("[T1] proxy at LAUNCH, no stealth, no init script")
        b = await pw.chromium.launch(headless=True, proxy=proxy)
        try:
            ctx = await b.new_context()
            page = await ctx.new_page()
            try:
                resp = await page.goto("https://www.google.com/",
                                       wait_until="commit", timeout=15_000)
                print(f"  OK  status={resp.status if resp else None} url={page.url}")
            except Exception as e:
                print(f"  ERR: {type(e).__name__}: {e}")
            finally:
                await ctx.close()
        finally:
            await b.close()

        print("\n[T2] proxy at LAUNCH + basic UA + no stealth")
        b = await pw.chromium.launch(headless=True, proxy=proxy)
        try:
            ctx = await b.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
            )
            page = await ctx.new_page()
            try:
                resp = await page.goto("https://www.google.com/",
                                       wait_until="commit", timeout=15_000)
                print(f"  OK  status={resp.status if resp else None} url={page.url}")
                await asyncio.sleep(3)
                html = await page.content()
                print(f"  html_len={len(html)}")
                print(f"  title={await page.title()!r}")
            except Exception as e:
                print(f"  ERR: {type(e).__name__}: {e}")
            finally:
                await ctx.close()
        finally:
            await b.close()

        print("\n[T3] Confirm the proxy path itself is functional — hit ipify")
        b = await pw.chromium.launch(headless=True, proxy=proxy)
        try:
            ctx = await b.new_context()
            page = await ctx.new_page()
            try:
                await page.goto("https://api.ipify.org?format=json",
                                wait_until="domcontentloaded", timeout=15_000)
                print(f"  ipify body: {await page.content()}")
            except Exception as e:
                print(f"  ERR: {type(e).__name__}: {e}")
            finally:
                await ctx.close()
        finally:
            await b.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(try_launch_proxy()) or 0)
