"""Ingest: accept scraped payloads from a client and write them.

A client (your browser extension, a worker, a script) submits either raw HTML
or already-normalized records. Everything goes through the *same* parser,
normalizer, dedupe and upsert path as the server-side scrapers, so a
client-sourced listing is indistinguishable downstream — same schema, same
scores, same price-history capture.

Security posture, in order of importance:

1. **Authenticated.** A bearer token is required. An open ingest endpoint lets
   anyone write arbitrary documents into the warehouse, which is worse than
   having no data at all — poisoned prices silently corrupt every median,
   yield and score.
2. **Parsed, never trusted.** Submitted HTML is run through the source's own
   parser. The client cannot hand us a JSON blob claiming a price; it hands us
   a page, and we extract from it. `/ingest/records` (which does accept
   structured data) is validated by the Pydantic schema and is intended for
   trusted internal workers.
3. **Size- and rate-limited.** Bounded payload and per-token request rate.
4. **Provenance recorded.** Every ingested record carries `ingest.client` and
   `ingest.received_at`, so client-sourced data can be audited or rolled back
   separately from server-sourced data.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from homz.common.schema import (
    ProjectRecord,
    PropertyRecord,
    RedditPostRecord,
    ScrapedRecord,
)
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

# source → (property parser, project parser)
_PARSERS: dict[str, tuple[Any, Any]] = {}


def _load_parsers() -> dict[str, tuple[Any, Any]]:
    """Imported lazily so the ingest module stays importable without bs4."""
    global _PARSERS
    if _PARSERS:
        return _PARSERS
    from homz.scrapers.housing import parser as housing
    from homz.scrapers.magicbricks import parser as magicbricks
    from homz.scrapers.squareyards import parser as squareyards

    _PARSERS = {
        "magicbricks": (magicbricks.parse_property_detail, magicbricks.parse_project_detail),
        "housing": (housing.parse_property_detail, housing.parse_project_detail),
        "squareyards": (None, squareyards.parse_project_detail),
    }
    return _PARSERS


SUPPORTED_SOURCES = ("magicbricks", "housing", "squareyards")


class IngestError(Exception):
    """Client error — maps to a 4xx."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def verify_token(authorization: str | None) -> str:
    """Constant-time bearer check. Returns a client label for provenance."""
    if not settings.ingest_token:
        raise IngestError(
            "ingest is disabled: set HOMZ_INGEST_TOKEN to enable it", status_code=503
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise IngestError("missing bearer token", status_code=401)

    presented = authorization.split(" ", 1)[1].strip()
    # compare_digest avoids leaking the token length/prefix through timing.
    if not hmac.compare_digest(presented, settings.ingest_token):
        raise IngestError("invalid token", status_code=401)
    return "client"


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    hits: deque[float] = field(default_factory=deque)


class RateLimiter:
    """Sliding-window limiter, per client key.

    In-process only: with several API replicas each gets its own window. That
    is acceptable here because the limit exists to stop a runaway client, not
    to meter billing.
    """

    def __init__(self, per_minute: int | None = None) -> None:
        self.per_minute = per_minute or settings.ingest_rate_limit_per_minute
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._buckets[key]
        while bucket.hits and now - bucket.hits[0] > 60.0:
            bucket.hits.popleft()
        if len(bucket.hits) >= self.per_minute:
            raise IngestError(
                f"rate limit exceeded ({self.per_minute}/min)", status_code=429
            )
        bucket.hits.append(now)


_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    accepted: int = 0
    inserted: int = 0
    updated: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "inserted": self.inserted,
            "updated": self.updated,
            "rejected": self.rejected,
            "errors": self.errors[:10],
            "ids": self.ids[:100],
        }


class IngestService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    # -- raw page ----------------------------------------------------------

    async def ingest_page(
        self,
        *,
        source: str,
        url: str,
        html: str,
        client: str = "client",
        kind: str = "auto",
        task_id: str | None = None,
    ) -> IngestResult:
        """Parse a submitted page and persist whatever it yields."""
        result = IngestResult()

        source = (source or "").strip().lower()
        if source not in SUPPORTED_SOURCES:
            raise IngestError(
                f"unsupported source {source!r}; expected one of "
                f"{', '.join(SUPPORTED_SOURCES)}"
            )
        if not url or not url.startswith(("http://", "https://")):
            raise IngestError("url must be an absolute http(s) URL")
        if not html or len(html) < 200:
            raise IngestError("html payload is empty or too small to be a page")
        if len(html.encode("utf-8", errors="ignore")) > settings.ingest_max_payload_bytes:
            raise IngestError("payload too large", status_code=413)

        property_parser, project_parser = _load_parsers()[source]

        # Archive the submission before parsing. If the parser is wrong for
        # this page shape, the payload is still recoverable and replayable —
        # the client should never have to re-scrape because of our bug.
        raw_key = None
        try:
            from homz.common.rawstore import RawStore

            raw_key = RawStore().put(
                source=source, url=url, content=html,
                metadata={"ingested": True, "client": client, "task_id": task_id},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest.archive_failed", url=url, error=str(exc)[:160])

        records: list[ScrapedRecord] = []
        try:
            wants_project = kind == "project" or (
                kind == "auto" and ("/project" in url or source == "squareyards")
            )
            if wants_project and project_parser is not None:
                parsed = project_parser(html, url, raw_html_key=raw_key)
                if parsed is not None:
                    records.append(parsed)
                    if source == "squareyards":
                        from homz.scrapers.squareyards import parser as sy

                        records.append(sy.project_to_property(parsed))
            elif property_parser is not None:
                parsed = property_parser(html, url, raw_html_key=raw_key)
                if parsed is not None:
                    records.append(parsed)
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest.parse_failed", source=source, url=url,
                        error=str(exc)[:300])
            result.rejected += 1
            result.errors.append(f"parse failed: {type(exc).__name__}: {str(exc)[:200]}")
            return result

        if not records:
            result.rejected += 1
            result.errors.append(
                "parser produced no records — the page may not be a detail page, "
                f"or {source}'s markup has changed (payload archived as {raw_key})"
            )
            await self._close_task(task_id, result)
            return result

        await self._persist(records, client=client, result=result)
        await self._close_task(task_id, result)
        return result

    # -- structured records -------------------------------------------------

    async def ingest_records(
        self,
        payloads: list[dict[str, Any]],
        *,
        client: str = "client",
        task_id: str | None = None,
    ) -> IngestResult:
        """Accept already-normalized records.

        Validated by the same Pydantic models the scrapers emit, so a
        malformed or hostile payload is rejected before it reaches Mongo.
        """
        result = IngestResult()
        if not payloads:
            raise IngestError("no records supplied")
        if len(payloads) > 500:
            raise IngestError("at most 500 records per request", status_code=413)

        records: list[ScrapedRecord] = []
        for index, payload in enumerate(payloads):
            record_type = (payload.pop("record_type", None) or "property").lower()
            model = {
                "property": PropertyRecord,
                "project": ProjectRecord,
                "reddit_post": RedditPostRecord,
            }.get(record_type)
            if model is None:
                result.rejected += 1
                result.errors.append(f"[{index}] unknown record_type {record_type!r}")
                continue
            try:
                records.append(model.model_validate(payload))
            except Exception as exc:  # noqa: BLE001
                result.rejected += 1
                result.errors.append(f"[{index}] validation failed: {str(exc)[:200]}")

        if not records:
            await self._close_task(task_id, result)
            return result

        await self._persist(records, client=client, result=result)
        await self._close_task(task_id, result)
        return result

    # -- shared write path --------------------------------------------------

    async def _close_task(self, task_id: str | None, result: IngestResult) -> None:
        """Settle the fill task this submission was answering.

        Lives in the service rather than the API layer so a task is closed
        identically whichever caller does the ingest — endpoint, CLI or worker.
        A submission that produced nothing is reported as a *failure*, so the
        task becomes retryable rather than silently counting as satisfied.
        """
        if not task_id:
            return
        from homz.services.ondemand import DemandFiller

        error = None
        if result.accepted == 0:
            error = "; ".join(result.errors[:2]) or "no records produced"
        try:
            await DemandFiller(self.db).complete(
                task_id, records_written=result.accepted, error=error
            )
        except Exception as exc:  # noqa: BLE001
            # The data is already written; a bookkeeping failure must not turn
            # a successful ingest into an error for the client.
            log.warning("ingest.task_close_failed", task=task_id, error=str(exc)[:200])

    async def _persist(
        self, records: list[ScrapedRecord], *, client: str, result: IngestResult
    ) -> IngestResult:
        from homz.db.repository import Repository

        repo = Repository(self.db)
        now = datetime.now(UTC)

        for record in records:
            try:
                # Provenance: mark where this came from so client-sourced data
                # can be audited or rolled back independently.
                if hasattr(record, "raw") and isinstance(record.raw, dict):
                    record.raw["ingest"] = {
                        "client": client,
                        "received_at": now.isoformat(),
                    }

                if isinstance(record, PropertyRecord):
                    record.finalize()
                    _id, is_new = await repo.upsert_property(record)
                    result.inserted += int(is_new)
                    result.updated += int(not is_new)
                    result.ids.append(_id)
                elif isinstance(record, ProjectRecord):
                    _id = await repo.upsert_project(record)
                    result.updated += 1
                    result.ids.append(_id)
                elif isinstance(record, RedditPostRecord):
                    _id = await repo.upsert_reddit_post(record)
                    result.updated += 1
                    result.ids.append(_id)
                else:
                    result.rejected += 1
                    result.errors.append(f"no writer for {type(record).__name__}")
                    continue
                result.accepted += 1
            except Exception as exc:  # noqa: BLE001
                result.rejected += 1
                result.errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
                log.warning("ingest.write_failed", error=str(exc)[:300])

        log.info("ingest.persisted", client=client, **result.as_dict() | {"ids": len(result.ids)})
        return result


def check_rate_limit(client: str) -> None:
    _rate_limiter.check(client)
