"""Scheduler.

Two deployment options, same job definitions:

  * **APScheduler** (`homz.scheduler.jobs:main`) — one long-running process.
    Use inside Docker where a cron daemon is awkward.
  * **crontab** (`deploy/crontab`) — invokes the CLI. Use on a plain VM.

Cadence rationale:
  * Rentals churn fastest → twice daily.
  * Sale listings → daily, off-peak IST so we are never competing with the
    portals' own traffic peak.
  * SquareYards is browser-driven and expensive → every other day.
  * Reddit → every 6h; posts are short-lived on /new.
  * Enrichment runs after the scrapes so it sees fresh rows; batch mode makes
    the latency irrelevant.

Everything is staggered so two browser-heavy jobs never overlap.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from homz.common.base import ScrapeJob
from homz.logging_setup import configure_logging, get_logger
from homz.settings import settings

log = get_logger(__name__)

# All times are Asia/Kolkata — the market these jobs serve.
TIMEZONE = "Asia/Kolkata"


async def job_scrape_source(source: str, jobs: list[ScrapeJob] | None = None) -> dict[str, Any]:
    from homz.etl.pipeline import run_source

    started = datetime.now(UTC)
    log.info("cron.scrape_start", source=source)
    try:
        result = await run_source(source, jobs=jobs)
        payload = result.as_dict()
        log.info("cron.scrape_done", **payload)
        return payload
    except Exception as exc:  # noqa: BLE001
        log.exception("cron.scrape_failed", source=source)
        return {
            "source": source,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "duration_s": (datetime.now(UTC) - started).total_seconds(),
        }


async def job_rentals() -> dict[str, Any]:
    """Rental listings only, across the fast-moving portals."""
    results = []
    for source in ("magicbricks", "housing"):
        jobs = [
            ScrapeJob(
                name="rent",
                city=city,
                listing_type="rent",
                max_pages=4,
                max_items=200,
            )
            for city in ("gurgaon", "noida", "delhi")
        ]
        results.append(await job_scrape_source(source, jobs))
    return {"rentals": results}


async def job_sale() -> dict[str, Any]:
    results = [await job_scrape_source(source) for source in ("magicbricks", "housing")]
    return {"sale": results}


async def job_squareyards() -> dict[str, Any]:
    return await job_scrape_source("squareyards")


async def job_reddit() -> dict[str, Any]:
    return await job_scrape_source("reddit")


async def job_etl() -> dict[str, Any]:
    from homz.etl.pipeline import backfill_locality_aggregates, finalize
    from homz.etl.price_history import generate_market_insights

    log.info("cron.etl_start")
    summary = await finalize()
    summary["locations_updated"] = await backfill_locality_aggregates()
    summary["insights_written"] = await generate_market_insights()
    log.info("cron.etl_done", **summary)
    return summary


async def job_enrich() -> dict[str, Any]:
    from homz.db.engine import session_scope
    from homz.enrichment.pipeline import EnrichmentPipeline

    log.info("cron.enrich_start")
    async with session_scope() as session:
        pipeline = EnrichmentPipeline(session)
        try:
            report = await pipeline.run_all()
        finally:
            await pipeline.aclose()
    log.info("cron.enrich_done", **report.as_dict())
    return report.as_dict()


async def job_scores_only() -> dict[str, Any]:
    """Deterministic rescoring — free, so it can run often."""
    from homz.db.engine import session_scope
    from homz.enrichment.pipeline import EnrichmentPipeline

    async with session_scope() as session:
        pipeline = EnrichmentPipeline(session, use_llm=False)
        builders = await pipeline.score_builders()
        properties = await pipeline.score_properties()
    return {"builders": builders, "properties": properties}


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=TIMEZONE,
        job_defaults={
            "coalesce": True,        # a missed run does not pile up
            "max_instances": 1,      # never run two copies of the same job
            "misfire_grace_time": 3600,
        },
    )

    def cron(expr: str) -> CronTrigger:
        return CronTrigger.from_crontab(expr, timezone=TIMEZONE)

    # --- scraping --------------------------------------------------------
    scheduler.add_job(job_sale, cron("30 2 * * *"), id="sale_listings",
                      name="Sale listings (MB + Housing)")
    scheduler.add_job(job_rentals, cron("0 6,18 * * *"), id="rentals",
                      name="Rental listings (MB + Housing)")
    scheduler.add_job(job_squareyards, cron("0 4 */2 * *"), id="squareyards",
                      name="SquareYards projects (browser)")
    scheduler.add_job(job_reddit, cron("15 */6 * * *"), id="reddit",
                      name="Reddit discussions")

    # --- downstream ------------------------------------------------------
    scheduler.add_job(job_etl, cron("0 8 * * *"), id="etl", name="ETL + market insights")
    scheduler.add_job(job_enrich, cron("30 9 * * *"), id="enrich", name="AI enrichment")
    scheduler.add_job(job_scores_only, cron("0 */4 * * *"), id="scores",
                      name="Deterministic rescoring")

    # --- housekeeping ----------------------------------------------------
    scheduler.add_job(
        _prune_raw, cron("0 3 * * 0"), id="prune_raw", name="Prune raw HTML archive"
    )
    return scheduler


async def _prune_raw() -> dict[str, int]:
    from homz.common.rawstore import RawStore

    return {"pruned": RawStore().prune()}


async def run_forever() -> None:
    configure_logging()
    scheduler = build_scheduler()
    scheduler.start()

    for job in scheduler.get_jobs():
        log.info("cron.registered", job=job.id, name=job.name, next_run=str(job.next_run_time))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows has no add_signal_handler; fall back to default handling.
        with contextlib.suppress(NotImplementedError):  # pragma: no cover
            loop.add_signal_handler(sig, stop.set)

    log.info("cron.started", timezone=TIMEZONE, env=settings.env)
    await stop.wait()

    log.info("cron.shutting_down")
    scheduler.shutdown(wait=True)
    from homz.db.engine import dispose_engine

    await dispose_engine()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
