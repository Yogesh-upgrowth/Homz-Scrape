"""SQLAlchemy 2.0 models mirroring sql/001_schema.sql.

The SQL file is the source of truth for DDL (triggers, generated columns and
materialized views have no clean ORM expression). These models exist so the
repository can build type-safe INSERT ... ON CONFLICT statements and the API
can query without hand-writing every SELECT.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pg_enum(name: str, *values: str) -> ENUM:
    # create_type=False: the enums are created by 001_schema.sql.
    return ENUM(*values, name=name, create_type=False, validate_strings=False)


SourceEnum = _pg_enum("source_enum", "magicbricks", "housing", "squareyards", "reddit")
ListingTypeEnum = _pg_enum(
    "listing_type_enum", "sale", "rent", "resale", "new_launch", "project",
    "commercial", "pg", "unknown",
)
PropertyTypeEnum = _pg_enum(
    "property_type_enum", "apartment", "builder_floor", "independent_house", "villa",
    "plot", "penthouse", "studio", "office", "retail_shop", "showroom", "warehouse",
    "co_working", "farmhouse", "serviced_apartment", "other",
)
PossessionStatusEnum = _pg_enum(
    "possession_status_enum", "ready_to_move", "under_construction", "new_launch",
    "upcoming", "completed", "unknown",
)
SegmentEnum = _pg_enum(
    "segment_enum", "affordable", "mid", "premium", "luxury", "ultra_luxury", "unknown"
)
SellerTypeEnum = _pg_enum("seller_type_enum", "owner", "agent", "builder", "unknown")
SentimentEnum = _pg_enum("sentiment_enum", "positive", "negative", "neutral", "mixed")
CityEnum = _pg_enum(
    "city_enum", "gurgaon", "noida", "greater_noida", "delhi", "faridabad",
    "ghaziabad", "sohna", "other_ncr", "unknown",
)
JobStatusEnum = _pg_enum("job_status_enum", "running", "success", "partial", "failed", "blocked")

_ts = DateTime(timezone=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _ts, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------


class Location(Base, TimestampMixin):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    city: Mapped[str] = mapped_column(CityEnum, default="unknown")
    state: Mapped[str | None] = mapped_column(Text)
    locality: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)
    sub_locality: Mapped[str | None] = mapped_column(Text)
    micro_market: Mapped[str | None] = mapped_column(Text)
    pincode: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]

    avg_price_per_sqft: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    avg_rent_per_month: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    listing_count: Mapped[int] = mapped_column(Integer, default=0)
    rental_yield_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    location_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Builder(Base, TimestampMixin):
    __tablename__ = "builders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(SourceEnum)
    source_id: Mapped[str] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)

    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    established_year: Mapped[int | None] = mapped_column(SmallInteger)
    headquarters: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)

    total_projects: Mapped[int | None] = mapped_column(Integer)
    ongoing_projects: Mapped[int | None] = mapped_column(Integer)
    completed_projects: Mapped[int | None] = mapped_column(Integer)
    upcoming_projects: Mapped[int | None] = mapped_column(Integer)

    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    rating_count: Mapped[int | None] = mapped_column(Integer)
    review_count: Mapped[int | None] = mapped_column(Integer)
    reviews: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    cities: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)

    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)

    trust_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    sentiment: Mapped[str | None] = mapped_column(SentimentEnum)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    reputation_summary: Mapped[str | None] = mapped_column(Text)
    enriched_at: Mapped[datetime | None] = mapped_column(_ts)

    raw_html_key: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scraped_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(SourceEnum)
    source_id: Mapped[str] = mapped_column(Text)
    project_url: Mapped[str] = mapped_column(Text)

    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    builder_id: Mapped[int | None] = mapped_column(ForeignKey("builders.id", ondelete="SET NULL"))
    builder_name: Mapped[str | None] = mapped_column(Text)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))

    status: Mapped[str] = mapped_column(PossessionStatusEnum, default="unknown")
    launch_date: Mapped[date | None] = mapped_column(Date)
    possession_date: Mapped[date | None] = mapped_column(Date)
    rera_number: Mapped[str | None] = mapped_column(Text)

    price_min: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    price_per_sqft: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_units: Mapped[int | None] = mapped_column(Integer)
    total_towers: Mapped[int | None] = mapped_column(Integer)
    project_area_acres: Mapped[float | None]

    configurations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    amenities: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    specifications: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    landmarks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    construction_updates: Mapped[list[str]] = mapped_column(JSONB, default=list)
    description: Mapped[str | None] = mapped_column(Text)

    investment_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    enriched_at: Mapped[datetime | None] = mapped_column(_ts)

    raw_html_key: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scraped_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())

    builder: Mapped[Builder | None] = relationship(lazy="noload")


class Property(Base, TimestampMixin):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(SourceEnum)
    source_id: Mapped[str] = mapped_column(Text)
    listing_url: Mapped[str] = mapped_column(Text)

    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    project_name: Mapped[str | None] = mapped_column(Text)
    builder_id: Mapped[int | None] = mapped_column(ForeignKey("builders.id", ondelete="SET NULL"))
    builder_name: Mapped[str | None] = mapped_column(Text)
    developer_name: Mapped[str | None] = mapped_column(Text)
    society_name: Mapped[str | None] = mapped_column(Text)

    listing_type: Mapped[str] = mapped_column(ListingTypeEnum, default="unknown")
    property_type: Mapped[str] = mapped_column(PropertyTypeEnum, default="other")
    property_type_raw: Mapped[str | None] = mapped_column(Text)
    segment: Mapped[str] = mapped_column(SegmentEnum, default="unknown")
    is_commercial: Mapped[bool] = mapped_column(Boolean, default=False)
    is_luxury: Mapped[bool] = mapped_column(Boolean, default=False)
    is_affordable: Mapped[bool] = mapped_column(Boolean, default=False)

    configuration: Mapped[str | None] = mapped_column(Text)
    bedrooms: Mapped[int | None] = mapped_column(SmallInteger)
    bathrooms: Mapped[int | None] = mapped_column(SmallInteger)
    balconies: Mapped[int | None] = mapped_column(SmallInteger)
    floor_number: Mapped[int | None] = mapped_column(SmallInteger)
    total_floors: Mapped[int | None] = mapped_column(SmallInteger)
    facing: Mapped[str | None] = mapped_column(Text)
    furnishing: Mapped[str | None] = mapped_column(Text)
    age_years: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))

    price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    price_display: Mapped[str | None] = mapped_column(Text)
    price_per_sqft: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    booking_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    maintenance_charge: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rent_monthly: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    security_deposit: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    is_price_on_request: Mapped[bool] = mapped_column(Boolean, default=False)

    area_value: Mapped[float | None]
    area_unit: Mapped[str | None] = mapped_column(Text)
    area_sqft: Mapped[float | None]
    carpet_area_sqft: Mapped[float | None]
    built_up_area_sqft: Mapped[float | None]
    super_built_up_area_sqft: Mapped[float | None]
    plot_area_sqft: Mapped[float | None]

    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    location_raw: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str] = mapped_column(CityEnum, default="unknown")
    sector: Mapped[str | None] = mapped_column(Text)
    locality: Mapped[str | None] = mapped_column(Text)
    micro_market: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]

    possession_status: Mapped[str] = mapped_column(PossessionStatusEnum, default="unknown")
    possession_date: Mapped[date | None] = mapped_column(Date)
    possession_raw: Mapped[str | None] = mapped_column(Text)
    rera_number: Mapped[str | None] = mapped_column(Text)
    rera_status: Mapped[str | None] = mapped_column(Text)
    total_units: Mapped[int | None] = mapped_column(Integer)
    project_area_acres: Mapped[float | None]
    launch_date: Mapped[date | None] = mapped_column(Date)

    amenities: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    specifications: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    unit_configurations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    landmarks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_seller_type: Mapped[str] = mapped_column(SellerTypeEnum, default="unknown")
    contact_company: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)

    listed_at: Mapped[datetime | None] = mapped_column(_ts)
    listing_date_raw: Mapped[str | None] = mapped_column(Text)
    updated_at_source: Mapped[datetime | None] = mapped_column(_ts)
    scraped_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    first_seen_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    delisted_at: Mapped[datetime | None] = mapped_column(_ts)

    content_hash: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    canonical_property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", ondelete="SET NULL")
    )
    duplicate_count: Mapped[int] = mapped_column(SmallInteger, default=0)

    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    investment_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    location_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    builder_trust_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    enriched_at: Mapped[datetime | None] = mapped_column(_ts)
    enrichment_version: Mapped[int] = mapped_column(SmallInteger, default=0)

    raw_html_key: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class PropertyImage(Base):
    __tablename__ = "property_images"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE")
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE")
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    price_per_sqft: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rent_monthly: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    source: Mapped[str] = mapped_column(SourceEnum)


class RedditPost(Base, TimestampMixin):
    __tablename__ = "reddit_posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, unique=True)
    subreddit: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    permalink: Mapped[str] = mapped_column(Text)

    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    created_utc: Mapped[datetime | None] = mapped_column(_ts)
    score: Mapped[int] = mapped_column(Integer, default=0)
    upvote_ratio: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    num_comments: Mapped[int] = mapped_column(Integer, default=0)
    flair: Mapped[str | None] = mapped_column(Text)
    is_self: Mapped[bool] = mapped_column(Boolean, default=True)
    over_18: Mapped[bool] = mapped_column(Boolean, default=False)

    sentiment: Mapped[str | None] = mapped_column(SentimentEnum)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    detected_builders: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    detected_projects: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    detected_sectors: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    detected_city: Mapped[str] = mapped_column(CityEnum, default="unknown")
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    enriched_at: Mapped[datetime | None] = mapped_column(_ts)

    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scraped_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())


class RedditCommentRow(Base):
    __tablename__ = "reddit_comments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    comment_id: Mapped[str] = mapped_column(Text, unique=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("reddit_posts.id", ondelete="CASCADE"))
    post_source_id: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_submitter: Mapped[bool] = mapped_column(Boolean, default=False)
    created_utc: Mapped[datetime | None] = mapped_column(_ts)
    permalink: Mapped[str | None] = mapped_column(Text)

    sentiment: Mapped[str | None] = mapped_column(SentimentEnum)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    detected_builders: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    detected_projects: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    detected_sectors: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    enriched_at: Mapped[datetime | None] = mapped_column(_ts)

    created_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())


class MarketInsight(Base):
    __tablename__ = "market_insights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(SourceEnum)
    source_id: Mapped[str] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(CityEnum, default="unknown")
    locality: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    property_type: Mapped[str | None] = mapped_column(PropertyTypeEnum)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit: Mapped[str | None] = mapped_column(Text)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    sample_size: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scraped_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), server_default=func.gen_random_uuid())
    source: Mapped[str] = mapped_column(Text)
    job: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(JobStatusEnum, default="running")
    started_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(_ts)
    duration_s: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    discovered: Mapped[int] = mapped_column(Integer, default=0)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    parsed: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped_known: Mapped[int] = mapped_column(Integer, default=0)
    skipped_robots: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class PropertyDuplicate(Base):
    __tablename__ = "property_duplicates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"))
    duplicate_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"))
    score: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    reason: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())


class EnrichmentQueue(Base):
    __tablename__ = "enrichment_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    priority: Mapped[int] = mapped_column(SmallInteger, default=5)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[str | None] = mapped_column(Text)
    enqueued_at: Mapped[datetime] = mapped_column(_ts, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(_ts)
