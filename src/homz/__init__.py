"""Homz Realtor — Real Estate Intelligence Platform for Delhi NCR.

Layout:
    homz.common      reusable scraping infrastructure (no portal knows about it)
    homz.scrapers    one package per source: scraper.py (discovery) + parser.py
    homz.db          schema models and idempotent upserts
    homz.etl         load, dedupe, aggregate, market trends
    homz.enrichment  rule extraction, Claude tier, deterministic scoring
    homz.search      query builder + FastAPI service
    homz.scheduler   APScheduler job definitions
"""

__version__ = "1.0.0"
