"""Persistence layer for MongoDB.

Every write is an idempotent upsert keyed on `_id` (the natural key), so
re-running a scraper updates rather than duplicates.

## The `$set` / `$setOnInsert` split

Mongo has no `ON CONFLICT DO UPDATE ... COALESCE(EXCLUDED.x, table.x)`, so the
"don't let a thin re-scrape blank a rich field" rule is expressed differently:

* `$set` — fields that reflect the *current* state of the listing and should
  always be overwritten (price, availability, scrape timestamps).
* `$setOnInsert` — fields written once at creation (`first_seen_at`).
* Descriptive fields that a partial scrape may legitimately lack are stripped
  from the update when `None`, by `_without_nulls()`. A search-card scrape that
  has no description simply doesn't mention `description`, so the value a
  detail-page scrape already stored survives.

## Price history without a trigger

Postgres captured price changes in an `AFTER UPDATE` trigger, which made
capture a database guarantee. Mongo has no triggers, so this moves into
`upsert_property()` — but not naively:

    find_one_and_update(..., upsert=True, return_document=BEFORE)

returns the document *as it was before the write*, atomically, in the same
round trip. So old price and new price are known without a separate read that
could race another writer. The subsequent `price_history` insert is a second
operation: if the process dies between them, one observation is lost — the
property document itself is still correct, and the next scrape re-detects the
delta against the stored price. That is the honest tradeoff versus a trigger.

(Atlas Change Streams could make this fully transactional, at the cost of a
separate always-on consumer process. Not worth it for a price log where a
missed sample self-heals.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, ReturnDocument, UpdateOne
from pymongo.errors import BulkWriteError, PyMongoError

from homz.common.parsing import normalize_name
from homz.common.schema import (
    BuilderRecord,
    MarketInsightRecord,
    ProjectRecord,
    PropertyRecord,
    RedditPostRecord,
)
from homz.common.schema import (
    Location as LocationSchema,
)
from homz.db import documents as D
from homz.db.codecs import as_decimal, to_bson_safe
from homz.logging_setup import get_logger

log = get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _without_nulls(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None/empty so a partial scrape cannot erase a
    field that a richer scrape already filled."""
    return {k: v for k, v in payload.items() if v is not None and v != [] and v != {}}


def _dump(model: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    return to_bson_safe(model.model_dump(exclude=exclude or set(), mode="python"))


def infer_builder_from_project(project_name: str | None) -> str | None:
    """Resolve a developer from a project name using the NCR gazetteer.

    "Godrej Aristocrat" → "Godrej Properties". Returns None when the name does
    not resolve to exactly one known developer, so an unrelated or ambiguous
    project is never misattributed.
    """
    if not project_name:
        return None
    from homz.enrichment.extractors import extract_builders

    matches = extract_builders(project_name)
    return matches[0] if len(matches) == 1 else None


def _geojson(location: LocationSchema) -> dict[str, Any] | None:
    """GeoJSON Point for the 2dsphere index — note [lng, lat] ordering."""
    if location.geo is None:
        return None
    return {"type": "Point", "coordinates": [location.geo.longitude, location.geo.latitude]}


class Repository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db
        self._location_cache: dict[str, str] = {}
        self._builder_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ locations

    async def upsert_location(self, location: LocationSchema) -> str | None:
        if location.city.value == "unknown" and not (location.locality or location.sector):
            return None

        slug = location.slug()
        if slug in self._location_cache:
            return slug

        now = utcnow()
        payload = _without_nulls({
            "locality": location.locality,
            "sector": location.sector,
            "sub_locality": location.sub_locality,
            "micro_market": location.micro_market,
            "pincode": location.pincode,
            "latitude": location.geo.latitude if location.geo else None,
            "longitude": location.geo.longitude if location.geo else None,
            "geo": _geojson(location),
        })
        await self.db[D.LOCATIONS].update_one(
            {"_id": slug},
            {
                "$set": {**payload, "updated_at": now},
                "$setOnInsert": {
                    "city": location.city.value,
                    "state": location.state,
                    "created_at": now,
                    "listing_count": 0,
                },
            },
            upsert=True,
        )
        self._location_cache[slug] = slug
        return slug

    # ------------------------------------------------------------------ builders

    async def resolve_builder(self, name: str | None, source: str) -> str | None:
        """Find-or-create a builder from just a name seen on a listing.

        `_id` is the normalized name, so "M3M India Pvt. Ltd." from MagicBricks
        and "M3M" from Housing converge on one document without a merge step.
        """
        normalized = normalize_name(name)
        if not normalized:
            return None
        if normalized in self._builder_cache:
            return normalized

        now = utcnow()
        await self.db[D.BUILDERS].update_one(
            {"_id": normalized},
            {
                "$set": {"updated_at": now},
                "$setOnInsert": {
                    "name": name.strip(),
                    "normalized_name": normalized,
                    "created_at": now,
                    "scraped_at": now,
                },
                # `sources` is deliberately NOT in $setOnInsert: two operators
                # touching the same path is a write conflict in Mongo, and
                # $addToSet already creates the array when it is missing.
                "$addToSet": {"sources": source},
            },
            upsert=True,
        )
        self._builder_cache[normalized] = normalized
        return normalized

    async def upsert_builder(self, record: BuilderRecord) -> str:
        normalized = normalize_name(record.name) or record.name.lower()
        contact = record.contact
        now = utcnow()

        always = {
            "name": record.name,
            "normalized_name": normalized,
            "reviews": to_bson_safe(record.reviews),
            "cities": record.cities,
            "raw": to_bson_safe(record.raw),
            "raw_html_key": record.raw_html_key,
            "scraped_at": record.scraped_at,
            "updated_at": now,
        }
        preserve = _without_nulls({
            "profile_url": record.profile_url,
            "description": record.description,
            "established_year": record.established_year,
            "headquarters": record.headquarters,
            "website": record.website,
            "total_projects": record.total_projects,
            "ongoing_projects": record.ongoing_projects,
            "completed_projects": record.completed_projects,
            "upcoming_projects": record.upcoming_projects,
            "rating": record.rating,
            "rating_count": record.rating_count,
            "review_count": record.review_count,
            "contact_name": contact.name if contact else None,
            "contact_phone": contact.phone if contact else None,
            "contact_email": contact.email if contact else None,
        })

        await self.db[D.BUILDERS].update_one(
            {"_id": normalized},
            {
                "$set": {**always, **preserve},
                "$setOnInsert": {"created_at": now},
                "$addToSet": {"sources": record.source.value},
            },
            upsert=True,
        )
        self._builder_cache[normalized] = normalized
        return normalized

    # ------------------------------------------------------------------ projects

    async def upsert_project(self, record: ProjectRecord) -> str:
        key = D.natural_key(record.source.value, record.source_id)
        location_id = await self.upsert_location(record.location)
        builder_name = record.builder_name or infer_builder_from_project(record.name)
        builder_id = await self.resolve_builder(builder_name, record.source.value)
        now = utcnow()

        always = {
            "source": record.source.value,
            "source_id": record.source_id,
            "project_url": record.project_url,
            "name": record.name,
            "normalized_name": normalize_name(record.name) or record.name.lower(),
            "status": record.status.value,
            "configurations": to_bson_safe([c.model_dump() for c in record.configurations]),
            "amenities": record.amenities,
            "specifications": to_bson_safe(record.specifications),
            "landmarks": to_bson_safe([lm.model_dump() for lm in record.landmarks]),
            "construction_updates": record.construction_updates,
            "images": to_bson_safe([i.model_dump() for i in record.images]),
            "location": to_bson_safe(record.location.model_dump()),
            "city": record.location.city.value,
            "sector": record.location.sector,
            "micro_market": record.location.micro_market,
            "raw": to_bson_safe(record.raw),
            "raw_html_key": record.raw_html_key,
            "scraped_at": record.scraped_at,
            "updated_at": now,
        }
        preserve = _without_nulls({
            "builder_id": builder_id,
            "builder_name": builder_name,
            "location_id": location_id,
            "launch_date": record.launch_date,
            "possession_date": record.possession_date,
            "rera_number": record.rera_number,
            "price_min": record.price_min,
            "price_max": record.price_max,
            "price_per_sqft": record.price_per_sqft,
            "total_units": record.total_units,
            "total_towers": record.total_towers,
            "project_area_acres": record.project_area_acres,
            "description": record.description,
        })

        await self.db[D.PROJECTS].update_one(
            {"_id": key},
            {"$set": {**always, **preserve}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return key

    # ------------------------------------------------------------------ properties

    async def upsert_property(self, record: PropertyRecord) -> tuple[str, bool]:
        """Upsert one listing. Returns (id, is_new).

        Captures a price-history observation as a side effect — see the module
        docstring for why this lives here rather than in a trigger.
        """
        if record.content_hash is None:
            record.finalize()

        key = D.natural_key(record.source.value, record.source_id)
        location_id = await self.upsert_location(record.location)

        # Most listings name the project but not the developer. Without this
        # inference the builders collection stays near-empty and every
        # builder-trust feature has nothing to score.
        builder_name = record.builder_name or record.developer_name
        if not builder_name:
            builder_name = infer_builder_from_project(
                record.project_name or record.society_name or record.title
            )
            if builder_name:
                record.builder_name = builder_name
        builder_id = await self.resolve_builder(builder_name, record.source.value)
        project_id = await self._resolve_project(record)

        contact = record.contact
        now = utcnow()

        # Current state of the listing — always overwritten.
        always: dict[str, Any] = {
            "source": record.source.value,
            "source_id": record.source_id,
            "listing_url": record.listing_url,
            "listing_type": record.listing_type.value,
            "property_type": record.property_type.value,
            "segment": record.segment.value,
            "is_commercial": record.is_commercial,
            "is_luxury": record.is_luxury,
            "is_affordable": record.is_affordable,
            "price": record.price,
            "price_per_sqft": record.price_per_sqft,
            "rent_monthly": record.rent_monthly,
            "is_price_on_request": record.is_price_on_request,
            "city": record.location.city.value,
            "possession_status": record.possession_status.value,
            "amenities": record.amenities,
            "specifications": to_bson_safe(record.specifications),
            "unit_configurations": to_bson_safe(
                [c.model_dump() for c in record.unit_configurations]
            ),
            "landmarks": to_bson_safe([lm.model_dump() for lm in record.landmarks]),
            "images": to_bson_safe([i.model_dump() for i in record.images]),
            "location": to_bson_safe(record.location.model_dump()),
            "contact_seller_type": contact.seller_type.value if contact else "unknown",
            "content_hash": record.content_hash,
            "dedupe_key": record.dedupe_key,
            "raw": to_bson_safe(record.raw),
            "raw_html_key": record.raw_html_key,
            "scraped_at": record.scraped_at,
            "last_seen_at": record.scraped_at,
            "is_active": True,
            "delisted_at": None,
            "updated_at": now,
        }

        # Descriptive fields — omitted entirely when absent, so a thin scrape
        # never blanks what a detail-page scrape already wrote.
        preserve = _without_nulls({
            "title": record.title,
            "description": record.description,
            "project_id": project_id,
            "project_name": record.project_name,
            "builder_id": builder_id,
            "builder_name": record.builder_name,
            "developer_name": record.developer_name,
            "society_name": record.society_name,
            "property_type_raw": record.property_type_raw,
            "configuration": record.configuration,
            "bedrooms": record.bedrooms,
            "bathrooms": record.bathrooms,
            "balconies": record.balconies,
            "floor_number": record.floor_number,
            "total_floors": record.total_floors,
            "facing": record.facing,
            "furnishing": record.furnishing,
            "age_years": record.age_years,
            "price_max": record.price_max,
            "price_display": record.price_display,
            "booking_amount": record.booking_amount,
            "maintenance_charge": record.maintenance_charge,
            "security_deposit": record.security_deposit,
            "area_value": record.area_value,
            "area_unit": record.area_unit.value if record.area_unit else None,
            "area_sqft": record.area_sqft,
            "carpet_area_sqft": record.carpet_area_sqft,
            "built_up_area_sqft": record.built_up_area_sqft,
            "super_built_up_area_sqft": record.super_built_up_area_sqft,
            "plot_area_sqft": record.plot_area_sqft,
            "location_id": location_id,
            "location_raw": record.location.raw,
            "sector": record.location.sector,
            "locality": record.location.locality,
            "micro_market": record.location.micro_market,
            "latitude": record.location.geo.latitude if record.location.geo else None,
            "longitude": record.location.geo.longitude if record.location.geo else None,
            "geo": _geojson(record.location),
            "possession_date": record.possession_date,
            "possession_raw": record.possession_raw,
            "rera_number": record.rera_number,
            "rera_status": record.rera_status,
            "total_units": record.total_units,
            "project_area_acres": record.project_area_acres,
            "launch_date": record.launch_date,
            "contact_name": contact.name if contact else None,
            "contact_company": contact.company if contact else None,
            "contact_phone": contact.phone if contact else None,
            "contact_email": contact.email if contact else None,
            "listed_at": record.listed_at,
            "listing_date_raw": record.listing_date_raw,
            "updated_at_source": record.updated_at_source,
        })

        # Atomic: returns the pre-write document, so old and new price are known
        # from one round trip with no read-modify-write race.
        before = await self.db[D.PROPERTIES].find_one_and_update(
            {"_id": key},
            {
                "$set": {**always, **preserve},
                "$setOnInsert": {
                    "_id": key,
                    "first_seen_at": record.scraped_at,
                    "created_at": now,
                    "canonical_id": None,
                    "duplicate_count": 0,
                    "tags": [],
                    "keywords": [],
                    "enriched_at": None,
                    "enrichment_version": 0,
                },
            },
            upsert=True,
            return_document=ReturnDocument.BEFORE,
            projection={"price": 1, "rent_monthly": 1, "price_per_sqft": 1},
        )

        is_new = before is None
        await self._record_price_observation(key, record, before)
        return key, is_new

    async def _record_price_observation(
        self, property_id: str, record: PropertyRecord, before: dict[str, Any] | None
    ) -> None:
        """Append to the price-history time series when the price moved.

        Mirrors the two Postgres triggers: seed an observation on insert, and
        record a delta whenever price or rent changes.
        """
        new_price = record.price or record.rent_monthly
        if new_price is None:
            return

        old_price = None
        if before is not None:
            old_price = as_decimal(before.get("price")) or as_decimal(before.get("rent_monthly"))
            if old_price == new_price:
                return  # unchanged — nothing to record

        change_amount = None
        change_pct = None
        if old_price is not None and old_price != 0:
            change_amount = new_price - old_price
            change_pct = float(
                (change_amount / old_price * Decimal(100)).quantize(Decimal("0.001"))
            )

        try:
            await self.db[D.PRICE_HISTORY].insert_one({
                # metaField: fields that identify the series rather than the
                # measurement. Time series collections index and bucket on this.
                "meta": {
                    "property_id": property_id,
                    "source": record.source.value,
                    "city": record.location.city.value,
                    "sector": record.location.sector,
                    "listing_type": record.listing_type.value,
                },
                "observed_at": utcnow(),
                "price": record.price,
                "price_per_sqft": record.price_per_sqft,
                "rent_monthly": record.rent_monthly,
                "previous_price": old_price,
                "change_amount": change_amount,
                "change_pct": change_pct,
            })
        except PyMongoError as exc:
            # A lost observation must not fail the ingest — the property row is
            # already correct and the next scrape re-detects the delta.
            log.warning("repo.price_history_failed", property_id=property_id,
                        error=str(exc)[:200])

    async def _resolve_project(self, record: PropertyRecord) -> str | None:
        normalized = normalize_name(record.project_name)
        if not normalized:
            return None
        query: dict[str, Any] = {"normalized_name": normalized}
        if record.location.city.value != "unknown":
            query["$or"] = [
                {"city": record.location.city.value},
                {"city": {"$exists": False}},
            ]
        found = await self.db[D.PROJECTS].find_one(query, projection={"_id": 1})
        return found["_id"] if found else None

    # ------------------------------------------------------------------ reddit

    async def upsert_reddit_post(self, record: RedditPostRecord) -> str:
        now = utcnow()
        always = {
            "source_id": record.source_id,
            "subreddit": record.subreddit,
            "url": record.url,
            "permalink": record.permalink,
            "title": record.title,
            "author": record.author,
            "created_utc": record.created_utc,
            # Score and comment count move constantly — always take the newest.
            "score": record.score,
            "upvote_ratio": record.upvote_ratio,
            "num_comments": record.num_comments,
            "is_self": record.is_self,
            "over_18": record.over_18,
            "detected_builders": record.detected_builders,
            "detected_projects": record.detected_projects,
            "detected_sectors": record.detected_sectors,
            "detected_city": (
                record.detected_city.value if record.detected_city else "unknown"
            ),
            "topics": record.topics,
            "keywords": record.keywords,
            "relevance_score": record.relevance_score,
            "raw": to_bson_safe(record.raw),
            "scraped_at": record.scraped_at,
            "updated_at": now,
            # A denormalized slice so rendering a post needs no second query.
            "top_comments": to_bson_safe([
                {"comment_id": c.comment_id, "author": c.author,
                 "body": (c.body or "")[:1000], "score": c.score}
                for c in sorted(record.comments, key=lambda c: c.score, reverse=True)[:5]
            ]),
        }
        # Enrichment output is preserved unless the new value is set, so a
        # plain re-scrape does not wipe an LLM pass.
        preserve = _without_nulls({
            "body": record.body,
            "flair": record.flair,
            "sentiment": record.sentiment.value if record.sentiment else None,
            "sentiment_score": record.sentiment_score,
            "summary": record.summary,
        })

        await self.db[D.REDDIT_POSTS].update_one(
            {"_id": record.source_id},
            {"$set": {**always, **preserve}, "$setOnInsert": {"created_at": now,
                                                              "enriched_at": None}},
            upsert=True,
        )

        if record.comments:
            await self._upsert_reddit_comments(record)
        return record.source_id

    async def _upsert_reddit_comments(self, record: RedditPostRecord) -> int:
        operations = []
        for comment in record.comments:
            payload = {
                "comment_id": comment.comment_id,
                "post_id": record.source_id,
                "post_source_id": record.source_id,
                "parent_id": comment.parent_id,
                "author": comment.author,
                "score": comment.score,
                "depth": comment.depth,
                "is_submitter": comment.is_submitter,
                "created_utc": comment.created_utc,
                "permalink": comment.permalink,
                "detected_builders": comment.detected_builders,
                "detected_projects": comment.detected_projects,
                "detected_sectors": comment.detected_sectors,
                "topics": comment.topics,
                "keywords": comment.keywords,
            }
            preserve = _without_nulls({
                "body": comment.body,
                "sentiment": comment.sentiment.value if comment.sentiment else None,
                "sentiment_score": comment.sentiment_score,
            })
            operations.append(
                UpdateOne(
                    {"_id": comment.comment_id},
                    {"$set": {**payload, **preserve},
                     "$setOnInsert": {"created_at": utcnow()}},
                    upsert=True,
                )
            )
        if not operations:
            return 0
        try:
            result = await self.db[D.REDDIT_COMMENTS].bulk_write(operations, ordered=False)
            return (result.upserted_count or 0) + (result.modified_count or 0)
        except BulkWriteError as exc:
            log.warning("repo.comment_bulk_partial", errors=len(exc.details.get("writeErrors", [])))
            return 0

    # ------------------------------------------------------------------ insights

    async def upsert_market_insight(self, record: MarketInsightRecord) -> str:
        key = D.natural_key(record.source.value, record.source_id)
        await self.db[D.MARKET_INSIGHTS].update_one(
            {"_id": key},
            {
                "$set": {
                    "metric": record.metric,
                    "city": record.city.value,
                    "locality": record.locality,
                    "sector": record.sector,
                    "property_type": (
                        record.property_type.value if record.property_type else None
                    ),
                    "period_start": record.period_start,
                    "period_end": record.period_end,
                    "value": record.value,
                    "unit": record.unit,
                    "change_pct": record.change_pct,
                    "sample_size": record.sample_size,
                    "source_url": record.source_url,
                    "notes": record.notes,
                    "scraped_at": record.scraped_at,
                },
                "$setOnInsert": {"source": record.source.value, "created_at": utcnow()},
            },
            upsert=True,
        )
        return key

    # ------------------------------------------------------------------ ops

    async def record_run(
        self, report: dict[str, Any], *, inserted: int = 0, updated: int = 0
    ) -> None:
        await self.db[D.SCRAPE_RUNS].insert_one({
            "source": report["source"],
            "job": report["job"],
            "status": report["status"],
            "started_at": _as_datetime(report["started_at"]),
            "finished_at": _as_datetime(report["finished_at"]),
            "duration_s": report["duration_s"],
            "discovered": report["discovered"],
            "fetched": report["fetched"],
            "parsed": report["parsed"],
            "inserted": inserted,
            "updated": updated,
            "skipped_known": report["skipped_known"],
            "skipped_robots": report["skipped_robots"],
            "errors": report["errors"],
            "blocked": report["blocked"],
            "details": {
                "error_samples": report.get("error_samples", []),
                "fetcher_stats": report.get("fetcher_stats", {}),
            },
        })

    async def mark_stale_inactive(self, source: str, *, older_than_days: int = 21) -> int:
        """A listing we have not seen in N days is treated as delisted."""
        from datetime import timedelta

        cutoff = utcnow() - timedelta(days=older_than_days)
        result = await self.db[D.PROPERTIES].update_many(
            {"source": source, "is_active": True, "last_seen_at": {"$lt": cutoff}},
            {"$set": {"is_active": False, "delisted_at": utcnow()}},
        )
        count = result.modified_count
        if count:
            log.info("repo.marked_delisted", source=source, count=count, days=older_than_days)
        return count

    async def link_duplicate(
        self, canonical_id: str, duplicate_id: str, score: float, reason: str
    ) -> None:
        if canonical_id == duplicate_id:
            return
        await self.db[D.PROPERTY_DUPLICATES].update_one(
            {"canonical_id": canonical_id, "duplicate_id": duplicate_id},
            {"$set": {"score": score, "reason": reason[:500], "detected_at": utcnow()}},
            upsert=True,
        )
        await self.db[D.PROPERTIES].update_one(
            {"_id": duplicate_id}, {"$set": {"canonical_id": canonical_id}}
        )
        await self.db[D.PROPERTIES].update_one(
            {"_id": canonical_id}, {"$inc": {"duplicate_count": 1}}
        )

    async def enqueue_enrichment(
        self, entity_type: str, entity_ids: list[str], *, priority: int = 5
    ) -> int:
        if not entity_ids:
            return 0
        operations = [
            UpdateOne(
                {"entity_type": entity_type, "entity_id": eid},
                {"$set": {"priority": priority, "processed_at": None},
                 "$setOnInsert": {"enqueued_at": utcnow(), "attempts": 0}},
                upsert=True,
            )
            for eid in entity_ids
        ]
        await self.db[D.ENRICHMENT_QUEUE].bulk_write(operations, ordered=False)
        return len(operations)

    async def counts(self) -> dict[str, int]:
        """Row counts. `count_documents({})` is exact but scans; for the
        unfiltered case `estimated_document_count()` reads metadata instead,
        which matters once collections reach millions of rows."""
        out: dict[str, int] = {}
        for label, collection in (
            ("properties", D.PROPERTIES),
            ("projects", D.PROJECTS),
            ("builders", D.BUILDERS),
            ("reddit_posts", D.REDDIT_POSTS),
            ("reddit_comments", D.REDDIT_COMMENTS),
            ("price_history", D.PRICE_HISTORY),
            ("locations", D.LOCATIONS),
            ("market_insights", D.MARKET_INSIGHTS),
        ):
            try:
                out[label] = await self.db[collection].estimated_document_count()
            except PyMongoError:
                out[label] = 0
        out["properties_active"] = await self.db[D.PROPERTIES].count_documents(
            {"is_active": True}
        )
        return out

    # ------------------------------------------------------------------ rollups

    async def refresh_market_views(self, **_: Any) -> None:
        """Rebuild the four rollup collections.

        `$merge` is the materialized-view equivalent: the pipeline's output
        replaces matching documents in the target collection. Unlike a Postgres
        `REFRESH MATERIALIZED VIEW`, readers never see an empty window — merge
        is document-by-document, so the collection stays queryable throughout.
        """
        await self._refresh_locality_trends()
        await self._refresh_rental_yield()
        await self._refresh_builder_scorecard()
        await self._refresh_supply_demand()
        log.info("repo.market_views_refreshed")

    async def _refresh_locality_trends(self) -> None:
        pipeline: list[dict[str, Any]] = [
            {"$match": {"is_active": True, "canonical_id": None}},
            {"$addFields": {
                "period": {"$dateTrunc": {
                    "date": {"$ifNull": ["$listed_at", "$first_seen_at"]},
                    "unit": "month",
                }},
                # $percentile needs a double; medians are statistics, not
                # ledger values, so precision loss here is immaterial.
                "ppsf_num": {"$toDouble": {"$ifNull": ["$price_per_sqft", 0]}},
                "price_num": {"$toDouble": {"$ifNull": ["$price", 0]}},
            }},
            {"$group": {
                "_id": {
                    "city": "$city", "sector": "$sector", "micro_market": "$micro_market",
                    "property_type": "$property_type", "listing_type": "$listing_type",
                    "period": "$period",
                },
                "listing_count": {"$sum": 1},
                "ppsf_values": {"$push": {"$cond": [{"$gt": ["$ppsf_num", 0]},
                                                    "$ppsf_num", "$$REMOVE"]}},
                "price_values": {"$push": {"$cond": [{"$gt": ["$price_num", 0]},
                                                     "$price_num", "$$REMOVE"]}},
                "avg_price_per_sqft": {"$avg": {"$cond": [{"$gt": ["$ppsf_num", 0]},
                                                          "$ppsf_num", None]}},
                "min_price": {"$min": {"$cond": [{"$gt": ["$price_num", 0]},
                                                 "$price_num", None]}},
                "max_price": {"$max": "$price_num"},
                "avg_rent": {"$avg": "$rent_monthly"},
                "avg_area_sqft": {"$avg": "$area_sqft"},
            }},
            {"$addFields": {
                "median_price_per_sqft": _median_expr("$ppsf_values"),
                "median_price": _median_expr("$price_values"),
            }},
            {"$project": {
                "_id": 0,
                "city": "$_id.city", "sector": "$_id.sector",
                "micro_market": "$_id.micro_market",
                "property_type": "$_id.property_type",
                "listing_type": "$_id.listing_type", "period": "$_id.period",
                "listing_count": 1, "median_price_per_sqft": 1, "avg_price_per_sqft": 1,
                "median_price": 1, "min_price": 1, "max_price": 1,
                "avg_rent": 1, "avg_area_sqft": 1,
                "refreshed_at": {"$literal": utcnow()},
            }},
            {"$merge": {
                "into": D.MV_LOCALITY_TRENDS,
                "on": "_id",
                "whenMatched": "replace",
                "whenNotMatched": "insert",
            }},
        ]
        # `on: _id` needs a deterministic key, so build one from the group keys.
        pipeline[-2]["$project"]["_id"] = {
            "$concat": [
                {"$ifNull": ["$_id.city", ""]}, "|", {"$ifNull": ["$_id.sector", ""]}, "|",
                {"$ifNull": ["$_id.micro_market", ""]}, "|",
                {"$ifNull": ["$_id.property_type", ""]}, "|",
                {"$ifNull": ["$_id.listing_type", ""]}, "|",
                {"$dateToString": {"date": "$_id.period", "format": "%Y-%m",
                                   "onNull": "none"}},
            ]
        }
        await self._run_merge(D.PROPERTIES, pipeline, D.MV_LOCALITY_TRENDS)

    async def _refresh_rental_yield(self) -> None:
        """Median annual rent ÷ median sale price, per (city, sector, bedrooms).

        Only emitted where both sides have ≥3 samples — a yield computed from
        one rental and one sale is noise presented as a number.
        """
        pipeline: list[dict[str, Any]] = [
            {"$match": {"is_active": True, "bedrooms": {"$ne": None}}},
            {"$group": {
                "_id": {"city": "$city", "sector": "$sector", "bedrooms": "$bedrooms"},
                "sale_prices": {"$push": {"$cond": [
                    {"$and": [
                        {"$in": ["$listing_type", ["sale", "resale", "new_launch"]]},
                        {"$gt": [{"$toDouble": {"$ifNull": ["$price", 0]}}, 0]},
                    ]},
                    {"$toDouble": "$price"}, "$$REMOVE",
                ]}},
                "rents": {"$push": {"$cond": [
                    {"$and": [
                        {"$eq": ["$listing_type", "rent"]},
                        {"$gt": [{"$toDouble": {"$ifNull": ["$rent_monthly", 0]}}, 0]},
                    ]},
                    {"$toDouble": "$rent_monthly"}, "$$REMOVE",
                ]}},
            }},
            {"$addFields": {
                "sale_sample": {"$size": "$sale_prices"},
                "rent_sample": {"$size": "$rents"},
            }},
            {"$match": {"sale_sample": {"$gte": 3}, "rent_sample": {"$gte": 3}}},
            {"$addFields": {
                "median_price": _median_expr("$sale_prices"),
                "median_rent": _median_expr("$rents"),
            }},
            {"$project": {
                "_id": {"$concat": [
                    {"$ifNull": ["$_id.city", ""]}, "|", {"$ifNull": ["$_id.sector", ""]},
                    "|", {"$toString": "$_id.bedrooms"},
                ]},
                "city": "$_id.city", "sector": "$_id.sector", "bedrooms": "$_id.bedrooms",
                "median_price": 1, "median_rent": 1,
                "sale_sample": 1, "rent_sample": 1,
                "rental_yield_pct": {"$round": [
                    {"$multiply": [
                        {"$divide": [{"$multiply": ["$median_rent", 12]},
                                     {"$cond": [{"$gt": ["$median_price", 0]},
                                                "$median_price", 1]}]},
                        100,
                    ]}, 3,
                ]},
                "refreshed_at": {"$literal": utcnow()},
            }},
            {"$merge": {"into": D.MV_RENTAL_YIELD, "on": "_id",
                        "whenMatched": "replace", "whenNotMatched": "insert"}},
        ]
        await self._run_merge(D.PROPERTIES, pipeline, D.MV_RENTAL_YIELD)

    async def _refresh_builder_scorecard(self) -> None:
        pipeline: list[dict[str, Any]] = [
            {"$lookup": {
                "from": D.PROJECTS, "localField": "_id",
                "foreignField": "builder_id", "as": "projects",
            }},
            {"$lookup": {
                "from": D.PROPERTIES, "localField": "_id",
                "foreignField": "builder_id",
                "pipeline": [{"$match": {"is_active": True}},
                             {"$project": {"price_per_sqft": 1}}],
                "as": "listings",
            }},
            {"$lookup": {
                "from": D.REDDIT_POSTS, "localField": "name",
                "foreignField": "detected_builders",
                "pipeline": [{"$project": {"sentiment": 1, "sentiment_score": 1, "topics": 1}}],
                "as": "chatter",
            }},
            {"$project": {
                "_id": 1,
                "builder_id": "$_id",
                "name": 1,
                "normalized_name": 1,
                "project_count": {"$size": "$projects"},
                "completed_count": {"$size": {"$filter": {
                    "input": "$projects", "as": "p",
                    "cond": {"$eq": ["$$p.status", "completed"]}}}},
                "ongoing_count": {"$size": {"$filter": {
                    "input": "$projects", "as": "p",
                    "cond": {"$eq": ["$$p.status", "under_construction"]}}}},
                "listing_count": {"$size": "$listings"},
                "avg_price_per_sqft": {"$avg": {
                    "$map": {"input": "$listings", "as": "l",
                             "in": {"$toDouble": {"$ifNull": ["$$l.price_per_sqft", 0]}}}}},
                "reddit_mentions": {"$size": "$chatter"},
                "reddit_positive": {"$size": {"$filter": {
                    "input": "$chatter", "as": "c",
                    "cond": {"$eq": ["$$c.sentiment", "positive"]}}}},
                "reddit_negative": {"$size": {"$filter": {
                    "input": "$chatter", "as": "c",
                    "cond": {"$eq": ["$$c.sentiment", "negative"]}}}},
                "reddit_avg_sentiment": {"$avg": "$chatter.sentiment_score"},
                "delay_mentions": {"$size": {"$filter": {
                    "input": "$chatter", "as": "c",
                    "cond": {"$or": [
                        {"$in": ["construction_delay", {"$ifNull": ["$$c.topics", []]}]},
                        {"$in": ["possession_issue", {"$ifNull": ["$$c.topics", []]}]},
                    ]}}}},
                "fraud_mentions": {"$size": {"$filter": {
                    "input": "$chatter", "as": "c",
                    "cond": {"$in": ["builder_fraud", {"$ifNull": ["$$c.topics", []]}]}}}},
                "refreshed_at": {"$literal": utcnow()},
            }},
            {"$merge": {"into": D.MV_BUILDER_SCORECARD, "on": "_id",
                        "whenMatched": "replace", "whenNotMatched": "insert"}},
        ]
        await self._run_merge(D.BUILDERS, pipeline, D.MV_BUILDER_SCORECARD)

    async def _refresh_supply_demand(self) -> None:
        from datetime import timedelta

        now = utcnow()
        d30, d90 = now - timedelta(days=30), now - timedelta(days=90)
        pipeline: list[dict[str, Any]] = [
            {"$match": {"canonical_id": None}},
            {"$group": {
                "_id": {"city": "$city", "sector": "$sector"},
                "new_last_30d": {"$sum": {"$cond": [{"$gt": ["$first_seen_at", d30]}, 1, 0]}},
                "new_last_90d": {"$sum": {"$cond": [{"$gt": ["$first_seen_at", d90]}, 1, 0]}},
                "delisted_last_90d": {"$sum": {"$cond": [
                    {"$and": [{"$eq": ["$is_active", False]},
                              {"$gt": [{"$ifNull": ["$delisted_at", d90]}, d90]}]}, 1, 0]}},
                "active_supply": {"$sum": {"$cond": ["$is_active", 1, 0]}},
                "new_launches": {"$sum": {"$cond": [
                    {"$and": ["$is_active",
                              {"$eq": ["$possession_status", "new_launch"]}]}, 1, 0]}},
                "days_on_market": {"$push": {"$cond": [
                    {"$eq": ["$is_active", False]},
                    {"$divide": [
                        {"$subtract": [{"$ifNull": ["$delisted_at", now]}, "$first_seen_at"]},
                        86_400_000,
                    ]},
                    "$$REMOVE",
                ]}},
            }},
            {"$project": {
                "_id": {"$concat": [{"$ifNull": ["$_id.city", ""]}, "|",
                                    {"$ifNull": ["$_id.sector", ""]}]},
                "city": "$_id.city", "sector": "$_id.sector",
                "new_last_30d": 1, "new_last_90d": 1, "delisted_last_90d": 1,
                "active_supply": 1, "new_launches": 1,
                "avg_days_on_market": {"$avg": "$days_on_market"},
                "refreshed_at": {"$literal": now},
            }},
            {"$merge": {"into": D.MV_SUPPLY_DEMAND, "on": "_id",
                        "whenMatched": "replace", "whenNotMatched": "insert"}},
        ]
        await self._run_merge(D.PROPERTIES, pipeline, D.MV_SUPPLY_DEMAND)

    async def _run_merge(self, source: str, pipeline: list[dict[str, Any]], target: str) -> None:
        try:
            # allowDiskUse: the $push-then-median pattern can exceed the 100 MB
            # in-memory group limit once a sector has thousands of listings.
            await self.db[source].aggregate(pipeline, allowDiskUse=True).to_list(length=1)
        except PyMongoError as exc:
            log.error("repo.rollup_failed", target=target, error=str(exc)[:300])
            raise


def _median_expr(array_field: str) -> dict[str, Any]:
    """Exact median of a numeric array, without requiring `$percentile`.

    `$percentile` needs MongoDB 7.0; sorting the array works from 5.2 and is
    exact rather than approximate, which matters because these medians feed the
    risk and investment scores.
    """
    sorted_array = {"$sortArray": {"input": array_field, "sortBy": 1}}
    size = {"$size": array_field}
    return {
        "$cond": [
            {"$eq": [size, 0]},
            None,
            {"$cond": [
                {"$eq": [{"$mod": [size, 2]}, 1]},
                # odd count → middle element
                {"$arrayElemAt": [sorted_array, {"$floor": {"$divide": [size, 2]}}]},
                # even count → mean of the two middle elements
                {"$avg": [
                    {"$arrayElemAt": [sorted_array, {"$subtract": [{"$divide": [size, 2]}, 1]}]},
                    {"$arrayElemAt": [sorted_array, {"$divide": [size, 2]}]},
                ]},
            ]},
        ]
    }


def _as_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = ["Repository", "infer_builder_from_project", "utcnow"]

# Re-exported for callers that used to sort with SQLAlchemy constants.
SORT_ASC, SORT_DESC = ASCENDING, DESCENDING
