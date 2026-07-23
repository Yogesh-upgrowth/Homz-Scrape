"""Price-history analytics.

Observations are appended by `Repository._record_price_observation` into a
time series collection. This module reads that series to produce the market
trends the platform surfaces: price movement, rental trends, supply/demand and
new launches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from homz.common.enums import City, Source, TrendMetric
from homz.common.schema import MarketInsightRecord
from homz.db import documents as D
from homz.db.codecs import as_decimal, jsonable
from homz.db.mongo import get_database
from homz.db.repository import Repository, _median_expr
from homz.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class PricePoint:
    observed_at: date
    price: Decimal | None
    price_per_sqft: Decimal | None
    change_pct: float | None


async def property_price_series(
    db: AsyncIOMotorDatabase, property_id: str, *, limit: int = 100
) -> list[PricePoint]:
    cursor = db[D.PRICE_HISTORY].find(
        {"meta.property_id": property_id}
    ).sort("observed_at", -1).limit(limit)

    return [
        PricePoint(
            observed_at=row["observed_at"].date(),
            price=as_decimal(row.get("price")),
            price_per_sqft=as_decimal(row.get("price_per_sqft")),
            change_pct=row.get("change_pct"),
        )
        async for row in cursor
    ]


async def locality_price_movement(
    db: AsyncIOMotorDatabase, *, days: int = 90, min_sample: int = 5
) -> list[dict[str, Any]]:
    """Median ₹/sqft now vs `days` ago, per (city, sector, listing_type).

    Medians, not means — one ₹80 Cr penthouse should not move a sector's number.
    """
    now = datetime.now(UTC)
    recent_from = now - timedelta(days=days)
    older_from = now - timedelta(days=days * 2)

    pipeline: list[dict[str, Any]] = [
        {"$match": {
            "observed_at": {"$gte": older_from},
            "price_per_sqft": {"$gt": 0},
        }},
        {"$addFields": {
            "window": {"$cond": [{"$gte": ["$observed_at", recent_from]}, "recent", "older"]},
            "ppsf": {"$toDouble": "$price_per_sqft"},
        }},
        {"$group": {
            "_id": {
                "city": "$meta.city",
                "sector": "$meta.sector",
                "listing_type": "$meta.listing_type",
                "window": "$window",
            },
            "values": {"$push": "$ppsf"},
            "n": {"$sum": 1},
        }},
        {"$addFields": {"median": _median_expr("$values")}},
        {"$group": {
            "_id": {"city": "$_id.city", "sector": "$_id.sector",
                    "listing_type": "$_id.listing_type"},
            "windows": {"$push": {"window": "$_id.window", "median": "$median", "n": "$n"}},
        }},
        {"$addFields": {
            "recent": {"$first": {"$filter": {
                "input": "$windows", "as": "w", "cond": {"$eq": ["$$w.window", "recent"]}}}},
            "older": {"$first": {"$filter": {
                "input": "$windows", "as": "w", "cond": {"$eq": ["$$w.window", "older"]}}}},
        }},
        {"$match": {
            "recent.n": {"$gte": min_sample},
            "older.n": {"$gte": min_sample},
        }},
        {"$project": {
            "_id": 0,
            "city": "$_id.city", "sector": "$_id.sector",
            "listing_type": "$_id.listing_type",
            "current_ppsf": "$recent.median",
            "previous_ppsf": "$older.median",
            "current_sample": "$recent.n",
            "previous_sample": "$older.n",
            "change_pct": {"$round": [
                {"$multiply": [
                    {"$divide": [
                        {"$subtract": ["$recent.median", "$older.median"]},
                        {"$cond": [{"$gt": ["$older.median", 0]}, "$older.median", 1]},
                    ]}, 100,
                ]}, 2,
            ]},
        }},
        {"$sort": {"change_pct": -1}},
    ]

    rows = await db[D.PRICE_HISTORY].aggregate(pipeline, allowDiskUse=True).to_list(length=500)
    return [jsonable(r) for r in rows]


async def supply_demand_snapshot(db: AsyncIOMotorDatabase) -> list[dict[str, Any]]:
    rows = await db[D.MV_SUPPLY_DEMAND].find().sort("active_supply", -1).to_list(length=500)
    return [jsonable(r) for r in rows]


async def new_launch_feed(db: AsyncIOMotorDatabase, *, days: int = 60) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = await db[D.PROJECTS].find(
        {"$or": [
            {"status": {"$in": ["new_launch", "upcoming"]}},
            {"created_at": {"$gte": cutoff}},
        ]},
        projection={
            "name": 1, "builder_name": 1, "status": 1, "launch_date": 1,
            "possession_date": 1, "price_min": 1, "price_max": 1, "rera_number": 1,
            "city": 1, "sector": 1, "micro_market": 1,
        },
    ).sort([("launch_date", -1), ("created_at", -1)]).to_list(length=200)
    return [jsonable(r) for r in rows]


async def generate_market_insights(*, days: int = 90) -> int:
    """Turn the computed trends into `market_insights` documents.

    `_id` is deterministic so re-running the job updates the same documents
    instead of appending duplicates.
    """
    db = get_database()
    repo = Repository(db)
    period_end = date.today()
    period_start = period_end - timedelta(days=days)
    written = 0

    for row in await locality_price_movement(db, days=days):
        city = _city(row.get("city"))
        sector = row.get("sector") or "all"
        key = f"ppsf:{city.value}:{sector}:{row.get('listing_type')}:{period_end.isoformat()}"
        await repo.upsert_market_insight(
            MarketInsightRecord(
                source=Source.MAGICBRICKS,  # derived from our own warehouse
                source_id=key,
                metric=TrendMetric.AVG_PRICE_PER_SQFT.value,
                city=city,
                sector=row.get("sector"),
                period_start=period_start,
                period_end=period_end,
                value=as_decimal(row.get("current_ppsf")),
                unit="INR/sqft",
                change_pct=row.get("change_pct"),
                sample_size=row.get("current_sample"),
                notes=f"median ₹/sqft over {days}d vs prior {days}d "
                      f"({row.get('listing_type')})",
            )
        )
        written += 1

    for row in await supply_demand_snapshot(db):
        city = _city(row.get("city"))
        sector = row.get("sector") or "all"
        key = f"supply:{city.value}:{sector}:{period_end.isoformat()}"
        await repo.upsert_market_insight(
            MarketInsightRecord(
                source=Source.MAGICBRICKS,
                source_id=key,
                metric=TrendMetric.LISTING_SUPPLY.value,
                city=city,
                sector=row.get("sector"),
                period_start=period_start,
                period_end=period_end,
                value=Decimal(str(row.get("active_supply") or 0)),
                unit="listings",
                sample_size=row.get("active_supply"),
                notes=f"{row.get('new_last_30d') or 0} new in 30d, "
                      f"{row.get('delisted_last_90d') or 0} delisted in 90d",
            )
        )
        written += 1

    async for row in db[D.MV_RENTAL_YIELD].find():
        city = _city(row.get("city"))
        sector = row.get("sector") or "all"
        key = f"yield:{city.value}:{sector}:{row.get('bedrooms')}:{period_end.isoformat()}"
        await repo.upsert_market_insight(
            MarketInsightRecord(
                source=Source.MAGICBRICKS,
                source_id=key,
                metric=TrendMetric.RENTAL_YIELD.value,
                city=city,
                sector=row.get("sector"),
                period_start=period_start,
                period_end=period_end,
                value=as_decimal(row.get("rental_yield_pct")),
                unit="percent",
                sample_size=min(
                    row.get("sale_sample") or 0, row.get("rent_sample") or 0
                ),
                notes=f"{row.get('bedrooms')} BHK gross yield",
            )
        )
        written += 1

    log.info("etl.market_insights_generated", count=written, window_days=days)
    return written


def _city(value: Any) -> City:
    try:
        return City(value)
    except (ValueError, TypeError):
        return City.UNKNOWN
