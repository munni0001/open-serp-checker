"""Session 3.1 phase 3 — does Firefox stealth kill Google's layer-2 captcha?

Phase 2 (persistent_context_firefox.py) proved: stock Firefox on Decodo
sticky :10001 passes TLS but gets /sorry/ on 10/10 /search queries. The
prime suspect is navigator.webdriver = true, which Playwright's Firefox
exposes by default (Chromium's recipe hides it via
--disable-blink-features=AutomationControlled + init script).

This is a fast N=3 probe. Apply the equivalent Firefox stealth surface,
run three sticky-context searches. If any come back with h3>0, promote to
the full N=10 measurement. If all three /sorry/, we know defense #2 needs
more than the webdriver flag flip.

Firefox stealth surface (vs Chromium):
- firefox_user_prefs at LAUNCH level (not context) — Playwright exposes
  a small allowlist of about:config prefs here.
- dom.webdriver.enabled = False → makes navigator.webdriver return false
  natively (not undefined).
- Init script overrides for navigator.webdriver, plugins, languages as
  belt-and-suspenders in case the pref alone doesn't cover every getter.
- Match locale + Accept-Language to the UA.

Not doing:
- Real Mozilla Firefox binary (Playwright ships a patched build; using
  Firefox proper needs `channel=` and a separate install path).
- Marionette/geckodriver poking — Playwright manages its own control
  channel; the pref approach is what we have.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DB = Path(__file__).parent.parent.parent / "serp_scraper.db"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) "
      "Gecko/20100101 Firefox/122.0")

KEYWORDS = [
    "foxy ai promo code",
    "heygen promo code",
    "apify promo code",
]

FIREFOX_PREFS = {
    # The main event — hides navigator.webdriver at the browser level.
    "dom.webdriver.enabled": False,
    # Match the UA / locale story.
    "intl.accept_languages": "en-US,en;q=0.9",
    # Don't leak "this is a fresh profile" telemetry probes.
    "browser.startup.homepage_override.mstone": "ignore",
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    # Reduce marionette fingerprinting side effects if any leak through.
    "useAutomationExtension": False,
}

STEALTH_INIT_JS = """
// Belt-and-suspenders in case dom.webdriver.enabled pref doesn't cover a getter.
Object.defineProperty(navigator, 'webdriver', { get: () => false });
// Firefox exposes an empty plugins array by default in headless; give it something.
Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""


def load_decodo(port: int) -> dict:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    r = db.execute("SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1").fetchone()
    db.close()
    return {"server": f"http://{r['host']}:{port}",
            "username": r["username"], "password": r["password"]}


async def check() -> int:
    proxy = load_decodo(10001)
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(
            headless=True,
            firefox_user_prefs=FIREFOX_PREFS,
        )
        try:
            ctx = await browser.new_context(
                proxy=proxy,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
                user_agent=UA,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            await ctx.add_init_script(STEALTH_INIT_JS)

            # Runtime self-check: is navigator.webdriver actually hidden now?
            probe = await ctx.new_page()
            await probe.goto("about:blank")
            wd = await probe.evaluate("navigator.webdriver")
            langs = await probe.evaluate("navigator.languages")
            print(f"self-check: navigator.webdriver = {wd!r}, languages = {langs!r}\n")
            await probe.close()

            # Warmup homepage in same context.
            wp = await ctx.new_page()
            print("warmup: https://www.google.com/ ...")
            try:
                r = await wp.goto("https://www.google.com/",
                                  wait_until="domcontentloaded", timeout=45_000)
                print(f"  warmup status={r.status if r else None} url={wp.url}\n")
            finally:
                await wp.close()

            # Three searches, pace 20s (shorter than shipping's 35s — this is a probe).
            for i, q in enumerate(KEYWORDS, 1):
                if i > 1:
                    print(f"  ...pacing 20s")
                    await asyncio.sleep(20)
                page = await ctx.new_page()
                try:
                    url = f"https://www.google.com/search?q={q.replace(' ', '+')}"
                    r = await page.goto(url, wait_until="domcontentloaded",
                                        timeout=45_000)
                    try:
                        await page.wait_for_selector("h3", timeout=6_000)
                    except Exception:
                        pass
                    html = await page.content()
                    h3s = len(re.findall(r"<h3", html, re.I))
                    sorry = "/sorry/" in (page.url or "").lower()
                    enablejs = "/httpservice/retry/enablejs" in html
                    tag = "SORRY" if sorry else ("ENABLEJS" if enablejs else "OK" if h3s else "NO_H3")
                    print(f"  [q{i}] {q!r:<28} status={r.status if r else 0} "
                          f"h3={h3s} final_url={page.url[:80]} → {tag}")
                finally:
                    await page.close()
        finally:
            await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
