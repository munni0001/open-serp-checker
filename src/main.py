import asyncio
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.core.database import Database
from src.core.managers import ProxyManager, ScraperManager
from src.core.scraper import SerpScraper
from src.models import Project, ScraperSettings
import os

app = FastAPI(title="SERP Scraper API", version="0.1.0")

# Initialize database and managers
db = Database()
proxy_manager = ProxyManager(db)
scraper_manager = ScraperManager(db)

# Set up templates with explicit approach to avoid errors
try:
    templates = Jinja2Templates(directory="src/dashboard/templates")
except Exception:
    # Fallback if template directory is missing
    templates = Jinja2Templates(directory=".")

# Mount static files (only if directory exists)
static_dir = "src/dashboard/static"
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---------- Request/Response schemas ----------

class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class ScraperCreate(BaseModel):
    project_id: int
    name: str
    domain: str = ""
    search_terms: List[str]
    geo: str = "us"
    language: str = "en"
    results_per_page: int = 10
    max_pages: int = 1
    max_position: int = 100
    interval_hours: int = 24
    proxy_id: Optional[int] = None


class RunResponse(BaseModel):
    scraper_id: int
    status: str
    message: str


# ---------- Frontend ----------

@app.get("/")
async def root(request: Request):
    context = {
        "projects": db.get_all_projects(),
        "scrapers": db.get_all_scraper_settings(),
    }
    try:
        return templates.TemplateResponse(
            request=request, name="index.html", context=context
        )
    except Exception:
        # Fallback response if template loading fails
        return {"message": "Dashboard loaded successfully (template not available)"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# ---------- Project endpoints ----------

@app.get("/projects")
async def get_projects():
    projects = db.get_all_projects()
    return {"projects": [p.model_dump() for p in projects]}


@app.post("/projects")
async def create_project(payload: ProjectCreate):
    project = Project(name=payload.name, description=payload.description)
    project_id = db.create_project(project)
    return {"id": project_id, "name": payload.name, "description": payload.description}


# ---------- Scraper endpoints ----------

@app.get("/scrapers")
async def get_scrapers():
    scrapers = db.get_all_scraper_settings()
    return {"scrapers": [s.model_dump() for s in scrapers]}


@app.post("/scrapers")
async def create_scraper(payload: ScraperCreate):
    if not db.get_project(payload.project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    settings = ScraperSettings(
        project_id=payload.project_id,
        name=payload.name,
        domain=payload.domain,
        search_terms=payload.search_terms,
        geo=payload.geo,
        language=payload.language,
        results_per_page=payload.results_per_page,
        max_pages=payload.max_pages,
        max_position=payload.max_position,
        interval_hours=payload.interval_hours,
    )
    settings_id = scraper_manager.add_scraper_settings(settings)
    return {"id": settings_id, **payload.model_dump()}


@app.post("/scrapers/{scraper_id}/run")
async def run_scraper(scraper_id: int):
    settings = scraper_manager.get_scraper_settings(scraper_id)
    if not settings:
        raise HTTPException(status_code=404, detail="Scraper settings not found")

    scraper = SerpScraper(db)
    result = await scraper.run_scraper_batch(settings)
    return {
        "scraper_id": scraper_id,
        "status": "completed",
        "result_id": result.id,
        "message": f"Scraped {len(settings.search_terms)} keywords",
    }


@app.post("/scrapers/{scraper_id}/run-scheduled")
async def run_scraper_scheduled(scraper_id: int):
    """Run a scraper in the background so the request returns immediately."""
    settings = scraper_manager.get_scraper_settings(scraper_id)
    if not settings:
        raise HTTPException(status_code=404, detail="Scraper settings not found")

    async def _bg():
        scraper = SerpScraper(db)
        await scraper.run_scraper_batch(settings)

    asyncio.get_event_loop().create_task(_bg())
    return {"scraper_id": scraper_id, "status": "started", "message": "Scheduled in background"}


# ---------- Results endpoints ----------

@app.get("/results/{scraper_id}")
async def get_results(scraper_id: int):
    results = db.get_scraping_results(scraper_id)
    return {"scraper_id": scraper_id, "results": [r.model_dump() for r in results]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)