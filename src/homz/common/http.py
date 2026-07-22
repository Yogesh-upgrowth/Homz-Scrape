"""The async HTTP fetcher — where every piece of middleware composes.

One call to `Fetcher.get()` runs, in order:

    robots gate → rate limiter slot → proxy selection → UA rotation
      → request → block detection → retry/backoff → raw archive → soup

Scrapers never touch httpx directly; they get `FetchResult` objects with a
parsed `BeautifulSoup` and the raw-archive key already attached.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

from homz.common.captcha import BlockedError, BlockKind, detect_block
from homz.common.proxy import ProxyEntry, ProxyPool
from homz.common.ratelimit import RateLimiter
from homz.common.rawstore import RawStore
from homz.common.retry import RetryPolicy, is_hard_block, with_retry
from homz.common.robots import RobotsDisallowed, RobotsGate
from homz.common.useragent import UserAgentRotator
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    text: str
    headers: dict[str, str]
    elapsed_s: float
    from_browser: bool = False
    raw_key: str | None = None
    _soup: BeautifulSoup | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def soup(self) -> BeautifulSoup:
        """Parsed DOM. lxml when available (it is, via requirements)."""
        if self._soup is None:
            self._soup = BeautifulSoup(self.text, "lxml")
        return self._soup

    def json(self) -> Any:
        import orjson

        return orjson.loads(self.text)


class Fetcher:
    """Shared HTTP client for one scrape run.

    Use as an async context manager so connections and the robots cache are
    reused across the whole job:

        async with Fetcher(source="magicbricks") as fetcher:
            result = await fetcher.get(url)
    """

    def __init__(
        self,
        *,
        source: str,
        rate_limiter: RateLimiter | None = None,
        proxy_pool: ProxyPool | None = None,
        robots: RobotsGate | None = None,
        raw_store: RawStore | None = None,
        retry_policy: RetryPolicy | None = None,
        default_headers: dict[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool = True,
    ) -> None:
        self.source = source
        self.limiter = rate_limiter or RateLimiter()
        self.proxies = proxy_pool or ProxyPool()
        self.robots = robots or RobotsGate()
        self.raw_store = raw_store or RawStore()
        self.retry_policy = retry_policy or RetryPolicy.from_settings()
        self.ua = UserAgentRotator()
        self.default_headers = default_headers or {}
        self.timeout = timeout or settings.request_timeout
        self.follow_redirects = follow_redirects

        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._crawl_delay_applied: set[str] = set()
        self.stats: dict[str, int] = {
            "requests": 0,
            "ok": 0,
            "blocked": 0,
            "robots_skipped": 0,
            "errors": 0,
        }

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Fetcher:
        await self._client_for(None)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            for client in self._clients.values():
                await client.aclose()
            self._clients.clear()

    async def _client_for(self, proxy_url: str | None) -> httpx.AsyncClient:
        async with self._lock:
            client = self._clients.get(proxy_url)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    proxy=proxy_url,
                    timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
                    follow_redirects=self.follow_redirects,
                    http2=True,
                    limits=httpx.Limits(
                        max_connections=settings.max_concurrency * 2,
                        max_keepalive_connections=settings.max_concurrency,
                    ),
                )
                self._clients[proxy_url] = client
            return client

    # -- core ---------------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        archive: bool = True,
        archive_ext: str = "html",
        allow_block: bool = False,
        expect_json: bool = False,
    ) -> FetchResult:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            archive=archive,
            archive_ext=archive_ext,
            allow_block=allow_block,
            expect_json=expect_json,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: Any | None = None,
        archive: bool = True,
        archive_ext: str = "html",
        allow_block: bool = False,
        expect_json: bool = False,
    ) -> FetchResult:
        host = RateLimiter.host_of(url)

        # --- robots gate ---------------------------------------------------
        probe_client = await self._client_for(None)
        ua_profile = self.ua.for_host(host)
        if not await self.robots.allowed(probe_client, url, ua_profile.user_agent):
            self.stats["robots_skipped"] += 1
            log.info("fetch.robots_disallow", url=url, source=self.source)
            raise RobotsDisallowed(url)

        await self._apply_crawl_delay(probe_client, url, host)

        proxy_entry: ProxyEntry | None = None

        async def _attempt() -> FetchResult:
            nonlocal proxy_entry
            proxy_entry = self.proxies.acquire(host)
            client = await self._client_for(proxy_entry.url if proxy_entry else None)

            request_headers = {**self.ua.headers(host), **self.default_headers, **(headers or {})}
            if expect_json:
                request_headers["Accept"] = "application/json, text/plain, */*"

            async with self.limiter.slot(url):
                self.stats["requests"] += 1
                response = await client.request(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    json=json_body,
                    data=data,
                )

            body = response.text
            signal = detect_block(
                status_code=response.status_code,
                body=body,
                headers=dict(response.headers),
                min_body_chars=200 if expect_json else 800,
            )
            if signal.is_blocked and not allow_block:
                self.stats["blocked"] += 1
                error = BlockedError(url, signal)
                self.proxies.report_failure(proxy_entry, hard_block=is_hard_block(error))
                if signal.retry_after:
                    await self.limiter.penalize(url, signal.retry_after)
                raise error

            if response.status_code >= 400 and not allow_block:
                response.raise_for_status()

            self.proxies.report_success(proxy_entry)
            self.stats["ok"] += 1

            raw_key = None
            if archive:
                raw_key = self.raw_store.put(
                    source=self.source,
                    url=url,
                    content=body,
                    extension="json" if expect_json else archive_ext,
                    metadata={"status": response.status_code, "method": method},
                )

            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                text=body,
                headers=dict(response.headers),
                elapsed_s=response.elapsed.total_seconds() if response.elapsed else 0.0,
                raw_key=raw_key,
            )

        async def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            self.proxies.report_failure(proxy_entry, hard_block=is_hard_block(exc))

        try:
            return await with_retry(
                _attempt,
                policy=self.retry_policy,
                context={"url": url, "source": self.source},
                on_retry=_on_retry,
            )
        except BlockedError as exc:
            log.error(
                "fetch.blocked",
                url=url,
                source=self.source,
                kind=exc.signal.kind.value,
                reason=exc.signal.reason,
            )
            if settings.abort_on_block and exc.signal.kind in {
                BlockKind.CAPTCHA,
                BlockKind.WAF,
                BlockKind.FORBIDDEN,
            }:
                raise
            raise
        except Exception:
            self.stats["errors"] += 1
            raise

    async def _apply_crawl_delay(self, client: httpx.AsyncClient, url: str, host: str) -> None:
        """Honour robots.txt Crawl-delay by tightening the host's bucket once."""
        if host in self._crawl_delay_applied:
            return
        self._crawl_delay_applied.add(host)
        delay = await self.robots.crawl_delay(client, url)
        if delay and delay > 0:
            rps = 1.0 / delay
            if rps < self.limiter.per_host_rps:
                self.limiter.set_host_rate(host, rps)
                log.info("fetch.crawl_delay_applied", host=host, delay_s=delay, rps=round(rps, 3))

    # -- convenience --------------------------------------------------------

    async def get_soup(self, url: str, **kwargs: Any) -> BeautifulSoup:
        result = await self.get(url, **kwargs)
        return result.soup()

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("expect_json", True)
        result = await self.get(url, **kwargs)
        return result.json()

    async def get_many(
        self, urls: list[str], *, tolerate_errors: bool = True, **kwargs: Any
    ) -> list[FetchResult]:
        """Fetch concurrently; the rate limiter still serialises per host."""

        async def _one(u: str) -> FetchResult | None:
            try:
                return await self.get(u, **kwargs)
            except (RobotsDisallowed, BlockedError):
                raise
            except Exception as exc:  # noqa: BLE001
                if not tolerate_errors:
                    raise
                log.warning("fetch.item_failed", url=u, error=str(exc)[:200])
                return None

        results = await asyncio.gather(*(_one(u) for u in urls), return_exceptions=tolerate_errors)
        out: list[FetchResult] = []
        for item in results:
            if isinstance(item, FetchResult):
                out.append(item)
            elif isinstance(item, BaseException) and not tolerate_errors:
                raise item
        return out
