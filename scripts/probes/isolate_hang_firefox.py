"""Session 3.1 — Firefox-vs-Chromium isolation on Decodo residential.

Same shape as isolate_hang.py, but swaps pw.chromium.launch(...) for
pw.firefox.launch(...) and uses a recent Firefox desktop UA. Everything
else — proxy pool, ports, wait_until, timeout, curl reference — is held
constant so any delta is attributable to browser TLS fingerprint.

Hypothesis (from Session 2.7.5): Google's edge silently drops Chromium's
TLS ClientHello coming from Decodo residential exits. Firefox has a very
different ClientHello — if the hypothesis is right, Firefox should pass
where Chromium hangs, on the exact same pool and IPs.
"""
from __future__ import annotations

import asyncio
import subprocess
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DB = Path(__file__).parent.parent.parent / "open_serp_checker.db"

# Firefox 122 desktop UA — matches a stock Firefox ClientHello shape.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) "
      "Gecko/20100101 Firefox/122.0")


def load_creds() -> tuple[str, str, str]:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    r = db.execute("SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1").fetchone()
    db.close()
    return r["username"], r["password"], r["host"]


def curl_check(user: str, pw: str, host: str, port: int, url: str = "https://www.google.com/") -> str:
    proxy = f"http://{user}:{pw}@{host}:{port}"
    try:
        r = subprocess.run(
            ["curl", "-x", proxy, "-sS", "-o", "/dev/null",
             "-w", "HTTP=%{http_code} time=%{time_total}",
             "--max-time", "15", url],
            capture_output=True, text=True, timeout=20,
        )
        base = r.stdout.strip() or f"ERROR rc={r.returncode} err={r.stderr.strip()[:80]}"
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


async def firefox_check(host: str, port: int, user: str, pw_: str) -> str:
    proxy = {"server": f"http://{host}:{port}", "username": user, "password": pw_}
    async with async_playwright() as pw:
        try:
            b = await pw.firefox.launch(headless=True)
            ctx = await b.new_context(user_agent=UA, proxy=proxy, locale="en-US",
                                      viewport={"width": 1280, "height": 800})
            try:
                page = await ctx.new_page()
                try:
                    resp = await page.goto("https://www.google.com/",
                                           wait_until="commit", timeout=15_000)
                    status = resp.status if resp else "None"
                    await asyncio.sleep(3)
                    return (f"OK status={status} url={page.url} "
                            f"title={await page.title()!r}")
                except Exception as e:
                    return f"ERR {type(e).__name__}: {str(e)[:120]}"
                finally:
                    await page.close()
            finally:
                await ctx.close()
                await b.close()
        except Exception as e:
            return f"LAUNCH_ERR {type(e).__name__}: {str(e)[:120]}"


async def main() -> int:
    user, pw_, host = load_creds()
    ports = [10000, 10001, 10004]  # rotating, sticky, sticky

    print(f"Host={host}  user={user}\n")

    for p in ports:
        mode = "rotating" if p == 10000 else "sticky"
        print(f"=== port {p} ({mode}) ===")
        print(f"  curl    : {curl_check(user, pw_, host, p)}")
        print(f"  firefox : {await firefox_check(host, p, user, pw_)}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
