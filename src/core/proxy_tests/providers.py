"""Shared provider metadata for proxy tests (4.3, extended 4.5).

Extracted from `tests/handshake.py` on the second concrete caller (pass_rate).
Every dashboard test uses the same three primitives:

  load_provider(id)                     -> credentials dict from proxies table
  proxy_url(creds, sticky=, country=)   -> "http://user:pass@host:port"
  pw_proxy_dict(creds, sticky=, country=) -> Playwright's {server, username, password}

4.5 adds a `country` kwarg — when set, the provider's country-aware username
format is used so the exit pool is filtered to that geo. Country code should
be uppercase (e.g. 'US', 'GB'); it's lowercased at format time.

Only Decodo is wired for MVP. Other providers can be added when they land.
"""
from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

DEFAULT_DB = "serp_scraper.db"

# provider -> gateway metadata. Hardcoded for MVP; expand as new providers land.
# Country-aware formats are used when a country code is supplied to
# proxy_url / pw_proxy_dict; otherwise the plain formats are used.
GATEWAYS: dict[str, dict[str, Any]] = {
    "decodo": {
        "rotating_port": 10000,
        "sticky_port": 10001,
        "rotating_username_fmt": "{user}",
        "sticky_username_fmt": "user-{user}-sessionduration-{minutes}",
        "rotating_username_country_fmt": "user-{user}-country-{cc}",
        "sticky_username_country_fmt":
            "user-{user}-country-{cc}-sessionduration-{minutes}",
    },
}


def load_provider(provider_id: int, db_path: str = DEFAULT_DB) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, provider, username, password, host FROM proxies WHERE id = ?",
        (provider_id,),
    ).fetchone()
    conn.close()
    if row is None:
        raise ValueError(f"proxy id {provider_id} not found")
    return dict(row)


def _resolve(
    creds: dict,
    sticky: bool,
    sticky_duration_min: int,
    country: str | None,
) -> tuple[int, str]:
    prov = creds["provider"]
    if prov not in GATEWAYS:
        raise NotImplementedError(
            f"proxy tests not configured for provider={prov!r}; only decodo in 4.x"
        )
    g = GATEWAYS[prov]
    cc = country.lower() if country else None
    if sticky:
        port = g["sticky_port"]
        if cc:
            user = g["sticky_username_country_fmt"].format(
                user=creds["username"], minutes=sticky_duration_min, cc=cc,
            )
        else:
            user = g["sticky_username_fmt"].format(
                user=creds["username"], minutes=sticky_duration_min,
            )
    else:
        port = g["rotating_port"]
        if cc:
            user = g["rotating_username_country_fmt"].format(
                user=creds["username"], cc=cc,
            )
        else:
            user = g["rotating_username_fmt"].format(user=creds["username"])
    return port, user


def proxy_url(
    creds: dict,
    sticky: bool = False,
    sticky_duration_min: int = 1,
    country: str | None = None,
) -> str:
    port, user = _resolve(creds, sticky, sticky_duration_min, country)
    return (
        f"http://{quote(user, safe='')}:{quote(creds['password'], safe='')}"
        f"@{creds['host']}:{port}"
    )


def pw_proxy_dict(
    creds: dict,
    sticky: bool = False,
    sticky_duration_min: int = 1,
    country: str | None = None,
) -> dict:
    port, user = _resolve(creds, sticky, sticky_duration_min, country)
    return {
        "server": f"http://{creds['host']}:{port}",
        "username": user,
        "password": creds["password"],
    }
