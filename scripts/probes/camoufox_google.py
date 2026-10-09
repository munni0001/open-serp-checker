"""Session 4.10 Phase 1 — does Camoufox survive Google /search on Decodo?

Camoufox is a patched Firefox binary + Python wrapper that spoofs the
fingerprint surface at the browser level (not via a stealth init script).
Prior data:
- `firefox_stealth_check.py` (Session 3.1): stock-Playwright Firefox +
  webdriver-hiding prefs went 3/3 on Decodo sticky :10001.
- Chromium+stealth-args: 0/3 on the same pool (TLS fingerprint block).
- Chromium is what pass_rate/warm/local_baseline all currently use.

The question this probe answers: does Camoufox (which the user has
shipped on other Google-adjacent work) survive Decodo residential
against /search, without any of our hand-rolled stealth patches?

Gate (per sessions/4.10-browser-engine-axis.md): 2/3 → OK to promote
to the Phase 2 engine axis. 0/3 or 1/3 → close and record.

Install (one-shot, not automated here):
    pip install 'camoufox[geoip]'
    python -m camoufox fetch    # ~120 MB patched Firefox binary

Not doing:
- No DB writes, no aggregation, no retry — probe tier is stdout-only.
- No hand-rolled stealth JS. The whole point of Camoufox is that you
  don't need to bolt Chromium-shaped patches onto Firefox-shaped
  fingerprints.
- No UA override. Camoufox picks a coherent UA/screen/webgl bundle.
- No resource blocking. Matches `firefox_stealth_check.py` for clean
  A/B; a diet variant probe comes later if this one earns it.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent.parent.parent / "open_serp_checker.db"

KEYWORDS = [
    "foxy ai promo code",
    "heygen promo code",
    "apify promo code",
]


def load_decodo(port: int) -> dict:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    r = db.execute(
        "SELECT username, password, host FROM proxies "
        "WHERE provider='decodo' LIMIT 1"
    ).fetchone()
    db.close()
    return {"server": f"http://{r['host']}:{port}",
            "username": r["username"], "password": r["password"]}


async def check() -> int:
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as e:
        print(f"camoufox import failed: {e}")
        print("install with: pip install 'camoufox[geoip]'")
        print("then run:     python -m camoufox fetch")
        return 2

    proxy = load_decodo(10001)
    print(f"proxy: {proxy['server']} (sticky)")

    try:
        cam = AsyncCamoufox(
            headless=True,
            proxy=proxy,
            humanize=True,   # mouse jitter, +200-600ms per nav
            geoip=True,      # spoof geo from exit IP; needs [geoip] extra
            locale="en-US",
        )
    except Exception as e:
        print(f"camoufox construct failed: {type(e).__name__}: {e}")
        if "geoip" in str(e).lower():
            print("hint: pip install 'camoufox[geoip]'")
        return 2

    async with cam as browser:
        # Runtime self-check: prove Camoufox is actually spoofing.
        probe = await browser.new_page()
        await probe.goto("about:blank")
        wd = await probe.evaluate("navigator.webdriver")
        ua = await probe.evaluate("navigator.userAgent")
        langs = await probe.evaluate("navigator.languages")
        sw = await probe.evaluate("screen.width")
        sh = await probe.evaluate("screen.height")
        print(f"self-check: webdriver={wd!r} langs={langs!r} "
              f"screen={sw}x{sh}\n            ua={ua!r}")
        warn = []
        if wd is True:
            warn.append("navigator.webdriver=true (spoof failed)")
        if "Firefox" not in (ua or ""):
            warn.append("UA not Firefox-shaped")
        if sw in (0, 1) or sh in (0, 1):
            warn.append(f"screen looks fake ({sw}x{sh})")
        if warn:
            print(f"  WARN: {'; '.join(warn)}")
        print()
        await probe.close()

        # Warmup homepage in same browser (Camoufox default context).
        wp = await browser.new_page()
        print("warmup: https://www.google.com/ ...")
        try:
            r = await wp.goto(
                "https://www.google.com/",
                wait_until="domcontentloaded", timeout=45_000,
            )
            print(f"  warmup status={r.status if r else None} "
                  f"url={wp.url}\n")
        finally:
            await wp.close()

        # Three searches, pace 20s (probe tempo — shipping uses 15-45s).
        oks = 0
        for i, q in enumerate(KEYWORDS, 1):
            if i > 1:
                print("  ...pacing 20s")
                await asyncio.sleep(20)
            page = await browser.new_page()
            try:
                url = f"https://www.google.com/search?q={q.replace(' ', '+')}"
                r = await page.goto(
                    url, wait_until="domcontentloaded", timeout=45_000,
                )
                try:
                    await page.wait_for_selector("h3", timeout=6_000)
                except Exception:
                    pass
                html = await page.content()
                h3s = len(re.findall(r"<h3", html, re.I))
                sorry = "/sorry/" in (page.url or "").lower()
                enablejs = "/httpservice/retry/enablejs" in html
                if sorry:
                    tag = "SORRY"
                elif enablejs:
                    tag = "ENABLEJS"
                elif h3s:
                    tag = "OK"
                    oks += 1
                else:
                    tag = "NO_H3"
                print(f"  [q{i}] {q!r:<28} status={r.status if r else 0} "
                      f"h3={h3s} final_url={page.url[:80]} → {tag}")
            finally:
                await page.close()

        print(f"\nresult: {oks}/3 OK  |  gate: >=2/3 to promote to 4.11")
        return 0 if oks >= 2 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
