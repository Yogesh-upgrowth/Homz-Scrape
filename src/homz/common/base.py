"""BaseScraper — the contract and the shared machinery.

A source-specific scraper subclasses this and implements `discover()` and
`parse_detail()`. Everything else — fetcher lifecycle, incremental state,
error accounting, block handling, the run report — lives here, so adding a
fifth portal is two methods, not a new pipeline.

    class MyScraper(BaseScraper):
        source = Source.MYPORTAL
        base_url = "https://example.com"

        async def discover(self, job): ...       # yield detail URLs
        async def parse_detail(self, result): ... # FetchResult -> records
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from homz.common.captcha import BlockedError
from homz.common.enums import JobStatus, Source
from homz.common.http import Fetcher, FetchResult
from homz.common.proxy import ProxyPool
from homz.common.ratelimit import RateLimiter
from homz.common.rawstore import RawStore
from homz.common.robots import RobotsDisallowed, RobotsGate
from homz.common.schema import ScrapedRecord
from homz.common.state import ScrapeState, StateStore
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)


@dataclass
class ScrapeJob:
    """One unit of work: what to crawl and how deep."""

    name: str
    city: str | None = None
    listing_type: str | None = None
    property_type: str | None = None
    max_pages: int = 5
    max_items: int = 500
    stop_after_known: int = 25
    incremental: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity for this job.

        `params` is part of the key: `scrape_state` is keyed on
        (source, job), so two Reddit jobs that differ only by subreddit must
        not collapse onto one cursor row — they would overwrite each other's
        incremental position every run.
        """
        parts = [self.name, self.city or "", self.listing_type or "", self.property_type or ""]
        parts.extend(f"{k}={v}" for k, v in sorted(self.params.items()) if v not in (None, ""))
        return ":".join(p for p in parts if p)


@dataclass
class ScrapeReport:
    source: str
    job: str
    status: JobStatus = JobStatus.RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    discovered: int = 0
    fetched: int = 0
    parsed: int = 0
    skipped_known: int = 0
    skipped_robots: int = 0
    errors: int = 0
    blocked: int = 0
    error_samples: list[str] = field(default_factory=list)
    fetcher_stats: dict[str, int] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()

    def record_error(self, exc: BaseException) -> None:
        self.errors += 1
        if len(self.error_samples) < 10:
            self.error_samples.append(f"{type(exc).__name__}: {str(exc)[:200]}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "job": self.job,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": round(self.duration_s, 2),
            "discovered": self.discovered,
            "fetched": self.fetched,
            "parsed": self.parsed,
            "skipped_known": self.skipped_known,
            "skipped_robots": self.skipped_robots,
            "errors": self.errors,
            "blocked": self.blocked,
            "error_samples": self.error_samples,
            "fetcher_stats": self.fetcher_stats,
        }


class BaseScraper(ABC):
    # --- subclass contract -------------------------------------------------
    source: Source
    base_url: str
    #: Set True when the source only renders under JS; gives the subclass a
    #: `self.browser` pool.
    needs_browser: bool = False
    #: Per-source politeness override (requests/second).
    host_rps: float | None = None
    #: Jobs this scraper knows how to run when none are supplied explicitly.
    default_jobs: tuple[ScrapeJob, ...] = ()

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        proxy_pool: ProxyPool | None = None,
        robots: RobotsGate | None = None,
        raw_store: RawStore | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self.limiter = rate_limiter or RateLimiter()
        if self.host_rps is not None:
            self.limiter.set_host_rate(RateLimiter.host_of(self.base_url), self.host_rps)
        self.proxies = proxy_pool or ProxyPool()
        self.robots = robots or RobotsGate()
        self.raw_store = raw_store or RawStore()
        self.state_store = state_store or StateStore()

        self.fetcher = Fetcher(
            source=self.source.value,
            rate_limiter=self.limiter,
            proxy_pool=self.proxies,
            robots=self.robots,
            raw_store=self.raw_store,
        )
        self._browser = None
        self.log = get_logger(f"scraper.{self.source.value}")

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> BaseScraper:
        await self.fetcher.__aenter__()
        if self.needs_browser:
            from homz.common.browser import BrowserPool

            self._browser = BrowserPool(
                source=self.source.value,
                rate_limiter=self.limiter,
                proxy_pool=self.proxies,
                raw_store=self.raw_store,
            )
            await self._browser.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._browser is not None:
            await self._browser.aclose()
        await self.fetcher.aclose()

    @property
    def browser(self):
        if self._browser is None:
            raise RuntimeError(
                f"{type(self).__name__} did not request a browser "
                "(set needs_browser = True and use `async with`)"
            )
        return self._browser

    # -- subclass hooks -----------------------------------------------------

    @abstractmethod
    def discover(self, job: ScrapeJob, state: ScrapeState) -> AsyncIterator[str]:
        """Yield detail-page URLs for this job (an async generator)."""
        raise NotImplementedError

    @abstractmethod
    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        """Turn one fetched page into zero or more normalized records."""
        raise NotImplementedError

    async def fetch_detail(self, url: str, job: ScrapeJob) -> FetchResult:
        """Override when a source needs a browser or special headers."""
        return await self.fetcher.get(url)

    # -- the run loop -------------------------------------------------------

    async def run_job(self, job: ScrapeJob) -> tuple[list[ScrapedRecord], ScrapeReport]:
        report = ScrapeReport(source=self.source.value, job=job.key)
        state = await self.state_store.load(self.source.value, job.key)
        records: list[ScrapedRecord] = []
        consecutive_known = 0
        started = time.monotonic()

        self.log.info(
            "job.start",
            job=job.key,
            max_pages=job.max_pages,
            max_items=job.max_items,
            incremental=job.incremental,
            last_run_at=state.last_run_at.isoformat() if state.last_run_at else None,
        )

        try:
            async for url in self.discover(job, state):
                report.discovered += 1
                if report.parsed >= job.max_items:
                    self.log.info("job.max_items_reached", job=job.key, items=report.parsed)
                    break

                try:
                    result = await self.fetch_detail(url, job)
                    report.fetched += 1
                except RobotsDisallowed:
                    report.skipped_robots += 1
                    continue
                except BlockedError as exc:
                    report.blocked += 1
                    report.record_error(exc)
                    if settings.abort_on_block:
                        report.status = JobStatus.BLOCKED
                        self.log.error("job.aborted_blocked", job=job.key, url=url)
                        break
                    continue
                except Exception as exc:  # noqa: BLE001
                    report.record_error(exc)
                    self.log.warning("job.fetch_failed", url=url, error=str(exc)[:200])
                    continue

                try:
                    parsed = await self.parse_detail(result, job)
                except Exception as exc:  # noqa: BLE001
                    report.record_error(exc)
                    self.log.warning(
                        "job.parse_failed", url=url, raw_key=result.raw_key,
                        error=str(exc)[:200],
                    )
                    continue

                new_in_page = 0
                for record in parsed:
                    content_hash = getattr(record, "content_hash", None)
                    if job.incremental and state.is_known(content_hash):
                        report.skipped_known += 1
                        continue
                    state.mark_seen(content_hash)
                    records.append(record)
                    report.parsed += 1
                    new_in_page += 1

                if job.incremental and new_in_page == 0 and parsed:
                    consecutive_known += 1
                    if consecutive_known >= job.stop_after_known:
                        self.log.info(
                            "job.incremental_stop", job=job.key, known_streak=consecutive_known
                        )
                        break
                else:
                    consecutive_known = 0

        except BlockedError as exc:
            report.blocked += 1
            report.record_error(exc)
            report.status = JobStatus.BLOCKED
        except asyncio.CancelledError:
            report.status = JobStatus.FAILED
            raise
        except Exception as exc:  # noqa: BLE001
            report.record_error(exc)
            report.status = JobStatus.FAILED
            self.log.exception("job.failed", job=job.key)

        if report.status is JobStatus.RUNNING:
            if report.errors and report.parsed:
                report.status = JobStatus.PARTIAL
            elif report.errors and not report.parsed:
                report.status = JobStatus.FAILED
            else:
                report.status = JobStatus.SUCCESS

        report.finished_at = datetime.now(UTC)
        report.fetcher_stats = dict(self.fetcher.stats)
        state.stats = {
            "last_parsed": report.parsed,
            "last_status": report.status.value,
            "last_duration_s": round(time.monotonic() - started, 2),
        }
        await self.state_store.save(state)

        self.log.info("job.done", **report.as_dict())
        return records, report

    async def run(
        self, jobs: list[ScrapeJob] | None = None
    ) -> tuple[list[ScrapedRecord], list[ScrapeReport]]:
        """Run every job for this source sequentially.

        Sequential is intentional: jobs share a host, and the rate limiter
        would serialise them anyway — running them in parallel only makes the
        logs harder to read.
        """
        jobs = jobs or list(self.default_jobs)
        if not jobs:
            raise ValueError(f"{type(self).__name__} has no jobs to run")

        all_records: list[ScrapedRecord] = []
        reports: list[ScrapeReport] = []
        for job in jobs:
            records, report = await self.run_job(job)
            all_records.extend(records)
            reports.append(report)
            if report.status is JobStatus.BLOCKED and settings.abort_on_block:
                self.log.error("source.aborted_remaining_jobs", source=self.source.value)
                break
        return all_records, reports

    # -- helpers for subclasses --------------------------------------------

    def abs_url(self, href: str | None) -> str | None:
        from homz.common.parsing import absolute_url

        return absolute_url(self.base_url, href)
