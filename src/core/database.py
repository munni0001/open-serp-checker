import sqlite3
from typing import List, Optional
from datetime import datetime
from src.models import Project, Proxy, ScraperSettings, ScrapingResult, Session

class Database:
    def __init__(self, db_path: str = "serp_scraper.db"):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Initialize the database and create tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create Projects table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        
        # Create Proxies table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                provider TEXT NOT NULL,
                username TEXT,
                password TEXT,
                host TEXT,
                port INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        
        # Create ScraperSettings table
        # NOTE: domain and max_position are appended at the END so that the
        # column order matches a legacy table migrated via ALTER TABLE ADD COLUMN.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scraper_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                search_terms TEXT NOT NULL,
                geo TEXT NOT NULL,
                language TEXT DEFAULT 'en',
                results_per_page INTEGER DEFAULT 10,
                max_pages INTEGER DEFAULT 1,
                interval_hours INTEGER DEFAULT 24,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                domain TEXT DEFAULT '',
                max_position INTEGER DEFAULT 100,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        ''')

        # Migrate legacy tables that are missing the new columns
        columns = [row[1] for row in cursor.execute('PRAGMA table_info(scraper_settings)').fetchall()]
        if 'domain' not in columns:
            cursor.execute("ALTER TABLE scraper_settings ADD COLUMN domain TEXT DEFAULT ''")
        if 'max_position' not in columns:
            cursor.execute("ALTER TABLE scraper_settings ADD COLUMN max_position INTEGER DEFAULT 100")
        
        # Create ScrapingResults table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scraping_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraper_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                rankings TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (scraper_id) REFERENCES scraper_settings (id)
            )
        ''')
        
        # Create Sessions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                settings_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects (id),
                FOREIGN KEY (settings_id) REFERENCES scraper_settings (id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def create_project(self, project: Project) -> int:
        """Create a new project."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO projects (name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        ''', (project.name, project.description, project.created_at.isoformat(), project.updated_at.isoformat()))
        
        project_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return project_id
    
    def get_project(self, project_id: int) -> Optional[Project]:
        """Retrieve a project by ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM projects WHERE id = ?', (project_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            return Project(
                id=row[0],
                name=row[1],
                description=row[2],
                created_at=datetime.fromisoformat(row[3]),
                updated_at=datetime.fromisoformat(row[4])
            )
        return None

    def get_all_projects(self) -> List[Project]:
        """Retrieve all projects from the database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM projects ORDER BY id DESC')
        rows = cursor.fetchall()

        conn.close()

        projects = []
        for row in rows:
            projects.append(Project(
                id=row[0],
                name=row[1],
                description=row[2],
                created_at=datetime.fromisoformat(row[3]),
                updated_at=datetime.fromisoformat(row[4])
            ))
        return projects

    def create_proxy(self, proxy: Proxy) -> int:
        """Create a new proxy configuration."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO proxies (name, type, provider, username, password, host, port, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            proxy.name, proxy.type, proxy.provider, proxy.username, proxy.password,
            proxy.host, proxy.port, proxy.created_at.isoformat(), proxy.updated_at.isoformat()
        ))
        
        proxy_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return proxy_id
    
    def get_proxy(self, proxy_id: int) -> Optional[Proxy]:
        """Retrieve a proxy by ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM proxies WHERE id = ?', (proxy_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            return Proxy(
                id=row[0],
                name=row[1],
                type=row[2],
                provider=row[3],
                username=row[4],
                password=row[5],
                host=row[6],
                port=row[7],
                created_at=datetime.fromisoformat(row[8]),
                updated_at=datetime.fromisoformat(row[9])
            )
        return None

    def get_all_proxies(self) -> List[Proxy]:
        """Retrieve all proxies from the database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM proxies')
        rows = cursor.fetchall()
        
        conn.close()
        
        proxies = []
        for row in rows:
            proxies.append(Proxy(
                id=row[0],
                name=row[1],
                type=row[2],
                provider=row[3],
                username=row[4],
                password=row[5],
                host=row[6],
                port=row[7],
                created_at=datetime.fromisoformat(row[8]),
                updated_at=datetime.fromisoformat(row[9])
            ))
        return proxies

    def create_scraper_settings(self, settings: ScraperSettings) -> int:
        """Create new scraper settings."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Convert list of search terms to comma-separated string for storage
        search_terms_str = ','.join(settings.search_terms)
        
        cursor.execute('''
            INSERT INTO scraper_settings (project_id, name, search_terms, geo, language,
            results_per_page, max_pages, interval_hours, created_at, updated_at, domain, max_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            settings.project_id, settings.name, search_terms_str, settings.geo,
            settings.language, settings.results_per_page, settings.max_pages,
            settings.interval_hours, settings.created_at.isoformat(), settings.updated_at.isoformat(),
            settings.domain, settings.max_position
        ))
        
        settings_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return settings_id
    
    def get_scraper_settings(self, settings_id: int) -> Optional[ScraperSettings]:
        """Retrieve scraper settings by ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM scraper_settings WHERE id = ?', (settings_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            # Convert comma-separated string back to list
            search_terms = row[3].split(',') if row[3] else []
            return ScraperSettings(
                id=row[0],
                project_id=row[1],
                name=row[2],
                search_terms=search_terms,
                geo=row[4],
                language=row[5],
                results_per_page=row[6],
                max_pages=row[7],
                interval_hours=row[8],
                created_at=datetime.fromisoformat(row[9]),
                updated_at=datetime.fromisoformat(row[10]),
                domain=row[11],
                max_position=row[12]
            )
        return None

    def get_all_scraper_settings(self) -> List[ScraperSettings]:
        """Retrieve all scraper settings from the database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM scraper_settings ORDER BY id DESC')
        rows = cursor.fetchall()

        conn.close()

        settings_list = []
        for row in rows:
            search_terms = row[3].split(',') if row[3] else []
            settings_list.append(ScraperSettings(
                id=row[0],
                project_id=row[1],
                name=row[2],
                search_terms=search_terms,
                geo=row[4],
                language=row[5],
                results_per_page=row[6],
                max_pages=row[7],
                interval_hours=row[8],
                created_at=datetime.fromisoformat(row[9]),
                updated_at=datetime.fromisoformat(row[10]),
                domain=row[11],
                max_position=row[12]
            ))
        return settings_list

    def create_scraping_result(self, result: ScrapingResult) -> int:
        """Create a new scraping result."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO scraping_results (scraper_id, timestamp, rankings, created_at)
            VALUES (?, ?, ?, ?)
        ''', (
            result.scraper_id, result.timestamp.isoformat(), str(result.rankings), result.created_at.isoformat()
        ))
        
        result_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return result_id
    
    def get_scraping_results(self, scraper_id: int) -> List[ScrapingResult]:
        """Retrieve all scraping results for a scraper."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM scraping_results WHERE scraper_id = ?', (scraper_id,))
        rows = cursor.fetchall()
        
        conn.close()
        
        results = []
        for row in rows:
            results.append(ScrapingResult(
                id=row[0],
                scraper_id=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                rankings=eval(row[3]),  # This is not secure but works for our case
                created_at=datetime.fromisoformat(row[4])
            ))
        return results

    def create_session(self, session: Session) -> int:
        """Create a new session."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # First, save the settings if they don't exist yet
        if session.settings.id is None:
            settings_id = self.create_scraper_settings(session.settings)
            session.settings.id = settings_id
        else:
            # If settings already exist, update them
            self.update_scraper_settings(session.settings)
            
        cursor.execute('''
            INSERT INTO sessions (project_id, name, description, status, settings_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            session.project_id, session.name, session.description, session.status,
            session.settings.id, session.created_at.isoformat(), session.updated_at.isoformat()
        ))
        
        session_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return session_id
    
    def get_session(self, session_id: int) -> Optional[Session]:
        """Retrieve a session by ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM sessions WHERE id = ?', (session_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            # Get the associated settings
            settings = self.get_scraper_settings(row[5])  # settings_id is at index 5
            if settings is None:
                return None
                
            return Session(
                id=row[0],
                project_id=row[1],
                name=row[2],
                description=row[3],
                status=row[4],
                settings=settings,
                created_at=datetime.fromisoformat(row[6]),
                updated_at=datetime.fromisoformat(row[7])
            )
        return None

    def update_scraper_settings(self, settings: ScraperSettings) -> bool:
        """Update existing scraper settings."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Convert list of search terms to comma-separated string for storage
        search_terms_str = ','.join(settings.search_terms)
        
        cursor.execute('''
            UPDATE scraper_settings SET 
                project_id=?, name=?, search_terms=?, geo=?, language=?, 
                results_per_page=?, max_pages=?, interval_hours=?, updated_at=?, domain=?, max_position=?
            WHERE id=?
        ''', (
            settings.project_id, settings.name, search_terms_str, settings.geo,
            settings.language, settings.results_per_page, settings.max_pages,
            settings.interval_hours, settings.updated_at.isoformat(),
            settings.domain, settings.max_position, settings.id
        ))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success