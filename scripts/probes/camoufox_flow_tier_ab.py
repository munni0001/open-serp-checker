"""Ad-hoc A/B — camoufox + local, two combos:
  1) home flow  + skinny tier   (scripts allowed → autocomplete/suggest fire)
  2) direct flow + default tier (baseline shape, matches historical numbers)

Both go through local_baseline (5 kw, no proxy). Sequential.
"""
from __future__ import annotations

import json
import sqlite3
import time
import traceback
from pathlib import Path

from src.core.proxy_tests.tests.local_baseline import run_local_baseline_sync

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "camoufox_flow_tier_ab.jsonl"

DB = Path(__file__).parent.parent.parent / "open_serp_checker.db"


def load_run(run_id: int) -> dict:
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT id, browser_engine, bandwidth_tier, flow, status, "
        "summary_json FROM proxy_test_runs WHERE id=?", (run_id,)
    ).fetchone()
    d = dict(row) if row else {}
    if d.get("summary_json"):
        try:
            d["summary"] = json.loads(d["summary_json"])
        except json.JSONDecodeError:
            d["summary"] = None
    qs = c.execute(
        "SELECT keyword, http_status, bytes_wire, passed, blocked_reason, "
        "json_extract(raw_json, '$.h3_count') as h3 "
        "FROM proxy_test_queries WHERE run_id=?", (run_id,)
    ).fetchall()
    d["queries"] = [dict(q) for q in qs]
    c.close()
    return d


def run_combo(label: str, **kwargs) -> None:
    print(f"\n=== {label} ===", flush=True)
    t0 = time.time()
    try:
        run_id, ok = run_local_baseline_sync(**kwargs)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return
    elapsed = round(time.time() - t0, 1)
    data = load_run(run_id)
    s = data.get("summary") or {}
    print(f"  run_id={run_id} elapsed={elapsed}s → pass={s.get('raw_pass')}/{s.get('n_keywords')} "
          f"kb_ok={s.get('avg_kb_success')} top_block={s.get('top_blocked_reason')}",
          flush=True)
    for q in data["queries"]:
        print(f"    {q['keyword']!r:<28} status={q['http_status']} "
              f"bytes={q['bytes_wire']:>7} h3={q['h3']} passed={q['passed']} "
              f"block={q['blocked_reason']}", flush=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({
            "label": label, "run_id": run_id, "elapsed_s": elapsed,
            "engine": data["browser_engine"], "tier": data["bandwidth_tier"],
            "flow": data["flow"], "summary": s, "queries": data["queries"],
        }) + "\n")


def main():
    print(f"log: {LOG_FILE}")
    run_combo(
        "camoufox + home + skinny + local",
        browser_engine="camoufox", bandwidth_tier="skinny", flow="home",
    )
    run_combo(
        "camoufox + direct + default + local",
        browser_engine="camoufox", bandwidth_tier="default", flow="direct",
    )


if __name__ == "__main__":
    main()
