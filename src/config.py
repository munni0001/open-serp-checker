from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_path: str = "open_serp_checker.db"
    host: str = "127.0.0.1"
    port: int = 8000
    serp_proxy_url: Optional[str] = None

    # SearXNG — URL of a self-hosted or private instance. Public instances
    # universally rate-limit or anti-bot the JSON API, so this is unset by
    # default. Spin up your own: `docker run -p 8080:8080 searxng/searxng`.
    searxng_url: Optional[str] = None

    # DataForSEO — Basic auth. Paid, parsed JSON, no captcha handling needed.
    dataforseo_login: Optional[str] = None
    dataforseo_password: Optional[str] = None

    # Serper — API key via X-API-KEY header. Paid, cheaper DataForSEO alternative.
    serper_api_key: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
