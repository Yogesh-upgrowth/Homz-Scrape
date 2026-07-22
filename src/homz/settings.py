"""Central configuration. Everything is env-driven; nothing is hardcoded per-env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HOMZ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- runtime ----------------------------------------------------------
    env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # ---- database ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://homz:homz@localhost:5432/homz"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # ---- scraping ---------------------------------------------------------
    respect_robots: bool = True
    abort_on_block: bool = True
    max_concurrency: int = 4
    per_host_rps: float = 0.5
    per_host_burst: int = 2
    request_timeout: float = 30.0
    max_retries: int = 4
    raw_html_dir: Path = Path("./data/raw")
    store_raw_html: bool = True
    raw_html_retention_days: int = 14

    # ---- proxies ----------------------------------------------------------
    proxies: list[str] = Field(default_factory=list)
    proxy_strategy: str = "round_robin"
    proxy_cooldown_seconds: int = 300

    # ---- playwright -------------------------------------------------------
    playwright_headless: bool = True
    playwright_browser: str = "chromium"
    playwright_nav_timeout: int = 45_000
    playwright_max_contexts: int = 2

    # ---- reddit -----------------------------------------------------------
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "python:com.homzrealtor.intel:v1.0.0 (by /u/homz_bot)"
    reddit_subreddits: list[str] = Field(
        default_factory=lambda: ["gurgaon", "noida", "delhi", "india_real_estate"]
    )
    reddit_comment_limit: int = 50

    # ---- llm --------------------------------------------------------------
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = 4096
    llm_effort: str = "medium"
    llm_enabled: bool = True
    llm_use_batch: bool = True
    llm_batch_size: int = 500
    llm_cache_system_prompt: bool = True

    # ---- api --------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_page_size: int = 25
    api_max_page_size: int = 100
    #: Browser origins allowed to call the API. The widget on homzrealtor.com is
    #: cross-origin, so it needs an explicit entry. Every endpoint is public and
    #: read-only, so this list is about correctness, not access control.
    api_cors_origins: list[str] = Field(
        default_factory=lambda: [
            "https://www.homzrealtor.com",
            "https://homzrealtor.com",
            "http://localhost:8000",
            "http://localhost:3000",
            "http://127.0.0.1:8000",
        ]
    )
    #: Serve web/ from the API itself. Convenient in dev; in production put the
    #: widget on a CDN and turn this off.
    api_serve_web: bool = True

    @field_validator("proxies", "reddit_subreddits", "api_cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept both a JSON list and a plain comma-separated env string."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return v
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def sync_database_url(self) -> str:
        """psycopg URL for tooling (alembic, one-off scripts)."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
