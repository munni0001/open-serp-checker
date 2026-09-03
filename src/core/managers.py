from typing import List, Optional
from src.models import Proxy, ScraperSettings
from src.core.database import Database

class ProxyManager:
    def __init__(self, db: Database):
        self.db = db

    def add_proxy(self, proxy: Proxy) -> int:
        return self.db.create_proxy(proxy)

    def get_proxy(self, proxy_id: int) -> Optional[Proxy]:
        return self.db.get_proxy(proxy_id)

    def get_all_proxies(self) -> List[Proxy]:
        return self.db.get_all_proxies()

class ScraperManager:
    def __init__(self, db: Database):
        self.db = db

    def add_scraper_settings(self, settings: ScraperSettings) -> int:
        return self.db.create_scraper_settings(settings)

    def get_scraper_settings(self, settings_id: int) -> Optional[ScraperSettings]:
        return self.db.get_scraper_settings(settings_id)

    def update_scraper_settings(self, settings: ScraperSettings) -> bool:
        return self.db.update_scraper_settings(settings)
