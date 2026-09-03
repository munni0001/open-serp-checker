from typing import Optional, List, Dict
from pydantic import BaseModel
from datetime import datetime
from enum import Enum

class ProxyType(str, Enum):
    FREE = "free"
    PAID = "paid"

class ProxyProvider(str, Enum):
    MANUAL = "manual"
    DATAFORSEO = "dataforseo"
    SERPER = "serper"
    # Add more providers as needed

class ScraperStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class Project(BaseModel):
    id: Optional[int] = None
    name: str
    description: Optional[str] = ""
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()

class Proxy(BaseModel):
    id: Optional[int] = None
    name: str
    type: ProxyType
    provider: ProxyProvider
    username: Optional[str] = ""
    password: Optional[str] = ""
    host: Optional[str] = ""
    port: Optional[int] = 0
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()

class ScraperSettings(BaseModel):
    id: Optional[int] = None
    project_id: int
    name: str
    domain: Optional[str] = ""  # Target domain to track rankings for
    search_terms: List[str]  # List of search terms
    geo: str  # e.g., "us", "uk"
    language: str = "en"
    results_per_page: int = 10
    max_pages: int = 1
    max_position: int = 100  # Only track results up to this position
    interval_hours: int = 24
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()

class ScrapingResult(BaseModel):
    id: Optional[int] = None
    scraper_id: int
    timestamp: datetime
    rankings: Dict[str, Dict[str, object]]  # e.g., {"term": {"position": 1, "url": "http://example.com"}}
    created_at: datetime = datetime.now()

class Session(BaseModel):
    id: Optional[int] = None
    project_id: int
    name: str
    description: Optional[str] = ""
    status: ScraperStatus = ScraperStatus.PENDING
    settings: 'ScraperSettings'  # Use string annotation to avoid circular import
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()