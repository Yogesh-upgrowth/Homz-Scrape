"""Search query construction.

All filtering is parameterized SQL — user input never reaches a string
concatenation. The dynamic part is *which* conditions get appended, never their
content.

Ranking blends three signals:
  * `ts_rank_cd` on the generated `search_vector` (relevance)
  * trigram similarity on project/builder name (typo tolerance)
  * freshness decay (a live listing beats a stale one at equal relevance)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from homz.settings import settings

SORT_OPTIONS: dict[str, str] = {
    "relevance": "rank DESC, p.last_seen_at DESC",
    "newest": "COALESCE(p.listed_at, p.first_seen_at) DESC NULLS LAST",
    "price_asc": "COALESCE(p.price, p.rent_monthly) ASC NULLS LAST",
    "price_desc": "COALESCE(p.price, p.rent_monthly) DESC NULLS LAST",
    "ppsf_asc": "p.price_per_sqft ASC NULLS LAST",
    "area_desc": "p.area_sqft DESC NULLS LAST",
    "investment": "p.investment_score DESC NULLS LAST",
    "lowest_risk": "p.risk_score ASC NULLS LAST",
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
    sort: str = "relevance"
    page: int = 1
    page_size: int = 25
    include_duplicates: bool = False

    @property
    def offset(self) -> int:
        return max(0, (self.page - 1) * self.limit)

    @property
    def limit(self) -> int:
        return max(1, min(self.page_size, settings.api_max_page_size))


def build_property_search(query: PropertySearchQuery) -> tuple[str, str, dict[str, Any]]:
    """Returns (rows_sql, count_sql, params)."""
    conditions: list[str] = ["p.is_active"]
    params: dict[str, Any] = {}

    if not query.include_duplicates:
        conditions.append("p.canonical_property_id IS NULL")

    # --- full text ---------------------------------------------------------
    rank_expr = "0.0"
    if query.q:
        params["q"] = query.q
        params["q_like"] = f"%{query.q}%"
        conditions.append(
            "(p.search_vector @@ websearch_to_tsquery('english', :q)"
            " OR p.project_name ILIKE :q_like"
            " OR p.builder_name ILIKE :q_like)"
        )
        rank_expr = (
            "ts_rank_cd(p.search_vector, websearch_to_tsquery('english', :q)) * 3"
            " + GREATEST("
            "     similarity(COALESCE(p.project_name, ''), :q),"
            "     similarity(COALESCE(p.builder_name, ''), :q)"
            "   )"
            # Freshness decay: full credit today, ~half after 60 days.
            " + (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - p.last_seen_at)) / 5184000.0))"
        )

    def add(condition: str, **kwargs: Any) -> None:
        conditions.append(condition)
        params.update(kwargs)

    if query.city:
        add("p.city = CAST(:city AS city_enum)", city=query.city)
    if query.sector:
        add("p.sector ILIKE :sector", sector=f"%{query.sector}%")
    if query.locality:
        add("p.locality ILIKE :locality", locality=f"%{query.locality}%")
    if query.micro_market:
        add("p.micro_market ILIKE :micro_market", micro_market=f"%{query.micro_market}%")
    if query.builder:
        add(
            "(p.builder_name ILIKE :builder OR b.normalized_name ILIKE :builder_norm)",
            builder=f"%{query.builder}%",
            builder_norm=f"%{query.builder.lower()}%",
        )
    if query.project:
        add("p.project_name ILIKE :project", project=f"%{query.project}%")
    if query.listing_type:
        add(
            "p.listing_type = CAST(:listing_type AS listing_type_enum)",
            listing_type=query.listing_type,
        )
    if query.property_type:
        add(
            "p.property_type = ANY(CAST(:property_type AS property_type_enum[]))",
            property_type=query.property_type,
        )
    if query.configuration:
        add("p.configuration ILIKE :configuration", configuration=f"%{query.configuration}%")
    if query.bedrooms_min is not None:
        add("p.bedrooms >= :bedrooms_min", bedrooms_min=query.bedrooms_min)
    if query.bedrooms_max is not None:
        add("p.bedrooms <= :bedrooms_max", bedrooms_max=query.bedrooms_max)

    # Budget filters apply to sale price or monthly rent, whichever the
    # listing carries — so one budget control works for both modes.
    if query.price_min is not None:
        add("COALESCE(p.price, p.rent_monthly) >= :price_min", price_min=query.price_min)
    if query.price_max is not None:
        add("COALESCE(p.price, p.rent_monthly) <= :price_max", price_max=query.price_max)

    if query.area_min is not None:
        add("p.area_sqft >= :area_min", area_min=query.area_min)
    if query.area_max is not None:
        add("p.area_sqft <= :area_max", area_max=query.area_max)
    if query.possession_status:
        add(
            "p.possession_status = ANY(CAST(:possession AS possession_status_enum[]))",
            possession=query.possession_status,
        )
    if query.segment:
        add("p.segment = ANY(CAST(:segment AS segment_enum[]))", segment=query.segment)
    if query.amenities:
        # `@>` uses the GIN index on amenities.
        add("p.amenities @> CAST(:amenities AS text[])", amenities=query.amenities)
    if query.keywords:
        add(
            "(p.keywords && CAST(:keywords AS text[]) OR p.tags && CAST(:keywords AS text[]))",
            keywords=query.keywords,
        )
    if query.is_commercial is not None:
        add("p.is_commercial = :is_commercial", is_commercial=query.is_commercial)
    if query.has_rera is not None:
        conditions.append(
            "p.rera_number IS NOT NULL" if query.has_rera else "p.rera_number IS NULL"
        )
    if query.min_investment_score is not None:
        add("p.investment_score >= :min_inv", min_inv=query.min_investment_score)
    if query.max_risk_score is not None:
        add("p.risk_score <= :max_risk", max_risk=query.max_risk_score)

    where_sql = " AND ".join(conditions)
    order_sql = SORT_OPTIONS.get(query.sort, SORT_OPTIONS["relevance"])
    if query.sort == "relevance" and not query.q:
        order_sql = SORT_OPTIONS["newest"]

    params["limit"] = query.limit
    params["offset"] = query.offset

    rows_sql = f"""
        SELECT
            p.id, p.source, p.source_id, p.listing_url, p.title,
            p.project_name, p.builder_name, p.society_name,
            p.listing_type, p.property_type, p.segment, p.configuration,
            p.bedrooms, p.bathrooms, p.area_sqft, p.carpet_area_sqft,
            p.price, p.price_max, p.price_display, p.price_per_sqft,
            p.rent_monthly, p.is_price_on_request,
            p.city, p.sector, p.locality, p.micro_market,
            p.latitude, p.longitude,
            p.possession_status, p.possession_date, p.rera_number,
            p.amenities, p.tags, p.keywords,
            p.investment_score, p.risk_score, p.location_score,
            p.builder_trust_score, p.ai_summary,
            p.listed_at, p.first_seen_at, p.last_seen_at, p.duplicate_count,
            (SELECT url FROM property_images pi
              WHERE pi.property_id = p.id
              ORDER BY pi.is_primary DESC, pi.position ASC LIMIT 1) AS primary_image,
            {rank_expr} AS rank
        FROM properties p
        LEFT JOIN builders b ON b.id = p.builder_id
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT :limit OFFSET :offset
    """

    count_sql = f"""
        SELECT COUNT(*) FROM properties p
        LEFT JOIN builders b ON b.id = p.builder_id
        WHERE {where_sql}
    """
    return rows_sql, count_sql, params


async def search_properties(
    session: AsyncSession, query: PropertySearchQuery
) -> tuple[list[dict[str, Any]], int]:
    rows_sql, count_sql, params = build_property_search(query)
    rows = (await session.execute(text(rows_sql), params)).mappings().all()
    total = (await session.execute(text(count_sql), params)).scalar_one()
    return [dict(row) for row in rows], int(total)


# ---------------------------------------------------------------------------
# facets / suggestions
# ---------------------------------------------------------------------------


async def facets(session: AsyncSession, query: PropertySearchQuery) -> dict[str, Any]:
    """Counts for the filter sidebar, honouring the current filters."""
    _, count_sql, params = build_property_search(query)
    base_where = count_sql.split("WHERE", 1)[1]

    async def group(column: str, limit: int = 25) -> list[dict[str, Any]]:
        sql = f"""
            SELECT {column} AS value, COUNT(*) AS count
            FROM properties p
            LEFT JOIN builders b ON b.id = p.builder_id
            WHERE {base_where} AND {column} IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}
        """
        rows = (await session.execute(text(sql), params)).mappings().all()
        return [{"value": str(r["value"]), "count": int(r["count"])} for r in rows]

    return {
        "city": await group("p.city", 12),
        "sector": await group("p.sector", 40),
        "micro_market": await group("p.micro_market", 20),
        "property_type": await group("p.property_type", 15),
        "possession_status": await group("p.possession_status", 6),
        "segment": await group("p.segment", 6),
        "builder": await group("p.builder_name", 30),
        "configuration": await group("p.configuration", 15),
    }


async def autocomplete(
    session: AsyncSession, term: str, *, limit: int = 10
) -> list[dict[str, str]]:
    """Typo-tolerant suggestions across projects, builders and localities."""
    rows = await session.execute(
        text(
            """
            (SELECT DISTINCT project_name AS value, 'project' AS kind,
                    similarity(project_name, :term) AS sim
             FROM properties
             WHERE project_name IS NOT NULL AND project_name % :term
             ORDER BY sim DESC LIMIT :limit)
            UNION ALL
            (SELECT DISTINCT name, 'builder', similarity(name, :term)
             FROM builders WHERE name % :term ORDER BY 3 DESC LIMIT :limit)
            UNION ALL
            (SELECT DISTINCT COALESCE(sector, locality), 'locality',
                    similarity(COALESCE(sector, locality), :term)
             FROM properties
             WHERE COALESCE(sector, locality) IS NOT NULL
               AND COALESCE(sector, locality) % :term
             ORDER BY 3 DESC LIMIT :limit)
            ORDER BY sim DESC
            LIMIT :limit
            """
        ),
        {"term": term, "limit": limit},
    )
    return [{"value": r[0], "kind": r[1]} for r in rows if r[0]]


# ---------------------------------------------------------------------------
# reddit search
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
    def offset(self) -> int:
        return max(0, (self.page - 1) * self.limit)


async def search_reddit(
    session: AsyncSession, query: RedditSearchQuery
) -> tuple[list[dict[str, Any]], int]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": query.limit, "offset": query.offset}
    rank = "0.0"

    if query.q:
        params["q"] = query.q
        conditions.append("r.search_vector @@ websearch_to_tsquery('english', :q)")
        rank = "ts_rank_cd(r.search_vector, websearch_to_tsquery('english', :q))"
    if query.builder:
        conditions.append("r.detected_builders && CAST(:builders AS text[])")
        params["builders"] = [query.builder]
    if query.project:
        conditions.append("r.detected_projects && CAST(:projects AS text[])")
        params["projects"] = [query.project]
    if query.sector:
        conditions.append("r.detected_sectors && CAST(:sectors AS text[])")
        params["sectors"] = [query.sector]
    if query.city:
        conditions.append("r.detected_city = CAST(:city AS city_enum)")
        params["city"] = query.city
    if query.topics:
        conditions.append("r.topics && CAST(:topics AS text[])")
        params["topics"] = query.topics
    if query.sentiment:
        conditions.append("r.sentiment = CAST(:sentiment AS sentiment_enum)")
        params["sentiment"] = query.sentiment
    if query.min_score is not None:
        conditions.append("r.score >= :min_score")
        params["min_score"] = query.min_score
    if query.subreddit:
        conditions.append("r.subreddit = :subreddit")
        params["subreddit"] = query.subreddit

    where_sql = " AND ".join(conditions)
    order = f"{rank} DESC, r.score DESC" if query.q else "r.created_utc DESC NULLS LAST"

    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT r.id, r.source_id, r.subreddit, r.permalink, r.title, r.body,
                           r.author, r.created_utc, r.score, r.num_comments,
                           r.sentiment, r.sentiment_score, r.detected_builders,
                           r.detected_projects, r.detected_sectors, r.detected_city,
                           r.topics, r.keywords, r.summary, r.relevance_score,
                           {rank} AS rank
                    FROM reddit_posts r
                    WHERE {where_sql}
                    ORDER BY {order}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    total = (
        await session.execute(
            text(f"SELECT COUNT(*) FROM reddit_posts r WHERE {where_sql}"), params
        )
    ).scalar_one()

    return [dict(row) for row in rows], int(total)
