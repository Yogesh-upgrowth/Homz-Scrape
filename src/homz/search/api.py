"""FastAPI search + intelligence API.

    uvicorn homz.search.api:app --host 0.0.0.0 --port 8000

Read-only. Everything is served from the warehouse; no endpoint triggers a
scrape, so a traffic spike can never turn into a traffic spike against a
source portal.

CORS is open to the origins in `HOMZ_API_CORS_ORIGINS` so the `<homz-search>`
widget on homzrealtor.com can call this cross-origin. Every endpoint is public
and read-only, so the origin list is about correctness, not access control.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from homz.db.engine import dispose_engine, get_db, healthcheck
from homz.logging_setup import configure_logging, get_logger
from homz.search.query import (
    PropertySearchQuery,
    RedditSearchQuery,
    autocomplete,
    facets,
    search_properties,
    search_reddit,
)
from homz.settings import settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("api.starting", env=settings.env, cors_origins=len(settings.api_cors_origins))
    yield
    await dispose_engine()
    log.info("api.stopped")


app = FastAPI(
    title="Homz Realtor — Real Estate Intelligence API",
    description=(
        "Unified search over Delhi NCR property listings, projects, builders "
        "and public discussion, with derived investment/risk scoring."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api_cors_origins,
    allow_origin_regex=r"https://.*\.homzrealtor\.com",
    allow_credentials=False,  # nothing here is per-user; no cookies needed
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    max_age=3600,
)

DB = Annotated[AsyncSession, Depends(get_db)]


class Page(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    results: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    database: bool
    counts: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    db_ok = await healthcheck()
    return HealthResponse(status="ok" if db_ok else "degraded", database=db_ok)


@app.get("/stats", tags=["ops"])
async def stats(db: DB) -> dict[str, Any]:
    from homz.db.repository import Repository

    counts = await Repository(db).counts()
    runs = (
        (
            await db.execute(
                text(
                    """
                    SELECT source, job, status, started_at, parsed, errors
                    FROM scrape_runs ORDER BY started_at DESC LIMIT 15
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    return {"counts": counts, "recent_runs": [dict(r) for r in runs]}


# ---------------------------------------------------------------------------
# property search
# ---------------------------------------------------------------------------


@app.get("/properties", response_model=Page, tags=["properties"])
async def properties(
    db: DB,
    q: str | None = Query(None, description="Free text: builder, project, sector, keywords"),
    city: str | None = Query(None, description="gurgaon | noida | delhi | ..."),
    sector: str | None = None,
    locality: str | None = None,
    micro_market: str | None = Query(None, description="e.g. 'Dwarka Expressway'"),
    builder: str | None = None,
    project: str | None = None,
    listing_type: str | None = Query(None, description="sale | rent | resale | new_launch"),
    property_type: list[str] | None = Query(None),
    configuration: str | None = Query(None, description="e.g. '3 BHK'"),
    bedrooms_min: int | None = None,
    bedrooms_max: int | None = None,
    price_min: Decimal | None = Query(None, description="INR; matches sale price or rent"),
    price_max: Decimal | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    possession_status: list[str] | None = Query(None),
    segment: list[str] | None = Query(None),
    amenities: list[str] | None = Query(None),
    keywords: list[str] | None = Query(None),
    is_commercial: bool | None = None,
    has_rera: bool | None = None,
    min_investment_score: float | None = Query(None, ge=0, le=100),
    max_risk_score: float | None = Query(None, ge=0, le=100),
    sort: str = Query("relevance"),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.api_page_size, ge=1, le=settings.api_max_page_size),
) -> Page:
    query = PropertySearchQuery(
        q=q,
        city=city,
        sector=sector,
        locality=locality,
        micro_market=micro_market,
        builder=builder,
        project=project,
        listing_type=listing_type,
        property_type=property_type or [],
        configuration=configuration,
        bedrooms_min=bedrooms_min,
        bedrooms_max=bedrooms_max,
        price_min=price_min,
        price_max=price_max,
        area_min=area_min,
        area_max=area_max,
        possession_status=possession_status or [],
        segment=segment or [],
        amenities=amenities or [],
        keywords=keywords or [],
        is_commercial=is_commercial,
        has_rera=has_rera,
        min_investment_score=min_investment_score,
        max_risk_score=max_risk_score,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    results, total = await search_properties(db, query)
    return Page(
        total=total,
        page=page,
        page_size=query.limit,
        pages=(total + query.limit - 1) // query.limit,
        results=results,
    )


@app.get("/properties/facets", tags=["properties"])
async def property_facets(
    db: DB,
    q: str | None = None,
    city: str | None = None,
    listing_type: str | None = None,
) -> dict[str, Any]:
    query = PropertySearchQuery(q=q, city=city, listing_type=listing_type)
    return await facets(db, query)


@app.get("/properties/{property_id}", tags=["properties"])
async def property_detail(property_id: int, db: DB) -> dict[str, Any]:
    row = (
        (await db.execute(text("SELECT * FROM properties WHERE id = :id"), {"id": property_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="property not found")

    record = dict(row)
    record.pop("search_vector", None)

    images = (
        (
            await db.execute(
                text(
                    """
                    SELECT url, caption, is_primary FROM property_images
                    WHERE property_id = :id ORDER BY is_primary DESC, position ASC
                    """
                ),
                {"id": property_id},
            )
        )
        .mappings()
        .all()
    )
    record["images"] = [dict(i) for i in images]

    history = (
        (
            await db.execute(
                text(
                    """
                    SELECT observed_at, price, price_per_sqft, rent_monthly, change_pct
                    FROM price_history WHERE property_id = :id
                    ORDER BY observed_at DESC LIMIT 50
                    """
                ),
                {"id": property_id},
            )
        )
        .mappings()
        .all()
    )
    record["price_history"] = [dict(h) for h in history]

    duplicates = (
        (
            await db.execute(
                text(
                    """
                    SELECT p.id, p.source, p.listing_url, p.price, d.score, d.reason
                    FROM property_duplicates d
                    JOIN properties p ON p.id = d.duplicate_id
                    WHERE d.canonical_id = :id
                    """
                ),
                {"id": property_id},
            )
        )
        .mappings()
        .all()
    )
    record["duplicate_listings"] = [dict(d) for d in duplicates]
    return record


@app.get("/autocomplete", tags=["properties"])
async def suggest(
    db: DB, term: str = Query(..., min_length=2), limit: int = Query(10, ge=1, le=25)
) -> list[dict[str, str]]:
    return await autocomplete(db, term, limit=limit)


# ---------------------------------------------------------------------------
# builders & projects
# ---------------------------------------------------------------------------


@app.get("/builders", tags=["builders"])
async def builders(
    db: DB,
    q: str | None = None,
    min_trust: float | None = Query(None, ge=0, le=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> Page:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    if q:
        conditions.append(
            "(b.search_vector @@ websearch_to_tsquery('english', :q) OR b.name ILIKE :like)"
        )
        params["q"] = q
        params["like"] = f"%{q}%"
    if min_trust is not None:
        conditions.append("b.trust_score >= :min_trust")
        params["min_trust"] = min_trust
    where_sql = " AND ".join(conditions)

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT b.id, b.name, b.normalized_name, b.description, b.website,
                           b.established_year, b.rating, b.rating_count,
                           b.total_projects, b.completed_projects, b.ongoing_projects,
                           b.trust_score, b.risk_score, b.sentiment,
                           b.reputation_summary,
                           s.reddit_mentions, s.reddit_positive, s.reddit_negative,
                           s.listing_count, s.avg_price_per_sqft
                    FROM builders b
                    LEFT JOIN mv_builder_scorecard s ON s.builder_id = b.id
                    WHERE {where_sql}
                    ORDER BY b.trust_score DESC NULLS LAST, b.total_projects DESC NULLS LAST
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
        await db.execute(text(f"SELECT COUNT(*) FROM builders b WHERE {where_sql}"), params)
    ).scalar_one()

    return Page(
        total=int(total),
        page=page,
        page_size=page_size,
        pages=(int(total) + page_size - 1) // page_size,
        results=[dict(r) for r in rows],
    )


@app.get("/builders/{builder_id}", tags=["builders"])
async def builder_detail(builder_id: int, db: DB) -> dict[str, Any]:
    row = (
        (await db.execute(text("SELECT * FROM builders WHERE id = :id"), {"id": builder_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="builder not found")

    record = dict(row)
    record.pop("search_vector", None)

    projects = (
        (
            await db.execute(
                text(
                    """
                    SELECT id, name, status, possession_date, price_min, price_max,
                           rera_number FROM projects WHERE builder_id = :id
                    ORDER BY COALESCE(possession_date, launch_date) DESC NULLS LAST
                    """
                ),
                {"id": builder_id},
            )
        )
        .mappings()
        .all()
    )
    record["projects"] = [dict(p) for p in projects]

    # Public discussion about this builder, most recent first.
    chatter = (
        (
            await db.execute(
                text(
                    """
                    SELECT source_id, permalink, title, score, sentiment, topics,
                           created_utc, summary
                    FROM reddit_posts
                    WHERE detected_builders && CAST(:names AS text[])
                    ORDER BY created_utc DESC NULLS LAST LIMIT 40
                    """
                ),
                {"names": [record["name"]]},
            )
        )
        .mappings()
        .all()
    )
    record["public_discussion"] = [dict(c) for c in chatter]
    return record


@app.get("/projects", tags=["projects"])
async def projects(
    db: DB,
    q: str | None = None,
    city: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> Page:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    if q:
        conditions.append("pr.search_vector @@ websearch_to_tsquery('english', :q)")
        params["q"] = q
    if city:
        conditions.append("l.city = CAST(:city AS city_enum)")
        params["city"] = city
    if status:
        conditions.append("pr.status = CAST(:status AS possession_status_enum)")
        params["status"] = status
    where_sql = " AND ".join(conditions)

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT pr.id, pr.name, pr.builder_name, pr.status, pr.launch_date,
                           pr.possession_date, pr.price_min, pr.price_max,
                           pr.price_per_sqft, pr.total_units, pr.rera_number,
                           pr.amenities, pr.investment_score, pr.risk_score,
                           l.city, l.sector, l.micro_market
                    FROM projects pr
                    LEFT JOIN locations l ON l.id = pr.location_id
                    WHERE {where_sql}
                    ORDER BY COALESCE(pr.launch_date, pr.created_at::date) DESC
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
        await db.execute(
            text(
                f"SELECT COUNT(*) FROM projects pr "
                f"LEFT JOIN locations l ON l.id = pr.location_id WHERE {where_sql}"
            ),
            params,
        )
    ).scalar_one()

    return Page(
        total=int(total),
        page=page,
        page_size=page_size,
        pages=(int(total) + page_size - 1) // page_size,
        results=[dict(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# reddit / sentiment
# ---------------------------------------------------------------------------


@app.get("/reddit", response_model=Page, tags=["reddit"])
async def reddit(
    db: DB,
    q: str | None = None,
    builder: str | None = None,
    project: str | None = None,
    sector: str | None = None,
    city: str | None = None,
    topics: list[str] | None = Query(None),
    sentiment: str | None = Query(None, description="positive | negative | neutral | mixed"),
    min_score: int | None = None,
    subreddit: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.api_page_size, ge=1, le=settings.api_max_page_size),
) -> Page:
    query = RedditSearchQuery(
        q=q,
        builder=builder,
        project=project,
        sector=sector,
        city=city,
        topics=topics or [],
        sentiment=sentiment,
        min_score=min_score,
        subreddit=subreddit,
        page=page,
        page_size=page_size,
    )
    results, total = await search_reddit(db, query)
    return Page(
        total=total,
        page=page,
        page_size=query.limit,
        pages=(total + query.limit - 1) // query.limit,
        results=results,
    )


@app.get("/reddit/{post_source_id}/comments", tags=["reddit"])
async def reddit_comments(post_source_id: str, db: DB) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT c.comment_id, c.author, c.body, c.score, c.depth,
                           c.sentiment, c.topics, c.created_utc, c.permalink
                    FROM reddit_comments c
                    WHERE c.post_source_id = :sid
                    ORDER BY c.score DESC LIMIT 200
                    """
                ),
                {"sid": post_source_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# market intelligence
# ---------------------------------------------------------------------------


@app.get("/market/trends", tags=["market"])
async def market_trends(
    db: DB,
    city: str | None = None,
    sector: str | None = None,
    listing_type: str = "sale",
    months: int = Query(12, ge=1, le=60),
) -> list[dict[str, Any]]:
    conditions = [
        "listing_type = CAST(:lt AS listing_type_enum)",
        "period > (CURRENT_DATE - make_interval(months => :months))",
    ]
    params: dict[str, Any] = {"lt": listing_type, "months": months}
    if city:
        conditions.append("city = CAST(:city AS city_enum)")
        params["city"] = city
    if sector:
        conditions.append("sector ILIKE :sector")
        params["sector"] = f"%{sector}%"

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT city, sector, micro_market, property_type, period,
                           listing_count, median_price_per_sqft, avg_price_per_sqft,
                           median_price, avg_rent, avg_area_sqft
                    FROM mv_locality_price_trends
                    WHERE {' AND '.join(conditions)}
                    ORDER BY period DESC, listing_count DESC
                    LIMIT 500
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@app.get("/market/yield", tags=["market"])
async def rental_yield(
    db: DB, city: str | None = None, bedrooms: int | None = None
) -> list[dict[str, Any]]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {}
    if city:
        conditions.append("city = CAST(:city AS city_enum)")
        params["city"] = city
    if bedrooms is not None:
        conditions.append("bedrooms = :bedrooms")
        params["bedrooms"] = bedrooms
    rows = (
        (
            await db.execute(
                text(
                    f"SELECT * FROM mv_rental_yield WHERE {' AND '.join(conditions)} "
                    f"ORDER BY rental_yield_pct DESC LIMIT 300"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@app.get("/market/supply-demand", tags=["market"])
async def supply_demand(db: DB, city: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    where = "TRUE"
    if city:
        where = "city = CAST(:city AS city_enum)"
        params["city"] = city
    rows = (
        (
            await db.execute(
                text(
                    f"SELECT * FROM mv_supply_demand WHERE {where} "
                    f"ORDER BY active_supply DESC LIMIT 300"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@app.get("/market/insights", tags=["market"])
async def market_insights(
    db: DB,
    metric: str | None = None,
    city: str | None = None,
    sector: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if metric:
        conditions.append("metric = :metric")
        params["metric"] = metric
    if city:
        conditions.append("city = CAST(:city AS city_enum)")
        params["city"] = city
    if sector:
        conditions.append("sector ILIKE :sector")
        params["sector"] = f"%{sector}%"
    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT metric, city, sector, period_start, period_end, value, unit,
                           change_pct, sample_size, notes
                    FROM market_insights
                    WHERE {' AND '.join(conditions)}
                    ORDER BY period_end DESC, ABS(COALESCE(change_pct, 0)) DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@app.get("/market/new-launches", tags=["market"])
async def new_launches(db: DB, days: int = Query(90, ge=1, le=365)) -> list[dict[str, Any]]:
    from homz.etl.price_history import new_launch_feed

    return await new_launch_feed(db, days=days)


# ---------------------------------------------------------------------------
# static widget hosting (dev convenience; use a CDN in production)
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
if settings.api_serve_web and _WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
    log.info("api.serving_web", directory=str(_WEB_DIR))
