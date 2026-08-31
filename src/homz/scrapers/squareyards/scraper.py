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
from collections.abc import AsyncIterator, Callable

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
        # SquareYards also lists individual rental units (`/rental-{slug}/{id}`,
        # tens of thousands per city) alongside builder projects — a separate
        # job because they're a separate record shape (parser.parse_rental_detail
        # -> PropertyRecord directly, no matching ProjectRecord). Deliberately
        # NOT hitting `/rental/search?...`: robots.txt disallows that path and
        # several of its query params outright; `/rent/property-for-rent-in-*`
        # is the crawlable equivalent and is what its own 404 page links to.
        # `max_pages` here only bounds the citywide firehose sample;
        # `_discover_from_search` sweeps one page per "Popular Locality" link
        # on top of that regardless, so a sector-scoped filter (e.g. "Sector
        # 56") draws from that sector's own page, not just whatever a
        # citywide sample of a few hundred out of tens of thousands happened
        # to include. `max_items` raised accordingly to leave room for it.
        ScrapeJob(name="rentals", city="gurgaon", listing_type="rent", max_pages=3, max_items=800),
        ScrapeJob(name="rentals", city="noida", listing_type="rent", max_pages=6, max_items=150),
        ScrapeJob(
            name="rentals", city="greater-noida", listing_type="rent", max_pages=5, max_items=120
        ),
        ScrapeJob(name="rentals", city="delhi", listing_type="rent", max_pages=5, max_items=120),
        ScrapeJob(
            name="rentals", city="faridabad", listing_type="rent", max_pages=4, max_items=100
        ),
        ScrapeJob(
            name="rentals", city="ghaziabad", listing_type="rent", max_pages=4, max_items=100
        ),
        # Same gap on the sale side: `/sale/property-for-sale-in-*` lists
        # individual resale units (villas, plots, independent houses, office
        # space, ...) that only ever exist as owner/broker listings, never as
        # a builder project — so the projects-only job above never surfaces
        # them regardless of how many sitemaps or listing pages it reads.
        ScrapeJob(name="resale", city="gurgaon", listing_type="resale", max_pages=3, max_items=800),
        ScrapeJob(name="resale", city="noida", listing_type="resale", max_pages=6, max_items=150),
        ScrapeJob(
            name="resale", city="greater-noida", listing_type="resale", max_pages=5, max_items=120
        ),
        ScrapeJob(name="resale", city="delhi", listing_type="resale", max_pages=5, max_items=120),
        ScrapeJob(
            name="resale", city="faridabad", listing_type="resale", max_pages=4, max_items=100
        ),
        ScrapeJob(
            name="resale", city="ghaziabad", listing_type="resale", max_pages=4, max_items=100
        ),
    )

    # -- discovery ----------------------------------------------------------

    async def discover(self, job: ScrapeJob, state: ScrapeState) -> AsyncIterator[str]:
        """Sitemaps + city listing page for projects; paginated + locality
        search sweep for individual rental/resale units.

        The listing page only ever exposes its first ~36 projects as JSON-LD,
        which is what capped earlier runs. The sitemaps SquareYards advertises
        in robots.txt carry every project it wants crawled, so they lead and
        the listing page backfills anything too new to be indexed yet.
        """
        emitted: set[str] = set()

        if job.listing_type in ("rent", "resale"):
            # `job.params["category"]` optionally scopes discovery to a
            # type-specific page (e.g. "apartments-for-rent") instead of the
            # unscoped citywide firehose — see build_rent_search_url's
            # `category` kwarg.
            category = job.params.get("category")
            if job.listing_type == "rent":
                build_url = lambda c, page=1: parser.build_rent_search_url(  # noqa: E731
                    c, page=page, **({"category": category} if category else {})
                )
                parse_urls = parser.parse_jsonld_rental_urls
            else:
                build_url = lambda c, page=1: parser.build_sale_search_url(  # noqa: E731
                    c, page=page, **({"category": category} if category else {})
                )
                parse_urls = parser.parse_resale_urls
            async for url in self._discover_from_search(
                job,
                build_url=build_url,
                parse_urls=parse_urls,
                log_prefix=f"{job.listing_type}_search",
            ):
                if url in emitted:
                    continue
                emitted.add(url)
                yield url
                if len(emitted) >= job.max_items:
                    return
            return

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

    async def _discover_from_search(
        self,
        job: ScrapeJob,
        *,
        build_url: Callable[..., str],
        parse_urls: Callable[[str], list[str]],
        log_prefix: str,
    ) -> AsyncIterator[str]:
        """Shared by rent and resale discovery: page the citywide feed, then
        sweep one page per "Popular Locality" link it advertises.

        Tens of thousands of individual units exist per city, so citywide
        paging alone is a sample, not full coverage — and a sector-scoped
        filter (e.g. "Sector 56") can easily draw zero from that sample even
        though the sector itself has hundreds of live listings. Crawling
        each locality's own page too is what gives sector filters real
        results, without touching `/rental/search?...` or `/resale/search?...`,
        both of which robots.txt disallows outright.
        """
        city = job.city or "gurgaon"
        localities: list[str] = []
        empty_pages = 0
        for page in range(1, job.max_pages + 1):
            search_url = build_url(city, page=page)
            try:
                result = await self.fetcher.get(search_url)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"{log_prefix}.page_failed", url=search_url, error=str(exc)[:200])
                break

            if page == 1:
                localities = parser.parse_locality_links(
                    result.text, city=city, base_url=build_url(city)
                )
                self.log.info(f"{log_prefix}.localities", city=city, found=len(localities))

            urls = parse_urls(result.text)
            self.log.info(f"{log_prefix}.page", city=city, page=page, url=search_url, found=len(urls))

            if not urls:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue
            empty_pages = 0

            for url in urls:
                yield url

        for locality_url in localities:
            try:
                result = await self.fetcher.get(locality_url)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    f"{log_prefix}.locality_failed", url=locality_url, error=str(exc)[:200]
                )
                continue

            urls = parse_urls(result.text)
            self.log.info(f"{log_prefix}.locality_page", city=city, url=locality_url, found=len(urls))
            for url in urls:
                yield url

    # -- fetch --------------------------------------------------------------
    # `fetch_detail` is inherited: a plain rate-limited GET is enough.

    # -- parse --------------------------------------------------------------

    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        url = result.final_url or result.url
        # An individual rental/resale unit is already a PropertyRecord — no
        # ProjectRecord counterpart to also emit, unlike a project PDP.
        if "/rental-" in url:
            rental = parser.parse_rental_detail(result.text, url, raw_html_key=result.raw_key)
            return [rental] if rental is not None else []
        if "/resale-" in url:
            resale = parser.parse_resale_detail(result.text, url, raw_html_key=result.raw_key)
            return [resale] if resale is not None else []

        project = parser.parse_project_detail(result.text, url, raw_html_key=result.raw_key)
        if project is None:
            return []
        # Emit both: the project row and its searchable property projection.
        return [project, parser.project_to_property(project)]
