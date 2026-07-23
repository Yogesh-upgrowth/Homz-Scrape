"""ETL orchestration: scrape → normalize → dedupe → load → aggregate.

This is the layer that turns a list of `ScrapedRecord` objects into rows, and
it owns the decisions that must not live in a scraper:

  * routing each record type to the right repository method
  * cross-source near-duplicate detection and canonical selection
  * marking listings that have disappeared from the source as delisted
  * refreshing the materialized rollups the scores and search depend on

Loading is chunked and each chunk commits independently, so a failure halfway
through a 5,000-record run keeps everything already written.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from homz.common.base import ScrapeJob
from homz.common.dedupe import DuplicateMatch, choose_canonical, find_duplicates
from homz.common.schema import (
    BuilderRecord,
    MarketInsightRecord,
    ProjectRecord,
    PropertyRecord,
    RedditPostRecord,
    ScrapedRecord,
)
from homz.common.state import StateStore
from homz.db.engine import session_scope
from homz.db.repository import Repository
from homz.logging_setup import get_logger
from homz.scrapers import PROPERTY_SOURCES, get_scraper

log = get_logger(__name__)

_CHUNK_SIZE = 200


@dataclass
class LoadResult:
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    duplicates_linked: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def note(self, kind: str) -> None:
        self.by_type[kind] = self.by_type.get(kind, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "failed": self.failed,
            "duplicates_linked": self.duplicates_linked,
            "by_type": self.by_type,
            "errors": self.errors[:10],
        }


@dataclass
class PipelineResult:
    source: str
    reports: list[dict[str, Any]] = field(default_factory=list)
    load: LoadResult = field(default_factory=LoadResult)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reports": self.reports,
            "load": self.load.as_dict(),
            "duration_s": round(
                ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds(),
                2,
            ),
        }


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


async def load_records(records: list[ScrapedRecord]) -> LoadResult:
    """Persist a batch of normalized records. Idempotent."""
    result = LoadResult()
    if not records:
        return result

    property_ids: dict[str, int] = {}

    for chunk_start in range(0, len(records), _CHUNK_SIZE):
        chunk = records[chunk_start : chunk_start + _CHUNK_SIZE]
        try:
            async with session_scope() as session:
                repo = Repository(session)
                for record in chunk:
                    try:
                        await _load_one(repo, record, result, property_ids)
                    except Exception as exc:  # noqa: BLE001
                        result.failed += 1
                        if len(result.errors) < 20:
                            result.errors.append(
                                f"{getattr(record, 'natural_key', '?')}: "
                                f"{type(exc).__name__}: {str(exc)[:200]}"
                            )
                        log.warning(
                            "etl.record_failed",
                            key=getattr(record, "natural_key", None),
                            error=str(exc)[:300],
                        )
        except Exception as exc:  # noqa: BLE001
            # A whole chunk failed (connection lost, deadlock). Count it and
            # keep going — the next chunk gets a fresh session.
            result.failed += len(chunk)
            result.errors.append(f"chunk failed: {type(exc).__name__}: {str(exc)[:200]}")
            log.error("etl.chunk_failed", size=len(chunk), error=str(exc)[:300])

    # Cross-source dedupe over the properties in this batch.
    properties = [r for r in records if isinstance(r, PropertyRecord)]
    if len(properties) > 1:
        result.duplicates_linked = await link_duplicates(properties, property_ids)

    log.info("etl.loaded", **result.as_dict())
    return result


async def _load_one(
    repo: Repository,
    record: ScrapedRecord,
    result: LoadResult,
    property_ids: dict[str, int],
) -> None:
    if isinstance(record, PropertyRecord):
        property_id, is_new = await repo.upsert_property(record)
        property_ids[record.natural_key] = property_id
        result.inserted += int(is_new)
        result.updated += int(not is_new)
        result.note("property")
    elif isinstance(record, ProjectRecord):
        await repo.upsert_project(record)
        result.updated += 1
        result.note("project")
    elif isinstance(record, BuilderRecord):
        await repo.upsert_builder(record)
        result.updated += 1
        result.note("builder")
    elif isinstance(record, RedditPostRecord):
        await repo.upsert_reddit_post(record)
        result.updated += 1
        result.note("reddit_post")
    elif isinstance(record, MarketInsightRecord):
        await repo.upsert_market_insight(record)
        result.updated += 1
        result.note("market_insight")
    else:  # pragma: no cover - guards a future record type
        raise TypeError(f"no loader for record type {type(record).__name__}")


async def link_duplicates(properties: list[PropertyRecord], property_ids: dict[str, int]) -> int:
    """Detect near-duplicates within the batch and point them at a canonical."""
    matches: list[DuplicateMatch] = find_duplicates(properties, threshold=0.75)
    if not matches:
        return 0

    by_key = {p.natural_key: p for p in properties}
    linked = 0

    async with session_scope() as session:
        repo = Repository(session)
        for match in matches:
            left, right = by_key.get(match.left), by_key.get(match.right)
            if left is None or right is None:
                continue
            canonical = choose_canonical([left, right])
            duplicate = right if canonical is left else left

            canonical_id = property_ids.get(canonical.natural_key)
            duplicate_id = property_ids.get(duplicate.natural_key)
            if not canonical_id or not duplicate_id:
                continue
            await repo.link_duplicate(canonical_id, duplicate_id, match.score, match.reason)
            linked += 1

    log.info("etl.duplicates_linked", count=linked, candidates=len(matches))
    return linked


# ---------------------------------------------------------------------------
# per-source runs
# ---------------------------------------------------------------------------


async def run_source(
    source: str,
    *,
    jobs: list[ScrapeJob] | None = None,
    load: bool = True,
    dry_run: bool = False,
) -> PipelineResult:
    """Scrape one source end-to-end and load the results."""
    scraper_cls = get_scraper(source)
    outcome = PipelineResult(source=source)

    async with session_scope() as session:
        state_store = StateStore(session)

        async with scraper_cls(state_store=state_store) as scraper:
            records, reports = await scraper.run(jobs)

    outcome.reports = [r.as_dict() for r in reports]
    log.info("etl.scraped", source=source, records=len(records))

    if dry_run:
        log.info("etl.dry_run_skipping_load", source=source, records=len(records))
        outcome.finished_at = datetime.now(UTC)
        return outcome

    if load and records:
        outcome.load = await load_records(records)

    # Record the run for observability.
    async with session_scope() as session:
        repo = Repository(session)
        for report in reports:
            await repo.record_run(
                report,
                inserted=outcome.load.inserted,
                updated=outcome.load.updated,
            )

    outcome.finished_at = datetime.now(UTC)
    return outcome


async def run_all_sources(
    sources: list[str] | None = None,
    *,
    dry_run: bool = False,
    sequential: bool = True,
) -> list[PipelineResult]:
    """Run every source.

    Sequential by default: each source has its own host, but they share the
    global concurrency cap and (usually) the same proxy pool, so running them
    in parallel mostly just makes failures harder to attribute.
    """
    from homz.scrapers import SCRAPERS

    sources = sources or list(SCRAPERS)
    results: list[PipelineResult] = []

    if sequential:
        for source in sources:
            try:
                results.append(await run_source(source, dry_run=dry_run))
            except Exception as exc:  # noqa: BLE001
                log.exception("etl.source_failed", source=source)
                failed = PipelineResult(source=source)
                failed.load.errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
                failed.finished_at = datetime.now(UTC)
                results.append(failed)
    else:
        gathered = await asyncio.gather(
            *(run_source(s, dry_run=dry_run) for s in sources), return_exceptions=True
        )
        for source, item in zip(sources, gathered, strict=True):
            if isinstance(item, PipelineResult):
                results.append(item)
            else:
                log.error("etl.source_failed", source=source, error=str(item)[:300])

    return results


# ---------------------------------------------------------------------------
# post-load maintenance
# ---------------------------------------------------------------------------


async def finalize(
    *, mark_stale_days: int = 21, refresh_views: bool = True, prune_raw: bool = True
) -> dict[str, Any]:
    """Housekeeping after a load: delist stale rows, refresh rollups, prune raw."""
    summary: dict[str, Any] = {}

    async with session_scope() as session:
        repo = Repository(session)
        delisted = 0
        for source in PROPERTY_SOURCES:
            delisted += await repo.mark_stale_inactive(source, older_than_days=mark_stale_days)
        summary["delisted"] = delisted

        await session.commit()

        if refresh_views:
            try:
                await repo.refresh_market_views(concurrent=True)
                summary["views_refreshed"] = True
            except Exception as exc:  # noqa: BLE001
                # CONCURRENTLY needs a populated unique index; on a fresh DB the
                # first refresh has to be non-concurrent.
                log.warning("etl.concurrent_refresh_failed_retrying", error=str(exc)[:200])
                await session.rollback()
                await repo.refresh_market_views(concurrent=False)
                summary["views_refreshed"] = True

        summary["counts"] = await repo.counts()

    if prune_raw:
        from homz.common.rawstore import RawStore

        summary["raw_partitions_pruned"] = RawStore().prune()

    log.info("etl.finalized", **summary)
    return summary


async def backfill_locality_aggregates() -> int:
    """Copy the materialized locality rollups onto `locations` so the API can
    read them without joining a view on every request."""
    async with session_scope() as session:
        result = await session.execute(
            text(
                """
                WITH agg AS (
                    SELECT p.location_id,
                           AVG(p.price_per_sqft) FILTER (WHERE p.price_per_sqft > 0)
                               AS avg_ppsf,
                           AVG(p.rent_monthly)   FILTER (WHERE p.rent_monthly > 0)
                               AS avg_rent,
                           COUNT(*)              AS listings
                    FROM properties p
                    WHERE p.is_active AND p.location_id IS NOT NULL
                    GROUP BY p.location_id
                )
                UPDATE locations l SET
                    avg_price_per_sqft = agg.avg_ppsf,
                    avg_rent_per_month = agg.avg_rent,
                    listing_count      = agg.listings,
                    rental_yield_pct   = CASE
                        WHEN agg.avg_ppsf > 0 AND agg.avg_rent > 0
                        THEN ROUND(((agg.avg_rent * 12)
                                    / NULLIF(agg.avg_ppsf * 1000, 0) * 100)::numeric, 3)
                        ELSE l.rental_yield_pct END,
                    updated_at = NOW()
                FROM agg
                WHERE l.id = agg.location_id
                """
            )
        )
        await session.commit()
        count = result.rowcount or 0
    log.info("etl.locality_aggregates_backfilled", locations=count)
    return count
