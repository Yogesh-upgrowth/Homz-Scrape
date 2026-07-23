"""Collection layout, indexes and Atlas Search definitions.

This module replaces `sql/001_schema.sql` and `sql/002_search.sql`. It is
idempotent: `ensure_schema()` can run on every deploy.

## Document design

The relational schema had 13 tables joined by foreign keys. Mongo inverts that
tradeoff — reads are cheap when the document is whole, and expensive when it
needs `$lookup`. So:

* **Embedded** into `properties`: location, contact, images, amenities,
  landmarks, unit configurations, specifications, scores. These are only ever
  read *with* the listing and are bounded in size, so a property detail page is
  one document fetch with no joins.
* **Separate collections**: `builders`, `projects`, `locations` — they are
  queried in their own right and shared across many listings, so duplicating
  them into every property would make a builder rename an N-document update.
  The listing carries a denormalized `builder_name` for display plus
  `builder_id` for the join when one is actually needed.
* **`reddit_comments` stays separate.** A viral thread can carry thousands of
  comments; embedding risks the 16 MB document ceiling and makes
  comment-level search impossible. The post embeds only `top_comments` (the
  few highest-scoring) so the common render path needs no second query.

## `_id` as the natural key

`_id = "<source>:<source_id>"` (e.g. `"magicbricks:4d4235373"`). This gives the
unique constraint for free, makes every upsert idempotent without a separate
lookup, and makes cross-references readable in the shell. Builders key on the
normalized name instead, so two portals spelling "M3M India Pvt Ltd" and "M3M"
converge on one document.

## price_history is a time series collection

Mongo 5.0+ native time series: automatic bucketing by `observed_at`, columnar
compression (typically 5-10x smaller than a regular collection at this shape),
and fast range scans per property. The tradeoff is that documents are immutable
— which suits an append-only price log exactly.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel
from pymongo.errors import OperationFailure

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# collection names
# ---------------------------------------------------------------------------

PROPERTIES = "properties"
PROJECTS = "projects"
BUILDERS = "builders"
LOCATIONS = "locations"
PRICE_HISTORY = "price_history"
REDDIT_POSTS = "reddit_posts"
REDDIT_COMMENTS = "reddit_comments"
MARKET_INSIGHTS = "market_insights"
SCRAPE_STATE = "scrape_state"
SCRAPE_RUNS = "scrape_runs"
PROPERTY_DUPLICATES = "property_duplicates"
ENRICHMENT_QUEUE = "enrichment_queue"
FILL_TASKS = "fill_tasks"

# Rollup collections — the materialized-view equivalents, rebuilt by the ETL
# with an aggregation pipeline ending in `$merge`.
MV_LOCALITY_TRENDS = "mv_locality_trends"
MV_RENTAL_YIELD = "mv_rental_yield"
MV_BUILDER_SCORECARD = "mv_builder_scorecard"
MV_SUPPLY_DEMAND = "mv_supply_demand"

ALL_COLLECTIONS = (
    PROPERTIES, PROJECTS, BUILDERS, LOCATIONS, PRICE_HISTORY,
    REDDIT_POSTS, REDDIT_COMMENTS, MARKET_INSIGHTS, SCRAPE_STATE,
    SCRAPE_RUNS, PROPERTY_DUPLICATES, ENRICHMENT_QUEUE, FILL_TASKS,
    MV_LOCALITY_TRENDS, MV_RENTAL_YIELD, MV_BUILDER_SCORECARD, MV_SUPPLY_DEMAND,
)


def natural_key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


# ---------------------------------------------------------------------------
# indexes
# ---------------------------------------------------------------------------

INDEXES: dict[str, list[IndexModel]] = {
    PROPERTIES: [
        # The hot filter path. Order matters: equality fields first, then the
        # range field, so a city+type+price query is a single index scan.
        IndexModel(
            [("city", ASCENDING), ("listing_type", ASCENDING),
             ("property_type", ASCENDING), ("price", ASCENDING)],
            name="ix_filter_primary",
            partialFilterExpression={"is_active": True, "canonical_id": None},
        ),
        IndexModel([("city", ASCENDING), ("sector", ASCENDING)], name="ix_city_sector"),
        IndexModel([("micro_market", ASCENDING)], name="ix_micro_market"),
        IndexModel([("price", ASCENDING)], name="ix_price", sparse=True),
        IndexModel([("rent_monthly", ASCENDING)], name="ix_rent", sparse=True),
        IndexModel([("price_per_sqft", ASCENDING)], name="ix_ppsf", sparse=True),
        IndexModel([("bedrooms", ASCENDING)], name="ix_bedrooms"),
        IndexModel([("possession_status", ASCENDING)], name="ix_possession"),
        IndexModel([("segment", ASCENDING)], name="ix_segment"),
        IndexModel([("builder_id", ASCENDING)], name="ix_builder", sparse=True),
        IndexModel([("project_id", ASCENDING)], name="ix_project", sparse=True),
        IndexModel([("location_id", ASCENDING)], name="ix_location", sparse=True),
        IndexModel([("dedupe_key", ASCENDING)], name="ix_dedupe_key"),
        IndexModel([("content_hash", ASCENDING)], name="ix_content_hash"),
        IndexModel([("is_active", ASCENDING), ("last_seen_at", DESCENDING)],
                   name="ix_active_seen"),
        IndexModel([("listed_at", DESCENDING)], name="ix_listed_at"),
        IndexModel([("rera_number", ASCENDING)], name="ix_rera", sparse=True),
        IndexModel([("amenities", ASCENDING)], name="ix_amenities"),   # multikey
        IndexModel([("tags", ASCENDING)], name="ix_tags"),
        IndexModel([("investment_score", DESCENDING)], name="ix_investment"),
        IndexModel([("risk_score", ASCENDING)], name="ix_risk"),
        # Enrichment worklist: nulls sort first ascending, so pending rows lead.
        IndexModel([("enriched_at", ASCENDING)], name="ix_needs_enrichment",
                   partialFilterExpression={"is_active": True}),
        # Geospatial. 2dsphere over a GeoJSON Point enables $near / $geoWithin —
        # a capability the Postgres schema did not have without PostGIS.
        IndexModel([("geo", "2dsphere")], name="ix_geo", sparse=True),
    ],
    PROJECTS: [
        IndexModel([("normalized_name", ASCENDING)], name="ix_normalized_name"),
        IndexModel([("builder_id", ASCENDING)], name="ix_builder", sparse=True),
        IndexModel([("location_id", ASCENDING)], name="ix_location", sparse=True),
        IndexModel([("status", ASCENDING)], name="ix_status"),
        IndexModel([("rera_number", ASCENDING)], name="ix_rera", sparse=True),
        IndexModel([("launch_date", DESCENDING)], name="ix_launch_date", sparse=True),
    ],
    BUILDERS: [
        IndexModel([("normalized_name", ASCENDING)], name="ix_normalized_name"),
        IndexModel([("trust_score", DESCENDING)], name="ix_trust"),
        IndexModel([("name", ASCENDING)], name="ix_name"),
    ],
    LOCATIONS: [
        IndexModel([("city", ASCENDING), ("sector", ASCENDING)], name="ix_city_sector"),
        IndexModel([("micro_market", ASCENDING)], name="ix_micro_market"),
        IndexModel([("listing_count", DESCENDING)], name="ix_listing_count"),
    ],
    REDDIT_POSTS: [
        IndexModel([("subreddit", ASCENDING), ("created_utc", DESCENDING)],
                   name="ix_subreddit_created"),
        IndexModel([("created_utc", DESCENDING)], name="ix_created"),
        IndexModel([("score", DESCENDING)], name="ix_score"),
        IndexModel([("detected_builders", ASCENDING)], name="ix_builders"),
        IndexModel([("detected_projects", ASCENDING)], name="ix_projects"),
        IndexModel([("detected_sectors", ASCENDING)], name="ix_sectors"),
        IndexModel([("topics", ASCENDING)], name="ix_topics"),
        IndexModel([("detected_city", ASCENDING)], name="ix_city"),
        IndexModel([("sentiment", ASCENDING)], name="ix_sentiment"),
        IndexModel([("enriched_at", ASCENDING)], name="ix_needs_enrichment"),
    ],
    REDDIT_COMMENTS: [
        IndexModel([("post_id", ASCENDING), ("score", DESCENDING)], name="ix_post_score"),
        IndexModel([("post_source_id", ASCENDING)], name="ix_post_source"),
        IndexModel([("detected_builders", ASCENDING)], name="ix_builders"),
        IndexModel([("sentiment", ASCENDING)], name="ix_sentiment"),
    ],
    MARKET_INSIGHTS: [
        IndexModel(
            [("metric", ASCENDING), ("city", ASCENDING), ("sector", ASCENDING),
             ("period_end", DESCENDING)],
            name="ix_metric_lookup",
        ),
    ],
    SCRAPE_RUNS: [
        IndexModel([("source", ASCENDING), ("started_at", DESCENDING)], name="ix_source_started"),
        IndexModel([("status", ASCENDING), ("started_at", DESCENDING)], name="ix_status_started"),
        # Keep 90 days of run history automatically — no cron job needed.
        IndexModel([("started_at", ASCENDING)], name="ix_ttl",
                   expireAfterSeconds=90 * 86400),
    ],
    PROPERTY_DUPLICATES: [
        IndexModel([("canonical_id", ASCENDING), ("duplicate_id", ASCENDING)],
                   name="uq_pair", unique=True),
        IndexModel([("duplicate_id", ASCENDING)], name="ix_duplicate"),
    ],
    ENRICHMENT_QUEUE: [
        IndexModel([("entity_type", ASCENDING), ("entity_id", ASCENDING)],
                   name="uq_entity", unique=True),
        IndexModel([("priority", ASCENDING), ("enqueued_at", ASCENDING)],
                   name="ix_pending", partialFilterExpression={"processed_at": None}),
    ],
    FILL_TASKS: [
        # The claim query: pending (or stale-claimed) and unexpired, oldest first.
        IndexModel([("status", ASCENDING), ("requested_at", ASCENDING)],
                   name="ix_claim"),
        IndexModel([("created_at", DESCENDING)], name="ix_created"),
        # Tasks self-expire, so an abandoned queue cannot grow without bound.
        IndexModel([("expires_at", ASCENDING)], name="ix_ttl", expireAfterSeconds=0),
    ],
    MV_LOCALITY_TRENDS: [
        IndexModel([("city", ASCENDING), ("sector", ASCENDING),
                    ("listing_type", ASCENDING), ("period", DESCENDING)],
                   name="ix_lookup"),
    ],
    MV_RENTAL_YIELD: [
        IndexModel([("city", ASCENDING), ("sector", ASCENDING), ("bedrooms", ASCENDING)],
                   name="ix_lookup"),
    ],
    MV_BUILDER_SCORECARD: [
        IndexModel([("normalized_name", ASCENDING)], name="ix_normalized_name"),
    ],
    MV_SUPPLY_DEMAND: [
        IndexModel([("city", ASCENDING), ("sector", ASCENDING)], name="ix_lookup"),
    ],
}

# Fallback full-text indexes, used when the cluster is not Atlas. A collection
# may only have ONE text index, so every searchable field goes in this one.
TEXT_INDEXES: dict[str, IndexModel] = {
    PROPERTIES: IndexModel(
        [("project_name", TEXT), ("builder_name", TEXT), ("society_name", TEXT),
         ("sector", TEXT), ("locality", TEXT), ("micro_market", TEXT),
         ("configuration", TEXT), ("title", TEXT), ("description", TEXT)],
        name="ix_text_search",
        weights={
            "project_name": 10, "builder_name": 10, "society_name": 8,
            "sector": 6, "locality": 6, "micro_market": 6,
            "configuration": 4, "title": 3, "description": 1,
        },
        default_language="english",
    ),
    REDDIT_POSTS: IndexModel(
        [("title", TEXT), ("body", TEXT)],
        name="ix_text_search",
        weights={"title": 10, "body": 2},
        default_language="english",
    ),
    BUILDERS: IndexModel(
        [("name", TEXT), ("description", TEXT)],
        name="ix_text_search",
        weights={"name": 10, "description": 1},
        default_language="english",
    ),
    PROJECTS: IndexModel(
        [("name", TEXT), ("builder_name", TEXT), ("description", TEXT)],
        name="ix_text_search",
        weights={"name": 10, "builder_name": 8, "description": 1},
        default_language="english",
    ),
}

# ---------------------------------------------------------------------------
# Atlas Search index definitions
# ---------------------------------------------------------------------------

# Field boosts mirror the tsvector weights the Postgres schema used: a project
# or builder name match outranks a description match by design, because someone
# typing "Godrej Aristocrat" wants that project, not every listing whose blurb
# mentions Godrej.
ATLAS_PROPERTY_SEARCH: dict[str, Any] = {
    "name": settings.atlas_search_index,
    "definition": {
        "mappings": {
            "dynamic": False,
            "fields": {
                "project_name": [
                    {"type": "string", "analyzer": "lucene.standard"},
                    {"type": "autocomplete", "tokenization": "edgeGram",
                     "minGrams": 2, "maxGrams": 15},
                ],
                "builder_name": [
                    {"type": "string", "analyzer": "lucene.standard"},
                    {"type": "autocomplete", "tokenization": "edgeGram",
                     "minGrams": 2, "maxGrams": 15},
                ],
                "society_name": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "sector": [
                    {"type": "string"},
                    {"type": "autocomplete", "tokenization": "edgeGram",
                     "minGrams": 2, "maxGrams": 15},
                ],
                "locality": {"type": "string"},
                "micro_market": {"type": "string"},
                "configuration": {"type": "string"},
                "amenities": {"type": "string"},
                "tags": {"type": "string"},
                "keywords": {"type": "string"},
                # Faceted + filtered fields. `token` is the filterable string
                # type; `stringFacet` additionally supports $searchMeta counts.
                "city": {"type": "stringFacet"},
                "listing_type": {"type": "token"},
                "property_type": {"type": "stringFacet"},
                "possession_status": {"type": "stringFacet"},
                "segment": {"type": "stringFacet"},
                "is_active": {"type": "boolean"},
                "is_commercial": {"type": "boolean"},
                "bedrooms": {"type": "number"},
                "price": {"type": "number"},
                "rent_monthly": {"type": "number"},
                "price_per_sqft": {"type": "number"},
                "area_sqft": {"type": "number"},
                "investment_score": {"type": "number"},
                "risk_score": {"type": "number"},
                "last_seen_at": {"type": "date"},
                "listed_at": {"type": "date"},
            },
        }
    },
}

ATLAS_REDDIT_SEARCH: dict[str, Any] = {
    "name": settings.atlas_reddit_index,
    "definition": {
        "mappings": {
            "dynamic": False,
            "fields": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "detected_builders": {"type": "string"},
                "detected_projects": {"type": "string"},
                "detected_sectors": {"type": "string"},
                "topics": {"type": "stringFacet"},
                "sentiment": {"type": "stringFacet"},
                "detected_city": {"type": "stringFacet"},
                "subreddit": {"type": "token"},
                "score": {"type": "number"},
                "created_utc": {"type": "date"},
            },
        }
    },
}

ATLAS_SEARCH_INDEXES: dict[str, list[dict[str, Any]]] = {
    PROPERTIES: [ATLAS_PROPERTY_SEARCH],
    REDDIT_POSTS: [ATLAS_REDDIT_SEARCH],
}


# ---------------------------------------------------------------------------
# schema creation
# ---------------------------------------------------------------------------


async def ensure_schema(db: AsyncIOMotorDatabase, *, backend: str = "text") -> dict[str, Any]:
    """Create collections, indexes and (on Atlas) search indexes. Idempotent."""
    report: dict[str, Any] = {
        "collections_created": [], "indexes_created": {},
        "text_indexes": [], "search_indexes": [], "warnings": [],
    }

    existing = set(await db.list_collection_names())

    # -- time series collection has to be created explicitly ---------------
    if PRICE_HISTORY not in existing:
        try:
            await db.create_collection(
                PRICE_HISTORY,
                timeseries={
                    "timeField": "observed_at",
                    "metaField": "meta",
                    # Price observations arrive at most a few times a day per
                    # listing; "hours" buckets keep them dense.
                    "granularity": "hours",
                },
            )
            report["collections_created"].append(PRICE_HISTORY)
        except OperationFailure as exc:
            # Mongo < 5.0 has no time series; fall back to a plain collection
            # so the pipeline still works.
            log.warning("schema.timeseries_unsupported", error=str(exc)[:200])
            await db.create_collection(PRICE_HISTORY)
            report["warnings"].append(
                "price_history created as a regular collection (server < 5.0)"
            )
            report["collections_created"].append(PRICE_HISTORY)

    for name in ALL_COLLECTIONS:
        if name not in existing and name != PRICE_HISTORY:
            try:
                await db.create_collection(name)
                report["collections_created"].append(name)
            except OperationFailure as exc:
                if exc.code != 48:  # NamespaceExists — harmless race
                    raise

    # -- regular indexes ----------------------------------------------------
    for name, models in INDEXES.items():
        if not models:
            continue
        try:
            created = await db[name].create_indexes(models)
            report["indexes_created"][name] = len(created)
        except OperationFailure as exc:
            log.warning("schema.index_failed", collection=name, error=str(exc)[:200])
            report["warnings"].append(f"{name}: {str(exc)[:160]}")

    # price_history indexes: time series collections index the meta subfields.
    try:
        await db[PRICE_HISTORY].create_indexes([
            IndexModel([("meta.property_id", ASCENDING), ("observed_at", DESCENDING)],
                       name="ix_property_time"),
            IndexModel([("meta.city", ASCENDING), ("observed_at", DESCENDING)],
                       name="ix_city_time"),
        ])
        report["indexes_created"][PRICE_HISTORY] = 2
    except OperationFailure as exc:
        report["warnings"].append(f"price_history indexes: {str(exc)[:160]}")

    # -- search -------------------------------------------------------------
    if backend == "atlas":
        report["search_indexes"] = await _ensure_atlas_search(db, report)
    else:
        report["text_indexes"] = await _ensure_text_indexes(db, report)

    log.info(
        "schema.ensured",
        backend=backend,
        collections=len(report["collections_created"]),
        warnings=len(report["warnings"]),
    )
    return report


async def _ensure_text_indexes(db: AsyncIOMotorDatabase, report: dict[str, Any]) -> list[str]:
    created: list[str] = []
    for name, model in TEXT_INDEXES.items():
        try:
            await db[name].create_indexes([model])
            created.append(name)
        except OperationFailure as exc:
            # 85 IndexOptionsConflict / 86 IndexKeySpecsConflict — an existing
            # text index with different fields. Surface it; do not silently drop.
            if exc.code in {85, 86}:
                report["warnings"].append(
                    f"{name}: a different text index already exists — drop "
                    f"'ix_text_search' to rebuild it ({str(exc)[:120]})"
                )
            else:
                report["warnings"].append(f"{name} text index: {str(exc)[:160]}")
    return created


async def _ensure_atlas_search(db: AsyncIOMotorDatabase, report: dict[str, Any]) -> list[str]:
    """Create Atlas Search indexes via the `createSearchIndexes` command.

    These build asynchronously — the command returns immediately and the index
    is queryable a little later. `search_index_status()` reports readiness.
    """
    created: list[str] = []
    for collection, definitions in ATLAS_SEARCH_INDEXES.items():
        for definition in definitions:
            try:
                await db.command({
                    "createSearchIndexes": collection,
                    "indexes": [definition],
                })
                created.append(f"{collection}.{definition['name']}")
                log.info("schema.atlas_search_created",
                         collection=collection, index=definition["name"])
            except OperationFailure as exc:
                message = str(exc)
                if "already exists" in message or exc.code == 68:  # IndexAlreadyExists
                    created.append(f"{collection}.{definition['name']} (existing)")
                else:
                    report["warnings"].append(
                        f"{collection}.{definition['name']}: {message[:200]}"
                    )
                    log.warning("schema.atlas_search_failed",
                                collection=collection, error=message[:200])
    return created


async def search_index_status(db: AsyncIOMotorDatabase) -> list[dict[str, Any]]:
    """Report Atlas Search index build state.

    A newly created index returns no results until `queryable` is true, which
    otherwise looks exactly like "the search is broken".
    """
    out: list[dict[str, Any]] = []
    for collection in ATLAS_SEARCH_INDEXES:
        try:
            cursor = await db[collection].aggregate([{"$listSearchIndexes": {}}])
            async for index in cursor:
                out.append({
                    "collection": collection,
                    "name": index.get("name"),
                    "status": index.get("status"),
                    "queryable": index.get("queryable", False),
                })
        except OperationFailure:
            out.append({"collection": collection, "error": "not an Atlas cluster"})
    return out


async def drop_all(db: AsyncIOMotorDatabase) -> list[str]:
    """Destructive — used by tests and `homz db reset`.

    `system.buckets.*` are the internal backing collections of time series
    collections. They are listed but must not be dropped directly: dropping
    the time series collection removes them, and dropping them first fails.
    """
    dropped = []
    for name in await db.list_collection_names():
        if name.startswith("system."):
            continue
        try:
            await db.drop_collection(name)
            dropped.append(name)
        except OperationFailure as exc:
            log.warning("schema.drop_failed", collection=name, error=str(exc)[:160])
    return dropped
