"""Freshness pass: confirm what is still live, fetch only what is new or oldest.

A full detail re-scrape of ~40k listings at the portals' polite rates takes
~15 hours (see COMPLIANCE.md for the rates). That is not needed to keep the feed
fresh. This pass does the cheap thing first:

1. **Discover** the segment's listing URLs (search pages / sitemaps — the same
   discovery the scrapers already do, at the same rate limits).
2. **Record sightings** — every id discovery showed is live today, so its
   `last_seen_at` is bumped without fetching its page.
3. **Fetch details** for a bounded budget: ids the warehouse has never seen
   first, then the *stalest* active records (oldest `last_seen_at`), so the
   ones discovery could not confirm still get refreshed on a rotation.
   A 404/410 on a stale record marks it removed immediately.

Compliance is unchanged: every request goes through the scraper's `Fetcher`
(robots.txt gate, per-host token bucket, block detection). A captcha/WAF/403
wall stops the run — it is reported, never retried around.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from homz.common.base import ScrapeJob
from homz.common.captcha import BlockedError
from homz.common.enums import JobStatus
from homz.common.robots import RobotsDisallowed
from homz.common.schema import Image, ProjectRecord, PropertyRecord
from homz.common.state import ScrapeState, StateStore
from homz.db import documents as D
from homz.db.mongo import get_database
from homz.db.repository import Repository
from homz.logging_setup import get_logger
from homz.scrapers import get_scraper

log = get_logger(__name__)

#: HTTP statuses that mean the portal no longer has the listing.
GONE_STATUSES = frozenset({404, 410})
_COMMIT_EVERY = 30


# ---------------------------------------------------------------------------
# image carry-over (pure)
# ---------------------------------------------------------------------------


def _is_resolved(image: Image) -> bool:
    """Processed and uploaded, or permanently unusable — nothing left to ingest."""
    from homz.images.ingest import _PERMANENT

    if image.blob_url:
        return True
    if image.fetch_error:
        head = image.fetch_error.split(":")[0]
        return head == "junk" or head in _PERMANENT
    return False


def carry_over_images(
    incoming: list[Image], existing: list[dict[str, Any]] | None
) -> tuple[list[Image], bool]:
    """Keep already-processed photos when a listing is re-scraped.

    A fresh parse returns portal URLs with no `blob_url`. Writing that over the
    stored document would send a listing that has branded photos back to the
    "images coming soon" placeholder. Images are matched by portal `url`; a
    match keeps its stored processing result. Returns the merged list and
    whether every image is resolved, i.e. no ingestion is needed.
    """
    by_url = {e.get("url"): e for e in (existing or []) if e.get("url")}
    merged: list[Image] = []
    for image in incoming:
        previous = by_url.get(image.url)
        if previous is not None:
            candidate = Image.model_validate(previous)
            if _is_resolved(candidate):
                merged.append(candidate)
                continue
        merged.append(image)
    return merged, all(_is_resolved(i) for i in merged)


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


@dataclass
class RefreshResult:
    source: str
    job: str
    status: JobStatus = JobStatus.RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    discovered: int = 0
    seen: int = 0
    touched: int = 0
    known_active: int = 0
    new_targets: int = 0
    stale_targets: int = 0
    fetched: int = 0
    loaded: int = 0
    removed: int = 0
    skipped_robots: int = 0
    errors: int = 0
    blocked: int = 0
    stopped: str | None = None
    dry_run: bool = False
    error_samples: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float | None:
        """Share of the active warehouse records this run's discovery confirmed."""
        return round(self.touched / self.known_active, 3) if self.known_active else None

    def note_error(self, exc: BaseException) -> None:
        self.errors += 1
        if len(self.error_samples) < 10:
            self.error_samples.append(f"{type(exc).__name__}: {str(exc)[:200]}")

    def as_dict(self) -> dict[str, Any]:
        end = self.finished_at or datetime.now(UTC)
        return {
            "source": self.source,
            "job": self.job,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": end.isoformat(),
            "duration_s": round((end - self.started_at).total_seconds(), 2),
            "discovered": self.discovered,
            "seen": self.seen,
            "touched": self.touched,
            "known_active": self.known_active,
            "coverage": self.coverage,
            "new_targets": self.new_targets,
            "stale_targets": self.stale_targets,
            "fetched": self.fetched,
            "loaded": self.loaded,
            "removed": self.removed,
            "skipped_known": 0,
            "skipped_robots": self.skipped_robots,
            "parsed": self.loaded,
            "errors": self.errors,
            "blocked": self.blocked,
            "stopped": self.stopped,
            "dry_run": self.dry_run,
            "error_samples": self.error_samples,
        }


def build_job(
    source: str, *, city: str, listing_type: str | None, property_type: str | None,
    max_pages: int | None,
) -> ScrapeJob:
    """A discovery job sized for a full segment sweep, mirroring `full_crawl.py`."""
    if source == "magicbricks":
        return ScrapeJob(
            name="refresh", city=city, listing_type=listing_type or "sale",
            property_type=property_type, max_pages=max_pages or 100,
            max_items=1_000_000, incremental=False,
        )
    if source == "squareyards":
        name = {"rent": "rentals", "resale": "resale"}.get(listing_type or "", "projects")
        return ScrapeJob(
            name=name, city=city, listing_type=listing_type if name != "projects" else None,
            max_pages=max_pages or 3, max_items=1_000_000, incremental=False,
        )
    raise ValueError(f"refresh supports magicbricks and squareyards, not {source!r}")


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


#: MagicBricks pagination repeats page 1 after this many pages.
_MB_MAX_PAGE = 100


def load_mb_localities(listing_type: str) -> list[str]:
    """Gurgaon localities MagicBricks publishes in its sitemap index (cached)."""
    path = (
        Path(__file__).resolve().parents[3]
        / "data" / "checkpoints" / "magicbricks_gurgaon_apartment_localities.json"
    )
    return json.loads(path.read_text(encoding="utf-8-sig"))[listing_type]


async def discover_mb_localities(
    scraper: Any, city: str, listing_type: str, localities: list[str]
) -> AsyncIterator[str]:
    """Sweep the flats category one locality at a time.

    A single citywide stream tops out near 3,000 listings because the portal
    loops back to page 1 after page 100. A page that adds no URL new to its
    locality is treated as empty, since `has_next_page` alone cannot tell a
    real next page from the loop.
    """
    from homz.scrapers.magicbricks import parser

    for index, locality in enumerate(localities, 1):
        seen: set[str] = set()
        empty = 0
        for page in range(1, _MB_MAX_PAGE + 1):
            url = parser.build_apartment_search_url(
                city, listing_type=listing_type, locality=locality, page=page
            )
            try:
                result = await scraper.fetcher.get(url)
            except BlockedError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh.locality_page_failed", url=url, error=str(exc)[:150])
                break
            urls = parser.parse_search_results(result.text)
            fresh = [u for u in urls if u not in seen]
            if not fresh:
                empty += 1
                if empty >= 2:
                    break
            else:
                empty = 0
                seen.update(fresh)
            for found in fresh:
                yield found
            if not urls or not parser.has_next_page(result.text):
                break
        log.info("refresh.locality", n=index, of=len(localities), locality=locality, found=len(seen))


async def _existing_keys(db: Any, keys: list[str]) -> set[str]:
    found: set[str] = set()
    for collection in (D.PROPERTIES, D.PROJECTS):
        for start in range(0, len(keys), 1000):
            cursor = db[collection].find(
                {"_id": {"$in": keys[start : start + 1000]}}, projection={"_id": 1}
            )
            async for doc in cursor:
                found.add(doc["_id"])
    return found


async def _existing_images(db: Any, record: PropertyRecord | ProjectRecord) -> list[dict] | None:
    collection = D.PROPERTIES if isinstance(record, PropertyRecord) else D.PROJECTS
    doc = await db[collection].find_one({"_id": record.natural_key}, projection={"images": 1})
    return (doc or {}).get("images")


async def refresh_source(
    source: str,
    job: ScrapeJob,
    *,
    detail_budget: int = 300,
    max_minutes: float | None = None,
    dry_run: bool = False,
    localities: list[str] | None = None,
) -> RefreshResult:
    """Run one freshness pass for one source/segment. See the module docstring."""
    from homz.etl.pipeline import load_records

    result = RefreshResult(source=source, job=f"refresh:{job.key}", dry_run=dry_run)
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    db = get_database()
    repo = Repository(db)

    scraper_cls = get_scraper(source)
    async with scraper_cls(state_store=StateStore(None)) as scraper:
        # 1. discovery ------------------------------------------------------
        url_ids: dict[str, list[str]] = {}
        state = ScrapeState(source=source, job=job.key)
        try:
            urls = (
                discover_mb_localities(
                    scraper, job.city or "gurgaon", job.listing_type or "sale", localities
                )
                if localities
                else scraper.discover(job, state)
            )
            async for url in urls:
                ids = scraper.source_ids_for_url(url)
                if ids and url not in url_ids:
                    url_ids[url] = ids
        except BlockedError as exc:
            result.blocked += 1
            result.note_error(exc)
            result.status = JobStatus.BLOCKED
            result.stopped = "blocked during discovery"
        except Exception as exc:  # noqa: BLE001
            result.note_error(exc)
            log.exception("refresh.discovery_failed", source=source)

        result.discovered = len(url_ids)
        seen_ids = sorted({i for ids in url_ids.values() for i in ids})
        result.seen = len(seen_ids)

        # 2. sightings ------------------------------------------------------
        result.known_active = sum(
            [
                await db[c].count_documents({"source": source, "is_active": {"$ne": False}})
                for c in (D.PROPERTIES, D.PROJECTS)
            ]
        )
        if seen_ids and not dry_run:
            result.touched = await repo.touch_seen(source, seen_ids)
        elif seen_ids:
            keys = [D.natural_key(source, i) for i in seen_ids]
            result.touched = len(await _existing_keys(db, keys))

        if result.status is JobStatus.BLOCKED:
            return await _finish(repo, result, dry_run)

        # 3. choose detail targets -----------------------------------------
        existing = await _existing_keys(db, [D.natural_key(source, i) for i in seen_ids])
        new_urls = [
            url for url, ids in url_ids.items()
            if not any(D.natural_key(source, i) in existing for i in ids)
        ]
        targets: list[tuple[str, str | None]] = [(u, None) for u in new_urls[:detail_budget]]
        result.new_targets = len(targets)
        remaining = detail_budget - len(targets)
        if remaining > 0:
            # A SquareYards project and its property projection share one URL;
            # fetch each page once.
            chosen = {u for u, _ in targets}
            # Over-ask, then trim after de-duplicating, so shared URLs do not
            # eat into the budget.
            for row in await repo.stalest_urls(source, remaining * 2):
                if row["url"] in chosen:
                    continue
                chosen.add(row["url"])
                targets.append((row["url"], row["id"]))
                if len(targets) >= detail_budget:
                    break
            result.stale_targets = len(targets) - result.new_targets

        if dry_run or not targets:
            result.stopped = "dry run" if dry_run else None
            return await _finish(repo, result, dry_run)

        # 4. fetch -----------------------------------------------------------
        fresh_buffer: list[Any] = []  # need photo ingestion
        carried_buffer: list[Any] = []  # images already processed

        async def flush() -> None:
            for buffer, ingest in ((fresh_buffer, True), (carried_buffer, False)):
                if buffer:
                    loaded = await load_records(list(buffer), ingest=ingest)
                    result.loaded += loaded.inserted + loaded.updated
                    buffer.clear()

        for url, stale_id in targets:
            if deadline and time.monotonic() > deadline:
                result.stopped = f"time budget of {max_minutes:g} min reached"
                break
            try:
                page = await scraper.fetch_detail(url, job)
                result.fetched += 1
            except RobotsDisallowed:
                result.skipped_robots += 1
                continue
            except BlockedError as exc:
                result.blocked += 1
                result.note_error(exc)
                result.status = JobStatus.BLOCKED
                result.stopped = "blocked while fetching details"
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in GONE_STATUSES and stale_id:
                    await repo.mark_removed(source, stale_id)
                    result.removed += 1
                else:
                    result.note_error(exc)
                continue
            except Exception as exc:  # noqa: BLE001
                result.note_error(exc)
                continue

            try:
                records = await scraper.parse_detail(page, job)
            except Exception as exc:  # noqa: BLE001
                result.note_error(exc)
                continue

            for record in records:
                images = getattr(record, "images", None)
                if images:
                    merged, resolved = carry_over_images(
                        list(images), await _existing_images(db, record)
                    )
                    record.images = merged
                    (carried_buffer if resolved else fresh_buffer).append(record)
                else:
                    carried_buffer.append(record)
            if len(fresh_buffer) + len(carried_buffer) >= _COMMIT_EVERY:
                await flush()

        await flush()

    return await _finish(repo, result, dry_run)


async def _finish(repo: Repository, result: RefreshResult, dry_run: bool) -> RefreshResult:
    result.finished_at = datetime.now(UTC)
    if result.status is JobStatus.RUNNING:
        if result.discovered == 0:
            # Discovering nothing is a failure, not a clean run (same rule as
            # BaseScraper.run_job) — and it is what stops delisting downstream.
            result.status = JobStatus.FAILED
            result.error_samples.append("discovered 0 listing URLs")
        elif result.errors and not result.loaded and not result.removed:
            result.status = JobStatus.PARTIAL
        else:
            result.status = JobStatus.SUCCESS
    if not dry_run:
        # Recorded like any scrape run: `guarded_mark_stale` reads the latest
        # status for the source and refuses to delist after a blocked/failed one.
        await repo.record_run(result.as_dict(), inserted=0, updated=result.loaded)
    log.info("refresh.done", **{k: v for k, v in result.as_dict().items() if k != "error_samples"})
    return result
