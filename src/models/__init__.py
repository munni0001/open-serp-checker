from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum

class ProxyType(str, Enum):
    FREE = "free"
    PAID = "paid"

class ProxyProvider(str, Enum):
    MANUAL = "manual"
    DATAFORSEO = "dataforseo"
    SERPER = "serper"

class ScraperStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class Project(BaseModel):
    id: Optional[int] = None
    name: str
    description: Optional[str] = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Proxy(BaseModel):
    id: Optional[int] = None
    name: str
    type: ProxyType
    provider: ProxyProvider
    username: Optional[str] = ""
    password: Optional[str] = ""
    host: Optional[str] = ""
    port: Optional[int] = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class ScraperSettings(BaseModel):
    id: Optional[int] = None
    project_id: int
    name: str
    domain: Optional[str] = ""
    search_terms: List[str]
    geo: str
    language: str = "en"
    results_per_page: int = 10
    max_pages: int = 1
    max_position: int = 100
    interval_hours: int = 24
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class ScrapingResult(BaseModel):
    id: Optional[int] = None
    scraper_id: int
    timestamp: datetime
    rankings: Dict[str, Dict[str, object]]
    created_at: datetime = Field(default_factory=datetime.utcnow)
