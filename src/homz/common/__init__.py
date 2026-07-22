"""Reusable scraping infrastructure — nothing here knows about any one portal."""

from homz.common.base import BaseScraper, ScrapeJob, ScrapeReport
from homz.common.http import Fetcher, FetchResult
from homz.common.schema import (
    BuilderRecord,
    Location,
    MarketInsightRecord,
    ProjectRecord,
    PropertyRecord,
    RedditComment,
    RedditPostRecord,
    ScrapedRecord,
)

__all__ = [
    "BaseScraper",
    "BuilderRecord",
    "FetchResult",
    "Fetcher",
    "Location",
    "MarketInsightRecord",
    "ProjectRecord",
    "PropertyRecord",
    "RedditComment",
    "RedditPostRecord",
    "ScrapeJob",
    "ScrapeReport",
    "ScrapedRecord",
]
