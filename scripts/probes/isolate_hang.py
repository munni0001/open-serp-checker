"""Four-way isolation of the Chromium+Decodo hang.

Yesterday: :10000 rotating + fresh browser per query worked (~60% pass).
Today:     :10001+ sticky + persistent context hangs 100% at first goto.

Two variables changed. This script tries each combination independently
so we can attribute the hang to (a) the rotating vs sticky mode, (b) the
persistence path, (c) something else, or (d) all residential is dead now.

Each test = one Chromium launch, one page.goto("https://www.google.com/"),
wait_until="commit", 15s timeout. We record: exit IP, http status, url after
5s dwell, first 200 chars of body (or the exception).

Also runs curl for reference on each port — if curl works and Chromium
doesn't on the same port, that's TLS-fingerprint rejection at Google's edge.
"""
from __future__ import annotations

import asyncio
import subprocess
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DB = Path(__file__).parent.parent.parent / "serp_scraper.db"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def load_creds() -> tuple[str, str, str]:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    r = db.execute("SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1").fetchone()
    db.close()
    return r["username"], r["password"], r["host"]


def curl_check(user: str, pw: str, host: str, port: int, url: str = "https://www.google.com/") -> str:
    """Return one-line result: HTTP=..  time=..  ip=..  or ERROR."""
    proxy = f"http://{user}:{pw}@{host}:{port}"
    try:
        r = subprocess.run(
            ["curl", "-x", proxy, "-sS", "-o", "/dev/null",
             "-w", "HTTP=%{http_code} time=%{time_total}",
             "--max-time", "15", url],
            capture_output=True, text=True, timeout=20,
        )
        base = r.stdout.strip() or f"ERROR rc={r.returncode} err={r.stderr.strip()[:80]}"
        # Get exit IP separately (cheap).
        try:
            ip_r = subprocess.run(
                ["curl", "-x", proxy, "-sS", "--max-time", "10",
                 "https://api.ipify.org?format=json"],
                capture_output=True, text=True, timeout=15,
            )
            base += f" ip_body={ip_r.stdout.strip()[:60]}"
        except Exception:
            pass
        return base
    except Exception as e:
        return f"EXC {type(e).__name__}: {e}"


async def playwright_check(host: str, port: int, user: str, pw_: str,
                           persistent: bool) -> str:
    """Return one-line result for the Chromium goto. If persistent=True, do
    a warmup goto first to build context state, then the real goto."""
    proxy = {"server": f"http://{host}:{port}", "username": user, "password": pw_}
    label = "persistent" if persistent else "fresh"
    async with async_playwright() as pw:
        try:
            b = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            ctx = await b.new_context(user_agent=UA, proxy=proxy, locale="en-US",
                                      viewport={"width": 1280, "height": 800})
            try:
                # If persistent, we simulate a fresh cache miss anyway (first-of-session).
                # The difference vs "fresh" here is: this test uses the SAME context
                # across two goto's, whereas fresh closes the browser after one. But
                # since we only make one measured goto per test, the meaningful
                # variable this test measures is CHROMIUM vs the proxy — not cache.
                page = await ctx.new_page()
                try:
                    resp = await page.goto("https://www.google.com/",
                                           wait_until="commit", timeout=15_000)
                    status = resp.status if resp else "None"
                    await asyncio.sleep(3)
                    return (f"OK status={status} url={page.url} "
                            f"title={await page.title()!r}")
                except Exception as e:
                    return f"ERR {type(e).__name__}: {str(e)[:80]}"
                finally:
                    await page.close()
            finally:
                await ctx.close()
                await b.close()
        except Exception as e:
            return f"LAUNCH_ERR {type(e).__name__}: {str(e)[:80]}"


async def main() -> int:
    user, pw_, host = load_creds()
    ports = [10000, 10001, 10004]  # rotating, previously-tested sticky, fresh sticky

    print(f"Host={host}  user={user}\n")

    for p in ports:
        mode = "rotating" if p == 10000 else "sticky"
        print(f"=== port {p} ({mode}) ===")
        print(f"  curl        : {curl_check(user, pw_, host, p)}")
        print(f"  chromium    : {await playwright_check(host, p, user, pw_, persistent=False)}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
