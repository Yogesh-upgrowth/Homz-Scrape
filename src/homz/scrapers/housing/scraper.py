"""Housing.com scraper.

Housing renders search results client-side, so discovery falls back to a
browser render when the plain HTTP response yields no hydration payload. Detail
pages usually ship `__NEXT_DATA__` server-side, so they stay on the cheap path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from homz.common.base import BaseScraper, ScrapeJob
from homz.common.enums import Source
from homz.common.http import FetchResult
from homz.common.schema import ScrapedRecord
from homz.common.state import ScrapeState
from homz.scrapers.housing import parser


class HousingScraper(BaseScraper):
    source = Source.HOUSING
    base_url = parser.BASE_URL
    needs_browser = True
    host_rps = 0.4

    default_jobs = (
        ScrapeJob(name="buy", city="gurgaon", listing_type="sale", max_pages=6, max_items=300),
        ScrapeJob(name="rent", city="gurgaon", listing_type="rent", max_pages=5, max_items=250),
        ScrapeJob(name="buy", city="noida", listing_type="sale", max_pages=5, max_items=250),
        ScrapeJob(name="rent", city="noida", listing_type="rent", max_pages=4, max_items=200),
        ScrapeJob(name="buy", city="new-delhi", listing_type="sale", max_pages=4, max_items=200),
        ScrapeJob(name="buy", city="faridabad", listing_type="sale", max_pages=3, max_items=120),
        ScrapeJob(name="buy", city="ghaziabad", listing_type="sale", max_pages=3, max_items=120),
    )

    async def discover(self, job: ScrapeJob, state: ScrapeState) -> AsyncIterator[str]:
        city = job.city or "gurgaon"
        start_page = int(state.cursor.get(f"{job.key}:page", 1)) if job.incremental else 1
        if start_page > job.max_pages:
            start_page = 1

        emitted: set[str] = set()
        empty_pages = 0

        for page in range(start_page, start_page + job.max_pages):
            search_url = parser.build_search_url(
                city=city,
                listing_type=job.listing_type or "sale",
                property_type=job.property_type,
                page=page,
            )

            html = await self._load_search_html(search_url)
            if html is None:
                break

            urls = parser.parse_search_results(html, base_url=self.base_url)
            self.log.info("search.page", page=page, url=search_url, found=len(urls))

            fresh = [u for u in urls if u not in emitted]
            if not fresh:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue
            empty_pages = 0

            for url in fresh:
                emitted.add(url)
                yield url
                if len(emitted) >= job.max_items:
                    state.cursor[f"{job.key}:page"] = page + 1
                    return

            state.cursor[f"{job.key}:page"] = page + 1

    async def _load_search_html(self, search_url: str) -> str | None:
        """Cheap path first; escalate to a browser only when it yields nothing."""
        try:
            result = await self.fetcher.get(search_url)
            if parser.parse_search_results(result.text, base_url=self.base_url):
                return result.text
        except Exception as exc:  # noqa: BLE001
            self.log.warning("search.http_failed", url=search_url, error=str(exc)[:200])

        try:
            rendered = await self.browser.render(
                search_url,
                wait_for="a[href*='/rent/'], a[href*='/buy/'], article",
                scroll=True,
                scroll_steps=5,
            )
            return rendered.text
        except Exception as exc:  # noqa: BLE001
            self.log.warning("search.browser_failed", url=search_url, error=str(exc)[:200])
            return None

    async def fetch_detail(self, url: str, job: ScrapeJob) -> FetchResult:
        result = await self.fetcher.get(url)
        # If the server didn't inline the payload, re-render. Detecting via the
        # marker string avoids paying for a full parse just to decide.
        if "__NEXT_DATA__" not in result.text and "application/ld+json" not in result.text:
            self.log.debug("detail.escalating_to_browser", url=url)
            return await self.browser.render(url, wait_for="h1", scroll=True, scroll_steps=3)
        return result

    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        url = result.final_url or result.url
        is_project = "/project" in url or job.name == "projects"
        parse = parser.parse_project_detail if is_project else parser.parse_property_detail
        record = parse(result.text, url, raw_html_key=result.raw_key)
        return [record] if record else []
