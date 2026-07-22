"""Incremental-scrape state.

Each (source, job) pair keeps a cursor row in `scrape_state`:

  * `last_run_at`      — when the job last completed
  * `cursor`           — an opaque JSON blob the scraper defines (page number,
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
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

_MAX_TRACKED_HASHES = 20_000


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

    Falls back to in-memory when no session is provided, which keeps unit tests
    and dry-runs free of a database. SQLAlchemy is imported lazily for the same
    reason: a parser must be usable without the DB stack installed.
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session
        self._memory: dict[tuple[str, str], ScrapeState] = {}

    async def load(self, source: str, job: str) -> ScrapeState:
        if self._session is None:
            return self._memory.setdefault((source, job), ScrapeState(source=source, job=job))

        from sqlalchemy import text

        row = (
            await self._session.execute(
                text(
                    "SELECT cursor, last_run_at, seen_hashes, stats "
                    "FROM scrape_state WHERE source = :source AND job = :job"
                ),
                {"source": source, "job": job},
            )
        ).first()

        if row is None:
            return ScrapeState(source=source, job=job)

        cursor, last_run_at, seen_hashes, stats = row
        return ScrapeState(
            source=source,
            job=job,
            cursor=cursor or {},
            last_run_at=last_run_at,
            seen_hashes=set(seen_hashes or []),
            stats=stats or {},
        )

    async def save(self, state: ScrapeState) -> None:
        state.touch()
        if self._session is None:
            self._memory[(state.source, state.job)] = state
            return

        from sqlalchemy import text

        await self._session.execute(
            text(
                """
                INSERT INTO scrape_state (source, job, cursor, last_run_at, seen_hashes, stats)
                VALUES (:source, :job, CAST(:cursor AS JSONB), :last_run_at,
                        :seen_hashes, CAST(:stats AS JSONB))
                ON CONFLICT (source, job) DO UPDATE SET
                    cursor       = EXCLUDED.cursor,
                    last_run_at  = EXCLUDED.last_run_at,
                    seen_hashes  = EXCLUDED.seen_hashes,
                    stats        = EXCLUDED.stats,
                    updated_at   = NOW()
                """
            ),
            {
                "source": state.source,
                "job": state.job,
                "cursor": _dumps(state.cursor),
                "last_run_at": state.last_run_at,
                # Persist a bounded tail — the full set would bloat the row.
                "seen_hashes": list(state.seen_hashes)[:_MAX_TRACKED_HASHES],
                "stats": _dumps(state.stats),
            },
        )
        await self._session.commit()
        log.debug("state.saved", source=state.source, job=state.job,
                  hashes=len(state.seen_hashes))


def _dumps(value: Any) -> str:
    import orjson

    return orjson.dumps(value, default=str).decode()
