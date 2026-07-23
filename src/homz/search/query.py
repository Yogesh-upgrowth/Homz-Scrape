"""Search over MongoDB.

Two backends behind one interface:

* **Atlas Search** (`$search`) — per-field boosts, fuzzy matching for typos,
  `$searchMeta` facet counts. This is the intended production path and is what
  the field boosts in `db/documents.py` are designed for.
* **`$text` fallback** — used when the cluster is self-hosted. Weighted, but no
  fuzzy matching, so "godrej aristocat" finds nothing. Autocomplete degrades to
  a prefix regex.

The backend is chosen once per process by probing the server
(`db.mongo.detect_backend`), so the same code runs against a local Docker Mongo
in development and Atlas in production.

All user input goes into the pipeline as *values*, never interpolated into
operator keys, so there is no injection surface. Regex input is escaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from homz.db import documents as D
from homz.db.codecs import jsonable
from homz.db.mongo import detect_backend
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

# Mongo sort specs. Atlas `$search` returns results in relevance order, so the
# "relevance" mode deliberately applies no `$sort` at all.
SORT_OPTIONS: dict[str, list[tuple[str, int]] | None] = {
    "relevance": None,
    "newest": [("listed_at", -1), ("first_seen_at", -1)],
    "price_asc": [("price", 1)],
    "price_desc": [("price", -1)],
    "ppsf_asc": [("price_per_sqft", 1)],
    "area_desc": [("area_sqft", -1)],
    "investment": [("investment_score", -1)],
    "lowest_risk": [("risk_score", 1)],
}

#: Fields returned by a search hit. Excluding `raw`, `description` and
#: `specifications` keeps the response small — a 200-result page would
#: otherwise carry megabytes of scraped prose nobody renders.
RESULT_PROJECTION: dict[str, Any] = {
    "source": 1, "source_id": 1, "listing_url": 1, "title": 1,
    "project_name": 1, "builder_name": 1, "society_name": 1,
    "listing_type": 1, "property_type": 1, "segment": 1, "configuration": 1,
    "bedrooms": 1, "bathrooms": 1, "area_sqft": 1, "carpet_area_sqft": 1,
    "price": 1, "price_max": 1, "price_display": 1, "price_per_sqft": 1,
    "rent_monthly": 1, "is_price_on_request": 1,
    "city": 1, "sector": 1, "locality": 1, "micro_market": 1,
    "latitude": 1, "longitude": 1,
    "possession_status": 1, "possession_date": 1, "rera_number": 1,
    "amenities": 1, "tags": 1, "keywords": 1,
    "investment_score": 1, "risk_score": 1, "location_score": 1,
    "builder_trust_score": 1, "ai_summary": 1,
    "listed_at": 1, "first_seen_at": 1, "last_seen_at": 1, "duplicate_count": 1,
    "primary_image": {
        "$let": {
            "vars": {"first": {"$arrayElemAt": ["$images", 0]}},
            "in": "$$first.url",
        }
    },
}


@dataclass
class PropertySearchQuery:
    q: str | None = None
    city: str | None = None
    sector: str | None = None
    locality: str | None = None
    micro_market: str | None = None
    builder: str | None = None
    project: str | None = None
    listing_type: str | None = None
    property_type: list[str] = field(default_factory=list)
    configuration: str | None = None
    bedrooms_min: int | None = None
    bedrooms_max: int | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    area_min: float | None = None
    area_max: float | None = None
    possession_status: list[str] = field(default_factory=list)
    segment: list[str] = field(default_factory=list)
    amenities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    is_commercial: bool | None = None
    has_rera: bool | None = None
    min_investment_score: float | None = None
    max_risk_score: float | None = None
    #: Geo radius search — a capability the Postgres schema lacked without PostGIS.
    near_lat: float | None = None
    near_lng: float | None = None
    radius_km: float | None = None
    sort: str = "relevance"
    page: int = 1
    page_size: int = 25
    include_duplicates: bool = False

    @property
    def limit(self) -> int:
        return max(1, min(self.page_size, settings.api_max_page_size))

    @property
    def skip(self) -> int:
        return max(0, (self.page - 1) * self.limit)


def _escape_regex(value: str) -> str:
    return re.escape(value)


def build_filter(query: PropertySearchQuery) -> dict[str, Any]:
    """The non-text part of the query — shared by both backends."""
    conditions: dict[str, Any] = {"is_active": True}
    if not query.include_duplicates:
        conditions["canonical_id"] = None

    if query.city:
        conditions["city"] = query.city
    if query.sector:
        conditions["sector"] = {"$regex": _escape_regex(query.sector), "$options": "i"}
    if query.locality:
        conditions["locality"] = {"$regex": _escape_regex(query.locality), "$options": "i"}
    if query.micro_market:
        conditions["micro_market"] = {
            "$regex": _escape_regex(query.micro_market), "$options": "i"
        }
    if query.builder:
        conditions["builder_name"] = {"$regex": _escape_regex(query.builder), "$options": "i"}
    if query.project:
        conditions["project_name"] = {"$regex": _escape_regex(query.project), "$options": "i"}
    if query.listing_type:
        conditions["listing_type"] = query.listing_type
    if query.property_type:
        conditions["property_type"] = {"$in": query.property_type}
    if query.configuration:
        conditions["configuration"] = {
            "$regex": _escape_regex(query.configuration), "$options": "i"
        }
    if query.possession_status:
        conditions["possession_status"] = {"$in": query.possession_status}
    if query.segment:
        conditions["segment"] = {"$in": query.segment}
    if query.amenities:
        conditions["amenities"] = {"$all": query.amenities}
    if query.keywords:
        conditions["$or"] = [
            {"keywords": {"$in": query.keywords}},
            {"tags": {"$in": query.keywords}},
        ]
    if query.is_commercial is not None:
        conditions["is_commercial"] = query.is_commercial
    if query.has_rera is not None:
        conditions["rera_number"] = {"$ne": None} if query.has_rera else None

    _range(conditions, "bedrooms", query.bedrooms_min, query.bedrooms_max)
    _range(conditions, "area_sqft", query.area_min, query.area_max)
    if query.min_investment_score is not None:
        conditions["investment_score"] = {"$gte": query.min_investment_score}
    if query.max_risk_score is not None:
        conditions["risk_score"] = {"$lte": query.max_risk_score}

    # Budget applies to sale price or monthly rent, whichever the listing
    # carries, so one control works for both modes.
    if query.price_min is not None or query.price_max is not None:
        bounds: dict[str, Any] = {}
        if query.price_min is not None:
            bounds["$gte"] = query.price_min
        if query.price_max is not None:
            bounds["$lte"] = query.price_max
        conditions.setdefault("$and", []).append(
            {"$or": [{"price": bounds}, {"rent_monthly": bounds}]}
        )

    if query.near_lat is not None and query.near_lng is not None and query.radius_km:
        conditions["geo"] = {
            "$geoWithin": {
                "$centerSphere": [
                    [query.near_lng, query.near_lat],
                    query.radius_km / 6378.1,  # radians: km ÷ Earth radius
                ]
            }
        }

    return conditions


def _range(target: dict[str, Any], key: str, low: Any, high: Any) -> None:
    if low is None and high is None:
        return
    bounds: dict[str, Any] = {}
    if low is not None:
        bounds["$gte"] = low
    if high is not None:
        bounds["$lte"] = high
    target[key] = bounds


# ---------------------------------------------------------------------------
# Atlas Search backend
# ---------------------------------------------------------------------------


def _atlas_search_stage(query: PropertySearchQuery) -> dict[str, Any]:
    """Compound `$search` mirroring the tsvector weights of the SQL version.

    `fuzzy` on the name fields is what makes "godrej aristocat" still find
    Godrej Aristocrat — the single largest UX gain over the Postgres
    implementation, which needed a separate trigram index for the same effect.
    """
    text = query.q or ""
    return {
        "$search": {
            "index": settings.atlas_search_index,
            "compound": {
                "should": [
                    {"text": {"query": text, "path": "project_name",
                              "score": {"boost": {"value": 10}},
                              "fuzzy": {"maxEdits": 1, "prefixLength": 2}}},
                    {"text": {"query": text, "path": "builder_name",
                              "score": {"boost": {"value": 10}},
                              "fuzzy": {"maxEdits": 1, "prefixLength": 2}}},
                    {"text": {"query": text, "path": "society_name",
                              "score": {"boost": {"value": 8}}}},
                    {"text": {"query": text, "path": "sector",
                              "score": {"boost": {"value": 6}}}},
                    {"text": {"query": text, "path": "locality",
                              "score": {"boost": {"value": 6}}}},
                    {"text": {"query": text, "path": "micro_market",
                              "score": {"boost": {"value": 6}}}},
                    {"text": {"query": text, "path": "configuration",
                              "score": {"boost": {"value": 4}}}},
                    {"text": {"query": text, "path": "title",
                              "score": {"boost": {"value": 3}}}},
                    {"text": {"query": text, "path": ["amenities", "tags"],
                              "score": {"boost": {"value": 2}}}},
                    {"text": {"query": text, "path": "description"}},
                ],
                "minimumShouldMatch": 1,
            },
            # Freshness decay: full credit today, roughly half at 60 days —
            # the same intent as the SQL ranking's recency term.
            "scoreDetails": False,
        }
    }


async def _atlas_pipeline(query: PropertySearchQuery) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = []
    if query.q:
        pipeline.append(_atlas_search_stage(query))
        pipeline.append({"$addFields": {"score": {"$meta": "searchScore"}}})

    filters = build_filter(query)
    if filters:
        pipeline.append({"$match": filters})

    sort_spec = SORT_OPTIONS.get(query.sort)
    if query.sort == "relevance" and query.q:
        # Blend text relevance with freshness so a stale exact match doesn't
        # outrank a fresh near-match.
        pipeline.append({"$addFields": {
            "_rank": {"$add": [
                {"$ifNull": ["$score", 0]},
                {"$divide": [
                    1,
                    {"$add": [1, {"$divide": [
                        {"$subtract": [datetime.now(UTC), "$last_seen_at"]},
                        5_184_000_000,  # 60 days in ms
                    ]}]},
                ]},
            ]}
        }})
        pipeline.append({"$sort": {"_rank": -1}})
    elif sort_spec:
        pipeline.append({"$sort": dict(sort_spec)})
    else:
        pipeline.append({"$sort": {"listed_at": -1, "first_seen_at": -1}})

    return pipeline


# ---------------------------------------------------------------------------
# $text fallback backend
# ---------------------------------------------------------------------------


async def _text_pipeline(query: PropertySearchQuery) -> list[dict[str, Any]]:
    filters = build_filter(query)
    pipeline: list[dict[str, Any]] = []

    if query.q:
        filters["$text"] = {"$search": query.q}
        pipeline.append({"$match": filters})
        pipeline.append({"$addFields": {"score": {"$meta": "textScore"}}})
    else:
        pipeline.append({"$match": filters})

    sort_spec = SORT_OPTIONS.get(query.sort)
    if query.sort == "relevance" and query.q:
        pipeline.append({"$sort": {"score": -1, "last_seen_at": -1}})
    elif sort_spec:
        pipeline.append({"$sort": dict(sort_spec)})
    else:
        pipeline.append({"$sort": {"listed_at": -1, "first_seen_at": -1}})

    return pipeline


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


async def search_properties(
    db: AsyncIOMotorDatabase, query: PropertySearchQuery
) -> tuple[list[dict[str, Any]], int]:
    backend = await detect_backend()
    base = (
        await _atlas_pipeline(query) if backend == "atlas" else await _text_pipeline(query)
    )

    # `$facet` runs the count and the page in one round trip. `$count` after
    # the same match is exact, unlike an estimate.
    pipeline = base + [
        {"$facet": {
            "results": [
                {"$skip": query.skip},
                {"$limit": query.limit},
                {"$project": RESULT_PROJECTION},
            ],
            "total": [{"$count": "value"}],
        }}
    ]

    cursor = db[D.PROPERTIES].aggregate(pipeline, allowDiskUse=True)
    payload = await cursor.to_list(length=1)
    if not payload:
        return [], 0

    block = payload[0]
    results = [jsonable(_with_id(row)) for row in block.get("results", [])]
    total_block = block.get("total", [])
    total = int(total_block[0]["value"]) if total_block else 0
    return results, total


def _with_id(row: dict[str, Any]) -> dict[str, Any]:
    """Expose `_id` as `id` — the frontend and API contract use `id`."""
    row = dict(row)
    row["id"] = row.pop("_id", None)
    return row


async def facets(db: AsyncIOMotorDatabase, query: PropertySearchQuery) -> dict[str, Any]:
    """Filter counts honouring the current filters.

    One `$facet` computes every group in a single pass over the matched set,
    which is the direct equivalent of the eight separate GROUP BY queries the
    SQL version issued.
    """
    backend = await detect_backend()
    base = (
        await _atlas_pipeline(query) if backend == "atlas" else await _text_pipeline(query)
    )
    # Sorting is irrelevant for counting and costs real time on large sets.
    base = [stage for stage in base if "$sort" not in stage]

    def group(field_name: str, limit: int) -> list[dict[str, Any]]:
        return [
            {"$match": {field_name: {"$ne": None}}},
            {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
            {"$project": {"_id": 0, "value": {"$toString": "$_id"}, "count": 1}},
        ]

    pipeline = base + [{
        "$facet": {
            "city": group("city", 12),
            "sector": group("sector", 40),
            "micro_market": group("micro_market", 20),
            "property_type": group("property_type", 15),
            "possession_status": group("possession_status", 6),
            "segment": group("segment", 6),
            "builder": group("builder_name", 30),
            "configuration": group("configuration", 15),
        }
    }]

    payload = await db[D.PROPERTIES].aggregate(pipeline, allowDiskUse=True).to_list(length=1)
    return jsonable(payload[0]) if payload else {}


async def autocomplete(
    db: AsyncIOMotorDatabase, term: str, *, limit: int = 10
) -> list[dict[str, str]]:
    """Typo-tolerant suggestions across projects, builders and localities."""
    backend = await detect_backend()
    if backend == "atlas":
        return await _atlas_autocomplete(db, term, limit)
    return await _regex_autocomplete(db, term, limit)


async def _atlas_autocomplete(
    db: AsyncIOMotorDatabase, term: str, limit: int
) -> list[dict[str, str]]:
    pipeline = [
        {"$search": {
            "index": settings.atlas_search_index,
            "compound": {"should": [
                {"autocomplete": {"query": term, "path": "project_name",
                                  "fuzzy": {"maxEdits": 1},
                                  "score": {"boost": {"value": 3}}}},
                {"autocomplete": {"query": term, "path": "builder_name",
                                  "fuzzy": {"maxEdits": 1},
                                  "score": {"boost": {"value": 2}}}},
                {"autocomplete": {"query": term, "path": "sector"}},
            ]},
        }},
        {"$match": {"is_active": True}},
        {"$limit": limit * 6},
        {"$project": {
            "_id": 0,
            "project_name": 1, "builder_name": 1,
            "locality": {"$ifNull": ["$sector", "$locality"]},
        }},
    ]
    rows = await db[D.PROPERTIES].aggregate(pipeline).to_list(length=limit * 6)
    return _dedupe_suggestions(rows, term, limit)


async def _regex_autocomplete(
    db: AsyncIOMotorDatabase, term: str, limit: int
) -> list[dict[str, str]]:
    """Prefix match. No typo tolerance — the honest limit of self-hosted."""
    pattern = {"$regex": f"^{_escape_regex(term)}", "$options": "i"}
    rows = await db[D.PROPERTIES].find(
        {"is_active": True,
         "$or": [{"project_name": pattern}, {"builder_name": pattern},
                 {"sector": pattern}, {"locality": pattern}]},
        projection={"_id": 0, "project_name": 1, "builder_name": 1,
                    "locality": {"$ifNull": ["$sector", "$locality"]}},
    ).limit(limit * 6).to_list(length=limit * 6)
    return _dedupe_suggestions(rows, term, limit)


def _dedupe_suggestions(
    rows: list[dict[str, Any]], term: str, limit: int
) -> list[dict[str, str]]:
    lowered = term.lower()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, key in (("project", "project_name"), ("builder", "builder_name"),
                      ("locality", "locality")):
        for row in rows:
            value = row.get(key)
            if not value or not isinstance(value, str):
                continue
            marker = f"{kind}:{value.lower()}"
            if marker in seen or lowered not in value.lower():
                continue
            seen.add(marker)
            out.append({"value": value, "kind": kind})
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# reddit
# ---------------------------------------------------------------------------


@dataclass
class RedditSearchQuery:
    q: str | None = None
    builder: str | None = None
    project: str | None = None
    sector: str | None = None
    city: str | None = None
    topics: list[str] = field(default_factory=list)
    sentiment: str | None = None
    min_score: int | None = None
    subreddit: str | None = None
    page: int = 1
    page_size: int = 25

    @property
    def limit(self) -> int:
        return max(1, min(self.page_size, settings.api_max_page_size))

    @property
    def skip(self) -> int:
        return max(0, (self.page - 1) * self.limit)


async def search_reddit(
    db: AsyncIOMotorDatabase, query: RedditSearchQuery
) -> tuple[list[dict[str, Any]], int]:
    conditions: dict[str, Any] = {}
    if query.builder:
        conditions["detected_builders"] = query.builder
    if query.project:
        conditions["detected_projects"] = query.project
    if query.sector:
        conditions["detected_sectors"] = query.sector
    if query.city:
        conditions["detected_city"] = query.city
    if query.topics:
        conditions["topics"] = {"$in": query.topics}
    if query.sentiment:
        conditions["sentiment"] = query.sentiment
    if query.min_score is not None:
        conditions["score"] = {"$gte": query.min_score}
    if query.subreddit:
        conditions["subreddit"] = query.subreddit

    backend = await detect_backend()
    pipeline: list[dict[str, Any]] = []

    if query.q and backend == "atlas":
        pipeline.append({"$search": {
            "index": settings.atlas_reddit_index,
            "compound": {"should": [
                {"text": {"query": query.q, "path": "title",
                          "score": {"boost": {"value": 5}},
                          "fuzzy": {"maxEdits": 1, "prefixLength": 2}}},
                {"text": {"query": query.q,
                          "path": ["detected_builders", "detected_projects"],
                          "score": {"boost": {"value": 4}}}},
                {"text": {"query": query.q, "path": "body"}},
            ], "minimumShouldMatch": 1},
        }})
        if conditions:
            pipeline.append({"$match": conditions})
    elif query.q:
        conditions["$text"] = {"$search": query.q}
        pipeline.append({"$match": conditions})
        pipeline.append({"$addFields": {"score_text": {"$meta": "textScore"}}})
        pipeline.append({"$sort": {"score_text": -1}})
    else:
        if conditions:
            pipeline.append({"$match": conditions})
        pipeline.append({"$sort": {"created_utc": -1}})

    pipeline.append({"$facet": {
        "results": [
            {"$skip": query.skip},
            {"$limit": query.limit},
            {"$project": {
                "source_id": 1, "subreddit": 1, "permalink": 1, "title": 1,
                "body": {"$substrCP": [{"$ifNull": ["$body", ""]}, 0, 2000]},
                "author": 1, "created_utc": 1, "score": 1, "num_comments": 1,
                "sentiment": 1, "sentiment_score": 1, "detected_builders": 1,
                "detected_projects": 1, "detected_sectors": 1, "detected_city": 1,
                "topics": 1, "keywords": 1, "summary": 1, "relevance_score": 1,
                "top_comments": 1,
            }},
        ],
        "total": [{"$count": "value"}],
    }})

    payload = await db[D.REDDIT_POSTS].aggregate(pipeline, allowDiskUse=True).to_list(length=1)
    if not payload:
        return [], 0
    block = payload[0]
    results = [jsonable(_with_id(row)) for row in block.get("results", [])]
    total_block = block.get("total", [])
    return results, int(total_block[0]["value"]) if total_block else 0
