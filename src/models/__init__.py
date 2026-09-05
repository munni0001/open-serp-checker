from typing import Literal, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


class ProxyType(str, Enum):
    FREE = "free"
    PAID = "paid"


class ProxyProvider(str, Enum):
    MANUAL = "manual"
    DECODO = "decodo"
    OXYLABS = "oxylabs"
    SHIFTER = "shifter"
    DATAFORSEO = "dataforseo"
    SERPER = "serper"
    OTHER = "other"


class Project(BaseModel):
    id: Optional[int] = None
    name: str
    description: Optional[str] = ""
    domain: Optional[str] = ""
    default_geo: str = "us"
    default_interval_hours: int = 24
    default_max_position: int = 100
    default_results_per_page: int = 10
    default_proxy_id: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


ProxyMode = Literal["rotating", "sticky"]


class Proxy(BaseModel):
    id: Optional[int] = None
    name: str
    type: ProxyType
    provider: ProxyProvider
    username: Optional[str] = ""
    password: Optional[str] = ""
    host: Optional[str] = ""
    port: Optional[int] = 0
    mode: ProxyMode = "rotating"
    sticky_duration_min: Optional[int] = None
    sticky_sessions: int = 1
    ip_blocklist_enabled: bool = False
    ip_blocklist_ttl_days: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Keyword(BaseModel):
    id: Optional[int] = None
    project_id: int
    term: str
    geo: str = "us"
    engine: str = "bing"
    interval_hours: int = 24
    max_position: int = 100
    results_per_page: int = 10
    proxy_id: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_run_at: Optional[datetime] = None


class KeywordResult(BaseModel):
    id: Optional[int] = None
    keyword_id: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    position: Optional[int] = None
    url: Optional[str] = ""
    found: bool = False
    status: Literal["ok", "blocked", "error"] = "ok"
    error: Optional[str] = None
    bytes_downloaded: int = 0
