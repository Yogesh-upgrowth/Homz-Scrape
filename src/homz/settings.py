"""Central configuration. Everything is env-driven; nothing is hardcoded per-env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: List settings are given as plain comma-separated strings in .env
#: (HOMZ_PROXIES=a,b), not JSON. Without NoDecode, pydantic-settings tries to
#: json.loads() the raw value at the *source* level — before any field
#: validator runs — so `HOMZ_PROXIES=` (empty) raises SettingsError instead of
#: reaching `_split_csv`. NoDecode hands the raw string to the validator.
CsvList = Annotated[list[str], NoDecode]


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

    # ---- database (MongoDB Atlas) -----------------------------------------
    #: mongodb+srv://user:pass@cluster.xxxxx.mongodb.net/?retryWrites=true&w=majority
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "homz"
    mongodb_max_pool_size: int = 20
    mongodb_min_pool_size: int = 0
    mongodb_timeout_ms: int = 20_000
    #: Atlas Search index names. Only used when the cluster is Atlas.
    atlas_search_index: str = "properties_search"
    atlas_autocomplete_index: str = "properties_autocomplete"
    atlas_reddit_index: str = "reddit_search"
    #: Force the search backend instead of auto-detecting the cluster type.
    #: auto | atlas | text
    search_backend: str = "auto"

    # ---- on-demand fill ---------------------------------------------------
    #: When a search returns fewer than this many rows, queue a fill task so
    #: the gap is scraped and the next identical search is a cache hit.
    ondemand_enabled: bool = True
    ondemand_min_results: int = 5
    #: Don't re-queue the same query more often than this.
    ondemand_cooldown_minutes: int = 360
    #: Hard ceiling on tasks created per day. Demand-driven crawling is still
    #: crawling — this is what stops a bot hammering search from turning into
    #: a thousand requests against a portal.
    ondemand_daily_budget: int = 500
    ondemand_task_ttl_hours: int = 24

    # ---- ingest (client-submitted scrapes) --------------------------------
    #: Bearer token clients must present to POST scraped payloads. Empty
    #: disables the ingest endpoints entirely — never leave it unset in prod,
    #: an open ingest endpoint lets anyone poison the warehouse.
    ingest_token: str = ""
    ingest_max_payload_bytes: int = 4_000_000
    ingest_rate_limit_per_minute: int = 120

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
    proxies: CsvList = Field(default_factory=list)
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
    reddit_subreddits: CsvList = Field(
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
    api_cors_origins: CsvList = Field(
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
        """Accept a JSON list or a plain comma-separated env string.

        These fields are `NoDecode`, so pydantic-settings hands over the raw
        string and this validator owns the parsing entirely — including the
        JSON form, which is no longer decoded for us.
        """
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                import json

                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    # Fall through to CSV rather than failing: a malformed
                    # JSON list is far more likely to be a stray bracket than
                    # a real intent to pass JSON.
                    pass
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def is_atlas(self) -> bool:
        """Atlas clusters use the mongodb+srv:// scheme or a *.mongodb.net host.

        Only a hint — `homz.db.mongo.detect_backend()` confirms against the
        live server, since a self-hosted cluster can sit behind any hostname.
        """
        uri = self.mongodb_uri.lower()
        return uri.startswith("mongodb+srv://") or "mongodb.net" in uri

    @property
    def redacted_mongodb_uri(self) -> str:
        """Safe for logs — strips credentials."""
        import re

        return re.sub(r"://[^@/]+@", "://***:***@", self.mongodb_uri)

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
