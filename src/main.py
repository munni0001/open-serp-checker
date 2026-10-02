import asyncio
import time
import uuid
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import settings
from src.core.database import Database
from src.core.managers import KeywordManager, ProxyManager
from src.core.proxy_tests import store as tests_store
from src.core.proxy_tests.tests.handshake import run_handshake_sync
from src.core.proxy_tests.tests.local_baseline import run_local_baseline_sync
from src.core.proxy_tests.tests.pass_rate import run_pass_rate_sync
from src.core.proxy_tests.tests.warm import run_warm_sync
from src.core.scraper import build_proxy_url
from src.models import Project, Proxy, ProxyProvider, ProxyType

app = FastAPI(title="SERP Scraper API", version="0.2.0")

db = Database(db_path=settings.database_path)
proxy_manager = ProxyManager(db)
keyword_manager = KeywordManager(db)

templates = Jinja2Templates(directory="src/dashboard/templates")
app.mount("/static", StaticFiles(directory="src/dashboard/static"), name="static")


# ---------- Request schemas ----------

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    domain: str = ""
    default_geo: str = "us"
    default_interval_hours: int = 24
    default_max_position: int = 100
    default_results_per_page: int = 10


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    domain: Optional[str] = None
    default_geo: Optional[str] = None
    default_interval_hours: Optional[int] = None
    default_max_position: Optional[int] = None
    default_results_per_page: Optional[int] = None
    default_proxy_id: Optional[int] = None


class KeywordCreate(BaseModel):
    term: str
    engine: Optional[str] = None
    geo: Optional[str] = None
    interval_hours: Optional[int] = None
    max_position: Optional[int] = None
    results_per_page: Optional[int] = None
    proxy_id: Optional[int] = None


class KeywordBulkCreate(BaseModel):
    terms: List[str]
    engine: Optional[str] = None
    geo: Optional[str] = None
    interval_hours: Optional[int] = None
    max_position: Optional[int] = None
    results_per_page: Optional[int] = None
    proxy_id: Optional[int] = None


class KeywordPatch(BaseModel):
    term: Optional[str] = None
    engine: Optional[str] = None
    geo: Optional[str] = None
    interval_hours: Optional[int] = None
    max_position: Optional[int] = None
    results_per_page: Optional[int] = None
    proxy_id: Optional[int] = None


class ProxyCreate(BaseModel):
    name: str
    type: ProxyType = ProxyType.PAID
    provider: ProxyProvider = ProxyProvider.MANUAL
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    mode: str = "rotating"
    sticky_duration_min: Optional[int] = None
    sticky_sessions: int = 1
    ip_blocklist_enabled: bool = False
    ip_blocklist_ttl_days: int = 1
    provisioning_notes: Optional[str] = None
    enabled_for_testing: bool = False


class ProxyPatch(BaseModel):
    name: Optional[str] = None
    type: Optional[ProxyType] = None
    provider: Optional[ProxyProvider] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    mode: Optional[str] = None
    sticky_duration_min: Optional[int] = None
    sticky_sessions: Optional[int] = None
    ip_blocklist_enabled: Optional[bool] = None
    ip_blocklist_ttl_days: Optional[int] = None
    provisioning_notes: Optional[str] = None
    enabled_for_testing: Optional[bool] = None


# ---------- Pages ----------

@app.get("/")
async def projects_page(request: Request):
    projects = db.get_all_projects()
    counts = db.count_keywords_by_project()
    return templates.TemplateResponse(
        request=request,
        name="projects.html",
        context={
            "projects": projects,
            "keyword_counts": counts,
        },
    )


@app.get("/project/{project_id}")
async def project_page(request: Request, project_id: int):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    summary = db.get_keyword_summary(project_id)
    proxies = db.get_all_proxies()
    proxy_names = {p.id: p.name for p in proxies}
    return templates.TemplateResponse(
        request=request,
        name="project.html",
        context={
            "project": project,
            "keyword_rows": summary,
            "proxies": [p.model_dump(mode="json") for p in proxies],
            "proxy_names": proxy_names,
        },
    )


@app.get("/proxies", response_class=None)
async def proxies_page(request: Request):
    proxies = db.get_all_proxies()
    assignments = db.proxy_assignments()
    return templates.TemplateResponse(
        request=request,
        name="proxies.html",
        context={
            "proxies": [p.model_dump(mode="json") for p in proxies],
            "assignments": assignments,
            "providers": [p.value for p in ProxyProvider],
            "types": [t.value for t in ProxyType],
        },
    )


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# ---------- Proxy API ----------

def _proxy_out(p: Proxy, assignments: dict) -> dict:
    row = assignments.get(p.id, {})
    return {
        **p.model_dump(mode="json"),
        "keyword_count": row.get("keywords", 0),
        "project_count": row.get("projects", 0),
    }


@app.get("/api/proxies")
async def list_proxies():
    proxies = db.get_all_proxies()
    assignments = db.proxy_assignments()
    return {"proxies": [_proxy_out(p, assignments) for p in proxies]}


@app.get("/api/proxies/{proxy_id}")
async def get_proxy(proxy_id: int):
    p = db.get_proxy(proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proxy not found")
    return _proxy_out(p, db.proxy_assignments())


@app.post("/api/proxies")
async def create_proxy(payload: ProxyCreate):
    proxy = Proxy(**payload.model_dump())
    proxy_id = proxy_manager.add_proxy(proxy)
    p = db.get_proxy(proxy_id)
    return _proxy_out(p, {})


@app.patch("/api/proxies/{proxy_id}")
async def update_proxy(proxy_id: int, payload: ProxyPatch):
    if not db.get_proxy(proxy_id):
        raise HTTPException(status_code=404, detail="Proxy not found")
    db.update_proxy(proxy_id, **payload.model_dump(exclude_unset=True))
    p = db.get_proxy(proxy_id)
    return _proxy_out(p, db.proxy_assignments())


@app.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: int):
    if not db.delete_proxy(proxy_id):
        raise HTTPException(status_code=404, detail="Proxy not found")
    return {"deleted": proxy_id}


@app.post("/api/proxies/{proxy_id}/test")
async def test_proxy(proxy_id: int):
    p = db.get_proxy(proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proxy not found")
    proxy_url = build_proxy_url(p)
    if not proxy_url:
        return {"ok": False, "latency_ms": 0, "http_status": None,
                "error": "Proxy has no host/port configured"}
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, proxy=proxy_url) as client:
            resp = await client.get("https://www.google.com/search?q=test",
                                    headers={"User-Agent": "Mozilla/5.0"})
        elapsed = int((time.perf_counter() - start) * 1000)
        return {"ok": resp.status_code == 200, "latency_ms": elapsed,
                "http_status": resp.status_code, "error": None}
    except Exception as e:
        elapsed = int((time.perf_counter() - start) * 1000)
        return {"ok": False, "latency_ms": elapsed, "http_status": None,
                "error": f"{type(e).__name__}: {e}"[:400]}


# ---------- Proxy Tests (4.2) ----------

@app.get("/proxies/{proxy_id}/tests")
async def proxy_tests_list(request: Request, proxy_id: int):
    p = db.get_proxy(proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proxy not found")
    import json as _json
    runs = tests_store.list_runs_for_provider(proxy_id, db_path=settings.database_path)
    for r in runs:
        s = r.get("summary_json")
        r["verdict"] = None
        if s:
            try:
                r["verdict"] = _json.loads(s).get("verdict")
            except _json.JSONDecodeError:
                pass
    return templates.TemplateResponse(
        request=request,
        name="proxy_tests_list.html",
        context={
            "proxy": p.model_dump(mode="json"),
            "runs": runs,
            "provisioning_notes": p.provisioning_notes,
        },
    )


@app.post("/proxies/{proxy_id}/tests/run")
async def proxy_tests_run(
    proxy_id: int,
    background: BackgroundTasks,
    test_name: str = "handshake",
    country: Optional[str] = None,
):
    p = db.get_proxy(proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proxy not found")
    cc = (country or "").strip().upper() or None
    if test_name == "handshake":
        background.add_task(run_handshake_sync, proxy_id, None, settings.database_path)
    elif test_name == "pass_rate":
        background.add_task(
            run_pass_rate_sync, proxy_id, None, settings.database_path, cc,
        )
    elif test_name == "warm":
        from src.core.proxy_tests.tests.warm import (
            STICKY_DURATION_MIN as _WARM_STICKY_MIN,
            KEYWORDS as _WARM_KEYWORDS,
        )
        background.add_task(
            run_warm_sync, proxy_id, None, settings.database_path,
            _WARM_STICKY_MIN, len(_WARM_KEYWORDS), cc,
        )
    else:
        raise HTTPException(status_code=400, detail=f"unsupported test_name: {test_name!r}")
    return RedirectResponse(url=f"/proxies/{proxy_id}/tests", status_code=303)


@app.get("/api/proxies/{proxy_id}/tests")
async def api_proxy_tests(proxy_id: int) -> dict:
    p = db.get_proxy(proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proxy not found")

    latest = tests_store.latest_runs_by_test_preset(
        proxy_id, db_path=settings.database_path
    )

    def _slot(test_name: str, preset: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
        run = latest.get((test_name, preset))
        if not run:
            return None, None
        return run.get("summary"), run.get("finished_at")

    handshake_summary, handshake_ts = _slot("handshake", None)
    pass_rate_cold, pr_cold_ts = _slot("pass_rate", "cold")
    warm_cache, wc_ts = _slot("warm", "cache_only")
    warm_cookie, wk_ts = _slot("warm", "cookie_only")
    warm_sticky, ws_ts = _slot("warm", "sticky_warm")

    run_dates: dict[str, str] = {}
    for key, ts in [
        ("handshake", handshake_ts),
        ("pass_rate.cold", pr_cold_ts),
        ("warm.cache_only", wc_ts),
        ("warm.cookie_only", wk_ts),
        ("warm.sticky_warm", ws_ts),
    ]:
        if ts:
            run_dates[key] = ts

    return {
        "provider": {
            "id": p.id,
            "name": p.name,
            "provider": p.provider.value if hasattr(p.provider, "value") else p.provider,
            "provisioning_notes": p.provisioning_notes,
        },
        "latest": {
            "handshake": handshake_summary,
            "pass_rate": {"cold": pass_rate_cold},
            "warm": {
                "cache_only": warm_cache,
                "cookie_only": warm_cookie,
                "sticky_warm": warm_sticky,
            },
        },
        "caveats": {
            "single_day_snapshot": True,
            "run_dates": run_dates,
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


# ---------- Local baseline (4.9) ----------


@app.post("/proxies/local_baseline/run")
async def local_baseline_run(background: BackgroundTasks):
    background.add_task(run_local_baseline_sync, None, settings.database_path)
    return RedirectResponse(url="/proxies", status_code=303)


@app.get("/proxies/local_baseline/{run_id}")
async def local_baseline_detail(request: Request, run_id: int):
    import json as _json
    data = tests_store.get_run(run_id, db_path=settings.database_path)
    if not data or data["run"]["test_name"] != "local_baseline":
        raise HTTPException(status_code=404, detail="Local baseline run not found")
    for q in data["queries"]:
        raw = q.get("raw_json")
        if isinstance(raw, str):
            try:
                q["raw"] = _json.loads(raw)
            except _json.JSONDecodeError:
                q["raw"] = {}
        else:
            q["raw"] = raw or {}
    # Synthesize a "proxy" stub so the shared template header renders sanely.
    proxy_stub = {
        "id": None,
        "name": "Local baseline (no proxy)",
        "provider": "local",
    }
    return templates.TemplateResponse(
        request=request,
        name="proxy_tests_detail.html",
        context={
            "proxy": proxy_stub,
            "run": data["run"],
            "queries": data["queries"],
            "is_local_baseline": True,
        },
    )


# ---------- Matrix runner (4.7) ----------


def _record_matrix_error(
    provider_id: int, matrix_run_id: str, stage: str, exc: Exception, db_path: str
) -> None:
    rid = tests_store.create_run(
        provider_id, "matrix",
        matrix_run_id=matrix_run_id,
        notes=f"{stage}: {type(exc).__name__}: {exc}"[:500],
        db_path=db_path,
    )
    tests_store.finalize_run(rid, "error", db_path=db_path)


def _run_matrix(
    matrix_run_id: str, provider_ids: List[int], country: Optional[str], db_path: str
) -> None:
    from src.core.proxy_tests.tests.warm import (
        STICKY_DURATION_MIN as _WARM_STICKY_MIN,
        KEYWORDS as _WARM_KEYWORDS,
    )

    # 4.9: gate the matrix on a local script-health check. If the script looks
    # automated to Google from a clean home IP, provider numbers are
    # untrustworthy and running them wastes proxy bandwidth.
    try:
        _, baseline_ok = run_local_baseline_sync(matrix_run_id, db_path)
    except Exception as e:
        # Baseline crashed — treat as fail. Tombstone against provider_id=NULL
        # via a synthetic 'matrix' row so the matrix page can show it.
        rid = tests_store.create_run(
            None, "matrix", matrix_run_id=matrix_run_id,
            notes=f"local_baseline: {type(e).__name__}: {e}"[:500],
            db_path=db_path,
        )
        tests_store.finalize_run(rid, "error", db_path=db_path)
        return

    if not baseline_ok:
        return  # abort — do not run providers

    for pid in provider_ids:
        try:
            run_handshake_sync(pid, matrix_run_id, db_path)
        except Exception as e:
            _record_matrix_error(pid, matrix_run_id, "handshake", e, db_path)
            continue
        try:
            run_pass_rate_sync(pid, matrix_run_id, db_path, country)
        except Exception as e:
            _record_matrix_error(pid, matrix_run_id, "pass_rate", e, db_path)
        try:
            run_warm_sync(
                pid, matrix_run_id, db_path,
                _WARM_STICKY_MIN, len(_WARM_KEYWORDS), country,
            )
        except Exception as e:
            _record_matrix_error(pid, matrix_run_id, "warm", e, db_path)


@app.post("/proxies/matrix/run")
async def matrix_run(
    background: BackgroundTasks, country: Optional[str] = None,
):
    cc = (country or "").strip().upper() or None
    enabled = [p for p in db.get_all_proxies() if p.enabled_for_testing]
    if not enabled:
        raise HTTPException(
            status_code=400,
            detail="No providers have enabled_for_testing=True",
        )
    matrix_run_id = uuid.uuid4().hex[:12]
    background.add_task(
        _run_matrix, matrix_run_id, [p.id for p in enabled], cc,
        settings.database_path,
    )
    return RedirectResponse(
        url=f"/proxies/matrix/{matrix_run_id}", status_code=303
    )


_MATRIX_SLOTS: List[tuple[str, Optional[str], str]] = [
    ("handshake", None, "Handshake"),
    ("pass_rate", "cold", "Cold pass"),
    ("warm", "cache_only", "Warm cache"),
    ("warm", "cookie_only", "Warm cookie"),
    ("warm", "sticky_warm", "Warm sticky"),
]


@app.get("/proxies/matrix/{matrix_run_id}")
async def matrix_detail(request: Request, matrix_run_id: str):
    runs = tests_store.list_runs_by_matrix_run_id(
        matrix_run_id, db_path=settings.database_path,
    )
    proxies = {p.id: p for p in db.get_all_proxies()}

    baseline = tests_store.get_matrix_baseline(
        matrix_run_id, db_path=settings.database_path,
    )

    grouped: dict[int, dict[tuple[str, Optional[str]], dict]] = {}
    error_tombstones: dict[int, list[dict]] = {}
    for r in runs:
        pid = r["provider_id"]
        # Baseline is rendered as a banner, not a matrix row.
        if r["test_name"] == "local_baseline":
            continue
        if r["test_name"] == "matrix":
            error_tombstones.setdefault(pid, []).append(r)
            continue
        if pid is None:
            continue
        grouped.setdefault(pid, {})[(r["test_name"], r["preset"])] = r

    provider_rows = []
    any_running = False
    for pid in sorted(set(list(grouped.keys()) + list(error_tombstones.keys()))):
        p = proxies.get(pid)
        slots = grouped.get(pid, {})
        slot_rows = []
        done_ct = 0
        for test_name, preset, label in _MATRIX_SLOTS:
            run = slots.get((test_name, preset))
            status = run["status"] if run else "missing"
            if status == "running":
                any_running = True
            if status == "done":
                done_ct += 1
            slot_rows.append({
                "label": label,
                "test_name": test_name,
                "preset": preset,
                "run": run,
                "summary": (run or {}).get("summary") or {},
                "status": status,
            })
        provider_rows.append({
            "provider_id": pid,
            "name": p.name if p else f"#{pid}",
            "provisioning_notes": p.provisioning_notes if p else None,
            "slots": slot_rows,
            "done_ct": done_ct,
            "total": len(_MATRIX_SLOTS),
            "errors": error_tombstones.get(pid, []),
        })

    if baseline and baseline.get("status") == "running":
        any_running = True

    return templates.TemplateResponse(
        request=request,
        name="proxy_matrix.html",
        context={
            "matrix_run_id": matrix_run_id,
            "providers": provider_rows,
            "any_running": any_running,
            "slot_headers": [label for _, _, label in _MATRIX_SLOTS],
            "baseline": baseline,
        },
    )


@app.get("/api/proxies/matrix/{matrix_run_id}")
async def api_matrix(matrix_run_id: str) -> dict:
    runs = tests_store.list_runs_by_matrix_run_id(
        matrix_run_id, db_path=settings.database_path,
    )
    if not runs:
        raise HTTPException(status_code=404, detail="matrix_run_id not found")

    proxies = {p.id: p for p in db.get_all_proxies()}

    baseline_row = tests_store.get_matrix_baseline(
        matrix_run_id, db_path=settings.database_path,
    )
    baseline_block: Optional[dict] = None
    if baseline_row:
        s = baseline_row.get("summary") or {}
        baseline_block = {
            "run_id": baseline_row["id"],
            "status": baseline_row["status"],
            "started_at": baseline_row.get("started_at"),
            "finished_at": baseline_row.get("finished_at"),
            "n_keywords": s.get("n_keywords"),
            "raw_pass": s.get("raw_pass"),
            "raw_pass_rate": s.get("raw_pass_rate"),
            "pass_threshold": s.get("pass_threshold"),
            "baseline_ok": s.get("baseline_ok"),
            "top_blocked_reason": s.get("top_blocked_reason"),
            "exit_ip": s.get("exit_ip"),
            "exit_country": s.get("exit_country"),
        }

    by_provider: dict[int, dict[tuple[str, Optional[str]], dict]] = {}
    errors_by_provider: dict[int, list[dict]] = {}
    for r in runs:
        pid = r["provider_id"]
        if r["test_name"] == "local_baseline":
            continue
        if r["test_name"] == "matrix":
            notes = r.get("notes") or ""
            stage = notes.split(":", 1)[0].strip() or None
            errors_by_provider.setdefault(pid, []).append({
                "stage": stage,
                "notes": notes or None,
                "finished_at": r.get("finished_at"),
            })
            continue
        if pid is None:
            continue
        by_provider.setdefault(pid, {})[(r["test_name"], r["preset"])] = r

    any_running = any(r["status"] == "running" for r in runs)

    providers_out: list[dict] = []
    for pid in sorted(set(list(by_provider.keys()) + list(errors_by_provider.keys()))):
        slots = by_provider.get(pid, {})
        p = proxies.get(pid)

        def _done_slot(t: str, preset: Optional[str]):
            run = slots.get((t, preset))
            if not run or run["status"] != "done":
                return None, None
            return run.get("summary"), run.get("finished_at")

        handshake_sum, hs_ts = _done_slot("handshake", None)
        cold_sum, cold_ts = _done_slot("pass_rate", "cold")
        cache_sum, cache_ts = _done_slot("warm", "cache_only")
        cookie_sum, cookie_ts = _done_slot("warm", "cookie_only")
        sticky_sum, sticky_ts = _done_slot("warm", "sticky_warm")

        run_dates: dict[str, str] = {}
        for key, ts in [
            ("handshake", hs_ts),
            ("pass_rate.cold", cold_ts),
            ("warm.cache_only", cache_ts),
            ("warm.cookie_only", cookie_ts),
            ("warm.sticky_warm", sticky_ts),
        ]:
            if ts:
                run_dates[key] = ts

        counts = {"done": 0, "running": 0, "error": 0, "missing": 0}
        for tname, preset, _label in _MATRIX_SLOTS:
            run = slots.get((tname, preset))
            key = run["status"] if run else "missing"
            counts[key] = counts.get(key, 0) + 1

        providers_out.append({
            "provider": {
                "id": pid,
                "name": p.name if p else f"#{pid}",
                "provider": (
                    p.provider.value if p and hasattr(p.provider, "value")
                    else (p.provider if p else None)
                ),
                "provisioning_notes": p.provisioning_notes if p else None,
            },
            "latest": {
                "handshake": handshake_sum,
                "pass_rate": {"cold": cold_sum},
                "warm": {
                    "cache_only": cache_sum,
                    "cookie_only": cookie_sum,
                    "sticky_warm": sticky_sum,
                },
            },
            "run_dates": run_dates,
            "status_counts": counts,
            "errors": errors_by_provider.get(pid, []),
        })

    return {
        "matrix_run_id": matrix_run_id,
        "generated_at": datetime.utcnow().isoformat(),
        "any_running": any_running,
        "baseline": baseline_block,
        "providers": providers_out,
        "caveats": {"single_day_snapshot": True},
    }


@app.get("/proxies/{proxy_id}/tests/{run_id}")
async def proxy_tests_detail(request: Request, proxy_id: int, run_id: int):
    p = db.get_proxy(proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proxy not found")
    import json as _json
    data = tests_store.get_run(run_id, db_path=settings.database_path)
    if not data or data["run"]["provider_id"] != proxy_id:
        raise HTTPException(status_code=404, detail="Run not found for this proxy")
    for q in data["queries"]:
        raw = q.get("raw_json")
        if isinstance(raw, str):
            try:
                q["raw"] = _json.loads(raw)
            except _json.JSONDecodeError:
                q["raw"] = {}
        else:
            q["raw"] = raw or {}
    return templates.TemplateResponse(
        request=request,
        name="proxy_tests_detail.html",
        context={"proxy": p.model_dump(mode="json"), "run": data["run"], "queries": data["queries"]},
    )


# ---------- Project API ----------

@app.get("/projects")
async def list_projects():
    projects = db.get_all_projects()
    counts = db.count_keywords_by_project()
    return {
        "projects": [
            {**p.model_dump(mode="json"), "keyword_count": counts.get(p.id, 0)}
            for p in projects
        ]
    }


@app.post("/projects")
async def create_project(payload: ProjectCreate):
    project = Project(**payload.model_dump())
    project_id = db.create_project(project)
    return {"id": project_id, **payload.model_dump()}


@app.patch("/projects/{project_id}")
async def update_project(project_id: int, payload: ProjectPatch):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    db.update_project(project_id, **payload.model_dump(exclude_unset=True))
    updated = db.get_project(project_id)
    return updated.model_dump(mode="json")


@app.delete("/projects/{project_id}")
async def delete_project(project_id: int):
    if not db.delete_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": project_id}


# ---------- Keyword API ----------

@app.get("/projects/{project_id}/keywords")
async def list_keywords(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"keywords": db.get_keyword_summary(project_id)}


@app.post("/projects/{project_id}/keywords")
async def create_keyword(project_id: int, payload: KeywordCreate):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    keyword_id = keyword_manager.add_keyword(
        project,
        payload.term,
        engine=payload.engine,
        geo=payload.geo,
        interval_hours=payload.interval_hours,
        max_position=payload.max_position,
        results_per_page=payload.results_per_page,
        proxy_id=payload.proxy_id,
    )
    return db.get_keyword(keyword_id).model_dump(mode="json")


@app.post("/projects/{project_id}/keywords/bulk")
async def create_keywords_bulk(project_id: int, payload: KeywordBulkCreate):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    terms = [t.strip() for t in payload.terms if t and t.strip()]
    if not terms:
        raise HTTPException(status_code=400, detail="No terms provided")

    created = []
    for term in terms:
        kw_id = keyword_manager.add_keyword(
            project, term,
            engine=payload.engine,
            geo=payload.geo,
            interval_hours=payload.interval_hours,
            max_position=payload.max_position,
            results_per_page=payload.results_per_page,
            proxy_id=payload.proxy_id,
        )
        created.append(kw_id)
    return {"created": created, "count": len(created)}


@app.patch("/keywords/{keyword_id}")
async def update_keyword(keyword_id: int, payload: KeywordPatch):
    if not db.get_keyword(keyword_id):
        raise HTTPException(status_code=404, detail="Keyword not found")
    db.update_keyword(keyword_id, **payload.model_dump(exclude_unset=True))
    return db.get_keyword(keyword_id).model_dump(mode="json")


@app.delete("/keywords/{keyword_id}")
async def delete_keyword(keyword_id: int):
    if not db.delete_keyword(keyword_id):
        raise HTTPException(status_code=404, detail="Keyword not found")
    return {"deleted": keyword_id}


@app.post("/projects/{project_id}/run-all")
async def run_all_project_keywords(project_id: int, background: BackgroundTasks):
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    keywords = db.get_keywords_for_project(project_id)
    if not keywords:
        raise HTTPException(status_code=400, detail="Project has no keywords")

    sem = asyncio.Semaphore(3)

    async def _run_one(kw_id: int):
        async with sem:
            try:
                await keyword_manager.run_keyword(kw_id)
            except Exception:
                pass

    async def _run_all():
        await asyncio.gather(*(_run_one(kw.id) for kw in keywords))

    background.add_task(_run_all)
    return {"project_id": project_id, "queued": len(keywords), "concurrency": 3}


@app.post("/keywords/{keyword_id}/run")
async def run_keyword(keyword_id: int):
    if not db.get_keyword(keyword_id):
        raise HTTPException(status_code=404, detail="Keyword not found")
    result = await keyword_manager.run_keyword(keyword_id)
    return result.model_dump(mode="json")


@app.post("/keywords/{keyword_id}/run-scheduled")
async def run_keyword_scheduled(keyword_id: int, background: BackgroundTasks):
    if not db.get_keyword(keyword_id):
        raise HTTPException(status_code=404, detail="Keyword not found")

    async def _run():
        await keyword_manager.run_keyword(keyword_id)

    background.add_task(_run)
    return {"keyword_id": keyword_id, "status": "started"}


@app.get("/keywords/{keyword_id}/history")
async def keyword_history(keyword_id: int):
    if not db.get_keyword(keyword_id):
        raise HTTPException(status_code=404, detail="Keyword not found")
    history = db.get_keyword_history(keyword_id)
    return {
        "timestamps": [h.timestamp.isoformat() for h in history],
        "positions": [h.position for h in history],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
