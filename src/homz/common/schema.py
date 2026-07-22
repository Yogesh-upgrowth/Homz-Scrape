"""The normalized schema — the contract every scraper must satisfy.

A source-specific parser's only job is to turn one page into one (or many) of
these objects. Nothing downstream (ETL, enrichment, search) ever imports a
source-specific type.

Design rules:
  * every field is optional except the identity triple
    (`source`, `source_id`, `listing_url`) — real listings are messy and a
    half-filled record still has value;
  * money is stored in INR as `Decimal`, never float;
  * area is normalized to sqft with the original value/unit preserved;
  * `raw` keeps the untouched source payload for debugging and re-parsing.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from homz.common.enums import (
    AreaUnit,
    City,
    ListingType,
    PossessionStatus,
    PropertyType,
    Segment,
    SellerType,
    Sentiment,
    Source,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HomzModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=False,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


# ---------------------------------------------------------------------------
# Sub-documents
# ---------------------------------------------------------------------------


class GeoPoint(HomzModel):
    latitude: float
    longitude: float

    @field_validator("latitude")
    @classmethod
    def _lat_range(cls, v: float) -> float:
        if not -90 <= v <= 90:
            raise ValueError(f"latitude out of range: {v}")
        return v

    @field_validator("longitude")
    @classmethod
    def _lon_range(cls, v: float) -> float:
        if not -180 <= v <= 180:
            raise ValueError(f"longitude out of range: {v}")
        return v


class Landmark(HomzModel):
    """A nearby POI. `category` is one of metro/school/hospital/mall/... ."""

    category: str
    name: str
    distance_km: float | None = None
    raw_distance: str | None = None


class Image(HomzModel):
    url: str
    caption: str | None = None
    is_primary: bool = False
    width: int | None = None
    height: int | None = None


class UnitConfiguration(HomzModel):
    """One row of a project's price/config table."""

    configuration: str | None = None  # "3 BHK"
    bedrooms: int | None = None
    bathrooms: int | None = None
    area_sqft: float | None = None
    carpet_area_sqft: float | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    price_display: str | None = None


class ContactInfo(HomzModel):
    """Only publicly published business contact details are ever stored."""

    name: str | None = None
    seller_type: SellerType = SellerType.UNKNOWN
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    profile_url: str | None = None


class Location(HomzModel):
    raw: str | None = None
    locality: str | None = None
    sector: str | None = None  # "Sector 82", "Sector 150"
    sub_locality: str | None = None
    city: City = City.UNKNOWN
    city_raw: str | None = None
    state: str | None = "Haryana"
    pincode: str | None = None
    micro_market: str | None = None  # "Dwarka Expressway", "Golf Course Road"
    geo: GeoPoint | None = None

    def slug(self) -> str:
        parts = [p for p in (self.city.value, self.sector or self.locality) if p]
        return "-".join(p.lower().replace(" ", "-") for p in parts) or "unknown"


# ---------------------------------------------------------------------------
# Core records
# ---------------------------------------------------------------------------


class PropertyRecord(HomzModel):
    """A single listing or project page, normalized."""

    # --- identity ---
    source: Source
    source_id: str = Field(description="Stable id within the source (listing/project id)")
    listing_url: str

    # --- descriptive ---
    title: str | None = None
    description: str | None = None
    project_name: str | None = None
    builder_name: str | None = None
    developer_name: str | None = None
    society_name: str | None = None

    # --- classification ---
    listing_type: ListingType = ListingType.UNKNOWN
    property_type: PropertyType = PropertyType.OTHER
    property_type_raw: str | None = None
    segment: Segment = Segment.UNKNOWN
    is_commercial: bool = False
    is_luxury: bool = False
    is_affordable: bool = False

    # --- configuration ---
    configuration: str | None = None  # "3 BHK", "2 BHK + Study"
    bedrooms: int | None = None
    bathrooms: int | None = None
    balconies: int | None = None
    floor_number: int | None = None
    total_floors: int | None = None
    facing: str | None = None
    furnishing: str | None = None
    age_years: float | None = None

    # --- money (INR) ---
    price: Decimal | None = None
    price_max: Decimal | None = None
    price_display: str | None = None
    price_per_sqft: Decimal | None = None
    booking_amount: Decimal | None = None
    maintenance_charge: Decimal | None = None
    rent_monthly: Decimal | None = None
    security_deposit: Decimal | None = None
    is_price_on_request: bool = False

    # --- area ---
    area_value: float | None = None
    area_unit: AreaUnit | None = None
    area_sqft: float | None = None
    carpet_area_sqft: float | None = None
    built_up_area_sqft: float | None = None
    super_built_up_area_sqft: float | None = None
    plot_area_sqft: float | None = None

    # --- location ---
    location: Location = Field(default_factory=Location)

    # --- project / status ---
    possession_status: PossessionStatus = PossessionStatus.UNKNOWN
    possession_date: date | None = None
    possession_raw: str | None = None
    rera_number: str | None = None
    rera_status: str | None = None
    total_units: int | None = None
    project_area_acres: float | None = None
    launch_date: date | None = None

    # --- rich sub-documents ---
    amenities: list[str] = Field(default_factory=list)
    specifications: dict[str, str] = Field(default_factory=dict)
    unit_configurations: list[UnitConfiguration] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)
    landmarks: list[Landmark] = Field(default_factory=list)
    contact: ContactInfo | None = None

    # --- provenance ---
    listed_at: datetime | None = None
    listing_date_raw: str | None = None
    updated_at_source: datetime | None = None
    scraped_at: datetime = Field(default_factory=_utcnow)
    raw_html_key: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    # --- derived (filled by ETL/enrichment, never by a parser) ---
    content_hash: str | None = None
    dedupe_key: str | None = None

    # -- helpers ------------------------------------------------------------

    @property
    def natural_key(self) -> str:
        return f"{self.source.value}:{self.source_id}"

    def compute_content_hash(self) -> str:
        """Hash of the volatile business fields.

        Two scrapes of the same listing hash identically unless something we
        care about changed — that is what drives incremental skip and the
        price-history trigger.
        """
        parts = [
            self.title or "",
            str(self.price or ""),
            str(self.price_per_sqft or ""),
            str(self.rent_monthly or ""),
            str(self.area_sqft or ""),
            self.configuration or "",
            self.possession_status.value,
            self.possession_raw or "",
            str(len(self.images)),
            ",".join(sorted(self.amenities)),
            (self.description or "")[:512],
        ]
        return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()

    def compute_dedupe_key(self) -> str:
        """Cross-source fuzzy identity.

        Same project + same configuration + same area bucket + same price
        bucket is almost certainly the same unit re-listed on another portal.
        """
        area_bucket = int(self.area_sqft // 25) if self.area_sqft else -1
        price_bucket = int(self.price // 100_000) if self.price else -1
        parts = [
            (self.project_name or self.society_name or self.title or "").lower().strip(),
            (self.location.sector or self.location.locality or "").lower().strip(),
            self.location.city.value,
            (self.configuration or "").lower().replace(" ", ""),
            str(area_bucket),
            str(price_bucket),
            self.listing_type.value,
        ]
        return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()

    def finalize(self) -> PropertyRecord:
        """Fill derived fields. Call once, at the end of parsing."""
        self.content_hash = self.compute_content_hash()
        self.dedupe_key = self.compute_dedupe_key()
        return self


class BuilderRecord(HomzModel):
    source: Source
    source_id: str
    profile_url: str | None = None

    name: str
    normalized_name: str | None = None
    description: str | None = None
    established_year: int | None = None
    headquarters: str | None = None
    website: str | None = None

    total_projects: int | None = None
    ongoing_projects: int | None = None
    completed_projects: int | None = None
    upcoming_projects: int | None = None
    project_names: list[str] = Field(default_factory=list)

    rating: float | None = None
    rating_count: int | None = None
    review_count: int | None = None
    reviews: list[dict[str, Any]] = Field(default_factory=list)

    contact: ContactInfo | None = None
    cities: list[str] = Field(default_factory=list)

    scraped_at: datetime = Field(default_factory=_utcnow)
    raw_html_key: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def natural_key(self) -> str:
        return f"{self.source.value}:{self.source_id}"


class ProjectRecord(HomzModel):
    """A builder project (distinct from an individual unit listing)."""

    source: Source
    source_id: str
    project_url: str

    name: str
    builder_name: str | None = None
    location: Location = Field(default_factory=Location)

    status: PossessionStatus = PossessionStatus.UNKNOWN
    launch_date: date | None = None
    possession_date: date | None = None
    rera_number: str | None = None

    price_min: Decimal | None = None
    price_max: Decimal | None = None
    price_per_sqft: Decimal | None = None
    total_units: int | None = None
    total_towers: int | None = None
    project_area_acres: float | None = None

    configurations: list[UnitConfiguration] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    specifications: dict[str, str] = Field(default_factory=dict)
    images: list[Image] = Field(default_factory=list)
    landmarks: list[Landmark] = Field(default_factory=list)
    construction_updates: list[str] = Field(default_factory=list)
    description: str | None = None

    scraped_at: datetime = Field(default_factory=_utcnow)
    raw_html_key: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def natural_key(self) -> str:
        return f"{self.source.value}:{self.source_id}"


class RedditComment(HomzModel):
    comment_id: str
    post_id: str
    parent_id: str | None = None
    author: str | None = None
    body: str | None = None
    score: int = 0
    created_utc: datetime | None = None
    permalink: str | None = None
    depth: int = 0
    is_submitter: bool = False

    # enrichment
    sentiment: Sentiment | None = None
    sentiment_score: float | None = None
    detected_builders: list[str] = Field(default_factory=list)
    detected_projects: list[str] = Field(default_factory=list)
    detected_sectors: list[str] = Field(default_factory=list)
    detected_city: City | None = None
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class RedditPostRecord(HomzModel):
    source: Source = Source.REDDIT
    source_id: str  # reddit fullname without prefix, e.g. "1abc2de"
    subreddit: str
    url: str
    permalink: str

    title: str
    body: str | None = None
    author: str | None = None
    created_utc: datetime | None = None
    score: int = 0
    upvote_ratio: float | None = None
    num_comments: int = 0
    flair: str | None = None
    is_self: bool = True
    over_18: bool = False

    comments: list[RedditComment] = Field(default_factory=list)

    # enrichment
    sentiment: Sentiment | None = None
    sentiment_score: float | None = None
    detected_builders: list[str] = Field(default_factory=list)
    detected_projects: list[str] = Field(default_factory=list)
    detected_sectors: list[str] = Field(default_factory=list)
    detected_city: City | None = None
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    summary: str | None = None
    relevance_score: float | None = None

    scraped_at: datetime = Field(default_factory=_utcnow)
    raw_html_key: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def natural_key(self) -> str:
        return f"reddit:{self.source_id}"

    def full_text(self, max_chars: int = 12_000) -> str:
        chunks = [self.title, self.body or ""]
        for c in self.comments[:25]:
            if c.body:
                chunks.append(f"[comment score={c.score}] {c.body}")
        return "\n\n".join(chunks)[:max_chars]


class MarketInsightRecord(HomzModel):
    """A single observation of a market metric for a locality/period."""

    source: Source
    source_id: str
    metric: str
    city: City = City.UNKNOWN
    locality: str | None = None
    sector: str | None = None
    property_type: PropertyType | None = None
    period_start: date | None = None
    period_end: date | None = None
    value: Decimal | None = None
    unit: str | None = None
    change_pct: float | None = None
    sample_size: int | None = None
    source_url: str | None = None
    notes: str | None = None
    scraped_at: datetime = Field(default_factory=_utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def natural_key(self) -> str:
        return f"{self.source.value}:{self.source_id}"


# Anything a scraper is allowed to yield.
ScrapedRecord = (
    PropertyRecord | ProjectRecord | BuilderRecord | RedditPostRecord | MarketInsightRecord
)
