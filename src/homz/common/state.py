"""Incremental-scrape state.

Each (source, job) pair keeps a cursor document in `scrape_state`:

  * `last_run_at`      — when the job last completed
  * `cursor`           — an opaque blob the scraper defines (page number,
                         reddit fullname, last listing id, …)
  * `seen_hashes`      — bounded set of recently seen content hashes, so a
                         re-listed page doesn't get re-parsed and re-written

A run that finds `stop_after_known` consecutive already-seen records stops
paginating — that is what turns a 20-page crawl into a 2-page one on the
second day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homz.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from motor.motor_asyncio import AsyncIOMotorDatabase

log = get_logger(__name__)

_MAX_TRACKED_HASHES = 20_000
_COLLECTION = "scrape_state"


@dataclass
class ScrapeState:
    source: str
    job: str
    cursor: dict[str, Any] = field(default_factory=dict)
    last_run_at: datetime | None = None
    seen_hashes: set[str] = field(default_factory=set)
    stats: dict[str, Any] = field(default_factory=dict)

    # -- incremental decisions ---------------------------------------------

    def is_known(self, content_hash: str | None) -> bool:
        return bool(content_hash) and content_hash in self.seen_hashes

    def mark_seen(self, content_hash: str | None) -> None:
        if not content_hash:
            return
        if len(self.seen_hashes) >= _MAX_TRACKED_HASHES:
            # Cheap bounded eviction: drop an arbitrary 10%.
            for _ in range(_MAX_TRACKED_HASHES // 10):
                self.seen_hashes.pop()
        self.seen_hashes.add(content_hash)

    def touch(self) -> None:
        self.last_run_at = datetime.now(UTC)


class StateStore:
    """Persistence for ScrapeState.

    Falls back to in-memory when no database handle is provided, which keeps
    unit tests and dry-runs free of a database.
    """

    def __init__(self, db: AsyncIOMotorDatabase | None = None) -> None:
        self._db = db
        self._memory: dict[tuple[str, str], ScrapeState] = {}

    async def load(self, source: str, job: str) -> ScrapeState:
        if self._db is None:
            return self._memory.setdefault((source, job), ScrapeState(source=source, job=job))

        document = await self._db[_COLLECTION].find_one({"_id": f"{source}::{job}"})
        if document is None:
            return ScrapeState(source=source, job=job)

        return ScrapeState(
            source=source,
            job=job,
            cursor=document.get("cursor") or {},
            last_run_at=document.get("last_run_at"),
            seen_hashes=set(document.get("seen_hashes") or []),
            stats=document.get("stats") or {},
        )

    async def save(self, state: ScrapeState) -> None:
        state.touch()
        if self._db is None:
            self._memory[(state.source, state.job)] = state
            return

        await self._db[_COLLECTION].update_one(
            {"_id": f"{state.source}::{state.job}"},
            {
                "$set": {
                    "source": state.source,
                    "job": state.job,
                    "cursor": state.cursor,
                    "last_run_at": state.last_run_at,
                    # Persist a bounded tail — the full set would bloat the
                    # document toward the 16 MB ceiling on a long-running job.
                    "seen_hashes": list(state.seen_hashes)[:_MAX_TRACKED_HASHES],
                    "stats": state.stats,
                    "updated_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        log.debug("state.saved", source=state.source, job=state.job,
                  hashes=len(state.seen_hashes))
