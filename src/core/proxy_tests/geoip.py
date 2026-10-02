"""IP -> country lookup for proxy test rows (4.5).

One helper: `lookup_country(ip)` returning an ISO-3166 alpha-2 code
(e.g. "US") or None on failure. Uses ip-api.com's free unauthenticated
endpoint (45 req/min limit — well above our test cadence).

Lookups happen from the dashboard host, NOT through the proxy, so no
provider bandwidth is burned and the reading is independent of the exit
IP allowing outbound HTTP.
"""
from __future__ import annotations

import httpx

_URL_FMT = "http://ip-api.com/json/{ip}?fields=countryCode"
_TIMEOUT_S = 5.0


def lookup_country(ip: str | None) -> str | None:
    if not ip:
        return None
    try:
        r = httpx.get(_URL_FMT.format(ip=ip), timeout=_TIMEOUT_S)
        if r.status_code == 200:
            cc = r.json().get("countryCode")
            return cc or None
    except Exception:
        return None
    return None
