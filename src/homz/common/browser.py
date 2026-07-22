"""Playwright pool for pages that genuinely need JavaScript.

Browsers are 10-50x more expensive than an HTTP GET, so the rule is: try
`Fetcher` first, fall back to `BrowserPool` only for routes that render
client-side (SquareYards PDPs, some Housing search pages).

The pool keeps one browser process and a bounded number of contexts. Each
context gets its own UA + viewport, and goes through the same rate limiter and
block detection as the HTTP path.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homz.common.captcha import BlockedError, detect_block
from homz.common.http import FetchResult
from homz.common.proxy import ProxyPool
from homz.common.ratelimit import RateLimiter
from homz.common.rawstore import RawStore
from homz.common.retry import RetryPolicy, with_retry
from homz.common.useragent import UserAgentRotator
from homz.logging_setup import get_logger
from homz.settings import settings

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

log = get_logger(__name__)

_VIEWPORTS = ((1920, 1080), (1536, 864), (1440, 900), (1366, 768))

# Cut bandwidth hard: images/fonts/media are never needed for data extraction.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_FRAGMENTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
    "clarity.ms",
    "segment.io",
    "moengage",
    "criteo",
)


@dataclass
class BrowserConfig:
    headless: bool = True
    browser_name: str = "chromium"
    nav_timeout_ms: int = 45_000
    max_contexts: int = 2
    block_assets: bool = True
    locale: str = "en-IN"
    timezone_id: str = "Asia/Kolkata"

    @classmethod
    def from_settings(cls) -> BrowserConfig:
        return cls(
            headless=settings.playwright_headless,
            browser_name=settings.playwright_browser,
            nav_timeout_ms=settings.playwright_nav_timeout,
            max_contexts=settings.playwright_max_contexts,
        )


class BrowserPool:
    def __init__(
        self,
        *,
        source: str,
        config: BrowserConfig | None = None,
        rate_limiter: RateLimiter | None = None,
        proxy_pool: ProxyPool | None = None,
        raw_store: RawStore | None = None,
    ) -> None:
        self.source = source
        self.config = config or BrowserConfig.from_settings()
        self.limiter = rate_limiter or RateLimiter()
        self.proxies = proxy_pool or ProxyPool()
        self.raw_store = raw_store or RawStore()
        self.ua = UserAgentRotator()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(self.config.max_contexts)
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            launcher = getattr(self._playwright, self.config.browser_name)
            self._browser = await launcher.launch(
                headless=self.config.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                ],
            )
            log.info("browser.started", browser=self.config.browser_name,
                     headless=self.config.headless)

    async def aclose(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
        log.info("browser.stopped", source=self.source)

    async def __aenter__(self) -> BrowserPool:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- contexts -----------------------------------------------------------

    @asynccontextmanager
    async def context(self, *, host: str | None = None):
        await self.start()
        assert self._browser is not None
        await self._semaphore.acquire()
        proxy_entry = self.proxies.acquire(host)
        ctx: BrowserContext | None = None
        try:
            profile = self.ua.for_host(host or self.source)
            width, height = random.choice(_VIEWPORTS)
            ctx = await self._browser.new_context(
                user_agent=profile.user_agent,
                viewport={"width": width, "height": height},
                locale=self.config.locale,
                timezone_id=self.config.timezone_id,
                proxy={"server": proxy_entry.url} if proxy_entry else None,
                extra_http_headers={"Accept-Language": profile.accept_language},
            )
            ctx.set_default_navigation_timeout(self.config.nav_timeout_ms)
            ctx.set_default_timeout(self.config.nav_timeout_ms)
            if self.config.block_assets:
                await ctx.route("**/*", _asset_blocker)
            yield ctx
            self.proxies.report_success(proxy_entry)
        except Exception:
            self.proxies.report_failure(proxy_entry)
            raise
        finally:
            if ctx is not None:
                await ctx.close()
            self._semaphore.release()

    @asynccontextmanager
    async def page(self, *, host: str | None = None):
        async with self.context(host=host) as ctx:
            page = await ctx.new_page()
            try:
                yield page
            finally:
                await page.close()

    # -- fetch --------------------------------------------------------------

    async def render(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        wait_until: str = "domcontentloaded",
        scroll: bool = True,
        scroll_steps: int = 4,
        settle_ms: int = 1200,
        actions: Any = None,
        archive: bool = True,
    ) -> FetchResult:
        """Navigate, let the page settle, return the rendered HTML.

        `actions(page)` is an optional async callable for per-source
        interactions (open an amenities modal, click "show more", …).
        """
        host = RateLimiter.host_of(url)
        policy = RetryPolicy(max_attempts=max(2, settings.max_retries - 1), base_delay=2.0)

        async def _attempt() -> FetchResult:
            async with self.limiter.slot(url), self.page(host=host) as page:
                response = await page.goto(url, wait_until=wait_until)
                status = response.status if response else 0

                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=self.config.nav_timeout_ms)
                    except Exception:
                        log.debug("browser.selector_timeout", url=url, selector=wait_for)

                if scroll:
                    await _autoscroll(page, steps=scroll_steps)

                if actions is not None:
                    try:
                        await actions(page)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("browser.actions_failed", url=url, error=str(exc)[:160])

                if settle_ms:
                    await page.wait_for_timeout(settle_ms)

                html = await page.content()
                final_url = page.url

            signal = detect_block(status_code=status or 200, body=html)
            if signal.is_blocked:
                raise BlockedError(url, signal)

            raw_key = (
                self.raw_store.put(
                    source=self.source,
                    url=url,
                    content=html,
                    metadata={"status": status, "renderer": "playwright"},
                )
                if archive
                else None
            )
            return FetchResult(
                url=url,
                final_url=final_url,
                status_code=status or 200,
                text=html,
                headers={},
                elapsed_s=0.0,
                from_browser=True,
                raw_key=raw_key,
            )

        return await with_retry(
            _attempt, policy=policy, context={"url": url, "source": self.source, "mode": "browser"}
        )


async def _asset_blocker(route, request) -> None:
    url = request.url
    if request.resource_type in _BLOCKED_RESOURCE_TYPES or any(
        fragment in url for fragment in _BLOCKED_URL_FRAGMENTS
    ):
        await route.abort()
    else:
        await route.continue_()


async def _autoscroll(page: Page, *, steps: int = 4, pause_ms: int = 400) -> None:
    """Trigger lazy-loaded sections without the infinite-scroll trap."""
    for _ in range(steps):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
        await page.wait_for_timeout(pause_ms)
    await page.evaluate("window.scrollTo(0, 0)")
