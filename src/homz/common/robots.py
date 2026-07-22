"""robots.txt compliance gate.

Fetched once per host, cached with a TTL, and consulted before every request.
When `HOMZ_RESPECT_ROBOTS=true` (the default) a disallowed URL is never
fetched — the crawler logs and skips it.

Also reads `Crawl-delay` and applies it to the host's rate limiter, so a site
that asks for one request every 10 seconds gets exactly that.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

_CACHE_TTL_SECONDS = 3600.0


@dataclass
class RobotsEntry:
    parser: urllib.robotparser.RobotFileParser | None
    fetched_at: float
    crawl_delay: float | None
    sitemaps: list[str]
    reachable: bool

    @property
    def stale(self) -> bool:
        return (time.monotonic() - self.fetched_at) > _CACHE_TTL_SECONDS


class RobotsGate:
    def __init__(self, *, respect: bool | None = None, timeout: float = 10.0) -> None:
        self.respect = settings.respect_robots if respect is None else respect
        self._timeout = timeout
        self._cache: dict[str, RobotsEntry] = {}

    @staticmethod
    def _robots_url(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        return origin, urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def _load(self, client: httpx.AsyncClient, url: str) -> RobotsEntry:
        origin, robots_url = self._robots_url(url)
        entry = self._cache.get(origin)
        if entry is not None and not entry.stale:
            return entry

        parser = urllib.robotparser.RobotFileParser()
        crawl_delay: float | None = None
        sitemaps: list[str] = []
        reachable = False

        try:
            response = await client.get(robots_url, timeout=self._timeout)
            if response.status_code == 200:
                text = response.text
                parser.parse(text.splitlines())
                reachable = True
                for line in text.splitlines():
                    lowered = line.strip().lower()
                    if lowered.startswith("sitemap:"):
                        sitemaps.append(line.split(":", 1)[1].strip())
            elif response.status_code in (401, 403):
                # Per RFC 9309 an inaccessible robots.txt means "disallow all".
                parser.parse(["User-agent: *", "Disallow: /"])
                reachable = True
                log.warning("robots.forbidden_treating_as_disallow_all", origin=origin)
            else:
                # 404 / 5xx → nothing is disallowed.
                parser.parse(["User-agent: *", "Allow: /"])
        except (httpx.HTTPError, UnicodeDecodeError) as exc:
            log.warning("robots.fetch_failed", origin=origin, error=str(exc))
            parser.parse(["User-agent: *", "Allow: /"])

        if reachable:
            try:
                delay = parser.crawl_delay("*")
                crawl_delay = float(delay) if delay else None
            except Exception:  # pragma: no cover - stdlib quirk on odd files
                crawl_delay = None

        entry = RobotsEntry(
            parser=parser,
            fetched_at=time.monotonic(),
            crawl_delay=crawl_delay,
            sitemaps=sitemaps,
            reachable=reachable,
        )
        self._cache[origin] = entry
        log.info(
            "robots.loaded",
            origin=origin,
            reachable=reachable,
            crawl_delay=crawl_delay,
            sitemaps=len(sitemaps),
        )
        return entry

    async def allowed(self, client: httpx.AsyncClient, url: str, user_agent: str = "*") -> bool:
        if not self.respect:
            return True
        entry = await self._load(client, url)
        if entry.parser is None:
            return True
        try:
            return entry.parser.can_fetch(user_agent, url)
        except Exception:  # pragma: no cover
            return True

    async def crawl_delay(self, client: httpx.AsyncClient, url: str) -> float | None:
        entry = await self._load(client, url)
        return entry.crawl_delay

    async def sitemaps(self, client: httpx.AsyncClient, url: str) -> list[str]:
        entry = await self._load(client, url)
        return list(entry.sitemaps)


class RobotsDisallowed(PermissionError):
    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows fetching {url}")
        self.url = url
