"""Pass Rate + Cost — Test 2+3 per session 4.3, rewired in 4.10.

Defaults are the validated winning recipe: camoufox + doconly + home
flow + persistent browser lifecycle. Rotating gateway (Decodo :10000,
bare username) still rotates the exit IP per HTTP connection — the
"measure the pool" semantic is preserved because each ipify call and
each google.com request opens its own connection to the gateway. What
we drop is the per-query BROWSER respawn, which was reading to Google
as a bot swarm (see project_lifecycle_beats_everything.md).

Retry-once-on-block now runs on the SAME context (no browser respawn).
A human doesn't relaunch their browser to retry a search.

Reference: `scripts/probes/sticky_cold_vs_warm.py` (variant A) for the
original shape; `google-scrape/root-test.py:run_camoufox` for the
persistent-lifecycle pattern this now matches. Do NOT edit the probes.
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
from src.core.proxy_tests.providers import load_provider, pw_proxy_dict
from src.core.proxy_tests.route_rules import (
    TIER_DEFAULT, TIER_DOCONLY, make_route_handler,
)
from src.core.proxy_tests.store import (
    add_query,
    create_run,
    finalize_run,
    get_run,
)

DEFAULT_DB = "serp_scraper.db"
IPIFY_URL = "https://api.ipify.org?format=json"

# Match the probe (`scripts/probes/sticky_cold_vs_warm.py`) verbatim so the
# validation gate is trivial.
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

PACING_MIN_S = 15.0
PACING_MAX_S = 45.0
FETCH_TIMEOUT_MS = 30_000
H3_PASS_BAR = 5  # 4.10: match google-scrape — h3>=5 filters ad-only stubs
SERP_WAIT_MS = 15_000


class _AttemptResult(dict):
    """Loose dict holder — no dataclass needed for a single-use struct."""


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


async def _open_browser(pw, pw_proxy: dict, engine: str, tier: str):
    browser = await launch_browser(pw, engine, pw_proxy=pw_proxy)
    context = await new_stealth_context(browser, engine, pw_proxy=pw_proxy)
    await context.route("**/*", make_route_handler(tier))
    return browser, context


async def _do_search(context, query: str, flow: str = FLOW_HOME) -> _AttemptResult:
    """One search on the given context via `initiate_search`. Bytes counted
    via `response` event: sum of content-length headers. Exit IP captured
    per-query via ipify (best-effort — rotating gateway may serve a
    different IP for the ipify connection than for the google.com one).
    """
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

    exit_ip = await _get_ip(context)
    page = await context.new_page()
    page.on("response", on_response)
    try:
        nav = await initiate_search(
            page, context, query, flow=flow, timeout_ms=FETCH_TIMEOUT_MS,
        )
        if not nav["ok"]:
            return _AttemptResult(
                exit_ip=exit_ip, http_status=nav["http_status"],
                bytes=total_bytes,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                passed=False, blocked_reason="timeout",
                h3_count=0, final_url=nav["final_url"],
                error=nav.get("error"),
            )

        status = nav["http_status"]
        final_url = nav["final_url"]

        if "/sorry/" in final_url.lower():
            return _AttemptResult(
                exit_ip=exit_ip, http_status=status, bytes=total_bytes,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                passed=False, blocked_reason="sorry",
                h3_count=0, final_url=final_url,
            )

        h3 = await wait_for_serp(page, timeout_ms=SERP_WAIT_MS,
                                  h3_bar=H3_PASS_BAR)
        final_url = page.url or final_url

        if "/sorry/" in final_url.lower():
            return _AttemptResult(
                exit_ip=exit_ip, http_status=status, bytes=total_bytes,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                passed=False, blocked_reason="sorry",
                h3_count=h3, final_url=final_url,
            )

        html = await page.content()
        enablejs = "/httpservice/retry/enablejs" in html

        if enablejs and h3 < H3_PASS_BAR:
            reason = "enablejs"
        elif h3 < H3_PASS_BAR:
            reason = "no_h3"
        else:
            reason = None

        passed = reason is None and status == 200

        return _AttemptResult(
            exit_ip=exit_ip, http_status=status, bytes=total_bytes,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            passed=passed, blocked_reason=reason,
            h3_count=h3, final_url=final_url,
        )
    finally:
        await page.close()


async def _run_one_keyword(context, query: str, flow: str) -> dict:
    """Search on the shared context. Retry-once-on-block on the SAME
    context — rotating gateway rotates IP per HTTP connection, so a
    second attempt naturally hits a different exit IP without spawning
    a new browser. That's the "measure the pool" semantic preserved
    with human-shaped traffic.
    """
    initial = await _do_search(context, query, flow=flow)
    retry: Optional[_AttemptResult] = None
    if not initial["passed"]:
        await asyncio.sleep(random.uniform(2.0, 4.0))
        retry = await _do_search(context, query, flow=flow)
    return {"initial": initial, "retry": retry}


async def _execute(
    run_id: int, creds: dict, db_path: str, country: Optional[str] = None,
    engine: str = ENGINE_CAMOUFOX,
    tier: str = TIER_DOCONLY,
    flow: str = FLOW_HOME,
    n_keywords: int = len(KEYWORDS),
) -> None:
    # 4.10 persistent lifecycle: one browser+context reused across all N
    # keywords. Rotating pool still rotates IP per HTTP connection.
    pw_proxy = pw_proxy_dict(creds, sticky=False, country=country)

    async with async_playwright() as pw:
        browser, context = await _open_browser(pw, pw_proxy, engine, tier)
        try:
            for i, kw in enumerate(KEYWORDS[:n_keywords], 1):
                if i > 1:
                    gap = random.uniform(PACING_MIN_S, PACING_MAX_S)
                    await asyncio.sleep(gap)

                try:
                    pair = await _run_one_keyword(context, kw, flow)
                except Exception as e:
                    add_query(
                        run_id, db_path=db_path,
                        keyword=kw,
                        exit_ip=None,
                        exit_country=None,
                        sticky_ip_held=None,
                        http_status=0,
                        got_429=0,
                        bytes_wire=0,
                        latency_ms=0,
                        passed=0,
                        blocked_reason="timeout",
                        retry_attempted=0,
                        retry_passed=None,
                        raw_json={
                            "error": f"{type(e).__name__}: {e}"[:400],
                        },
                    )
                    continue

                initial = pair["initial"]
                retry = pair["retry"]
                retry_attempted = 1 if retry is not None else 0
                retry_passed: Optional[int]
                if retry is None:
                    retry_passed = None
                else:
                    retry_passed = 1 if retry["passed"] else 0

                raw = {
                    "initial": {
                        "exit_ip": initial["exit_ip"],
                        "http_status": initial["http_status"],
                        "bytes": initial["bytes"],
                        "h3_count": initial["h3_count"],
                        "final_url": initial.get("final_url", ""),
                        "passed": bool(initial["passed"]),
                        "blocked_reason": initial.get("blocked_reason"),
                        "error": initial.get("error"),
                    },
                    "retry": None if retry is None else {
                        "exit_ip": retry["exit_ip"],
                        "http_status": retry["http_status"],
                        "bytes": retry["bytes"],
                        "h3_count": retry["h3_count"],
                        "final_url": retry.get("final_url", ""),
                        "passed": bool(retry["passed"]),
                        "blocked_reason": retry.get("blocked_reason"),
                        "error": retry.get("error"),
                    },
                    "final_ok": bool(initial["passed"]) or bool(retry and retry["passed"]),
                }

                exit_country = await asyncio.to_thread(
                    lookup_country, initial["exit_ip"]
                )
                add_query(
                    run_id, db_path=db_path,
                    keyword=kw,
                    exit_ip=initial["exit_ip"],
                    exit_country=exit_country,
                    sticky_ip_held=None,
                    http_status=initial["http_status"],
                    got_429=1 if initial["http_status"] == 429 else 0,
                    bytes_wire=initial["bytes"],
                    latency_ms=initial["latency_ms"],
                    passed=1 if initial["passed"] else 0,
                    blocked_reason=initial.get("blocked_reason"),
                    retry_attempted=retry_attempted,
                    retry_passed=retry_passed,
                    raw_json=raw,
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


def _aggregate(
    run_id: int, db_path: str, requested_country: Optional[str] = None,
) -> dict:
    data = get_run(run_id, db_path=db_path)
    queries = data["queries"] if data else []

    n = len(queries)
    raw_pass = sum(1 for q in queries if q.get("passed"))
    retry_conv = sum(
        1 for q in queries
        if q.get("passed") or (q.get("retry_attempted") and q.get("retry_passed"))
    )
    retry_attempts = sum(1 for q in queries if q.get("retry_attempted"))

    success_bytes = [
        q["bytes_wire"] for q in queries
        if q.get("passed") and q.get("bytes_wire") is not None
    ]
    blocked_bytes = [
        q["bytes_wire"] for q in queries
        if not q.get("passed") and q.get("bytes_wire") is not None
    ]

    retry_success_bytes: list[int] = []
    for q in queries:
        raw = q.get("raw_json")
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if not raw or not isinstance(raw, dict):
            continue
        retry = raw.get("retry")
        if retry and retry.get("passed") and retry.get("bytes") is not None:
            retry_success_bytes.append(retry["bytes"])

    unique_ips = sorted({q["exit_ip"] for q in queries if q.get("exit_ip")})

    def _mean(xs: list[int]) -> Optional[float]:
        return round(sum(xs) / len(xs) / 1024, 1) if xs else None

    avg_kb_success = _mean(success_bytes)
    avg_kb_blocked = _mean(blocked_bytes)
    avg_kb_retry_success = _mean(retry_success_bytes)

    raw_pass_rate = round(raw_pass / n, 3) if n else 0.0
    retry_pass_rate = round(retry_conv / n, 3) if n else 0.0

    if raw_pass_rate > 0 and avg_kb_success is not None and avg_kb_blocked is not None:
        effective_kb_per_success = round(
            avg_kb_success
            + ((1 - raw_pass_rate) / raw_pass_rate) * avg_kb_blocked,
            1,
        )
    else:
        effective_kb_per_success = None

    blocked_reasons = [
        q["blocked_reason"] for q in queries if q.get("blocked_reason")
    ]
    top_blocked_reason = None
    if blocked_reasons:
        counts: dict[str, int] = {}
        for r in blocked_reasons:
            counts[r] = counts.get(r, 0) + 1
        top_blocked_reason = max(counts, key=counts.get)

    country_breakdown: dict[str, int] = {}
    for q in queries:
        cc = q.get("exit_country")
        if cc:
            country_breakdown[cc] = country_breakdown.get(cc, 0) + 1
    total_with_country = sum(country_breakdown.values())
    if requested_country and total_with_country:
        matched = country_breakdown.get(requested_country, 0)
        geo_honesty_pct = round(matched / total_with_country, 3)
    else:
        geo_honesty_pct = None

    got_429_count = sum(1 for q in queries if q.get("got_429"))

    return {
        "preset": "cold",
        "n_keywords": n,
        "raw_pass": raw_pass,
        "raw_pass_rate": raw_pass_rate,
        "retry_converted_pass": retry_conv,
        "retry_converted_pass_rate": retry_pass_rate,
        "retry_attempts": retry_attempts,
        "avg_kb_success": avg_kb_success,
        "avg_kb_blocked": avg_kb_blocked,
        "avg_kb_retry_success": avg_kb_retry_success,
        "effective_kb_per_success": effective_kb_per_success,
        "unique_exit_ips": len(unique_ips),
        "unique_exit_ips_list": unique_ips,
        "requested_country": requested_country,
        "unique_countries": len(country_breakdown),
        "country_breakdown": country_breakdown,
        "geo_honesty_pct": geo_honesty_pct,
        "got_429_count": got_429_count,
        "top_blocked_reason": top_blocked_reason,
    }


def run_pass_rate_sync(
    provider_id: int,
    matrix_run_id: Optional[str] = None,
    db_path: str = DEFAULT_DB,
    country: Optional[str] = None,
    browser_engine: str = ENGINE_CAMOUFOX,
    bandwidth_tier: str = TIER_DOCONLY,
    flow: str = FLOW_HOME,
    n_keywords: int = len(KEYWORDS),
) -> int:
    creds = load_provider(provider_id, db_path)
    run_id = create_run(
        provider_id, "pass_rate", preset="cold",
        matrix_run_id=matrix_run_id, browser_engine=browser_engine,
        bandwidth_tier=bandwidth_tier, flow=flow,
        db_path=db_path,
    )
    try:
        asyncio.run(_execute(
            run_id, creds, db_path, country=country, engine=browser_engine,
            tier=bandwidth_tier, flow=flow, n_keywords=n_keywords,
        ))
        summary = _aggregate(run_id, db_path, requested_country=country)
        finalize_run(run_id, "done", summary=summary, db_path=db_path)
    except Exception as e:
        finalize_run(
            run_id, "error",
            notes=f"{type(e).__name__}: {e}"[:500],
            db_path=db_path,
        )
        raise
    return run_id
