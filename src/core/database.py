import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

from src.models import Keyword, KeywordResult, Project, Proxy


class Database:
    def __init__(self, db_path: str = "serp_scraper.db"):
        self.db_path = db_path
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self):
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                domain TEXT DEFAULT '',
                default_geo TEXT DEFAULT 'us',
                default_interval_hours INTEGER DEFAULT 24,
                default_max_position INTEGER DEFAULT 100,
                default_results_per_page INTEGER DEFAULT 10,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')

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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                term TEXT NOT NULL,
                geo TEXT NOT NULL DEFAULT 'us',
                engine TEXT NOT NULL DEFAULT 'bing',
                interval_hours INTEGER NOT NULL DEFAULT 24,
                max_position INTEGER NOT NULL DEFAULT 100,
                results_per_page INTEGER NOT NULL DEFAULT 10,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_keywords_project ON keywords(project_id)')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keyword_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                position INTEGER,
                url TEXT,
                found INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (keyword_id) REFERENCES keywords (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_keyword_results_kw_ts ON keyword_results(keyword_id, timestamp DESC)')

        conn.commit()
        conn.close()

    # ---------- Projects ----------

    def _row_to_project(self, row) -> Project:
        return Project(
            id=row[0],
            name=row[1],
            description=row[2] or "",
            domain=row[3] or "",
            default_geo=row[4],
            default_interval_hours=row[5],
            default_max_position=row[6],
            default_results_per_page=row[7],
            created_at=datetime.fromisoformat(row[8]),
            updated_at=datetime.fromisoformat(row[9]),
        )

    def create_project(self, project: Project) -> int:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO projects (name, description, domain, default_geo,
                default_interval_hours, default_max_position, default_results_per_page,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            project.name, project.description, project.domain, project.default_geo,
            project.default_interval_hours, project.default_max_position, project.default_results_per_page,
            project.created_at.isoformat(), project.updated_at.isoformat(),
        ))
        project_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return project_id

    def get_project(self, project_id: int) -> Optional[Project]:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM projects WHERE id = ?', (project_id,))
        row = cursor.fetchone()
        conn.close()
        return self._row_to_project(row) if row else None

    def get_all_projects(self) -> List[Project]:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM projects ORDER BY id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [self._row_to_project(r) for r in rows]

    def update_project(self, project_id: int, **fields) -> None:
        allowed = {
            'name', 'description', 'domain', 'default_geo',
            'default_interval_hours', 'default_max_position', 'default_results_per_page',
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        updates['updated_at'] = datetime.utcnow().isoformat()
        cols = ', '.join(f'{k}=?' for k in updates)
        conn = self._connect()
        conn.execute(f'UPDATE projects SET {cols} WHERE id=?', (*updates.values(), project_id))
        conn.commit()
        conn.close()

    def delete_project(self, project_id: int) -> bool:
        conn = self._connect()
        cursor = conn.execute('DELETE FROM projects WHERE id = ?', (project_id,))
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
        return deleted

    def count_keywords_by_project(self) -> dict:
        conn = self._connect()
        rows = conn.execute(
            'SELECT project_id, COUNT(*) FROM keywords GROUP BY project_id'
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

    # ---------- Proxies ----------

    def create_proxy(self, proxy: Proxy) -> int:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO proxies (name, type, provider, username, password, host, port, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            proxy.name, proxy.type, proxy.provider, proxy.username, proxy.password,
            proxy.host, proxy.port, proxy.created_at.isoformat(), proxy.updated_at.isoformat(),
        ))
        proxy_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return proxy_id

    def get_proxy(self, proxy_id: int) -> Optional[Proxy]:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM proxies WHERE id = ?', (proxy_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return Proxy(
            id=row[0], name=row[1], type=row[2], provider=row[3],
            username=row[4], password=row[5], host=row[6], port=row[7],
            created_at=datetime.fromisoformat(row[8]),
            updated_at=datetime.fromisoformat(row[9]),
        )

    def get_all_proxies(self) -> List[Proxy]:
        conn = self._connect()
        rows = conn.execute('SELECT * FROM proxies').fetchall()
        conn.close()
        return [
            Proxy(
                id=r[0], name=r[1], type=r[2], provider=r[3],
                username=r[4], password=r[5], host=r[6], port=r[7],
                created_at=datetime.fromisoformat(r[8]),
                updated_at=datetime.fromisoformat(r[9]),
            )
            for r in rows
        ]

    # ---------- Keywords ----------

    def _row_to_keyword(self, row) -> Keyword:
        return Keyword(
            id=row[0],
            project_id=row[1],
            term=row[2],
            geo=row[3],
            engine=row[4],
            interval_hours=row[5],
            max_position=row[6],
            results_per_page=row[7],
            created_at=datetime.fromisoformat(row[8]),
            updated_at=datetime.fromisoformat(row[9]),
            last_run_at=datetime.fromisoformat(row[10]) if row[10] else None,
        )

    def create_keyword(self, keyword: Keyword) -> int:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO keywords (project_id, term, geo, engine, interval_hours,
                max_position, results_per_page, created_at, updated_at, last_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            keyword.project_id, keyword.term, keyword.geo, keyword.engine,
            keyword.interval_hours, keyword.max_position, keyword.results_per_page,
            keyword.created_at.isoformat(), keyword.updated_at.isoformat(),
            keyword.last_run_at.isoformat() if keyword.last_run_at else None,
        ))
        keyword_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return keyword_id

    def get_keyword(self, keyword_id: int) -> Optional[Keyword]:
        conn = self._connect()
        row = conn.execute('SELECT * FROM keywords WHERE id = ?', (keyword_id,)).fetchone()
        conn.close()
        return self._row_to_keyword(row) if row else None

    def get_keywords_for_project(self, project_id: int) -> List[Keyword]:
        conn = self._connect()
        rows = conn.execute(
            'SELECT * FROM keywords WHERE project_id = ? ORDER BY id ASC',
            (project_id,),
        ).fetchall()
        conn.close()
        return [self._row_to_keyword(r) for r in rows]

    def update_keyword(self, keyword_id: int, **fields) -> None:
        allowed = {'term', 'geo', 'engine', 'interval_hours', 'max_position', 'results_per_page'}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        updates['updated_at'] = datetime.utcnow().isoformat()
        cols = ', '.join(f'{k}=?' for k in updates)
        conn = self._connect()
        conn.execute(f'UPDATE keywords SET {cols} WHERE id=?', (*updates.values(), keyword_id))
        conn.commit()
        conn.close()

    def touch_keyword_run(self, keyword_id: int, ts: datetime) -> None:
        conn = self._connect()
        conn.execute(
            'UPDATE keywords SET last_run_at=?, updated_at=? WHERE id=?',
            (ts.isoformat(), datetime.utcnow().isoformat(), keyword_id),
        )
        conn.commit()
        conn.close()

    def delete_keyword(self, keyword_id: int) -> bool:
        conn = self._connect()
        cursor = conn.execute('DELETE FROM keywords WHERE id = ?', (keyword_id,))
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
        return deleted

    # ---------- Keyword results ----------

    def create_keyword_result(self, result: KeywordResult) -> int:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO keyword_results (keyword_id, timestamp, position, url, found)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            result.keyword_id, result.timestamp.isoformat(),
            result.position, result.url, 1 if result.found else 0,
        ))
        result_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return result_id

    def get_keyword_history(self, keyword_id: int, limit: int = 200) -> List[KeywordResult]:
        conn = self._connect()
        rows = conn.execute(
            'SELECT * FROM keyword_results WHERE keyword_id = ? ORDER BY timestamp ASC LIMIT ?',
            (keyword_id, limit),
        ).fetchall()
        conn.close()
        return [
            KeywordResult(
                id=r[0], keyword_id=r[1],
                timestamp=datetime.fromisoformat(r[2]),
                position=r[3], url=r[4] or "", found=bool(r[5]),
            )
            for r in rows
        ]

    def get_keyword_summary(self, project_id: int) -> List[dict]:
        """
        For every keyword in the project return: keyword row + latest/1d-ago/7d-ago
        positions. Uses the most recent result in each time window
        (now = latest ever; 1d = latest ≤ now-1d; 7d = latest ≤ now-7d).
        """
        keywords = self.get_keywords_for_project(project_id)
        if not keywords:
            return []

        now = datetime.utcnow()
        cutoff_1d = (now - timedelta(days=1)).isoformat()
        cutoff_7d = (now - timedelta(days=7)).isoformat()

        conn = self._connect()
        rows = []
        for kw in keywords:
            latest = conn.execute(
                'SELECT position, timestamp FROM keyword_results '
                'WHERE keyword_id = ? ORDER BY timestamp DESC LIMIT 1',
                (kw.id,),
            ).fetchone()
            pos_1d = conn.execute(
                'SELECT position FROM keyword_results '
                'WHERE keyword_id = ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1',
                (kw.id, cutoff_1d),
            ).fetchone()
            pos_7d = conn.execute(
                'SELECT position FROM keyword_results '
                'WHERE keyword_id = ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1',
                (kw.id, cutoff_7d),
            ).fetchone()
            rows.append({
                'keyword': kw.model_dump(mode='json'),
                'latest_position': latest[0] if latest else None,
                'latest_timestamp': latest[1] if latest else None,
                'position_1d_ago': pos_1d[0] if pos_1d else None,
                'position_7d_ago': pos_7d[0] if pos_7d else None,
            })
        conn.close()
        return rows
