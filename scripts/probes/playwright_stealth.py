"""Probe 3b — try stealth variants + real Chrome binary against Google.

Prior probe showed Playwright's default (chromium-headless-shell) gets
redirected to /sorry/ captcha. Google detects headless-mode fingerprint.

Tests 4 variants against 1 query to keep the run tight:
  V1  headless-shell, no stealth               (baseline: known to fail)
  V2  headless-shell + stealth init_script     (does patching webdriver help?)
  V3  real Chrome + stealth + AutomationControlled off, HEADLESS
  V4  real Chrome + stealth + AutomationControlled off, HEADED (sanity)
"""
from __future__ import annotations

import asyncio
import re
import sys
from playwright.async_api import async_playwright, Browser, BrowserContext

QUERY = "best proxy services"
URL = f"https://www.google.com/search?q={QUERY.replace(' ', '+')}"

STEALTH_INIT_JS = """
// The big three Playwright/CDP tells.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// Chrome-specific globals expected by fingerprinters.
window.chrome = window.chrome || { runtime: {} };
// navigator.permissions.query({name:'notifications'}) returns 'default' in headless.
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p)
  );
}
// Non-empty plugins / languages.
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]


async def probe(pw, label: str, *, channel: str | None, headless: bool,
                stealth: bool, args: list[str] | None = None) -> None:
    launch_kwargs = {"headless": headless}
    if channel:
        launch_kwargs["channel"] = channel
    if args:
        launch_kwargs["args"] = args
    b: Browser = await pw.chromium.launch(**launch_kwargs)
    ctx: BrowserContext = await b.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1280, "height": 800},
    )
    if stealth:
        await ctx.add_init_script(STEALTH_INIT_JS)
    p = await ctx.new_page()
    try:
        r = await p.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            await p.wait_for_selector("h3", timeout=5_000)
        except Exception:
            pass
        html = await p.content()
        final = p.url
        h3 = len(re.findall(r"<h3", html, re.I))
        low = html.lower()
        page_type = (
            "SERP" if h3 > 0 else
            "captcha_sorry" if "/sorry/" in final.lower() or "unusual traffic" in low else
            "enablejs" if "/httpservice/retry/enablejs" in low else
            "unknown"
        )
        print(f"  [{label}] status={r.status if r else '?'} bytes={len(html):>7} "
              f"h3={h3:>2} type={page_type} final={final[:80]}")
    except Exception as e:
        print(f"  [{label}] ERROR {type(e).__name__}: {e}")
    finally:
        await ctx.close()
        await b.close()


async def main() -> int:
    print(f"Query: {QUERY!r}\n")
    async with async_playwright() as pw:
        print("V1 — headless-shell, no stealth (baseline)")
        await probe(pw, "V1", channel=None, headless=True, stealth=False)

        print("\nV2 — headless-shell + stealth init_script")
        await probe(pw, "V2", channel=None, headless=True, stealth=True, args=STEALTH_ARGS)

        print("\nV3 — real Chrome + stealth, HEADLESS")
        await probe(pw, "V3", channel="chrome", headless=True, stealth=True, args=STEALTH_ARGS)

        print("\nV4 — real Chrome + stealth, HEADED (a Chrome window will briefly appear)")
        await probe(pw, "V4", channel="chrome", headless=False, stealth=True, args=STEALTH_ARGS)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
