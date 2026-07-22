"""Scraper registry.

Adding a source is: create the package, subclass `BaseScraper`, register it
here. Everything downstream (CLI, scheduler, ETL) discovers it through
`SCRAPERS`.
"""

from __future__ import annotations

from homz.common.base import BaseScraper
from homz.common.enums import Source
from homz.scrapers.housing import HousingScraper
from homz.scrapers.magicbricks import MagicBricksScraper
from homz.scrapers.reddit import RedditScraper
from homz.scrapers.squareyards import SquareYardsScraper

SCRAPERS: dict[str, type[BaseScraper]] = {
    Source.MAGICBRICKS.value: MagicBricksScraper,
    Source.HOUSING.value: HousingScraper,
    Source.SQUAREYARDS.value: SquareYardsScraper,
    Source.REDDIT.value: RedditScraper,
}

PROPERTY_SOURCES: tuple[str, ...] = (
    Source.MAGICBRICKS.value,
    Source.HOUSING.value,
    Source.SQUAREYARDS.value,
)


def get_scraper(name: str) -> type[BaseScraper]:
    try:
        return SCRAPERS[name.lower()]
    except KeyError:
        raise KeyError(
            f"unknown source {name!r}; available: {', '.join(sorted(SCRAPERS))}"
        ) from None


__all__ = [
    "PROPERTY_SOURCES",
    "SCRAPERS",
    "HousingScraper",
    "MagicBricksScraper",
    "RedditScraper",
    "SquareYardsScraper",
    "get_scraper",
]
