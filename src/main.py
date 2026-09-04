import time
from typing import List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import settings
from src.core.database import Database
from src.core.managers import KeywordManager, ProxyManager
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


class ProxyPatch(BaseModel):
    name: Optional[str] = None
    type: Optional[ProxyType] = None
    provider: Optional[ProxyProvider] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None


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
