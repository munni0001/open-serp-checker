"""Session 3.1 phase 4 — does humanized behavior lift pass rate on Decodo?

Both Decodo and Oxylabs land at ~45-50% pass rate on the same current engine
(fresh Chromium per fetch, image/media/font blocked). Script is fine, IPs are
the ceiling. Two levers left within "same IP": (a) look more human,
(b) burn IPs less aggressively. This tests (a).

Interleaved A/B, N=10 on Decodo :10000. Alternating variants keeps pool-state
drift symmetric between A and B (a straight "10 A then 10 B" would be
confounded by IPs cycling in/out of Google's reputation mid-run).

A = current baseline: warmup → search → wait_for_selector('h3') → close.
B = humanized: warmup → search → wait for h3 → random 3-7s dwell +
    scroll gesture + 1-2 mouse moves → close. Random 15-45s inter-query
    pacing instead of back-to-back.

Both A and B use the same launch args, same UA, same block set, same
stealth init script. The only differences are the post-load behavior and
the inter-query gap.

Real-world caveats:
- Playwright's synthetic mouse events have event.isTrusted === false.
  Google can detect that. Included anyway because noise beats silence
  and cost is zero.
- Scroll via page.mouse.wheel() DOES produce scroll events that update
  Google's on-page telemetry — that's a real signal, not theatre.
- Random pacing is the biggest lever here — same-cadence request bursts
  are exactly what rate limiters trigger on.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

DB_PATH = Path(__file__).parent.parent.parent / "serp_scraper.db"

KEYWORDS = [
    "foxy ai promo code",
    "heygen promo code",
    "apify promo code",
    "webshare coupon code",
    "gamestop promo code",
    "midjourney promo code",
    "twilio promo code",
    "bnesim coupon",
    "retroid discount code",
    "paymore discount code",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]

STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def load_decodo(port: int) -> Optional[dict]:
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    r = db.execute("SELECT username, password, host FROM proxies WHERE provider='decodo' LIMIT 1").fetchone()
    db.close()
    if not r:
        return None
    return {"server": f"http://{r['host']}:{port}",
            "username": r["username"], "password": r["password"]}


@dataclass
class Result:
    idx: int
    variant: str          # "A" (baseline) or "B" (humanized)
    query: str
    exit_ip: Optional[str]
    final_url: str
    status: int
    h3_count: int
    sorry: bool
    enablejs: bool
    latency_ms: int
    total_bytes: int
    error: Optional[str] = None


async def _get_ip(context) -> Optional[str]:
    page = await context.new_page()
    try:
        r = await page.goto("https://api.ipify.org?format=json",
                            wait_until="domcontentloaded", timeout=10_000)
        if r and r.status == 200:
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', await page.content())
            return m.group(1) if m else None
    except Exception:
        return None
    finally:
        await page.close()


async def run_one(pw, proxy: dict, query: str, variant: str, idx: int) -> Result:
    """One keyword through a fresh browser + fresh context. Matches production
    engine's fetch() shape so this measures the actual engine, not a probe-only
    codepath. Only difference between A and B is post-load behavior."""
    t0 = time.perf_counter()
    total_bytes = 0

    def on_response(resp):
        nonlocal total_bytes
        try:
            cl = resp.headers.get("content-length")
            if cl:
                total_bytes += int(cl)
        except Exception:
            pass

    browser = await pw.chromium.launch(headless=True, args=STEALTH_ARGS)
    try:
        context = await browser.new_context(
            proxy=proxy, locale="en-US",
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        await context.add_init_script(STEALTH_INIT_JS)

        async def route(r):
            if r.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await r.abort()
            else:
                await r.continue_()
        await context.route("**/*", route)

        exit_ip = await _get_ip(context)

        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto("https://www.google.com/",
                            wait_until="domcontentloaded", timeout=30_000)
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            resp = await page.goto(search_url, wait_until="domcontentloaded",
                                   timeout=30_000)
            try:
                await page.wait_for_selector("h3", timeout=5_000)
            except Exception:
                pass

            if variant == "B":
                # 1. Random dwell (real users read the SERP).
                await asyncio.sleep(random.uniform(3.0, 7.0))
                # 2. Scroll — real signal. Google's on-page telemetry
                #    logs scroll events regardless of event.isTrusted.
                try:
                    await page.mouse.wheel(0, random.randint(300, 700))
                    await asyncio.sleep(random.uniform(0.8, 1.6))
                    await page.mouse.wheel(0, random.randint(200, 500))
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                except Exception:
                    pass
                # 3. Fake mouse moves — mostly theatre (isTrusted=false)
                #    but noise beats silence at zero cost.
                try:
                    for _ in range(random.randint(1, 2)):
                        x = random.randint(200, 1000)
                        y = random.randint(200, 700)
                        await page.mouse.move(x, y, steps=random.randint(8, 20))
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                except Exception:
                    pass
            else:
                await asyncio.sleep(0.5)  # matches production dwell

            html = await page.content()
            final_url = page.url
            return Result(
                idx=idx, variant=variant, query=query, exit_ip=exit_ip,
                final_url=final_url,
                status=resp.status if resp else 0,
                h3_count=len(re.findall(r"<h3", html, re.I)),
                enablejs="/httpservice/retry/enablejs" in html,
                sorry="/sorry/" in (final_url or "").lower(),
                latency_ms=int((time.perf_counter() - t0) * 1000),
                total_bytes=total_bytes,
            )
        finally:
            await page.close()
            await context.close()
    finally:
        await browser.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10000, help="Decodo port. 10000=rotating.")
    ap.add_argument("--n", type=int, default=len(KEYWORDS))
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    random.seed(args.seed)

    proxy = load_decodo(args.port)
    if not proxy:
        print("(!) No Decodo row in DB.", file=sys.stderr)
        return 2

    keywords = KEYWORDS[: args.n]
    # Interleave: A, B, A, B, ... — keeps pool-state drift symmetric.
    schedule = [("A" if i % 2 == 0 else "B", kw) for i, kw in enumerate(keywords)]

    print(f"Humanize A/B — Decodo :{args.port} — {len(schedule)} queries interleaved\n")

    results: list[Result] = []
    async with async_playwright() as pw:
        for i, (variant, kw) in enumerate(schedule, 1):
            # Random pacing between queries. First query fires immediately.
            if i > 1:
                if variant == "B":
                    gap = random.uniform(15, 45)
                else:
                    gap = random.uniform(15, 45)  # even A gets random pacing;
                    # otherwise "faster A" would confound with "less humanized A".
                    # This isolates the *behavior* change, not the pacing.
                print(f"  ...pacing {gap:.1f}s")
                await asyncio.sleep(gap)

            try:
                r = await run_one(pw, proxy, kw, variant, i)
                tag = "SORRY" if r.sorry else ("ENABLEJS" if r.enablejs else
                                                "OK" if r.h3_count > 0 else "NO_H3")
                print(f"  [{variant} q{i}] {kw!r:<28} ip={r.exit_ip or '?':<15} "
                      f"h3={r.h3_count:>2} bytes={r.total_bytes/1024:>6.0f}KB "
                      f"lat={r.latency_ms:>5}ms → {tag}")
                results.append(r)
            except Exception as e:
                print(f"  [{variant} q{i}] ERR: {e}")
                results.append(Result(
                    idx=i, variant=variant, query=kw, exit_ip=None,
                    final_url="", status=0, h3_count=0, sorry=False, enablejs=False,
                    latency_ms=0, total_bytes=0, error=f"{type(e).__name__}: {e}"[:200],
                ))

    a = [r for r in results if r.variant == "A"]
    b = [r for r in results if r.variant == "B"]
    a_pass = sum(1 for r in a if r.h3_count > 0 and not r.sorry)
    b_pass = sum(1 for r in b if r.h3_count > 0 and not r.sorry)
    a_bytes = sum(r.total_bytes for r in a) / max(len(a), 1)
    b_bytes = sum(r.total_bytes for r in b) / max(len(b), 1)

    print("\n=== A/B summary ===")
    print(f"  A (baseline):   {a_pass}/{len(a)} pass  avg={a_bytes/1024:.0f} KB")
    print(f"  B (humanized):  {b_pass}/{len(b)} pass  avg={b_bytes/1024:.0f} KB")
    delta = b_pass - a_pass
    print(f"  Delta:          {'+' if delta >= 0 else ''}{delta} passes  "
          f"({'humanize helps' if delta > 0 else 'humanize hurts' if delta < 0 else 'no effect'})")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = log_dir / f"humanize_check_decodo{args.port}_{ts}.json"
    p.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nLog: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
