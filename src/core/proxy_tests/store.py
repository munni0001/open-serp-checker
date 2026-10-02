"""Thin persistence layer for proxy test runs (4.1).

Keeps only what the plan calls out. Runner (4.2+) writes through this;
dashboard routes read through this. No caching, no ORM.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

DEFAULT_DB = "serp_scraper.db"

_QUERY_COLS = (
    "keyword", "exit_ip", "exit_country", "sticky_ip_held", "http_status",
    "got_429", "bytes_wire", "latency_ms", "passed", "blocked_reason",
    "retry_attempted", "retry_passed", "raw_json",
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.utcnow().isoformat()


def create_run(
    provider_id: int | None,
    test_name: str,
    preset: str | None = None,
    matrix_run_id: str | None = None,
    notes: str | None = None,
    browser_engine: str = "chromium_stealth",
    bandwidth_tier: str = "default",
    flow: str = "direct",
    db_path: str = DEFAULT_DB,
) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        "INSERT INTO proxy_test_runs "
        "(provider_id, matrix_run_id, test_name, preset, started_at, status, "
        " notes, browser_engine, bandwidth_tier, flow) "
        "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
        (provider_id, matrix_run_id, test_name, preset, _now(), notes,
         browser_engine, bandwidth_tier, flow),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def add_query(run_id: int, db_path: str = DEFAULT_DB, **fields: Any) -> int:
    unknown = set(fields) - set(_QUERY_COLS)
    if unknown:
        raise ValueError(f"unknown query columns: {sorted(unknown)}")

    cols = ["run_id"] + list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    values: list[Any] = [run_id]
    for k in fields:
        v = fields[k]
        if k == "raw_json" and v is not None and not isinstance(v, str):
            v = json.dumps(v)
        elif isinstance(v, bool):
            v = 1 if v else 0
        values.append(v)

    conn = _connect(db_path)
    cur = conn.execute(
        f"INSERT INTO proxy_test_queries ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )
    qid = cur.lastrowid
    conn.commit()
    conn.close()
    return qid


def finalize_run(
    run_id: int,
    status: str,
    summary: dict | None = None,
    notes: str | None = None,
    db_path: str = DEFAULT_DB,
) -> None:
    if status not in ("done", "error"):
        raise ValueError(f"status must be 'done' or 'error', got {status!r}")
    conn = _connect(db_path)
    conn.execute(
        "UPDATE proxy_test_runs SET status=?, finished_at=?, summary_json=?, "
        "notes=COALESCE(?, notes) WHERE id=?",
        (
            status,
            _now(),
            json.dumps(summary) if summary is not None else None,
            notes,
            run_id,
        ),
    )
    conn.commit()
    conn.close()


def get_run(run_id: int, db_path: str = DEFAULT_DB) -> dict | None:
    conn = _connect(db_path)
    run_row = conn.execute(
        "SELECT * FROM proxy_test_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        conn.close()
        return None
    q_rows = conn.execute(
        "SELECT * FROM proxy_test_queries WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    conn.close()
    run = dict(run_row)
    if run.get("summary_json"):
        try:
            run["summary"] = json.loads(run["summary_json"])
        except json.JSONDecodeError:
            run["summary"] = None
    return {"run": run, "queries": [dict(r) for r in q_rows]}


def list_runs_for_provider(
    provider_id: int, limit: int = 50, db_path: str = DEFAULT_DB
) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM proxy_test_runs WHERE provider_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
        (provider_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_runs_by_test_preset(
    provider_id: int, db_path: str = DEFAULT_DB
) -> dict[tuple[str, str | None], dict]:
    """Return {(test_name, preset): run_dict} of latest completed runs.

    Only status='done' runs are considered. Ties on finished_at broken
    by id DESC. summary_json is pre-parsed into a `summary` key.
    """
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM proxy_test_runs "
        "WHERE provider_id = ? AND status = 'done' "
        "ORDER BY finished_at DESC, id DESC",
        (provider_id,),
    ).fetchall()
    conn.close()

    latest: dict[tuple[str, str | None], dict] = {}
    for r in rows:
        key = (r["test_name"], r["preset"])
        if key in latest:
            continue
        d = dict(r)
        if d.get("summary_json"):
            try:
                d["summary"] = json.loads(d["summary_json"])
            except json.JSONDecodeError:
                d["summary"] = None
        latest[key] = d
    return latest


def get_matrix_baseline(
    matrix_run_id: str, db_path: str = DEFAULT_DB
) -> dict | None:
    """Return the local_baseline run for this matrix (provider_id IS NULL),
    with parsed summary, or None if not present."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM proxy_test_runs "
        "WHERE matrix_run_id = ? AND provider_id IS NULL "
        "  AND test_name = 'local_baseline' "
        "ORDER BY started_at DESC LIMIT 1",
        (matrix_run_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    if d.get("summary_json"):
        try:
            d["summary"] = json.loads(d["summary_json"])
        except json.JSONDecodeError:
            d["summary"] = None
    return d


def list_runs_by_matrix_run_id(
    matrix_run_id: str, db_path: str = DEFAULT_DB
) -> list[dict]:
    """Return every proxy_test_run tagged with this matrix_run_id.

    Ordered by provider_id, then start time. summary_json pre-parsed.
    """
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM proxy_test_runs WHERE matrix_run_id = ? "
        "ORDER BY provider_id ASC, started_at ASC",
        (matrix_run_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("summary_json"):
            try:
                d["summary"] = json.loads(d["summary_json"])
            except json.JSONDecodeError:
                d["summary"] = None
        out.append(d)
    return out
