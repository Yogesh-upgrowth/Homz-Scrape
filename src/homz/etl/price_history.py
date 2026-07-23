"""Price-history analytics.

The `price_history` rows themselves are written by a database trigger (see
sql/001_schema.sql) so a price change is captured even if application code
forgets to. This module reads that table to produce the market-trend records
the platform surfaces: price movement, rental trends, supply/demand and new
launches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from homz.common.enums import City, Source, TrendMetric
from homz.common.schema import MarketInsightRecord
from homz.db.engine import session_scope
from homz.db.repository import Repository
from homz.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class PricePoint:
    observed_at: date
    price: Decimal | None
    price_per_sqft: Decimal | None
    change_pct: float | None


async def property_price_series(
    session: AsyncSession, property_id: int, *, limit: int = 100
) -> list[PricePoint]:
    rows = await session.execute(
        text(
            """
            SELECT observed_at::date AS d, price, price_per_sqft, change_pct
            FROM price_history
            WHERE property_id = :pid
            ORDER BY observed_at DESC
            LIMIT :limit
            """
        ),
        {"pid": property_id, "limit": limit},
    )
    return [
        PricePoint(
            observed_at=row[0],
            price=row[1],
            price_per_sqft=row[2],
            change_pct=float(row[3]) if row[3] is not None else None,
        )
        for row in rows
    ]


async def locality_price_movement(
    session: AsyncSession, *, days: int = 90, min_sample: int = 5
) -> list[dict[str, Any]]:
    """Median ₹/sqft now vs `days` ago, per (city, sector, listing_type).

    Medians, not means — one ₹80 Cr penthouse should not move a sector's number.
    """
    rows = await session.execute(
        text(
            """
            WITH recent AS (
                SELECT p.city, p.sector, p.listing_type,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY ph.price_per_sqft)
                           AS ppsf,
                       COUNT(*) AS n
                FROM price_history ph
                JOIN properties p ON p.id = ph.property_id
                WHERE ph.observed_at > NOW() - make_interval(days => :days)
                  AND ph.price_per_sqft > 0
                GROUP BY 1,2,3
            ), older AS (
                SELECT p.city, p.sector, p.listing_type,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY ph.price_per_sqft)
                           AS ppsf,
                       COUNT(*) AS n
                FROM price_history ph
                JOIN properties p ON p.id = ph.property_id
                WHERE ph.observed_at BETWEEN NOW() - make_interval(days => :days * 2)
                                         AND NOW() - make_interval(days => :days)
                  AND ph.price_per_sqft > 0
                GROUP BY 1,2,3
            )
            SELECT r.city, r.sector, r.listing_type,
                   r.ppsf AS current_ppsf, o.ppsf AS previous_ppsf,
                   ROUND(((r.ppsf - o.ppsf) / NULLIF(o.ppsf, 0) * 100)::numeric, 2)
                       AS change_pct,
                   r.n AS current_sample, o.n AS previous_sample
            FROM recent r
            JOIN older o USING (city, sector, listing_type)
            WHERE r.n >= :min_sample AND o.n >= :min_sample
            ORDER BY ABS(COALESCE((r.ppsf - o.ppsf) / NULLIF(o.ppsf, 0), 0)) DESC
            """
        ),
        {"days": days, "min_sample": min_sample},
    )
    return [dict(row) for row in rows.mappings()]


async def supply_demand_snapshot(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        text("SELECT * FROM mv_supply_demand ORDER BY active_supply DESC")
    )
    return [dict(row) for row in rows.mappings()]


async def new_launch_feed(session: AsyncSession, *, days: int = 60) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT pr.id, pr.name, pr.builder_name, pr.status, pr.launch_date,
                   pr.possession_date, pr.price_min, pr.price_max, pr.rera_number,
                   l.city, l.sector, l.micro_market
            FROM projects pr
            LEFT JOIN locations l ON l.id = pr.location_id
            WHERE pr.status IN ('new_launch','upcoming')
               OR pr.created_at > NOW() - make_interval(days => :days)
            ORDER BY COALESCE(pr.launch_date, pr.created_at::date) DESC
            LIMIT 200
            """
        ),
        {"days": days},
    )
    return [dict(row) for row in rows.mappings()]


async def generate_market_insights(*, days: int = 90) -> int:
    """Turn the computed trends into `market_insights` rows.

    `source_id` is deterministic so re-running the job updates the same rows
    instead of appending duplicates.
    """
    period_end = date.today()
    period_start = period_end - timedelta(days=days)
    written = 0

    async with session_scope() as session:
        repo = Repository(session)

        for row in await locality_price_movement(session, days=days):
            city = _city(row["city"])
            sector = row["sector"] or "all"
            key = f"ppsf:{city.value}:{sector}:{row['listing_type']}:{period_end.isoformat()}"
            await repo.upsert_market_insight(
                MarketInsightRecord(
                    source=Source.MAGICBRICKS,  # derived from our own warehouse
                    source_id=key,
                    metric=TrendMetric.AVG_PRICE_PER_SQFT.value,
                    city=city,
                    sector=row["sector"],
                    period_start=period_start,
                    period_end=period_end,
                    value=row["current_ppsf"],
                    unit="INR/sqft",
                    change_pct=float(row["change_pct"]) if row["change_pct"] is not None else None,
                    sample_size=row["current_sample"],
                    notes=(
                        f"median ₹/sqft over {days}d vs prior {days}d ({row['listing_type']})"
                    ),
                )
            )
            written += 1

        for row in await supply_demand_snapshot(session):
            city = _city(row["city"])
            sector = row["sector"] or "all"
            key = f"supply:{city.value}:{sector}:{period_end.isoformat()}"
            await repo.upsert_market_insight(
                MarketInsightRecord(
                    source=Source.MAGICBRICKS,
                    source_id=key,
                    metric=TrendMetric.LISTING_SUPPLY.value,
                    city=city,
                    sector=row["sector"],
                    period_start=period_start,
                    period_end=period_end,
                    value=Decimal(str(row["active_supply"] or 0)),
                    unit="listings",
                    sample_size=row["active_supply"],
                    notes=(
                        f"{row['new_last_30d'] or 0} new in 30d, "
                        f"{row['delisted_last_90d'] or 0} delisted in 90d"
                    ),
                )
            )
            written += 1

        rows = await session.execute(text("SELECT * FROM mv_rental_yield"))
        for row in rows.mappings():
            city = _city(row["city"])
            sector = row["sector"] or "all"
            key = f"yield:{city.value}:{sector}:{row['bedrooms']}:{period_end.isoformat()}"
            await repo.upsert_market_insight(
                MarketInsightRecord(
                    source=Source.MAGICBRICKS,
                    source_id=key,
                    metric=TrendMetric.RENTAL_YIELD.value,
                    city=city,
                    sector=row["sector"],
                    period_start=period_start,
                    period_end=period_end,
                    value=row["rental_yield_pct"],
                    unit="percent",
                    sample_size=min(row["sale_sample"], row["rent_sample"]),
                    notes=f"{row['bedrooms']} BHK gross yield",
                )
            )
            written += 1

        await session.commit()

    log.info("etl.market_insights_generated", count=written, window_days=days)
    return written


def _city(value: Any) -> City:
    try:
        return City(value)
    except (ValueError, TypeError):
        return City.UNKNOWN
