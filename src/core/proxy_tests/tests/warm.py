"""Warm Efficiency — Test 4 per session 4.4.

Ports the W1/W2/W3 variants of `scripts/probes/vary_warmth.py` to the
dashboard. One `run_warm_sync()` call = one sticky IP session = three
`proxy_test_runs` rows (one per preset):

    W1 -> preset='cache_only'   (context reused, cookies wiped per query)
    W2 -> preset='cookie_only'  (fresh context per query, preload donor cookies)
    W3 -> preset='sticky_warm'  (context reused, nothing cleared)

Interleaved keyword-major schedule so each preset sees the same 10 queries
at similar difficulty. Retry-off — warm is about steady-state cost.

Reference: `scripts/probes/vary_warmth.py`. Do NOT edit the probe (per
`feedback_probe_scripts_untouched`); the port must match its numbers
within +/-10% on the same day.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Optional

from playwright.async_api import async_playwright

from src.core.proxy_tests.browsers import (
    ENGINE_CHROMIUM_STEALTH,
    launch_browser,
    new_stealth_context,
)
from src.core.proxy_tests.geoip import lookup_country
from src.core.proxy_tests.providers import load_provider, pw_proxy_dict
from src.core.proxy_tests.route_rules import TIER_DEFAULT, make_route_handler
from src.core.proxy_tests.store import (
    add_query,
    create_run,
    finalize_run,
    get_run,
)

DEFAULT_DB = "open_serp_checker.db"
IPIFY_URL = "https://api.ipify.org?format=json"

# Match `scripts/probes/vary_warmth.py` verbatim so the validation gate is trivial.
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

STICKY_DURATION_MIN = 10
PACING_MIN_S = 15.0
PACING_MAX_S = 45.0
FETCH_TIMEOUT_MS = 30_000
WARMUP_TIMEOUT_MS = 30_000
W2_REFRESH_EVERY = 5

VARIANTS = ("W1", "W2", "W3")
PRESET_OF = {"W1": "cache_only", "W2": "cookie_only", "W3": "sticky_warm"}


# ---------- shared Playwright primitives (ported from probe) ----------

async def _get_ip(context) -> Optional[str]:
    page = await context.new_page()
    try:
        r = await page.goto(IPIFY_URL, wait_until="domcontentloaded", timeout=10_000)
        if r and r.status == 200:
            m = re.search(r'"ip"\s*:\s*"([^"]+)"', await page.content())
            return m.group(1) if m else None
    except Exception:
        return None
    finally:
        await page.close()
    return None


async def _open_browser_context(pw, pw_proxy: dict, engine: str, tier: str):
    browser = await launch_browser(pw, engine, pw_proxy=pw_proxy)
    context = await new_stealth_context(browser, engine, pw_proxy=pw_proxy)
    await context.route("**/*", make_route_handler(tier))
    return browser, context


async def _open_context_on(browser, pw_proxy: dict, engine: str, tier: str):
    context = await new_stealth_context(browser, engine, pw_proxy=pw_proxy)
    await context.route("**/*", make_route_handler(tier))
    return context


async def _do_warmup(context, retries: int = 2) -> tuple[bool, int]:
    """Visit https://www.google.com/ on a dedicated page. Retry once on timeout.
    Returns (success, total_bytes)."""
    warmup_bytes = 0

    def on_response(resp):
        nonlocal warmup_bytes
        try:
            cl = resp.headers.get("content-length")
            if cl:
                warmup_bytes += int(cl)
        except Exception:
            pass

    for attempt in range(retries):
        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto(
                "https://www.google.com/",
                wait_until="domcontentloaded",
                timeout=WARMUP_TIMEOUT_MS,
            )
            await page.close()
            return True, warmup_bytes
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            if attempt < retries - 1:
                await asyncio.sleep(5.0)
    return False, warmup_bytes


async def _do_search_only(context, query: str) -> dict:
    """SERP fetch only — no warmup. wait_until='commit', /sorry/ URL bail,
    post-DCL re-check, h3 timeout 3s. Bytes = sum of response content-length."""
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
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        try:
            resp = await page.goto(
                search_url, wait_until="commit", timeout=FETCH_TIMEOUT_MS,
            )
        except Exception as e:
            return {
                "exit_ip": exit_ip, "http_status": 0, "search_bytes": total_bytes,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "passed": False, "blocked_reason": "timeout",
                "h3_count": 0, "final_url": "",
                "error": f"search: {type(e).__name__}: {e}"[:200],
            }

        final_url = page.url or ""
        status = resp.status if resp else 0

        if "/sorry/" in final_url.lower():
            return {
                "exit_ip": exit_ip, "http_status": status, "search_bytes": total_bytes,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "passed": False, "blocked_reason": "sorry",
                "h3_count": 0, "final_url": final_url,
            }

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        final_url = page.url or final_url

        if "/sorry/" in final_url.lower():
            return {
                "exit_ip": exit_ip, "http_status": status, "search_bytes": total_bytes,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "passed": False, "blocked_reason": "sorry",
                "h3_count": 0, "final_url": final_url,
            }

        try:
            await page.wait_for_selector("h3", timeout=3_000)
        except Exception:
            pass
        await asyncio.sleep(0.5)

        html = await page.content()
        final_url = page.url
        h3 = len(re.findall(r"<h3", html, re.I))
        enablejs = "/httpservice/retry/enablejs" in html

        if enablejs and h3 == 0:
            reason = "enablejs"
        elif h3 == 0:
            reason = "no_h3"
        else:
            reason = None

        passed = reason is None and status == 200

        return {
            "exit_ip": exit_ip, "http_status": status, "search_bytes": total_bytes,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "passed": passed, "blocked_reason": reason,
            "h3_count": h3, "final_url": final_url,
        }
    finally:
        await page.close()


def _warmup_failed_result() -> dict:
    return {
        "exit_ip": None, "http_status": 0, "search_bytes": 0,
        "latency_ms": 0, "passed": False, "blocked_reason": "timeout",
        "h3_count": 0, "final_url": "",
        "error": "warmup failed after retry",
    }


# ---------- W1/W2/W3 runners (state machines, ported from probe) ----------

async def _ensure_ctx(pw, pw_proxy: dict, state: dict) -> tuple[bool, bool, int]:
    """W1/W3 shared: ensure state['context'] is live and warmed."""
    if state.get("context") is not None and state.get("warmup_ok"):
        return False, True, 0
    if state.get("browser") is not None:
        try:
            await state["browser"].close()
        except Exception:
            pass
    state["browser"], state["context"] = await _open_browser_context(
        pw, pw_proxy, state["engine"], state["tier"],
    )
    ok, wb = await _do_warmup(state["context"])
    state["warmup_ok"] = ok
    if not ok:
        try:
            await state["context"].close()
        except Exception:
            pass
        try:
            await state["browser"].close()
        except Exception:
            pass
        state["browser"] = None
        state["context"] = None
    return True, ok, wb


async def _run_W1(pw, pw_proxy: dict, state: dict, query: str) -> dict:
    """cache_only: shared context, clear_cookies before each search."""
    warmup_this_call, warmup_ok, warmup_bytes = await _ensure_ctx(pw, pw_proxy, state)
    if not warmup_ok:
        r = _warmup_failed_result()
        r.update({
            "variant": "W1",
            "warmup_this_call": warmup_this_call,
            "warmup_bytes": warmup_bytes,
            "cookies_state": "empty",
            "cache_state": "empty",
        })
        return r

    context = state["context"]
    await context.clear_cookies()

    res = await _do_search_only(context, query)
    res.update({
        "variant": "W1",
        "warmup_this_call": warmup_this_call,
        "warmup_bytes": warmup_bytes,
        "cookies_state": "wiped",
        "cache_state": "warm",
    })
    return res


async def _run_W3(pw, pw_proxy: dict, state: dict, query: str) -> dict:
    """sticky_warm: shared context, nothing cleared."""
    warmup_this_call, warmup_ok, warmup_bytes = await _ensure_ctx(pw, pw_proxy, state)
    if not warmup_ok:
        r = _warmup_failed_result()
        r.update({
            "variant": "W3",
            "warmup_this_call": warmup_this_call,
            "warmup_bytes": warmup_bytes,
            "cookies_state": "empty",
            "cache_state": "empty",
        })
        return r

    context = state["context"]
    res = await _do_search_only(context, query)
    res.update({
        "variant": "W3",
        "warmup_this_call": warmup_this_call,
        "warmup_bytes": warmup_bytes,
        "cookies_state": "preserved",
        "cache_state": "warm",
    })
    return res


async def _ensure_donor(pw, pw_proxy: dict, state: dict) -> tuple[bool, bool, int]:
    """W2: ensure donor context exists and holds a cookie snapshot."""
    if state.get("donor_ctx") is not None and state.get("warmup_ok"):
        return False, True, 0
    if state.get("browser") is not None:
        try:
            await state["browser"].close()
        except Exception:
            pass
    state["browser"], state["donor_ctx"] = await _open_browser_context(
        pw, pw_proxy, state["engine"], state["tier"],
    )
    ok, wb = await _do_warmup(state["donor_ctx"])
    state["warmup_ok"] = ok
    if not ok:
        try:
            await state["donor_ctx"].close()
        except Exception:
            pass
        try:
            await state["browser"].close()
        except Exception:
            pass
        state["browser"] = None
        state["donor_ctx"] = None
        state["donor_cookies"] = []
        state["queries_since_refresh"] = 0
        return True, False, wb
    state["donor_cookies"] = await state["donor_ctx"].cookies()
    state["queries_since_refresh"] = 0
    return True, True, wb


async def _refresh_donor(state: dict) -> tuple[bool, int]:
    ok, wb = await _do_warmup(state["donor_ctx"])
    if ok:
        state["donor_cookies"] = await state["donor_ctx"].cookies()
        state["queries_since_refresh"] = 0
    return ok, wb


async def _run_W2(pw, pw_proxy: dict, state: dict, query: str) -> dict:
    """cookie_only: fresh context per query on shared browser, preload donor cookies."""
    warmup_this_call, warmup_ok, warmup_bytes = await _ensure_donor(pw, pw_proxy, state)
    if not warmup_ok:
        r = _warmup_failed_result()
        r.update({
            "variant": "W2",
            "warmup_this_call": warmup_this_call,
            "warmup_bytes": warmup_bytes,
            "cookies_state": "empty",
            "cache_state": "empty",
        })
        return r

    if (not warmup_this_call
            and state.get("queries_since_refresh", 0) >= W2_REFRESH_EVERY):
        ok, extra = await _refresh_donor(state)
        if ok:
            warmup_this_call = True
            warmup_bytes += extra

    context = await _open_context_on(
        state["browser"], pw_proxy, state["engine"], state["tier"],
    )
    try:
        cookies_to_load = state.get("donor_cookies") or []
        if cookies_to_load:
            await context.add_cookies(cookies_to_load)

        res = await _do_search_only(context, query)
        res.update({
            "variant": "W2",
            "warmup_this_call": warmup_this_call,
            "warmup_bytes": warmup_bytes,
            "cookies_state": "preloaded",
            "cache_state": "empty",
            "donor_cookies_loaded": len(cookies_to_load),
        })
    finally:
        try:
            await context.close()
        except Exception:
            pass

    state["queries_since_refresh"] = state.get("queries_since_refresh", 0) + 1
    return res


# ---------- orchestration ----------

async def _execute(
    run_ids: dict[str, int],
    creds: dict,
    db_path: str,
    keywords: list[str],
    sticky_min: int,
    country: Optional[str] = None,
    engine: str = ENGINE_CHROMIUM_STEALTH,
    tier: str = TIER_DEFAULT,
) -> None:
    pw_proxy = pw_proxy_dict(
        creds, sticky=True, sticky_duration_min=sticky_min, country=country,
    )

    w1_state: dict = {"browser": None, "context": None, "warmup_ok": False,
                      "engine": engine, "tier": tier}
    w2_state: dict = {
        "browser": None, "donor_ctx": None, "warmup_ok": False,
        "donor_cookies": [], "queries_since_refresh": 0,
        "engine": engine, "tier": tier,
    }
    w3_state: dict = {"browser": None, "context": None, "warmup_ok": False,
                      "engine": engine, "tier": tier}

    first_ip_by_run: dict[int, Optional[str]] = {}

    async with async_playwright() as pw:
        try:
            i = 0
            for kw in keywords:
                for variant in VARIANTS:
                    i += 1
                    if i > 1:
                        gap = random.uniform(PACING_MIN_S, PACING_MAX_S)
                        await asyncio.sleep(gap)

                    preset = PRESET_OF[variant]
                    run_id = run_ids[preset]

                    try:
                        if variant == "W1":
                            res = await _run_W1(pw, pw_proxy, w1_state, kw)
                        elif variant == "W2":
                            res = await _run_W2(pw, pw_proxy, w2_state, kw)
                        else:
                            res = await _run_W3(pw, pw_proxy, w3_state, kw)
                    except Exception as e:
                        res = {
                            "variant": variant,
                            "exit_ip": None, "http_status": 0,
                            "search_bytes": 0, "latency_ms": 0,
                            "passed": False, "blocked_reason": "timeout",
                            "h3_count": 0, "final_url": "",
                            "warmup_this_call": False, "warmup_bytes": 0,
                            "cookies_state": "empty", "cache_state": "empty",
                            "error": f"{type(e).__name__}: {e}"[:200],
                        }

                    exit_ip = res.get("exit_ip")
                    if run_id not in first_ip_by_run:
                        first_ip_by_run[run_id] = exit_ip
                    first_ip = first_ip_by_run[run_id]
                    if exit_ip is None or first_ip is None:
                        sticky_held = None
                    else:
                        sticky_held = 1 if exit_ip == first_ip else 0

                    warmup_bytes = res.get("warmup_bytes", 0)
                    search_bytes = res.get("search_bytes", 0)
                    total_bytes = warmup_bytes + search_bytes

                    exit_country = await asyncio.to_thread(
                        lookup_country, exit_ip
                    )

                    add_query(
                        run_id, db_path=db_path,
                        keyword=kw,
                        exit_ip=exit_ip,
                        exit_country=exit_country,
                        sticky_ip_held=sticky_held,
                        http_status=res.get("http_status", 0),
                        got_429=1 if res.get("http_status") == 429 else 0,
                        bytes_wire=total_bytes,
                        latency_ms=res.get("latency_ms", 0),
                        passed=1 if res.get("passed") else 0,
                        blocked_reason=res.get("blocked_reason"),
                        retry_attempted=0,
                        retry_passed=None,
                        raw_json={
                            "variant": variant,
                            "warmup_this_call": res.get("warmup_this_call", False),
                            "warmup_bytes": warmup_bytes,
                            "search_bytes": search_bytes,
                            "cookies_state": res.get("cookies_state"),
                            "cache_state": res.get("cache_state"),
                            "final_url": res.get("final_url", ""),
                            "h3_count": res.get("h3_count", 0),
                            "donor_cookies_loaded": res.get("donor_cookies_loaded"),
                            "error": res.get("error"),
                        },
                    )
        finally:
            for st in (w1_state, w2_state, w3_state):
                br = st.get("browser")
                if br is not None:
                    try:
                        await br.close()
                    except Exception:
                        pass


def _aggregate_preset(
    run_id: int, preset: str, db_path: str,
    requested_country: Optional[str] = None,
) -> dict:
    data = get_run(run_id, db_path=db_path)
    queries = data["queries"] if data else []
    n = len(queries)

    passes = sum(1 for q in queries if q.get("passed"))
    pass_rate = round(passes / n, 3) if n else 0.0

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

    avg_kb_success = _mean_kb(success_bytes)
    avg_kb_blocked = _mean_kb(blocked_bytes)

    first_query_kb = (
        round(queries[0]["bytes_wire"] / 1024, 1)
        if queries and queries[0].get("bytes_wire") is not None
        else None
    )
    subsequent_bytes = [
        q["bytes_wire"] for q in queries[1:]
        if q.get("bytes_wire") is not None
    ]
    subsequent_avg_kb = _mean_kb(subsequent_bytes)

    unique_ips = sorted({q["exit_ip"] for q in queries if q.get("exit_ip")})
    sticky_held_count = sum(1 for q in queries if q.get("sticky_ip_held") == 1)

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

    sticky_ip_held_pct = round(sticky_held_count / n, 3) if n else None
    got_429_count = sum(1 for q in queries if q.get("got_429"))

    return {
        "preset": preset,
        "n_keywords": n,
        "raw_pass": passes,
        "raw_pass_rate": pass_rate,
        "avg_kb_success": avg_kb_success,
        "avg_kb_blocked": avg_kb_blocked,
        "first_query_kb": first_query_kb,
        "subsequent_avg_kb": subsequent_avg_kb,
        "unique_exit_ips": len(unique_ips),
        "unique_exit_ips_list": unique_ips,
        "sticky_ip_held_count": sticky_held_count,
        "sticky_ip_held_pct": sticky_ip_held_pct,
        "requested_country": requested_country,
        "unique_countries": len(country_breakdown),
        "country_breakdown": country_breakdown,
        "geo_honesty_pct": geo_honesty_pct,
        "got_429_count": got_429_count,
        "top_blocked_reason": top_blocked_reason,
    }


def run_warm_sync(
    provider_id: int,
    matrix_run_id: Optional[str] = None,
    db_path: str = DEFAULT_DB,
    sticky_min: int = STICKY_DURATION_MIN,
    n_keywords: int = len(KEYWORDS),
    country: Optional[str] = None,
    browser_engine: str = ENGINE_CHROMIUM_STEALTH,
    bandwidth_tier: str = TIER_DEFAULT,
) -> list[int]:
    creds = load_provider(provider_id, db_path)
    presets = ["cache_only", "cookie_only", "sticky_warm"]
    run_ids: dict[str, int] = {}
    for preset in presets:
        run_ids[preset] = create_run(
            provider_id, "warm", preset=preset,
            matrix_run_id=matrix_run_id, browser_engine=browser_engine,
            bandwidth_tier=bandwidth_tier,
            db_path=db_path,
        )
    try:
        asyncio.run(_execute(
            run_ids, creds, db_path,
            keywords=KEYWORDS[:n_keywords],
            sticky_min=sticky_min,
            country=country,
            engine=browser_engine,
            tier=bandwidth_tier,
        ))
        for preset, rid in run_ids.items():
            summary = _aggregate_preset(
                rid, preset, db_path, requested_country=country,
            )
            finalize_run(rid, "done", summary=summary, db_path=db_path)
    except Exception as e:
        for rid in run_ids.values():
            finalize_run(
                rid, "error",
                notes=f"{type(e).__name__}: {e}"[:500],
                db_path=db_path,
            )
        raise
    return list(run_ids.values())
