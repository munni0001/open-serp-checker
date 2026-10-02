"""Handshake test — Test 1 per session 4.2.

Runs 4 clients (curl, curl_cffi, chromium, firefox) x 3 attempts each against
https://www.google.com/ through the provider's rotating gateway. Each attempt
draws a fresh IP from the rotating pool. Diagnostic purpose: separates TLS-
fingerprint drops (Chromium hangs) from HTTP-layer reputation blocks (SERP
returns /sorry/).

Owns its own create_run / finalize_run lifecycle. No shared runner extracted
yet — single caller. Do NOT edit scripts/probes/* (reference tier).
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from curl_cffi import requests as cc_requests
from playwright.async_api import async_playwright

from src.core.engines.google import (
    DEFAULT_USER_AGENT as _PROD_UA,
    _BLOCKED_RESOURCE_TYPES as _PROD_BLOCKED_RESOURCES,
    _STEALTH_ARGS as _PROD_STEALTH_ARGS,
    _STEALTH_INIT_JS as _PROD_STEALTH_JS,
)
from src.core.proxy_tests.geoip import lookup_country
from src.core.proxy_tests.providers import (
    load_provider,
    proxy_url as build_proxy_url,
    pw_proxy_dict,
)
from src.core.proxy_tests.store import (
    add_query,
    create_run,
    finalize_run,
    get_run,
)

DEFAULT_DB = "serp_scraper.db"
TARGET_URL = "https://www.google.com/"
IPIFY_URL = "https://api.ipify.org?format=json"
TIMEOUT_S = 45
ATTEMPTS_PER_CLIENT = 3
CLIENTS = ("curl", "curl_cffi", "chromium", "chromium_stealth", "firefox")


# ---------- curl (real binary) ----------

def _curl_attempt(proxy_url: str) -> dict:
    exit_ip = _curl_ipify(proxy_url)
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            [
                "curl", "-sS", "-o", "/dev/null",
                "-w", "%{http_code}|%{size_download}|%{url_effective}",
                "-x", proxy_url,
                "--connect-timeout", "20",
                "--max-time", str(TIMEOUT_S),
                TARGET_URL,
            ],
            capture_output=True, text=True, timeout=TIMEOUT_S + 5,
        )
        latency = int((time.perf_counter() - t0) * 1000)
        if r.returncode != 0:
            reason = "timeout" if "timed out" in (r.stderr or "").lower() else "http_error"
            return {
                "exit_ip": exit_ip, "http_status": 0, "bytes_wire": 0,
                "latency_ms": latency, "passed": 0, "blocked_reason": reason,
                "raw_json": {"stderr": (r.stderr or "")[:400]},
            }
        status_s, size_s, final_url = (r.stdout.strip().split("|", 2) + ["", ""])[:3]
        status = int(status_s or 0)
        size = int(size_s or 0)
        blocked = _classify_block(status, final_url, body="")
        return {
            "exit_ip": exit_ip, "http_status": status, "bytes_wire": size,
            "latency_ms": latency, "passed": 1 if (status == 200 and blocked is None) else 0,
            "blocked_reason": blocked,
            "raw_json": {"final_url": final_url},
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_ip": exit_ip, "http_status": 0, "bytes_wire": 0,
            "latency_ms": TIMEOUT_S * 1000, "passed": 0, "blocked_reason": "timeout",
            "raw_json": {"stderr": "subprocess timeout"},
        }


def _curl_ipify(proxy_url: str) -> str | None:
    try:
        r = subprocess.run(
            ["curl", "-sS", "-x", proxy_url, "--max-time", "15", IPIFY_URL],
            capture_output=True, text=True, timeout=20,
        )
        return _parse_ipify(r.stdout)
    except Exception:
        return None


# ---------- curl_cffi (Chromium ClientHello from Python) ----------

def _curl_cffi_attempt(proxy_url: str) -> dict:
    proxies = {"http": proxy_url, "https": proxy_url}
    exit_ip = _cc_ipify(proxies)
    t0 = time.perf_counter()
    try:
        r = cc_requests.get(
            TARGET_URL,
            impersonate="chrome120",
            proxies=proxies,
            timeout=TIMEOUT_S,
            allow_redirects=True,
        )
        latency = int((time.perf_counter() - t0) * 1000)
        body = r.text or ""
        blocked = _classify_block(r.status_code, str(r.url), body)
        return {
            "exit_ip": exit_ip, "http_status": r.status_code, "bytes_wire": len(r.content or b""),
            "latency_ms": latency,
            "passed": 1 if (r.status_code == 200 and blocked is None) else 0,
            "blocked_reason": blocked,
            "raw_json": {"final_url": str(r.url)},
        }
    except Exception as e:
        latency = int((time.perf_counter() - t0) * 1000)
        reason = "timeout" if "timeout" in str(e).lower() else "http_error"
        return {
            "exit_ip": exit_ip, "http_status": 0, "bytes_wire": 0,
            "latency_ms": latency, "passed": 0, "blocked_reason": reason,
            "raw_json": {"err": f"{type(e).__name__}: {e}"[:400]},
        }


def _cc_ipify(proxies: dict) -> str | None:
    try:
        r = cc_requests.get(IPIFY_URL, impersonate="chrome120", proxies=proxies, timeout=15)
        return _parse_ipify(r.text)
    except Exception:
        return None


# ---------- Playwright: Chromium / Firefox ----------

async def _playwright_attempt(browser_kind: str, proxy_dict: dict, stealth: bool = False) -> dict:
    async with async_playwright() as pw:
        launcher = getattr(pw, browser_kind)
        launch_kwargs = {"headless": True}
        if stealth and browser_kind == "chromium":
            launch_kwargs["args"] = _PROD_STEALTH_ARGS
        browser = await launcher.launch(**launch_kwargs)
        try:
            ctx_kwargs = {"proxy": proxy_dict}
            if stealth:
                ctx_kwargs["user_agent"] = _PROD_UA
                ctx_kwargs["locale"] = "en-US"
                ctx_kwargs["viewport"] = {"width": 1280, "height": 800}
            ctx = await browser.new_context(**ctx_kwargs)
            if stealth:
                await ctx.add_init_script(_PROD_STEALTH_JS)

                async def _route(route):
                    if route.request.resource_type in _PROD_BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()
                await ctx.route("**/*", _route)
            exit_ip = await _pw_ipify(ctx)
            page = await ctx.new_page()
            t0 = time.perf_counter()
            try:
                resp = await page.goto(
                    TARGET_URL, wait_until="domcontentloaded",
                    timeout=TIMEOUT_S * 1000,
                )
                latency = int((time.perf_counter() - t0) * 1000)
                body = await page.content()
                final_url = page.url
                status = resp.status if resp else 0
                blocked = _classify_block(status, final_url, body)
                bytes_wire = len(body.encode("utf-8", errors="ignore"))
                return {
                    "exit_ip": exit_ip, "http_status": status, "bytes_wire": bytes_wire,
                    "latency_ms": latency,
                    "passed": 1 if (status == 200 and blocked is None) else 0,
                    "blocked_reason": blocked,
                    "raw_json": {"final_url": final_url},
                }
            except Exception as e:
                latency = int((time.perf_counter() - t0) * 1000)
                reason = "timeout" if "timeout" in str(e).lower() else "http_error"
                return {
                    "exit_ip": exit_ip, "http_status": 0, "bytes_wire": 0,
                    "latency_ms": latency, "passed": 0, "blocked_reason": reason,
                    "raw_json": {"err": f"{type(e).__name__}: {e}"[:400]},
                }
        finally:
            await browser.close()


async def _pw_ipify(ctx) -> str | None:
    page = await ctx.new_page()
    try:
        r = await page.goto(IPIFY_URL, wait_until="domcontentloaded", timeout=20_000)
        if r and r.status == 200:
            return _parse_ipify(await page.content())
    except Exception:
        return None
    finally:
        await page.close()
    return None


# ---------- shared helpers ----------

def _parse_ipify(text: str | None) -> str | None:
    if not text:
        return None
    import re
    m = re.search(r'"ip"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


def _classify_block(status: int, final_url: str, body: str) -> str | None:
    """Return None if response looks like a real Google homepage, else a reason."""
    if status == 0:
        return "http_error"
    if "/sorry/" in (final_url or "").lower():
        return "sorry"
    low = (body or "").lower()
    if "/httpservice/retry/enablejs" in low and "unusual traffic" not in low:
        return "enablejs"
    if status >= 400:
        return "http_error"
    return None


# ---------- orchestration ----------

async def _execute(run_id: int, creds: dict, db_path: str) -> None:
    proxy_url = build_proxy_url(creds)
    pw_proxy = pw_proxy_dict(creds)

    for client in CLIENTS:
        for ip_no in range(1, ATTEMPTS_PER_CLIENT + 1):
            if client == "curl":
                res = await asyncio.to_thread(_curl_attempt, proxy_url)
            elif client == "curl_cffi":
                res = await asyncio.to_thread(_curl_cffi_attempt, proxy_url)
            elif client == "chromium":
                res = await _playwright_attempt("chromium", pw_proxy, stealth=False)
            elif client == "chromium_stealth":
                res = await _playwright_attempt("chromium", pw_proxy, stealth=True)
            elif client == "firefox":
                res = await _playwright_attempt("firefox", pw_proxy, stealth=False)
            else:
                continue

            raw = res.pop("raw_json", {})
            raw = {"client": client, "ip_no": ip_no, **raw}
            exit_country = await asyncio.to_thread(
                lookup_country, res.get("exit_ip")
            )
            add_query(
                run_id,
                db_path=db_path,
                keyword=None,
                exit_country=exit_country,
                sticky_ip_held=None,
                got_429=1 if res.get("http_status") == 429 else 0,
                retry_attempted=0,
                retry_passed=None,
                raw_json=raw,
                **res,
            )


def _aggregate(run_id: int, db_path: str) -> dict:
    data = get_run(run_id, db_path=db_path)
    queries = data["queries"] if data else []
    by_client: dict[str, dict] = {}
    unique_ips: set[str] = set()
    country_breakdown: dict[str, int] = {}
    got_429_count = 0

    for q in queries:
        cc = q.get("exit_country")
        if cc:
            country_breakdown[cc] = country_breakdown.get(cc, 0) + 1
        if q.get("got_429"):
            got_429_count += 1
        raw = q.get("raw_json")
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        client = (raw or {}).get("client", "unknown")
        slot = by_client.setdefault(client, {
            "pass": 0, "total": 0, "latencies": [], "blocked_reasons": []
        })
        slot["total"] += 1
        if q.get("passed"):
            slot["pass"] += 1
            if q.get("latency_ms") is not None:
                slot["latencies"].append(q["latency_ms"])
        else:
            if q.get("blocked_reason"):
                slot["blocked_reasons"].append(q["blocked_reason"])
        if q.get("exit_ip"):
            unique_ips.add(q["exit_ip"])

    for slot in by_client.values():
        lats = slot.pop("latencies")
        slot["avg_latency_ms"] = int(sum(lats) / len(lats)) if lats else None
        reasons = slot.pop("blocked_reasons")
        slot["top_blocked_reason"] = _mode(reasons) if reasons else None

    return {
        "verdict": _verdict(by_client),
        "by_client": by_client,
        "unique_exit_ips": len(unique_ips),
        "unique_countries": len(country_breakdown),
        "country_breakdown": country_breakdown,
        "got_429_count": got_429_count,
        "attempts_total": sum(s["total"] for s in by_client.values()),
    }


def _mode(xs: list[str]) -> str:
    counts: dict[str, int] = {}
    for x in xs:
        counts[x] = counts.get(x, 0) + 1
    return max(counts, key=counts.get)


def _verdict(by_client: dict) -> str:
    """Verdict rules — the label describes the pool relative to the openserp
    product story, not just raw TLS status.

    Only 'dead' should trigger a matrix short-circuit downstream. Every other
    verdict means Tests 2-4 are still worth running — 'stealth_required' in
    particular is the *most* interesting case, since it's exactly what the
    openserp stealth preset was built to rescue.
    """
    def rate(name: str) -> tuple[int, int]:
        s = by_client.get(name, {"pass": 0, "total": 0})
        return s["pass"], s["total"]

    if all(p == 0 for p, _ in (rate(c) for c in CLIENTS)):
        return "dead"

    vanilla_chr_p, vanilla_chr_t = rate("chromium")
    stealth_chr_p, stealth_chr_t = rate("chromium_stealth")

    vanilla_dead = vanilla_chr_t > 0 and vanilla_chr_p == 0
    stealth_ok = stealth_chr_t > 0 and stealth_chr_p >= 2
    if vanilla_dead and stealth_ok:
        return "stealth_required"

    stealth_dead = stealth_chr_t > 0 and stealth_chr_p == 0
    non_browser_ok = any(rate(c)[0] >= 2 for c in ("curl", "curl_cffi"))
    if vanilla_dead and stealth_dead and non_browser_ok:
        return "chromium_unusable"

    if all(p >= 2 for p, t in (rate(c) for c in CLIENTS) if t > 0):
        return "clean"

    return "reputation_block"


def run_handshake_sync(
    provider_id: int,
    matrix_run_id: str | None = None,
    db_path: str = DEFAULT_DB,
) -> int:
    creds = load_provider(provider_id, db_path)
    run_id = create_run(
        provider_id, "handshake",
        matrix_run_id=matrix_run_id, db_path=db_path,
    )
    try:
        asyncio.run(_execute(run_id, creds, db_path))
        summary = _aggregate(run_id, db_path)
        finalize_run(run_id, "done", summary=summary, db_path=db_path)
    except Exception as e:
        finalize_run(
            run_id, "error",
            notes=f"{type(e).__name__}: {e}"[:500],
            db_path=db_path,
        )
        raise
    return run_id
