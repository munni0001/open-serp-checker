"""Local Baseline (4.9) — script-only control run against Google, no proxy.

Purpose: prove the production stealth recipe still looks non-automated to
Google from a clean home IP before we spend proxy bandwidth on a matrix.

- 5 keywords (subset of the pass_rate list), same stealth args/UA/init JS.
- No proxy, no retry, no country.
- Pass threshold = >= 3 of 5 keywords pass.
- Exit IP + country resolved once (they don't change during a run).
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Optional

from playwright.async_api import async_playwright

from src.core.proxy_tests.browsers import (
    ENGINE_CAMOUFOX,
    ENGINE_CHROMIUM_STEALTH,
    launch_browser,
    new_stealth_context,
)
from src.core.proxy_tests.flows import (
    FLOW_DIRECT, FLOW_HOME, initiate_search, wait_for_serp,
)
from src.core.proxy_tests.geoip import lookup_country
from src.core.proxy_tests.route_rules import (
    TIER_DEFAULT, TIER_DOCONLY, make_route_handler,
)
from src.core.proxy_tests.store import (
    add_query,
    create_run,
    finalize_run,
    get_run,
)

DEFAULT_DB = "open_serp_checker.db"
IPIFY_URL = "https://api.ipify.org?format=json"

# First 5 from pass_rate.KEYWORDS so numbers are directly comparable.
KEYWORDS = [
    "foxy ai promo code",
    "heygen promo code",
    "apify promo code",
    "webshare coupon code",
    "gamestop promo code",
]

PASS_THRESHOLD = 3  # >= 3 of 5 keywords pass → baseline green
H3_PASS_BAR = 5     # 4.10: match google-scrape — h3>=5 filters ad-only/stub pages

PACING_MIN_S = 15.0
PACING_MAX_S = 45.0
FETCH_TIMEOUT_MS = 30_000
SERP_WAIT_MS = 15_000  # poll for h3>=5, longer than old 3s wait_for_selector


async def _get_ip(context) -> Optional[str]:
    page = await context.new_page()
    try:
        r = await page.goto(
            IPIFY_URL, wait_until="domcontentloaded", timeout=10_000
        )
        if r and r.status == 200:
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', await page.content())
            return m.group(1) if m else None
    except Exception:
        return None
    finally:
        await page.close()
    return None


async def _open_browser(pw, engine: str, tier: str):
    browser = await launch_browser(pw, engine, pw_proxy=None)
    context = await new_stealth_context(browser, engine, pw_proxy=None)
    await context.route("**/*", make_route_handler(tier))
    return browser, context


async def _do_search(context, query: str, flow: str = FLOW_DIRECT) -> dict:
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

    page = await context.new_page()
    page.on("response", on_response)
    try:
        nav = await initiate_search(
            page, context, query, flow=flow, timeout_ms=FETCH_TIMEOUT_MS,
        )
        if not nav["ok"]:
            return {
                "http_status": nav["http_status"], "bytes": total_bytes,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "passed": False, "blocked_reason": "timeout",
                "h3_count": 0, "final_url": nav["final_url"],
                "error": nav.get("error"),
            }

        status = nav["http_status"]
        final_url = nav["final_url"]

        if "/sorry/" in final_url.lower():
            return {
                "http_status": status, "bytes": total_bytes,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "passed": False, "blocked_reason": "sorry",
                "h3_count": 0, "final_url": final_url,
            }

        h3 = await wait_for_serp(page, timeout_ms=SERP_WAIT_MS,
                                  h3_bar=H3_PASS_BAR)
        final_url = page.url or final_url

        if "/sorry/" in final_url.lower():
            return {
                "http_status": status, "bytes": total_bytes,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "passed": False, "blocked_reason": "sorry",
                "h3_count": h3, "final_url": final_url,
            }

        html = await page.content()
        enablejs = "/httpservice/retry/enablejs" in html

        if enablejs and h3 < H3_PASS_BAR:
            reason = "enablejs"
        elif h3 < H3_PASS_BAR:
            reason = "no_h3"
        else:
            reason = None

        passed = reason is None and status == 200

        return {
            "http_status": status, "bytes": total_bytes,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "passed": passed, "blocked_reason": reason,
            "h3_count": h3, "final_url": final_url,
        }
    finally:
        await page.close()


async def _execute(run_id: int, db_path: str, exit_ip: Optional[str],
                   exit_country: Optional[str],
                   engine: str = ENGINE_CHROMIUM_STEALTH,
                   tier: str = TIER_DEFAULT,
                   flow: str = FLOW_DIRECT) -> None:
    # 4.10 lifecycle fix: reuse one browser+context across ALL queries so
    # google.com sees one browser session doing multiple searches — not N
    # freshly-spawned browsers hitting from the same IP in 3-4 minutes,
    # which is textbook block-detection bait. Matches google-scrape's
    # `run_camoufox` shape. (pass_rate/warm keep per-query lifecycle;
    # they measure rotating-pool behavior where the fresh-context reset
    # is the point of the test.)
    async with async_playwright() as pw:
        browser, context = await _open_browser(pw, engine, tier)
        try:
            for i, kw in enumerate(KEYWORDS, 1):
                if i > 1:
                    await asyncio.sleep(
                        random.uniform(PACING_MIN_S, PACING_MAX_S)
                    )

                try:
                    result = await _do_search(context, kw, flow=flow)
                except Exception as e:
                    result = {
                        "http_status": 0, "bytes": 0,
                        "latency_ms": 0, "passed": False,
                        "blocked_reason": "timeout",
                        "h3_count": 0, "final_url": "",
                        "error": f"{type(e).__name__}: {e}"[:200],
                    }

                add_query(
                    run_id, db_path=db_path,
                    keyword=kw,
                    exit_ip=exit_ip,
                    exit_country=exit_country,
                    sticky_ip_held=None,
                    http_status=result["http_status"],
                    got_429=1 if result["http_status"] == 429 else 0,
                    bytes_wire=result["bytes"],
                    latency_ms=result["latency_ms"],
                    passed=1 if result["passed"] else 0,
                    blocked_reason=result.get("blocked_reason"),
                    retry_attempted=0,
                    retry_passed=None,
                    raw_json={
                        "http_status": result["http_status"],
                        "bytes": result["bytes"],
                        "h3_count": result["h3_count"],
                        "final_url": result.get("final_url", ""),
                        "passed": bool(result["passed"]),
                        "blocked_reason": result.get("blocked_reason"),
                        "error": result.get("error"),
                    },
                )
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


async def _preflight_ip(engine: str = ENGINE_CHROMIUM_STEALTH,
                        ) -> tuple[Optional[str], Optional[str]]:
    """Grab home IP + country once before the run — cheap and stable."""
    async with async_playwright() as pw:
        browser = await launch_browser(pw, engine, pw_proxy=None)
        context = await new_stealth_context(browser, engine, pw_proxy=None)
        try:
            ip = await _get_ip(context)
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
    country = None
    if ip:
        country = await asyncio.to_thread(lookup_country, ip)
    return ip, country


def _aggregate(run_id: int, db_path: str) -> dict:
    data = get_run(run_id, db_path=db_path)
    queries = data["queries"] if data else []

    n = len(queries)
    raw_pass = sum(1 for q in queries if q.get("passed"))

    success_bytes = [
        q["bytes_wire"] for q in queries
        if q.get("passed") and q.get("bytes_wire") is not None
    ]
    blocked_bytes = [
        q["bytes_wire"] for q in queries
        if not q.get("passed") and q.get("bytes_wire") is not None
    ]

    def _mean_kb(xs: list[int]) -> Optional[float]:
        return round(sum(xs) / len(xs) / 1024, 1) if xs else None

    blocked_reasons = [q["blocked_reason"] for q in queries if q.get("blocked_reason")]
    top_blocked_reason = None
    if blocked_reasons:
        counts: dict[str, int] = {}
        for r in blocked_reasons:
            counts[r] = counts.get(r, 0) + 1
        top_blocked_reason = max(counts, key=counts.get)

    exit_ip = next((q.get("exit_ip") for q in queries if q.get("exit_ip")), None)
    exit_country = next((q.get("exit_country") for q in queries if q.get("exit_country")), None)

    raw_pass_rate = round(raw_pass / n, 3) if n else 0.0

    return {
        "preset": "cold",
        "n_keywords": n,
        "raw_pass": raw_pass,
        "raw_pass_rate": raw_pass_rate,
        "pass_threshold": PASS_THRESHOLD,
        "baseline_ok": raw_pass >= PASS_THRESHOLD,
        "avg_kb_success": _mean_kb(success_bytes),
        "avg_kb_blocked": _mean_kb(blocked_bytes),
        "top_blocked_reason": top_blocked_reason,
        "exit_ip": exit_ip,
        "exit_country": exit_country,
    }


def run_local_baseline_sync(
    matrix_run_id: Optional[str] = None,
    db_path: str = DEFAULT_DB,
    browser_engine: str = ENGINE_CAMOUFOX,
    bandwidth_tier: str = TIER_DOCONLY,
    flow: str = FLOW_HOME,
) -> tuple[int, bool]:
    """Run the local baseline. Returns (run_id, baseline_ok).

    baseline_ok = True when raw_pass >= PASS_THRESHOLD (3/5).
    Matrix runner uses this to decide whether to proceed with providers.

    4.10 defaults are the validated winning recipe: camoufox + doconly +
    home flow + persistent browser lifecycle (via _execute). On 2026-09-12
    this produced 5/5 at ~60 KB per SERP on a home IP. If the baseline
    doesn't clear the gate under this recipe, the script itself is
    broken — no proxy will save it.

    Callers can override any of these for A/B testing, but the shipping
    matrix should use the defaults so proxy numbers are compared under a
    known-good local reference.
    """
    run_id = create_run(
        None, "local_baseline", preset="cold",
        matrix_run_id=matrix_run_id, browser_engine=browser_engine,
        bandwidth_tier=bandwidth_tier, flow=flow,
        db_path=db_path,
    )
    try:
        exit_ip, exit_country = asyncio.run(_preflight_ip(browser_engine))
        asyncio.run(_execute(
            run_id, db_path, exit_ip, exit_country, engine=browser_engine,
            tier=bandwidth_tier, flow=flow,
        ))
        summary = _aggregate(run_id, db_path)
        finalize_run(run_id, "done", summary=summary, db_path=db_path)
        return run_id, bool(summary["baseline_ok"])
    except Exception as e:
        finalize_run(
            run_id, "error",
            notes=f"{type(e).__name__}: {e}"[:500],
            db_path=db_path,
        )
        raise
