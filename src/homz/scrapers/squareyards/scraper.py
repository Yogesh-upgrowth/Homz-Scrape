"""SquareYards scraper.

SquareYards PDPs are JS-rendered and hide amenities behind a modal, so this is
the one source that always needs Playwright. The modal interaction is expressed
as a `page_actions` callable passed to `BrowserPool.render()`, which keeps the
Playwright specifics in one small place instead of spread through the parser.

This replaces the standalone Puppeteer scripts at the repo root
(`gurgaonPDPScraper.js` and siblings) — same selectors, but with rate limiting,
retries, block detection, incremental state and the normalized schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from homz.common.base import BaseScraper, ScrapeJob
from homz.common.enums import Source
from homz.common.http import FetchResult
from homz.common.schema import ScrapedRecord
from homz.common.state import ScrapeState
from homz.scrapers.squareyards import parser


async def _open_amenities_modal(page) -> None:
    """Amenities live behind a popup; open it so they land in `page.content()`.

    Failures are non-fatal — the inline amenities list is a partial fallback.
    """
    button = await page.query_selector(".amenities-link-popup-btn")
    if button is None:
        return
    await button.click()
    try:
        await page.wait_for_selector(".accordion-item", timeout=5_000)
    finally:
        close = await page.query_selector(".modal-close button")
        if close is not None:
            await close.click()


class SquareYardsScraper(BaseScraper):
    source = Source.SQUAREYARDS
    base_url = parser.BASE_URL
    needs_browser = True
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
        city = job.city or "gurgaon"
        listing_url = parser.build_city_url(city, listing_type=job.listing_type or "sale")

        try:
            result = await self.browser.render(
                listing_url,
                wait_for=".project-card, .listing-card-box",
                scroll=True,
                scroll_steps=6,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("discover.render_failed", url=listing_url, error=str(exc)[:200])
            return

        urls = parser.parse_project_cards(result.text, base_url=self.base_url)
        self.log.info("discover.cards", city=city, url=listing_url, found=len(urls))

        state.cursor[f"{job.key}:last_listing_url"] = listing_url
        for url in urls[: job.max_items]:
            yield url

    # -- fetch --------------------------------------------------------------

    async def fetch_detail(self, url: str, job: ScrapeJob) -> FetchResult:
        return await self.browser.render(
            url,
            wait_for="h1",
            scroll=True,
            scroll_steps=5,
            actions=_open_amenities_modal,
        )

    # -- parse --------------------------------------------------------------

    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        url = result.final_url or result.url
        project = parser.parse_project_detail(result.text, url, raw_html_key=result.raw_key)
        if project is None:
            return []
        # Emit both: the project row and its searchable property projection.
        return [project, parser.project_to_property(project)]
