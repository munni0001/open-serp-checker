"""Cross-provider run of the Session 3.7 arm-B probe (minted identity + in-page fetch).

Imports the probe primitives from `minted_identity_inpage_fetch.py` so the arm-B
shape stays byte-identical to the Decodo baseline — the point of this session is
"one shape, N pools," not a redesign.

Per Session 3.9: run arm B n=20 against each provider on the same keyword set,
same day, interleaved so pool drift is visible. Decodo :10000 serves as the
control (should re-confirm 3.7's 10/10 within ±10pp same-day).

The load-bearing column is `unique_ips` — the arm-B model assumes the exit IP
holds for the lifetime of the parked connection. If a provider rotates per
request, we'll see ~N unique IPs across N queries and the model collapses.

Usage:
    source venv/bin/activate
    python scripts/probes/minted_identity_cross_provider.py --provider decodo
    python scripts/probes/minted_identity_cross_provider.py --provider oxylabs --n 20 --pace 2
    python scripts/probes/minted_identity_cross_provider.py --provider all --pace 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from camoufox.async_api import AsyncCamoufox

# Import the probe primitives — do NOT redefine them. Keeping arm B byte-identical
# across providers is the whole premise.
sys.path.insert(0, str(Path(__file__).parent))
from minted_identity_inpage_fetch import (  # noqa: E402
    KEYWORDS,
    QueryResult,
    _attach_wire_listener,
    mint,
    run_arm_b,
)

DB_PATH = Path(__file__).parent.parent.parent / "open_serp_checker.db"


# Per-provider rotating-endpoint shape. The DB stores one base row per provider;
# this table encodes the known-working rotating pattern (port + username suffix)
# for arm-B parity. "rotating" here means per-connection rotation — the provider's
# own semantics; the probe doesn't force rotation, it just picks the pool.
#
# Keep this in sync with .env (local, gitignored) when the user adds new providers.
PROVIDER_CONFIGS = {
    "decodo": {
        "host": "gate.decodo.com",
        "port": 10000,  # :10000 = per-connection rotating; :10001-10010 = sticky
        "user_suffix": "",
    },
    "oxylabs": {
        "host": "pr.oxylabs.io",
        "port": 7777,
        "user_prefix": "customer-",
        "user_suffix": "",  # add -cc-us for US geo if needed
    },
    "webshare": {
        # Webshare's docs say -rotate suffix on the username gives a rotating pool.
        "host": "p.webshare.io",
        "port": 80,
        "user_suffix": "",  # user already includes -rotate in DB row
    },
    "byteful": {
        # Byteful rotates per request on the smartpath endpoint. Any port in the
        # 8000-8999 range works; the user field encodes session/geo.
        "host": "residential.byteful.com",
        "port": 8881,
        "user_suffix": "",
    },
    "dataimpulse": {
        "host": "gw.dataimpulse.com",
        "port": 823,  # :823 = per-request rotating; :10000-10004 = sticky 20min
        "user_suffix": "",
    },
    "shifter": {
        "host": "p.shifter.io",
        "port": 443,
        "user_prefix": "customer-",
        "user_suffix": "",
    },
}


def load_proxy(provider: str) -> Optional[dict]:
    """Load a rotating-mode proxy for the given provider, shaped for Camoufox.

    Returns `{server, username, password}` or None if the DB row is missing.
    Shape per PROVIDER_CONFIGS, not whatever port/user is literally in the DB —
    the DB row stores the base credential; the provider-specific rotating
    endpoint is encoded here.
    """
    cfg = PROVIDER_CONFIGS.get(provider)
    if cfg is None:
        return None
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT username, password FROM proxies WHERE provider=? LIMIT 1",
        (provider,),
    ).fetchone()
    db.close()
    if not row:
        return None
    base_user = row["username"] or ""
    # Strip common prefixes that may be in the DB so we can re-apply canonically.
    base_user = base_user.removeprefix("customer-").removeprefix("user-")
    user = cfg.get("user_prefix", "") + base_user + cfg.get("user_suffix", "")
    # Firefox under Camoufox ignores URL-embedded proxy auth — must pass user/pw
    # as separate dict keys for Playwright's launch-time proxy auth to work.
    return {
        "server": f"http://{cfg['host']}:{cfg['port']}",
        "username": user,
        "password": row["password"] or "",
    }


async def run_provider(provider: str, keywords: list[str], pace: float,
                       measure_ip: bool) -> tuple[list[QueryResult], bool]:
    """Mint one identity on `provider`, run arm B over `keywords`. Returns results
    and a `minted` flag so the caller can report pool-burn vs. SERP failures.
    """
    proxy = load_proxy(provider)
    if not proxy:
        print(f"(!) No DB row for provider={provider!r}; skipping.", file=sys.stderr)
        return [], False
    print(f"\n=== {provider} — {proxy['server']} (user={proxy['username'][:40]}) ===")

    results: list[QueryResult] = []
    cam = AsyncCamoufox(
        headless=True, proxy=proxy, humanize=False, geoip=True, locale="en-US",
    )
    browser = await cam.__aenter__()
    try:
        parked = None
        minted = False
        for attempt in range(1, 4):
            parked, minted = await mint(browser)
            if minted:
                break
            print(f"  mint attempt {attempt} failed — relaunching...")
            await parked.close()
            await cam.__aexit__(None, None, None)
            cam = AsyncCamoufox(
                headless=True, proxy=proxy, humanize=False, geoip=True, locale="en-US",
            )
            browser = await cam.__aenter__()
        if not minted:
            print(f"  (!) {provider}: 3 mint attempts failed — pool burned right now.")
            return results, False

        wire_records: list = []
        _attach_wire_listener(parked, wire_records)
        for i, q in enumerate(keywords, 1):
            if i > 1 and pace > 0:
                await asyncio.sleep(pace)
            t0 = time.perf_counter()
            r = await run_arm_b(parked, q, t0, wire_records, measure_ip=measure_ip)
            r.idx = i
            tag = ("OK" if r.ok else ("SORRY" if r.sorry
                   else ("ENABLEJS" if r.enablejs else "FAIL")))
            wire = f" wire={r.wire_bytes/1024:.1f}KB" if r.wire_bytes else ""
            ip = f" ip={r.ip}" if r.ip else ""
            print(f"  [q{i}] {q!r:<28} h3={r.h3_count} status={r.status} "
                  f"body={r.body_bytes/1024:.1f}KB{wire}{ip} "
                  f"lat={r.latency_ms}ms -> {tag}"
                  + (f"  err={r.error}" if r.error else ""))
            results.append(r)
        await parked.close()
    finally:
        try:
            await cam.__aexit__(None, None, None)
        except Exception:
            pass
    return results, True


def summarize(provider: str, results: list[QueryResult]) -> dict:
    oks = [r for r in results if r.ok]
    body_kb = [r.body_bytes for r in oks]
    wire_kb = [r.wire_bytes for r in oks if r.wire_bytes]
    lat = [r.latency_ms for r in oks]
    ips = {r.ip for r in results if r.ip}
    summary = {
        "provider": provider,
        "n": len(results),
        "ok": len(oks),
        "pass_rate": (len(oks) / len(results)) if results else 0.0,
        "unique_ips": len(ips),
        "body_kb_avg": (sum(body_kb) / len(body_kb) / 1024) if body_kb else 0,
        "wire_kb_avg": (sum(wire_kb) / len(wire_kb) / 1024) if wire_kb else 0,
        "lat_ms_avg": (sum(lat) / len(lat)) if lat else 0,
    }
    verdict = (
        "model-holds" if summary["unique_ips"] <= 2 and summary["pass_rate"] >= 0.8
        else "model-fails" if summary["unique_ips"] >= len(results) * 0.5
        else "short-lived-connection"
    )
    summary["verdict"] = verdict
    return summary


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="all",
                    help=f"One of {list(PROVIDER_CONFIGS)} or 'all'.")
    ap.add_argument("--n", type=int, default=len(KEYWORDS))
    ap.add_argument("--pace", type=float, default=2.0)
    ap.add_argument("--no-ip", action="store_true",
                    help="Skip the ipify IP check (saves wire bytes).")
    args = ap.parse_args()

    providers = (list(PROVIDER_CONFIGS) if args.provider == "all"
                 else [args.provider])
    for p in providers:
        if p not in PROVIDER_CONFIGS:
            print(f"(!) Unknown provider: {p}. Available: {list(PROVIDER_CONFIGS)}",
                  file=sys.stderr)
            return 2

    keywords = (KEYWORDS * max(1, (args.n + len(KEYWORDS) - 1) // len(KEYWORDS)))[: args.n]
    print(f"Cross-provider minted-identity probe — {len(keywords)} keywords/provider, "
          f"providers={providers}, measure_ip={not args.no_ip}, pace={args.pace}s\n")

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    all_summaries = []
    for provider in providers:
        results, minted = await run_provider(
            provider, keywords, args.pace, measure_ip=not args.no_ip,
        )
        if not results:
            all_summaries.append({
                "provider": provider, "n": 0, "ok": 0, "pass_rate": 0.0,
                "unique_ips": 0, "verdict": "pool-burned" if not minted else "no-run",
            })
            continue
        s = summarize(provider, results)
        all_summaries.append(s)
        log_path = log_dir / f"minted_identity_cross_provider_{provider}_{ts}.json"
        log_path.write_text(json.dumps({
            "provider": provider,
            "ts": ts,
            "summary": s,
            "results": [asdict(r) for r in results],
        }, indent=2))
        print(f"  Log: {log_path}")

    print("\n=== Matrix ===")
    print(f"{'provider':<14} {'ok':>6} {'pass':>7} {'ips':>5} "
          f"{'body':>7} {'wire':>7} {'lat':>7}  verdict")
    for s in all_summaries:
        pct = f"{s['pass_rate']*100:.0f}%" if s["n"] else "-"
        body = f"{s.get('body_kb_avg', 0):.1f}" if s["n"] else "-"
        wire = f"{s.get('wire_kb_avg', 0):.1f}" if s["n"] else "-"
        lat = f"{s.get('lat_ms_avg', 0):.0f}" if s["n"] else "-"
        print(f"{s['provider']:<14} {s['ok']:>3}/{s['n']:<2} {pct:>7} "
              f"{s['unique_ips']:>5} {body:>7} {wire:>7} {lat:>7}  {s['verdict']}")

    matrix_path = log_dir / f"minted_identity_cross_provider_matrix_{ts}.json"
    matrix_path.write_text(json.dumps(all_summaries, indent=2))
    print(f"\nMatrix: {matrix_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
