"""Probe 2b — try every curl_cffi impersonation profile against Google.

Probe 2 showed chrome120 didn't beat the enablejs interstitial even though
TLS fingerprint objectively changed. Question: is ANY curl_cffi profile
(newer Chrome, Safari, Firefox, mobile) enough to bypass, or is Google
fingerprinting something beyond TLS/HTTP2?
"""
from __future__ import annotations

import re
import time
from typing import List

from curl_cffi import requests as cc_requests

QUERY = "semrush review"

# Profiles curl_cffi 0.16 ships. Order roughly newest-first.
PROFILES: List[str] = [
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome116",
    "chrome110",
    "chrome99",
    "safari184",
    "safari180",
    "safari17_2_ios",
    "safari17_0",
    "safari15_5",
    "firefox133",
    "firefox135",
    "edge101",
    "edge99",
]


def try_profile(profile: str) -> dict:
    try:
        r = cc_requests.get(
            "https://www.google.com/search",
            params={"q": QUERY, "oq": QUERY, "sourceid": "chrome",
                    "source": "chrome.ob", "ie": "UTF-8"},
            impersonate=profile,
            timeout=20,
            allow_redirects=True,
            headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+000",
            },
        )
        html = r.text
        return {
            "profile": profile,
            "status": r.status_code,
            "bytes": len(html),
            "h3": len(re.findall(r"<h3", html, re.I)),
            "enablejs": "/httpservice/retry/enablejs" in html,
            "sorry": "/sorry/" in str(r.url).lower(),
            "final_url": str(r.url)[:100],
        }
    except Exception as e:
        return {"profile": profile, "error": f"{type(e).__name__}: {e}"[:120]}


def main() -> None:
    print(f"{'profile':<20} {'status':<6} {'bytes':>7} {'h3':>3} {'enablejs':<8} {'sorry':<6} {'final':<40}")
    for p in PROFILES:
        r = try_profile(p)
        if "error" in r:
            print(f"{r['profile']:<20} ERROR {r['error']}")
        else:
            marker = "OK " if r["h3"] > 0 else "-- "
            print(f"{marker}{r['profile']:<17} {r['status']:<6} {r['bytes']:>7} {r['h3']:>3} "
                  f"{str(r['enablejs']):<8} {str(r['sorry']):<6} {r['final_url']:<40}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
