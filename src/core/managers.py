from typing import List, Optional
from src.models import Proxy, ScraperSettings, Session
from src.core.database import Database

class ProxyManager:
    def __init__(self, db: Database):
        self.db = db
    
    def add_proxy(self, proxy: Proxy) -> int:
        """Add a new proxy to the database."""
        return self.db.create_proxy(proxy)
    
    def get_proxy(self, proxy_id: int) -> Optional[Proxy]:
        """Retrieve a proxy by ID."""
        return self.db.get_proxy(proxy_id)
    
    def get_all_proxies(self) -> List[Proxy]:
        """Retrieve all proxies from the database."""
        return self.db.get_all_proxies()

class ScraperManager:
    def __init__(self, db: Database):
        self.db = db
    
    def add_scraper_settings(self, settings: ScraperSettings) -> int:
        """Add new scraper settings to the database."""
        return self.db.create_scraper_settings(settings)
    
    def get_scraper_settings(self, settings_id: int) -> Optional[ScraperSettings]:
        """Retrieve scraper settings by ID."""
        return self.db.get_scraper_settings(settings_id)
    
    def update_scraper_settings(self, settings: ScraperSettings) -> bool:
        """Update existing scraper settings."""
        return self.db.update_scraper_settings(settings)
    
    def add_session(self, session: Session) -> int:
        """Add a new session to the database."""
        return self.db.create_session(session)
    
    def get_session(self, session_id: int) -> Optional[Session]:
        """Retrieve a session by ID."""
        return self.db.get_session(session_id)

# The proxy manager and scraper manager are designed to be extended with
# actual proxy providers and scraping logic as needed.