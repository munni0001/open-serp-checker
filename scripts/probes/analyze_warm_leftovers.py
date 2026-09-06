"""Analyze per-query leftover bandwidth from a persistent_context log.

Reads the JSON produced by persistent_context.py and prints, per warm query
(q2 onwards), which requests actually hit the wire — grouped by host and
resource_type. The intent is to identify the specific XHRs/scripts that
re-fire on every search regardless of cache, so we know what to block.

Usage:
    source venv/bin/activate
    python scripts/probes/analyze_warm_leftovers.py \
        scripts/probes/logs/persistent_context_homeip_<ts>.json
    python scripts/probes/analyze_warm_leftovers.py <log> --min-bytes 1024
    python scripts/probes/analyze_warm_leftovers.py <log> --show-urls
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


def _url_key(url: str) -> str:
    """Collapse a URL to host + path prefix, dropping query strings.
    Google's dynamic script URLs have long query params that make every one
    look unique; we group by path so patterns are visible.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url[:80]
    return f"{p.netloc}{p.path}"[:100]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_path", type=Path)
    ap.add_argument("--min-bytes", type=int, default=100,
                    help="Hide requests below this size when listing URLs (default: 100).")
    ap.add_argument("--show-urls", action="store_true",
                    help="Also print the top URL-keys that fire every warm query.")
    args = ap.parse_args()

    data = json.loads(args.log_path.read_text())
    ok = [q for q in data if q.get("error") is None]
    if len(ok) < 2:
        print("Log has fewer than 2 successful queries — nothing warm to analyze.")
        return 1

    warm = ok[1:]  # q2 onwards
    print(f"Analyzing {len(warm)} warm queries (q2–q{len(ok)}) from {args.log_path.name}\n")

    # Per-query header — bytes / reqs / cached — one line per query.
    print("=== Per warm query — headline ===")
    print(f"{'q':>2}  {'reqs':>5} {'cached':>7} {'wire_KB':>9} {'query':<40}")
    for q in warm:
        wire = sum(r["encoded_bytes"] for r in q["requests"] if not r.get("from_cache"))
        cached = sum(1 for r in q["requests"] if r.get("from_cache"))
        print(f"{q['idx']:>2}  {q['request_count']:>5} {cached:>7} {wire/1024:>9.1f}  {q['query'][:40]}")

    # Per-query × host × resource_type — the "what re-fires" table.
    print("\n=== Per warm query — host × resource_type (wire bytes only, cached omitted) ===")
    for q in warm:
        bins: dict[tuple, list] = defaultdict(lambda: [0, 0])  # (host, rt) -> [n, bytes]
        for r in q["requests"]:
            if r.get("from_cache"):
                continue
            bins[(r["host"], r["resource_type"])][0] += 1
            bins[(r["host"], r["resource_type"])][1] += r["encoded_bytes"]
        total = sum(v[1] for v in bins.values()) or 1
        print(f"\n  q{q['idx']} — {q['query']!r} — total wire {total/1024:.1f} KB")
        print(f"  {'host':<28} {'type':<12} {'n':>3} {'KB':>8} {'%':>5}")
        for (host, rt), (n, byt) in sorted(bins.items(), key=lambda kv: -kv[1][1]):
            print(f"  {host:<28} {rt:<12} {n:>3} {byt/1024:>8.1f} {100*byt/total:>4.0f}%")

    # Cross-query "always fires" table — URL-key seen in ALL warm queries with non-cache bytes.
    # These are the highest-value block candidates.
    print("\n=== Recurring wire requests across warm queries (block candidates) ===")
    counts: dict[str, dict] = defaultdict(lambda: {
        "queries": set(), "bytes": 0, "n": 0,
        "host": "", "rt": "", "sample_url": "",
    })
    for q in warm:
        for r in q["requests"]:
            if r.get("from_cache"):
                continue
            if r["encoded_bytes"] < args.min_bytes:
                continue
            key = _url_key(r["url"])
            counts[key]["queries"].add(q["idx"])
            counts[key]["bytes"] += r["encoded_bytes"]
            counts[key]["n"] += 1
            counts[key]["host"] = r["host"]
            counts[key]["rt"] = r["resource_type"]
            if not counts[key]["sample_url"]:
                counts[key]["sample_url"] = r["url"]

    # Rank by "fires per query × avg size" — the actual bang-per-block value.
    rows = []
    for key, info in counts.items():
        q_hit = len(info["queries"])
        if q_hit < 2:
            continue
        avg_size = info["bytes"] / info["n"]
        score = q_hit * avg_size
        rows.append((score, q_hit, info["n"], info["bytes"], key, info))
    rows.sort(key=lambda x: -x[0])

    print(f"{'queries':>7} {'reqs':>5} {'total_KB':>9} {'avg_KB':>8}  key")
    for score, q_hit, n, byt, key, info in rows[:25]:
        avg = byt / n / 1024
        print(f"{q_hit:>7} {n:>5} {byt/1024:>9.1f} {avg:>8.1f}  {key}")

    if args.show_urls:
        print("\n=== Sample URLs (top 10 candidates, full URL, first observation) ===")
        for _, _, _, _, key, info in rows[:10]:
            print(f"  [{info['rt']}]  {info['sample_url']}")

    # Aggregate leftover per query — after removing all cached responses, what's left?
    print("\n=== Leftover totals — steady-state per-query wire cost, warm ===")
    per_query_wire = []
    for q in warm:
        w = sum(r["encoded_bytes"] for r in q["requests"] if not r.get("from_cache"))
        per_query_wire.append(w)
    avg = sum(per_query_wire) / len(per_query_wire)
    print(f"  n={len(per_query_wire)} avg={avg/1024:.1f} KB  "
          f"min={min(per_query_wire)/1024:.1f}  max={max(per_query_wire)/1024:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
