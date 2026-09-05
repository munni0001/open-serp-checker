"""Probe 2 — TLS/HTTP2 fingerprint comparison.

Confirms curl_cffi(impersonate="chrome120") actually produces Chrome's
TLS ClientHello + HTTP/2 fingerprint. Compares against plain httpx.

Also does the payoff test: re-hits Google's search page with both clients
and reports h3_count. If curl_cffi returns h3 > 0 and httpx returns 0,
that's end-to-end proof that TLS was the only ceiling.

Usage:
    source venv/bin/activate
    python scripts/probes/tls_fingerprint.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
from curl_cffi import requests as cc_requests

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Public JA3/JA4 reporters.
BROWSERLEAKS_TLS = "https://tls.browserleaks.com/json"
# scrapfly's fingerprint API is a solid second opinion (JA3 + Akamai HTTP2 fp)
SCRAPFLY_FP = "https://tools.scrapfly.io/api/fp/anything"


def _short(d: Dict[str, Any], keys: list[str]) -> Dict[str, Any]:
    return {k: d.get(k) for k in keys}


def probe_browserleaks_httpx() -> Dict[str, Any]:
    with httpx.Client(timeout=15, headers={"User-Agent": CHROME_UA}) as c:
        r = c.get(BROWSERLEAKS_TLS)
        return r.json()


def probe_browserleaks_curlcffi() -> Dict[str, Any]:
    r = cc_requests.get(BROWSERLEAKS_TLS, impersonate="chrome120", timeout=15,
                        headers={"User-Agent": CHROME_UA})
    return r.json()


def google_h3_httpx() -> Dict[str, Any]:
    headers = {
        "User-Agent": CHROME_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+000",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as c:
        r = c.get("https://www.google.com/search",
                  params={"q": "semrush review", "oq": "semrush review",
                          "sourceid": "chrome", "source": "chrome.ob", "ie": "UTF-8"})
    html = r.text
    return {
        "status": r.status_code,
        "bytes": len(html),
        "h3_count": len(re.findall(r"<h3", html, re.I)),
        "final_url": str(r.url),
        "title": (re.search(r"<title[^>]*>([^<]*)</title>", html, re.I).group(1)
                  if re.search(r"<title", html, re.I) else ""),
    }


def google_h3_curlcffi() -> Dict[str, Any]:
    headers = {
        "User-Agent": CHROME_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+000",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    # curl_cffi's impersonate handles Sec-Ch-Ua* automatically. Passing them
    # here would double-set. Leave them to the impersonate profile.
    r = cc_requests.get(
        "https://www.google.com/search",
        params={"q": "semrush review", "oq": "semrush review",
                "sourceid": "chrome", "source": "chrome.ob", "ie": "UTF-8"},
        impersonate="chrome120",
        headers=headers,
        timeout=20,
        allow_redirects=True,
    )
    html = r.text
    return {
        "status": r.status_code,
        "bytes": len(html),
        "h3_count": len(re.findall(r"<h3", html, re.I)),
        "final_url": str(r.url),
        "title": (re.search(r"<title[^>]*>([^<]*)</title>", html, re.I).group(1)
                  if re.search(r"<title", html, re.I) else ""),
    }


def main() -> int:
    out: Dict[str, Any] = {}

    print("=== TLS fingerprint via browserleaks.com/json ===")
    try:
        bl_httpx = probe_browserleaks_httpx()
        out["browserleaks_httpx"] = bl_httpx
        print("\n[httpx]")
        for k in ("ja3_hash", "ja3n_hash", "ja3_text", "ja4", "ja4_r",
                  "user_agent", "akamai_hash", "akamai_text", "http_version"):
            v = bl_httpx.get(k)
            if v is not None:
                print(f"  {k}: {str(v)[:140]}")
    except Exception as e:
        out["browserleaks_httpx"] = {"error": repr(e)}
        print(f"  [httpx] ERROR: {e!r}")

    try:
        bl_cc = probe_browserleaks_curlcffi()
        out["browserleaks_curlcffi"] = bl_cc
        print("\n[curl_cffi chrome120]")
        for k in ("ja3_hash", "ja3n_hash", "ja3_text", "ja4", "ja4_r",
                  "user_agent", "akamai_hash", "akamai_text", "http_version"):
            v = bl_cc.get(k)
            if v is not None:
                print(f"  {k}: {str(v)[:140]}")
    except Exception as e:
        out["browserleaks_curlcffi"] = {"error": repr(e)}
        print(f"  [curl_cffi] ERROR: {e!r}")

    # Diff summary
    print("\n[diff]")
    for k in ("ja3_hash", "ja3n_hash", "ja4", "akamai_hash", "http_version"):
        a = (out.get("browserleaks_httpx") or {}).get(k)
        b = (out.get("browserleaks_curlcffi") or {}).get(k)
        same = "SAME" if a == b else "DIFF"
        print(f"  {k}: {same}  httpx={a!s:.60}  curl={b!s:.60}")

    print("\n=== Google end-to-end test ===")
    print("\n[httpx GET /search?q=semrush review]")
    try:
        gh = google_h3_httpx()
        out["google_httpx"] = gh
        for k, v in gh.items():
            print(f"  {k}: {str(v)[:100]}")
    except Exception as e:
        out["google_httpx"] = {"error": repr(e)}
        print(f"  ERROR: {e!r}")

    print("\n[curl_cffi chrome120 GET /search?q=semrush review]")
    try:
        gc = google_h3_curlcffi()
        out["google_curlcffi"] = gc
        for k, v in gc.items():
            print(f"  {k}: {str(v)[:100]}")
    except Exception as e:
        out["google_curlcffi"] = {"error": repr(e)}
        print(f"  ERROR: {e!r}")

    # Persist
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"tls_fingerprint_{ts}.json"
    log_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nLog: {log_path}")

    # Verdict
    print("\n=== Verdict ===")
    gh_h3 = (out.get("google_httpx") or {}).get("h3_count", 0)
    gc_h3 = (out.get("google_curlcffi") or {}).get("h3_count", 0)
    if gc_h3 > 0 and gh_h3 == 0:
        print("  PROOF: curl_cffi bypasses Google's JS interstitial, httpx does not.")
        print("  -> Ship Approach C (curl_cffi in google.py).")
    elif gc_h3 > 0 and gh_h3 > 0:
        print("  Both clients got h3s. Google's classifier is unstable today; re-run to confirm.")
    elif gc_h3 == 0 and gh_h3 == 0:
        print("  Neither got h3s. TLS alone isn't enough — need Probe 3 (proxy) or B (2-step).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
