"""Probe 1 — URL/param/header variant matrix vs Google.

Goal: prove we can make Google return real organic results (h3 count > 0)
from the caller's home IP with SOME request shape. Everything downstream
(proxy tuning, TLS impersonation) assumes we've first found a request shape
that works when IP reputation is a non-issue.

Usage:
    source venv/bin/activate
    python scripts/probes/url_variants.py

Emits a table to stdout and a JSON log to scripts/probes/logs/url_variants_<ts>.json.
Never hits network in tests — this is a manual diagnostic.

Pacing: 1 request/sec with 500ms jitter. 6 variants × 5 queries = 30 requests,
~45s total. Well below what a human browsing would do.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

# --- Config ---

QUERIES = [
    "best crm for small business",
    "semrush review",
    "how to make sourdough starter",
    "python asyncio semaphore example",
    "seo audit checklist 2026",
]

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

FULL_CHROME_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+000",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Priority": "u=0, i",
}

UA_ONLY_HEADERS = {"User-Agent": CHROME_UA}


def params_bare(q: str) -> Dict[str, str]:
    return {"q": q}


def params_current_a(q: str) -> Dict[str, str]:
    # Matches src/core/engines/google.py::fetch today (approach A).
    return {
        "q": q,
        "oq": q,
        "sourceid": "chrome",
        "source": "chrome.ob",
        "ie": "UTF-8",
    }


def params_a_no_omnibox(q: str) -> Dict[str, str]:
    return {"q": q, "oq": q, "sourceid": "chrome", "ie": "UTF-8"}


def params_classic_scraper(q: str) -> Dict[str, str]:
    # The shape we KNOW is botlike — kept as a negative control.
    return {"q": q, "hl": "en", "gl": "us", "num": "10"}


def params_q_ie(q: str) -> Dict[str, str]:
    return {"q": q, "ie": "UTF-8"}


VARIANTS = [
    # (name, params_fn, headers, description)
    ("V1_bare_min",       params_bare,           UA_ONLY_HEADERS,     "?q= only, UA only"),
    ("V2_bare_full_hdrs", params_bare,           FULL_CHROME_HEADERS, "?q= only, full Chrome headers"),
    ("V3_current_A",      params_current_a,      FULL_CHROME_HEADERS, "current approach A"),
    ("V4_classic",        params_classic_scraper, FULL_CHROME_HEADERS, "classic scraper params (hl,gl,num)"),
    ("V5_A_min_hdrs",     params_current_a,      UA_ONLY_HEADERS,     "approach A params, UA-only headers"),
    ("V6_q_ie",           params_q_ie,           FULL_CHROME_HEADERS, "?q= + ie=UTF-8, full Chrome headers"),
]

SEARCH_URL = "https://www.google.com/search"


@dataclass
class ProbeResult:
    variant: str
    query: str
    status: int
    bytes: int
    h3_count: int
    final_url: str
    is_sorry: bool
    title: str
    block_reason: Optional[str]
    latency_ms: int
    error: Optional[str] = None


def count_h3s(html: str) -> int:
    return len(re.findall(r"<h3\b", html, re.IGNORECASE))


def extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]*)</title>", html, re.IGNORECASE)
    return (m.group(1).strip() if m else "")[:80]


def classify_block(status: int, html: str, final_url: str, h3_count: int) -> Optional[str]:
    if status == 429:
        return "http_429"
    low = final_url.lower()
    if "/sorry/" in low:
        return "sorry_captcha"
    if "consent.google." in low:
        return "consent_interstitial"
    title = extract_title(html).lower()
    if title == "before you continue to google":
        return "consent_interstitial"
    if status == 200 and h3_count == 0 and "<body" in html.lower():
        return "soft_block_empty_serp"
    if status != 200:
        return f"http_{status}"
    return None


async def probe(client: httpx.AsyncClient, variant_name: str, headers: Dict[str, str],
                params: Dict[str, str], query: str) -> ProbeResult:
    t0 = time.perf_counter()
    try:
        resp = await client.get(SEARCH_URL, params=params, headers=headers)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        html = resp.text
        h3_count = count_h3s(html)
        final_url = str(resp.url)
        return ProbeResult(
            variant=variant_name,
            query=query,
            status=resp.status_code,
            bytes=len(html),
            h3_count=h3_count,
            final_url=final_url,
            is_sorry="/sorry/" in final_url.lower(),
            title=extract_title(html),
            block_reason=classify_block(resp.status_code, html, final_url, h3_count),
            latency_ms=latency_ms,
        )
    except Exception as e:
        return ProbeResult(
            variant=variant_name, query=query, status=0, bytes=0, h3_count=0,
            final_url="", is_sorry=False, title="", block_reason="exception",
            latency_ms=int((time.perf_counter() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


async def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"url_variants_{ts}.json"

    print(f"Probe 1: URL variants vs Google (unproxied, home IP)")
    print(f"  variants: {len(VARIANTS)}  queries: {len(QUERIES)}  total: {len(VARIANTS)*len(QUERIES)}")
    print(f"  log:      {log_path}\n")

    results: List[ProbeResult] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for vi, (name, params_fn, headers, desc) in enumerate(VARIANTS, 1):
            print(f"[{vi}/{len(VARIANTS)}] {name}  ({desc})")
            for qi, q in enumerate(QUERIES, 1):
                r = await probe(client, name, headers, params_fn(q), q)
                results.append(r)
                flag = "OK " if r.h3_count > 0 else "XX "
                print(f"    {flag} q{qi}: status={r.status} bytes={r.bytes:>6} "
                      f"h3={r.h3_count:>2} block={r.block_reason or '-'} "
                      f"lat={r.latency_ms}ms")
                await asyncio.sleep(1.0 + random.random() * 0.5)

    log_path.write_text(json.dumps([asdict(r) for r in results], indent=2))

    print("\n=== Summary by variant ===")
    print(f"{'variant':<22} {'pass':>5} {'h3_avg':>7} {'bytes_avg':>10} {'top_block':<25}")
    for name, _, _, _ in VARIANTS:
        rows = [r for r in results if r.variant == name]
        passed = sum(1 for r in rows if r.h3_count > 0)
        h3_avg = sum(r.h3_count for r in rows) / len(rows)
        b_avg = sum(r.bytes for r in rows) / len(rows)
        blocks = [r.block_reason for r in rows if r.block_reason]
        top_block = max(set(blocks), key=blocks.count) if blocks else "-"
        print(f"{name:<22} {passed}/{len(rows):<3} {h3_avg:>7.1f} {int(b_avg):>10} {top_block:<25}")

    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
