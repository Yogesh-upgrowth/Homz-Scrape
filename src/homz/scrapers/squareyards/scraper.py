"""SquareYards scraper.

This source used to drive Playwright, on the assumption that PDPs were
JS-rendered and that amenities were only reachable by clicking a modal open.
Both are false as of 2026-08: the server-rendered HTML already carries the
price box, unit/status box, configurations, RERA number, `#priceList`,
`#mapLandmarks`, `#specifications`, `#recentUpdates` and the amenity accordion
items the modal used to reveal. Listing pages render their card anchors
client-side, but publish the same projects as schema.org JSON-LD server-side.

So the browser bought nothing and cost a great deal: headless Chromium is
fingerprinted by the site's WAF and served HTTP 403, while a plain request for
the same URL returns 200. Dropping it fixes the block *by asking for less* —
one cheap HTML GET instead of a full render with its asset traffic. Rate
limiting, robots compliance and block detection are unchanged.

This replaces the standalone Puppeteer scripts at the repo root
(`gurgaonPDPScraper.js` and siblings) — same selectors, but with rate limiting,
retries, block detection, incremental state and the normalized schema.
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
from homz.scrapers.squareyards import parser

# Project sitemaps only — the index also lists builders, localities and budget
# pages, which are not PDPs.
_PROJECT_SITEMAP_RE = re.compile(r"sitemap-(?:focus)?project\d*\.xml$", re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I)


def _city_matcher(city: str):
    """URL test for a city slug that will not confuse Noida with Greater Noida.

    SquareYards PDP paths embed the city as a hyphen-delimited slug, either as
    `/{city}-residential-property/...` or as a `-{city}-npd-{id}` suffix, so a
    bare substring test on "noida" would also match every Greater Noida
    project. Anchoring on the surrounding hyphens keeps them distinct.
    """
    slug = city.strip().lower().replace(" ", "-")
    pattern = re.compile(rf"(?:^|[/-]){re.escape(slug)}(?:[/-]|$)")

    def matches(url: str) -> bool:
        path = url.lower().split("squareyards.com", 1)[-1]
        if not pattern.search(path):
            return False
        # "noida" must not swallow "greater-noida".
        return not (slug == "noida" and "greater-noida" in path)

    return matches


class SquareYardsScraper(BaseScraper):
    source = Source.SQUAREYARDS
    base_url = parser.BASE_URL
    # Server-rendered: JSON-LD on listing pages, full markup on PDPs.
    needs_browser = False
    host_rps = 0.33  # ~1 request every 3s

    default_jobs = (
        ScrapeJob(name="projects", city="gurgaon", max_pages=1, max_items=200),
        ScrapeJob(name="projects", city="noida", max_pages=1, max_items=150),
        ScrapeJob(name="projects", city="greater-noida", max_pages=1, max_items=120),
        ScrapeJob(name="projects", city="delhi", max_pages=1, max_items=120),
        ScrapeJob(name="projects", city="faridabad", max_pages=1, max_items=100),
        ScrapeJob(name="projects", city="ghaziabad", max_pages=1, max_items=100),
    )

    # -- discovery ----------------------------------------------------------

    async def discover(self, job: ScrapeJob, state: ScrapeState) -> AsyncIterator[str]:
        """Sitemaps first, then the city listing page.

        The listing page only ever exposes its first ~36 projects as JSON-LD,
        which is what capped earlier runs. The sitemaps SquareYards advertises
        in robots.txt carry every project it wants crawled, so they lead and
        the listing page backfills anything too new to be indexed yet.
        """
        emitted: set[str] = set()

        async for url in self._discover_from_sitemaps(job):
            if url in emitted:
                continue
            emitted.add(url)
            yield url
            if len(emitted) >= job.max_items:
                return

        async for url in self._discover_from_listing(job, state):
            if url in emitted:
                continue
            emitted.add(url)
            yield url
            if len(emitted) >= job.max_items:
                return

    async def _discover_from_sitemaps(self, job: ScrapeJob) -> AsyncIterator[str]:
        city = job.city or "gurgaon"
        matches_city = _city_matcher(city)

        try:
            client = await self.fetcher._client_for(None)
            indexes = await self.robots.sitemaps(client, self.base_url)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("sitemap.discovery_failed", error=str(exc)[:160])
            return

        found = 0
        for index_url in indexes:
            try:
                index = await self.fetcher.get(index_url, archive=False)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("sitemap.index_failed", url=index_url, error=str(exc)[:160])
                continue

            for child in _LOC_RE.findall(index.text):
                if not _PROJECT_SITEMAP_RE.search(child):
                    continue
                try:
                    sitemap = await self.fetcher.get(child, archive=False)
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("sitemap.fetch_failed", url=child, error=str(exc)[:160])
                    continue

                hits = 0
                for loc in _LOC_RE.findall(sitemap.text):
                    if not matches_city(loc):
                        continue
                    hits += 1
                    found += 1
                    yield canonical_url(loc)
                    if found >= job.max_items:
                        self.log.info("sitemap.discovered", city=city, found=found)
                        return
                self.log.debug("sitemap.scanned", url=child, city=city, matched=hits)

        self.log.info("sitemap.discovered", city=city, found=found)

    async def _discover_from_listing(
        self, job: ScrapeJob, state: ScrapeState
    ) -> AsyncIterator[str]:
        city = job.city or "gurgaon"
        listing_url = parser.build_city_url(city, listing_type=job.listing_type or "sale")

        try:
            result = await self.fetcher.get(listing_url)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("discover.fetch_failed", url=listing_url, error=str(exc)[:200])
            return

        urls = parser.parse_project_cards(result.text, base_url=self.base_url)
        self.log.info("discover.cards", city=city, url=listing_url, found=len(urls))

        state.cursor[f"{job.key}:last_listing_url"] = listing_url
        for url in urls:
            yield url

    # -- fetch --------------------------------------------------------------
    # `fetch_detail` is inherited: a plain rate-limited GET is enough.

    # -- parse --------------------------------------------------------------

    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        url = result.final_url or result.url
        project = parser.parse_project_detail(result.text, url, raw_html_key=result.raw_key)
        if project is None:
            return []
        # Emit both: the project row and its searchable property projection.
        return [project, parser.project_to_property(project)]
