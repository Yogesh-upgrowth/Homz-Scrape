"""MagicBricks scraper — discovery + orchestration only.

Discovery order:
  1. sitemap.xml (declared in robots.txt) — the sanctioned, cheapest path
  2. paginated public search pages — used when the sitemap yields too few
     fresh URLs for the requested job

All parsing lives in `parser.py`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from homz.common.base import BaseScraper, ScrapeJob
from homz.common.enums import Source
from homz.common.http import FetchResult
from homz.common.parsing import canonical_url
from homz.common.schema import ScrapedRecord
from homz.common.state import ScrapeState
from homz.scrapers.magicbricks import parser

_DETAIL_URL_RE = re.compile(r"propertyDetails|pdpid", re.I)


class MagicBricksScraper(BaseScraper):
    source = Source.MAGICBRICKS
    base_url = parser.BASE_URL
    # MagicBricks renders search results server-side; no browser needed.
    needs_browser = False
    host_rps = 0.4  # ~1 request every 2.5s — deliberately conservative

    default_jobs = (
        ScrapeJob(name="sale", city="gurgaon", listing_type="sale", max_pages=8, max_items=400),
        ScrapeJob(name="rent", city="gurgaon", listing_type="rent", max_pages=6, max_items=300),
        ScrapeJob(name="sale", city="noida", listing_type="sale", max_pages=6, max_items=300),
        ScrapeJob(name="rent", city="noida", listing_type="rent", max_pages=4, max_items=200),
        ScrapeJob(name="sale", city="delhi", listing_type="sale", max_pages=5, max_items=250),
        ScrapeJob(name="sale", city="faridabad", listing_type="sale", max_pages=3, max_items=150),
        ScrapeJob(name="sale", city="ghaziabad", listing_type="sale", max_pages=3, max_items=150),
        # Commercial sub-types each live under their own URL prefix (see
        # parser._COMMERCIAL_PREFIXES) — the old single "commercial-real-estate"
        # job was silently a no-op, since build_search_url ignored property_type
        # entirely and just re-scraped the residential sale feed.
        ScrapeJob(
            name="commercial-shop", city="gurgaon", listing_type="sale",
            property_type="shop", max_pages=3, max_items=150,
        ),
        ScrapeJob(
            name="commercial-shop-rent", city="gurgaon", listing_type="rent",
            property_type="shop", max_pages=3, max_items=150,
        ),
        ScrapeJob(
            name="commercial-office", city="gurgaon", listing_type="sale",
            property_type="office", max_pages=3, max_items=150,
        ),
        ScrapeJob(
            name="commercial-office-rent", city="gurgaon", listing_type="rent",
            property_type="office", max_pages=3, max_items=150,
        ),
        ScrapeJob(
            name="commercial-showroom", city="gurgaon", listing_type="sale",
            property_type="showroom", max_pages=2, max_items=100,
        ),
        ScrapeJob(
            name="commercial-land", city="gurgaon", listing_type="sale",
            property_type="commercial-land", max_pages=2, max_items=100,
        ),
    )

    # -- discovery ----------------------------------------------------------

    async def discover(self, job: ScrapeJob, state: ScrapeState) -> AsyncIterator[str]:
        emitted: set[str] = set()

        async for url in self._discover_from_sitemaps(job):
            if url not in emitted:
                emitted.add(url)
                yield url
            if len(emitted) >= job.max_items:
                return

        async for url in self._discover_from_search(job, state):
            if url not in emitted:
                emitted.add(url)
                yield url
            if len(emitted) >= job.max_items:
                return

    async def _discover_from_sitemaps(self, job: ScrapeJob) -> AsyncIterator[str]:
        """robots.txt advertises sitemaps; that is the site telling us what it
        wants crawled, so we start there."""
        try:
            sitemaps = await self.robots.sitemaps(
                await self.fetcher._client_for(None), self.base_url
            )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("sitemap.discovery_failed", error=str(exc)[:160])
            return

        city_token = (job.city or "").lower().replace(" ", "-")
        relevant = [s for s in sitemaps if not city_token or city_token in s.lower()]
        # If none are city-specific, sample a few generic property sitemaps.
        for sitemap_url in (relevant or sitemaps)[:3]:
            try:
                result = await self.fetcher.get(sitemap_url, archive=False)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("sitemap.fetch_failed", url=sitemap_url, error=str(exc)[:160])
                continue
            for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", result.text):
                if _DETAIL_URL_RE.search(loc):
                    yield canonical_url(loc)
                elif loc.endswith(".xml") and city_token and city_token in loc.lower():
                    # Nested sitemap index — follow one level.
                    try:
                        nested = await self.fetcher.get(loc, archive=False)
                    except Exception:  # noqa: BLE001
                        continue
                    for nested_loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", nested.text):
                        if _DETAIL_URL_RE.search(nested_loc):
                            yield canonical_url(nested_loc)

    async def _discover_from_search(
        self, job: ScrapeJob, state: ScrapeState
    ) -> AsyncIterator[str]:
        city = job.city or "gurgaon"
        start_page = 1
        if job.incremental:
            start_page = int(state.cursor.get(f"{job.key}:page", 1))
            if start_page > job.max_pages:
                start_page = 1  # wrap around on the next run

        empty_pages = 0
        for page in range(start_page, start_page + job.max_pages):
            search_url = parser.build_search_url(
                city=city,
                listing_type=job.listing_type or "sale",
                property_type=job.property_type,
                page=page,
            )
            try:
                result = await self.fetcher.get(search_url)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("search.page_failed", url=search_url, error=str(exc)[:200])
                break

            urls = parser.parse_search_results(result.text, base_url=self.base_url)
            self.log.info("search.page", page=page, url=search_url, found=len(urls))

            if not urls:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue
            empty_pages = 0

            for url in urls:
                yield url

            state.cursor[f"{job.key}:page"] = page + 1
            if not parser.has_next_page(result.text):
                break

    # -- parsing ------------------------------------------------------------

    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        is_project = "/project" in result.url or job.name == "projects"
        parse = parser.parse_project_detail if is_project else parser.parse_property_detail
        record = parse(result.text, result.final_url or result.url, raw_html_key=result.raw_key)
        return [record] if record else []
