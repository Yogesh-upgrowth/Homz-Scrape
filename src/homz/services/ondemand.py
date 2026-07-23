"""On-demand fill: turn a search miss into a scrape task.

The flow this implements:

    user searches  →  Mongo has nothing (or too little)
                   →  a *fill task* is queued describing what to fetch
                   →  a client claims the task, scrapes, POSTs the result back
                   →  the next identical search is a cache hit

This is demand-driven crawling, and it is genuinely gentler than blanket
crawling — nothing is fetched unless a real person asked for it. But it does
break the previously-true property that "the API can never generate traffic
against a source", so it carries its own guardrails:

* **Cooldown per query.** The same search cannot re-queue for
  `ondemand_cooldown_minutes`. Without this, a user hammering refresh on a
  genuinely empty query becomes a scraping loop.
* **Daily budget.** A hard ceiling on tasks created per day, so a bot
  crawling *our* search cannot amplify into a thousand requests at a portal.
* **Never blocks the response.** The search returns whatever the DB has
  immediately, with `backfill: {...}` telling the client work was queued.
  Waiting on a live scrape would put a 30-second portal fetch inside a user's
  page load.
* **Deduplicated.** Task `_id` is a hash of the normalized query, so ten users
  searching the same thing create one task.

Tasks are *descriptions of work*, not URLs to blindly fetch — the claiming
client decides how to satisfy them, and still goes through robots/rate limits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

FILL_TASKS = "fill_tasks"

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: Fields of a search that define "the same query" for dedupe/cooldown.
#: Pagination and sort deliberately excluded — page 2 of a query is not a
#: different information need.
_IDENTITY_FIELDS = (
    "q", "city", "sector", "locality", "micro_market", "builder", "project",
    "listing_type", "property_type", "configuration",
    "bedrooms_min", "bedrooms_max", "price_min", "price_max",
    "possession_status", "segment",
)


@dataclass
class FillDecision:
    should_fill: bool
    reason: str
    task_id: str | None = None
    status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"queued": self.should_fill, "reason": self.reason}
        if self.task_id:
            payload["task_id"] = self.task_id
        if self.status:
            payload["status"] = self.status
        return payload


def query_fingerprint(query: Any) -> str:
    """Stable id for a search, ignoring pagination and sort."""
    parts: list[str] = []
    for field_name in _IDENTITY_FIELDS:
        value = getattr(query, field_name, None)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ",".join(sorted(str(v) for v in value))
        parts.append(f"{field_name}={str(value).strip().lower()}")
    if not parts:
        return "empty"
    return hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:24]


def describe_query(query: Any) -> dict[str, Any]:
    """The task payload a scraping client needs to satisfy the request."""
    out: dict[str, Any] = {}
    for field_name in _IDENTITY_FIELDS:
        value = getattr(query, field_name, None)
        if value not in (None, "", []):
            out[field_name] = str(value) if not isinstance(value, list) else list(value)
    return out


class DemandFiller:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    # -- decision -----------------------------------------------------------

    async def consider(self, query: Any, result_count: int) -> FillDecision:
        """Called after every search. Cheap: one indexed lookup on a hit."""
        if not settings.ondemand_enabled:
            return FillDecision(False, "ondemand disabled")
        if result_count >= settings.ondemand_min_results:
            return FillDecision(False, "enough results")

        fingerprint = query_fingerprint(query)
        if fingerprint == "empty":
            # A bare "show me everything" that returns nothing means the
            # warehouse is empty — that is a seeding problem, not a gap to
            # fill one query at a time.
            return FillDecision(False, "unfiltered query")

        now = datetime.now(UTC)
        existing = await self.db[FILL_TASKS].find_one(
            {"_id": fingerprint},
            projection={"status": 1, "cooldown_until": 1, "completed_at": 1},
        )

        if existing:
            cooldown_until = existing.get("cooldown_until")
            if existing.get("status") in {STATUS_PENDING, STATUS_CLAIMED}:
                return FillDecision(False, "already queued", fingerprint,
                                    existing.get("status"))
            if cooldown_until and cooldown_until > now:
                return FillDecision(False, "cooling down", fingerprint,
                                    existing.get("status"))

        if not await self._within_budget(now):
            log.warning("ondemand.budget_exhausted", budget=settings.ondemand_daily_budget)
            return FillDecision(False, "daily budget exhausted")

        try:
            await self.db[FILL_TASKS].update_one(
                {"_id": fingerprint},
                {
                    "$set": {
                        "query": describe_query(query),
                        "status": STATUS_PENDING,
                        "requested_at": now,
                        "cooldown_until": now + timedelta(
                            minutes=settings.ondemand_cooldown_minutes
                        ),
                        "expires_at": now + timedelta(
                            hours=settings.ondemand_task_ttl_hours
                        ),
                        "claimed_by": None,
                        "claimed_at": None,
                        "completed_at": None,
                        "result_count_at_request": result_count,
                    },
                    "$setOnInsert": {"created_at": now},
                    "$inc": {"request_count": 1},
                },
                upsert=True,
            )
        except PyMongoError as exc:
            # A queueing failure must never break the search response.
            log.warning("ondemand.queue_failed", error=str(exc)[:200])
            return FillDecision(False, "queue unavailable")

        log.info("ondemand.task_queued", task=fingerprint, found=result_count)
        return FillDecision(True, "queued for backfill", fingerprint, STATUS_PENDING)

    async def _within_budget(self, now: datetime) -> bool:
        since = now - timedelta(days=1)
        created = await self.db[FILL_TASKS].count_documents({"created_at": {"$gte": since}})
        return created < settings.ondemand_daily_budget

    # -- client-facing queue ------------------------------------------------

    async def claim(self, *, worker: str, limit: int = 1) -> list[dict[str, Any]]:
        """Atomically hand pending tasks to a worker.

        `find_one_and_update` is what makes this safe with several clients
        polling: the status flips to `claimed` in the same operation that reads
        it, so two workers cannot take the same task.
        """
        now = datetime.now(UTC)
        # A task claimed but never completed (client crashed, tab closed) is
        # returned to the pool rather than being stuck forever.
        stale = now - timedelta(minutes=30)
        claimed: list[dict[str, Any]] = []

        for _ in range(max(1, min(limit, 25))):
            task = await self.db[FILL_TASKS].find_one_and_update(
                {
                    "$or": [
                        {"status": STATUS_PENDING},
                        {"status": STATUS_CLAIMED, "claimed_at": {"$lt": stale}},
                    ],
                    "expires_at": {"$gt": now},
                },
                {"$set": {"status": STATUS_CLAIMED, "claimed_by": worker,
                          "claimed_at": now},
                 "$inc": {"attempts": 1}},
                sort=[("requested_at", 1)],
                return_document=ReturnDocument.AFTER,
            )
            if task is None:
                break
            claimed.append({
                "task_id": task["_id"],
                "query": task.get("query", {}),
                "attempts": task.get("attempts", 1),
                "requested_at": task.get("requested_at"),
            })

        if claimed:
            log.info("ondemand.tasks_claimed", worker=worker, count=len(claimed))
        return claimed

    async def complete(
        self, task_id: str, *, records_written: int = 0, error: str | None = None
    ) -> bool:
        now = datetime.now(UTC)
        status = STATUS_FAILED if error else STATUS_DONE
        result = await self.db[FILL_TASKS].update_one(
            {"_id": task_id},
            {"$set": {
                "status": status,
                "completed_at": now,
                "records_written": records_written,
                "last_error": error[:500] if error else None,
                # A failed task becomes retryable sooner than a successful one,
                # but not immediately — a source that just failed will likely
                # fail again.
                "cooldown_until": now + timedelta(
                    minutes=30 if error else settings.ondemand_cooldown_minutes
                ),
            }},
        )
        log.info("ondemand.task_completed", task=task_id, status=status,
                 records=records_written)
        return result.matched_count > 0

    async def stats(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        pipeline = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
        rows = await self.db[FILL_TASKS].aggregate(pipeline).to_list(length=10)
        by_status = {row["_id"]: row["count"] for row in rows}
        today = await self.db[FILL_TASKS].count_documents(
            {"created_at": {"$gte": now - timedelta(days=1)}}
        )
        return {
            "by_status": by_status,
            "created_last_24h": today,
            "daily_budget": settings.ondemand_daily_budget,
            "budget_remaining": max(0, settings.ondemand_daily_budget - today),
            "enabled": settings.ondemand_enabled,
            "min_results_threshold": settings.ondemand_min_results,
        }
