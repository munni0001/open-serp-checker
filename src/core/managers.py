from typing import List, Optional

from src.core.database import Database
from src.core.scraper import SerpScraper
from src.models import Keyword, KeywordResult, Project, Proxy


class ProxyManager:
    def __init__(self, db: Database):
        self.db = db

    def add_proxy(self, proxy: Proxy) -> int:
        return self.db.create_proxy(proxy)

    def get_proxy(self, proxy_id: int) -> Optional[Proxy]:
        return self.db.get_proxy(proxy_id)

    def get_all_proxies(self) -> List[Proxy]:
        return self.db.get_all_proxies()


class KeywordManager:
    def __init__(self, db: Database):
        self.db = db

    def add_keyword(self, project: Project, term: str, **overrides) -> int:
        """Create a keyword, seeding config from the project's defaults."""
        term = term.strip()
        if not term:
            raise ValueError("Keyword term cannot be empty")
        keyword = Keyword(
            project_id=project.id,
            term=term,
            geo=overrides.get("geo") or project.default_geo,
            engine=overrides.get("engine") or "bing",
            interval_hours=overrides.get("interval_hours") or project.default_interval_hours,
            max_position=overrides.get("max_position") or project.default_max_position,
            results_per_page=overrides.get("results_per_page") or project.default_results_per_page,
        )
        return self.db.create_keyword(keyword)

    async def run_keyword(self, keyword_id: int, proxy=None) -> KeywordResult:
        keyword = self.db.get_keyword(keyword_id)
        if keyword is None:
            raise ValueError(f"Keyword {keyword_id} not found")
        project = self.db.get_project(keyword.project_id)
        if project is None:
            raise ValueError(f"Project {keyword.project_id} not found")
        scraper = SerpScraper(self.db)
        return await scraper.scrape_keyword(keyword, project, proxy=proxy)
