"""FastAPI search + intelligence API (MongoDB).

    uvicorn homz.search.api:app --host 0.0.0.0 --port 8000

Two surfaces:

* **Public read** (`/properties`, `/builders`, `/market/*`, …) — served
  entirely from the warehouse. CORS-open to `HOMZ_API_CORS_ORIGINS` so the
  `<homz-search>` widget can call it cross-origin.
* **Authenticated ingest** (`/ingest/*`) — where client-side scraping submits
  what it collected, and where it claims work from the on-demand fill queue.
  Bearer-token authenticated and rate-limited.

A search that finds nothing queues a *fill task* rather than scraping inline,
so no user request ever blocks on a portal fetch, and the daily task budget
caps how much traffic search demand can generate against a source.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from homz.db import documents as D
from homz.db.codecs import jsonable
from homz.db.mongo import close_client, detect_backend, get_database, healthcheck, server_info
from homz.logging_setup import configure_logging, get_logger
from homz.search.query import (
    PropertySearchQuery,
    RedditSearchQuery,
    autocomplete,
    facets,
    search_properties,
    search_reddit,
)
from homz.services.ingest import (
    IngestError,
    IngestService,
    check_rate_limit,
    verify_token,
)
from homz.services.ondemand import DemandFiller
from homz.settings import settings

log = get_logger(__name__)


async def get_db() -> AsyncIOMotorDatabase:
    return get_database()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    backend = await detect_backend()
    log.info(
        "api.starting",
        env=settings.env,
        database=settings.mongodb_database,
        search_backend=backend,
        cors_origins=len(settings.api_cors_origins),
    )
    if backend != "atlas":
        log.warning(
            "api.text_search_fallback",
            detail="not an Atlas cluster — search has no fuzzy/typo tolerance",
        )
    yield
    await close_client()
    log.info("api.stopped")


app = FastAPI(
    title="Homz Realtor — Real Estate Intelligence API",
    description=(
        "Unified search over Delhi NCR property listings, projects, builders "
        "and public discussion, with derived investment/risk scoring."
    ),
    version="2.0.0",
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

DB = Annotated[AsyncIOMotorDatabase, Depends(get_db)]


class Page(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    results: list[dict[str, Any]]
    #: Present when the search found little or nothing and a scrape was
    #: queued to fill the gap. The client can poll and re-search shortly.
    backfill: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: str
    database: bool
    search_backend: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    db_ok = await healthcheck()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database=db_ok,
        search_backend=await detect_backend() if db_ok else None,
    )


@app.get("/stats", tags=["ops"])
async def stats(db: DB) -> dict[str, Any]:
    from homz.db.repository import Repository

    counts = await Repository(db).counts()
    runs = await db[D.SCRAPE_RUNS].find(
        projection={"_id": 0, "source": 1, "job": 1, "status": 1,
                    "started_at": 1, "parsed": 1, "errors": 1},
    ).sort("started_at", -1).limit(15).to_list(length=15)
    return {
        "counts": counts,
        "recent_runs": jsonable(runs),
        "server": await server_info(),
    }


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
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    radius_km: float | None = Query(None, gt=0, le=100),
    sort: str = Query("relevance"),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.api_page_size, ge=1, le=settings.api_max_page_size),
) -> Page:
    query = PropertySearchQuery(
        q=q, city=city, sector=sector, locality=locality, micro_market=micro_market,
        builder=builder, project=project, listing_type=listing_type,
        property_type=property_type or [], configuration=configuration,
        bedrooms_min=bedrooms_min, bedrooms_max=bedrooms_max,
        price_min=price_min, price_max=price_max,
        area_min=area_min, area_max=area_max,
        possession_status=possession_status or [], segment=segment or [],
        amenities=amenities or [], keywords=keywords or [],
        is_commercial=is_commercial, has_rera=has_rera,
        min_investment_score=min_investment_score, max_risk_score=max_risk_score,
        near_lat=near_lat, near_lng=near_lng, radius_km=radius_km,
        sort=sort, page=page, page_size=page_size,
    )
    results, total = await search_properties(db, query)

    # Cache-miss → queue a scrape. Never blocks: the caller gets whatever the
    # warehouse already has, plus a note that a backfill is in flight.
    backfill = None
    if page == 1:
        decision = await DemandFiller(db).consider(query, total)
        if decision.task_id:
            backfill = decision.as_dict()

    return Page(
        total=total, page=page, page_size=query.limit,
        pages=(total + query.limit - 1) // query.limit, results=results,
        backfill=backfill,
    )


@app.get("/properties/facets", tags=["properties"])
async def property_facets(
    db: DB,
    q: str | None = None,
    city: str | None = None,
    listing_type: str | None = None,
) -> dict[str, Any]:
    return await facets(db, PropertySearchQuery(q=q, city=city, listing_type=listing_type))


@app.get("/properties/{property_id}", tags=["properties"])
async def property_detail(property_id: str, db: DB) -> dict[str, Any]:
    document = await db[D.PROPERTIES].find_one({"_id": property_id})
    if document is None:
        raise HTTPException(status_code=404, detail="property not found")

    record = jsonable(document)
    record["id"] = record.pop("_id")

    history = await db[D.PRICE_HISTORY].find(
        {"meta.property_id": property_id},
        projection={"_id": 0, "observed_at": 1, "price": 1, "price_per_sqft": 1,
                    "rent_monthly": 1, "change_pct": 1},
    ).sort("observed_at", -1).limit(50).to_list(length=50)
    record["price_history"] = jsonable(history)

    duplicates = await db[D.PROPERTY_DUPLICATES].aggregate([
        {"$match": {"canonical_id": property_id}},
        {"$lookup": {
            "from": D.PROPERTIES, "localField": "duplicate_id", "foreignField": "_id",
            "pipeline": [{"$project": {"source": 1, "listing_url": 1, "price": 1}}],
            "as": "listing",
        }},
        {"$unwind": "$listing"},
        {"$project": {"_id": 0, "id": "$listing._id", "source": "$listing.source",
                      "listing_url": "$listing.listing_url", "price": "$listing.price",
                      "score": 1, "reason": 1}},
    ]).to_list(length=20)
    record["duplicate_listings"] = jsonable(duplicates)
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
    import re

    conditions: dict[str, Any] = {}
    if q:
        conditions["name"] = {"$regex": re.escape(q), "$options": "i"}
    if min_trust is not None:
        conditions["trust_score"] = {"$gte": min_trust}

    pipeline: list[dict[str, Any]] = [
        {"$match": conditions} if conditions else {"$match": {}},
        {"$lookup": {
            "from": D.MV_BUILDER_SCORECARD, "localField": "_id",
            "foreignField": "_id", "as": "_card",
        }},
        {"$addFields": {"card": {"$first": "$_card"}}},
        {"$project": {
            "name": 1, "normalized_name": 1, "description": 1, "website": 1,
            "established_year": 1, "rating": 1, "rating_count": 1,
            "total_projects": 1, "completed_projects": 1, "ongoing_projects": 1,
            "trust_score": 1, "risk_score": 1, "sentiment": 1, "reputation_summary": 1,
            "reddit_mentions": "$card.reddit_mentions",
            "reddit_positive": "$card.reddit_positive",
            "reddit_negative": "$card.reddit_negative",
            "listing_count": "$card.listing_count",
            "avg_price_per_sqft": "$card.avg_price_per_sqft",
        }},
        {"$sort": {"trust_score": -1, "total_projects": -1}},
        {"$facet": {
            "results": [{"$skip": (page - 1) * page_size}, {"$limit": page_size}],
            "total": [{"$count": "value"}],
        }},
    ]
    payload = await db[D.BUILDERS].aggregate(pipeline).to_list(length=1)
    block = payload[0] if payload else {"results": [], "total": []}
    results = [{**jsonable(r), "id": r["_id"]} for r in block["results"]]
    total = int(block["total"][0]["value"]) if block["total"] else 0

    return Page(
        total=total, page=page, page_size=page_size,
        pages=(total + page_size - 1) // page_size, results=results,
    )


@app.get("/builders/{builder_id}", tags=["builders"])
async def builder_detail(builder_id: str, db: DB) -> dict[str, Any]:
    document = await db[D.BUILDERS].find_one({"_id": builder_id})
    if document is None:
        raise HTTPException(status_code=404, detail="builder not found")

    record = jsonable(document)
    record["id"] = record.pop("_id")

    projects = await db[D.PROJECTS].find(
        {"builder_id": builder_id},
        projection={"name": 1, "status": 1, "possession_date": 1,
                    "price_min": 1, "price_max": 1, "rera_number": 1},
    ).sort("possession_date", -1).limit(100).to_list(length=100)
    record["projects"] = jsonable(projects)

    chatter = await db[D.REDDIT_POSTS].find(
        {"detected_builders": record["name"]},
        projection={"source_id": 1, "permalink": 1, "title": 1, "score": 1,
                    "sentiment": 1, "topics": 1, "created_utc": 1, "summary": 1},
    ).sort("created_utc", -1).limit(40).to_list(length=40)
    record["public_discussion"] = jsonable(chatter)
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
    import re

    conditions: dict[str, Any] = {}
    if q:
        conditions["name"] = {"$regex": re.escape(q), "$options": "i"}
    if city:
        conditions["city"] = city
    if status:
        conditions["status"] = status

    total = await db[D.PROJECTS].count_documents(conditions)
    rows = await db[D.PROJECTS].find(
        conditions,
        projection={"name": 1, "builder_name": 1, "status": 1, "launch_date": 1,
                    "possession_date": 1, "price_min": 1, "price_max": 1,
                    "price_per_sqft": 1, "total_units": 1, "rera_number": 1,
                    "amenities": 1, "investment_score": 1, "risk_score": 1,
                    "city": 1, "sector": 1, "micro_market": 1},
    ).sort([("launch_date", -1), ("created_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)

    results = [{**jsonable(r), "id": r["_id"]} for r in rows]
    return Page(
        total=total, page=page, page_size=page_size,
        pages=(total + page_size - 1) // page_size, results=results,
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
        q=q, builder=builder, project=project, sector=sector, city=city,
        topics=topics or [], sentiment=sentiment, min_score=min_score,
        subreddit=subreddit, page=page, page_size=page_size,
    )
    results, total = await search_reddit(db, query)
    return Page(
        total=total, page=page, page_size=query.limit,
        pages=(total + query.limit - 1) // query.limit, results=results,
    )


@app.get("/reddit/{post_source_id}/comments", tags=["reddit"])
async def reddit_comments(post_source_id: str, db: DB) -> list[dict[str, Any]]:
    rows = await db[D.REDDIT_COMMENTS].find(
        {"post_source_id": post_source_id},
        projection={"_id": 0, "comment_id": 1, "author": 1, "body": 1, "score": 1,
                    "depth": 1, "sentiment": 1, "topics": 1, "created_utc": 1,
                    "permalink": 1},
    ).sort("score", -1).limit(200).to_list(length=200)
    return jsonable(rows)


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
    from datetime import UTC, datetime, timedelta

    conditions: dict[str, Any] = {
        "listing_type": listing_type,
        "period": {"$gte": datetime.now(UTC) - timedelta(days=months * 31)},
    }
    if city:
        conditions["city"] = city
    if sector:
        import re

        conditions["sector"] = {"$regex": re.escape(sector), "$options": "i"}

    rows = await db[D.MV_LOCALITY_TRENDS].find(
        conditions, projection={"_id": 0}
    ).sort([("period", -1), ("listing_count", -1)]).limit(500).to_list(length=500)
    return jsonable(rows)


@app.get("/market/yield", tags=["market"])
async def rental_yield(
    db: DB, city: str | None = None, bedrooms: int | None = None
) -> list[dict[str, Any]]:
    conditions: dict[str, Any] = {}
    if city:
        conditions["city"] = city
    if bedrooms is not None:
        conditions["bedrooms"] = bedrooms
    rows = await db[D.MV_RENTAL_YIELD].find(
        conditions, projection={"_id": 0}
    ).sort("rental_yield_pct", -1).limit(300).to_list(length=300)
    return jsonable(rows)


@app.get("/market/supply-demand", tags=["market"])
async def supply_demand(db: DB, city: str | None = None) -> list[dict[str, Any]]:
    conditions = {"city": city} if city else {}
    rows = await db[D.MV_SUPPLY_DEMAND].find(
        conditions, projection={"_id": 0}
    ).sort("active_supply", -1).limit(300).to_list(length=300)
    return jsonable(rows)


@app.get("/market/insights", tags=["market"])
async def market_insights(
    db: DB,
    metric: str | None = None,
    city: str | None = None,
    sector: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    conditions: dict[str, Any] = {}
    if metric:
        conditions["metric"] = metric
    if city:
        conditions["city"] = city
    if sector:
        import re

        conditions["sector"] = {"$regex": re.escape(sector), "$options": "i"}

    rows = await db[D.MARKET_INSIGHTS].find(
        conditions,
        projection={"_id": 0, "metric": 1, "city": 1, "sector": 1, "period_start": 1,
                    "period_end": 1, "value": 1, "unit": 1, "change_pct": 1,
                    "sample_size": 1, "notes": 1},
    ).sort("period_end", -1).limit(limit).to_list(length=limit)
    return jsonable(rows)


@app.get("/market/new-launches", tags=["market"])
async def new_launches(db: DB, days: int = Query(90, ge=1, le=365)) -> list[dict[str, Any]]:
    from homz.etl.price_history import new_launch_feed

    return await new_launch_feed(db, days=days)


# ---------------------------------------------------------------------------
# ingest — client-side scraping submits its results here
#
# This is the only write surface on the API. It is token-authenticated,
# rate-limited and size-bounded, and every payload is run through the same
# parser the server-side scrapers use, so client-sourced data is validated
# rather than trusted.
# ---------------------------------------------------------------------------


class IngestPagePayload(BaseModel):
    source: str = Field(description="magicbricks | housing | squareyards")
    url: str = Field(description="Absolute URL the HTML was captured from")
    html: str = Field(description="Raw page HTML")
    kind: str = Field("auto", description="auto | property | project")
    task_id: str | None = Field(None, description="Fill task this satisfies, if any")


class IngestRecordsPayload(BaseModel):
    records: list[dict[str, Any]] = Field(
        description="Normalized records; each needs a record_type of "
                    "property | project | reddit_post"
    )
    task_id: str | None = None


def _auth(authorization: str | None) -> str:
    try:
        client = verify_token(authorization)
        check_rate_limit(client)
        return client
    except IngestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/ingest/page", tags=["ingest"])
async def ingest_page(
    db: DB,
    payload: IngestPagePayload,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Submit one scraped page. Parsed server-side, then upserted."""
    client = _auth(authorization)
    service = IngestService(db)
    try:
        result = await service.ingest_page(
            source=payload.source, url=payload.url, html=payload.html,
            client=client, kind=payload.kind, task_id=payload.task_id,
        )
    except IngestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    # The service settles payload.task_id itself — see IngestService._close_task.
    return result.as_dict()


@app.post("/ingest/records", tags=["ingest"])
async def ingest_records(
    db: DB,
    payload: IngestRecordsPayload,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Submit already-normalized records (trusted internal workers)."""
    client = _auth(authorization)
    try:
        result = await IngestService(db).ingest_records(
            payload.records, client=client, task_id=payload.task_id
        )
    except IngestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return result.as_dict()


# ---------------------------------------------------------------------------
# fill tasks — what the client should scrape next
# ---------------------------------------------------------------------------


@app.get("/ingest/tasks", tags=["ingest"])
async def claim_tasks(
    db: DB,
    worker: str = Query("client", max_length=64, description="Worker identity"),
    limit: int = Query(1, ge=1, le=25),
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Claim pending fill tasks.

    Claiming is atomic, so several clients can poll concurrently without two
    of them taking the same task. A task claimed but not completed within 30
    minutes returns to the pool.
    """
    _auth(authorization)
    tasks = await DemandFiller(db).claim(worker=worker, limit=limit)
    return {"tasks": jsonable(tasks), "count": len(tasks)}


@app.post("/ingest/tasks/{task_id}/complete", tags=["ingest"])
async def complete_task(
    db: DB,
    task_id: str,
    records_written: int = Body(0, embed=True),
    error: str | None = Body(None, embed=True),
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Report a task finished (or failed, so it can be retried sooner)."""
    _auth(authorization)
    found = await DemandFiller(db).complete(
        task_id, records_written=records_written, error=error
    )
    if not found:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task_id": task_id}


@app.get("/ingest/stats", tags=["ingest"])
async def ingest_stats(db: DB) -> dict[str, Any]:
    """Queue depth and remaining daily crawl budget. Unauthenticated: it
    exposes no data, only counters, and is useful for monitoring."""
    return await DemandFiller(db).stats()


# ---------------------------------------------------------------------------
# static widget hosting (dev convenience; use a CDN in production)
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
if settings.api_serve_web and _WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
    log.info("api.serving_web", directory=str(_WEB_DIR))
