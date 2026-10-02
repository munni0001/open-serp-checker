"""Ad-hoc driver — chromium_stealth × doconly, then camoufox × doconly,
across local / decodo-rotating / oxylabs-rotating, 5 keywords each.

Not a shipping test. Lives in scripts/probes/ as a one-off orchestrator
(user request 2026-09-12: line up 6 runs sequentially, log per-run
summary as we go, and be interruptible with partial data preserved in
the DB).

Output: stdout progress + one JSON blob per run appended to
scripts/probes/logs/matrix_doconly_5kw.jsonl. DB is authoritative;
JSONL is convenience for quick eyeballing.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import traceback
from pathlib import Path

from src.core.proxy_tests.tests.local_baseline import run_local_baseline_sync
from src.core.proxy_tests.tests.pass_rate import run_pass_rate_sync

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "matrix_doconly_5kw.jsonl"

DB = Path(__file__).parent.parent.parent / "serp_scraper.db"


def find_provider(name: str) -> int | None:
    c = sqlite3.connect(DB)
    r = c.execute(
        "SELECT id FROM proxies WHERE provider=? LIMIT 1", (name,)
    ).fetchone()
    c.close()
    return r[0] if r else None


def load_run(run_id: int) -> dict:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT id, browser_engine, bandwidth_tier, test_name, preset, "
        "status, summary_json, finished_at FROM proxy_test_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    c.close()
    if not row:
        return {"error": "run_id not found"}
    d = dict(row)
    if d.get("summary_json"):
        try:
            d["summary"] = json.loads(d["summary_json"])
        except json.JSONDecodeError:
            d["summary"] = None
    return d


def log(entry: dict):
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _short(summary: dict | None) -> str:
    if not summary:
        return "no summary"
    parts = []
    if "raw_pass" in summary:
        parts.append(
            f"pass={summary.get('raw_pass')}/{summary.get('n_keywords')}"
        )
    if "raw_pass_rate" in summary:
        parts.append(f"rate={summary.get('raw_pass_rate')}")
    if "avg_kb_success" in summary and summary.get("avg_kb_success") is not None:
        parts.append(f"kb_ok={summary['avg_kb_success']}")
    if "top_blocked_reason" in summary and summary.get("top_blocked_reason"):
        parts.append(f"top_block={summary['top_blocked_reason']}")
    return " ".join(parts)


def run_combo(label: str, run_fn, **kwargs) -> None:
    print(f"\n=== {label} ===", flush=True)
    t0 = time.time()
    try:
        result = run_fn(**kwargs)
        run_id = result[0] if isinstance(result, tuple) else result
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        log({
            "label": label, "kwargs": {k: v for k, v in kwargs.items()
                                       if k != "provider_id"},
            "provider_id": kwargs.get("provider_id"),
            "error": f"{type(e).__name__}: {e}", "elapsed_s": elapsed,
        })
        return
    elapsed = round(time.time() - t0, 1)
    data = load_run(run_id)
    summary = data.get("summary")
    print(
        f"  run_id={run_id} status={data['status']} elapsed={elapsed}s "
        f"→ {_short(summary)}", flush=True,
    )
    log({
        "label": label, "run_id": run_id, "engine": data["browser_engine"],
        "tier": data["bandwidth_tier"], "test": data["test_name"],
        "status": data["status"], "elapsed_s": elapsed,
        "summary": summary,
    })


def main():
    decodo_id = find_provider("decodo")
    oxylabs_id = find_provider("oxylabs")
    if not decodo_id or not oxylabs_id:
        print(f"missing provider(s): decodo={decodo_id} oxylabs={oxylabs_id}")
        sys.exit(1)
    print(f"providers: decodo={decodo_id} oxylabs={oxylabs_id}")
    print(f"log: {LOG_FILE}")

    combos = [
        # --- chromium stealth + doconly ---
        ("chromium_stealth + doconly | local",
         run_local_baseline_sync,
         {"browser_engine": "chromium_stealth", "bandwidth_tier": "doconly"}),
        ("chromium_stealth + doconly | decodo-rotating",
         run_pass_rate_sync,
         {"provider_id": decodo_id, "browser_engine": "chromium_stealth",
          "bandwidth_tier": "doconly", "n_keywords": 5}),
        ("chromium_stealth + doconly | oxylabs-rotating",
         run_pass_rate_sync,
         {"provider_id": oxylabs_id, "browser_engine": "chromium_stealth",
          "bandwidth_tier": "doconly", "n_keywords": 5}),
        # --- camoufox + doconly ---
        ("camoufox + doconly | local",
         run_local_baseline_sync,
         {"browser_engine": "camoufox", "bandwidth_tier": "doconly"}),
        ("camoufox + doconly | decodo-rotating",
         run_pass_rate_sync,
         {"provider_id": decodo_id, "browser_engine": "camoufox",
          "bandwidth_tier": "doconly", "n_keywords": 5}),
        ("camoufox + doconly | oxylabs-rotating",
         run_pass_rate_sync,
         {"provider_id": oxylabs_id, "browser_engine": "camoufox",
          "bandwidth_tier": "doconly", "n_keywords": 5}),
    ]

    t_all = time.time()
    for label, fn, kwargs in combos:
        run_combo(label, fn, **kwargs)
    print(f"\nALL DONE in {round(time.time() - t_all, 1)}s")


if __name__ == "__main__":
    main()
