"""Category listing feeds — Sale / Rent / Pg / Commercial, per city.

Mirrors `feed.py`'s proven pattern (static export, no Mongo at request time)
but for the `properties` collection instead of `projects`: individual
listings, not builder-project pages. Categories map onto `ListingType`
values rather than the price-derived `is_commercial` flag, per a deliberate
product decision — "Commercial" is exactly `listing_type == "commercial"`,
and "Sale" is one dataset covering sale/resale/new_launch/project, with
Resale-vs-New-Launch left as a client-side filter (see `listingType` on each
record) rather than a separate top-level dataset.

Property-type and Resale/New-Launch filtering are both meant to happen
client-side over one pre-fetched category+city file — that is why every
record carries raw `priceValue`/`areaValue`/`bedrooms`/`propertyType`/
`listingType` fields alongside the display strings the legacy Projects feed
already has, not just formatted text.
"""

from __future__ import annotations

from typing import Any

from homz.common.enums import ListingType
from homz.common.schema import PropertyRecord
from homz.services.feed import (
    _STATUS_LABELS,
    CITY_KEYS,
    _about,
    _amenities,
    _iso,
    _landmarks,
    _location,
    _split_images,
)

CATEGORY_LISTING_TYPES: dict[str, frozenset[ListingType]] = {
    "Sale": frozenset(
        {ListingType.SALE, ListingType.RESALE, ListingType.NEW_LAUNCH, ListingType.PROJECT}
    ),
    "Rent": frozenset({ListingType.RENT}),
    "Pg": frozenset({ListingType.PG}),
    "Commercial": frozenset({ListingType.COMMERCIAL}),
}


def category_of(listing_type: ListingType) -> str | None:
    for name, types in CATEGORY_LISTING_TYPES.items():
        if listing_type in types:
            return name
    return None  # ListingType.UNKNOWN — withheld, not silently bucketed


def segment_name(city_key: str, category: str) -> str:
    """("ggn", "Rent") -> "ggnRentProperties"."""
    return f"{city_key}{category}Properties"


def all_segments() -> list[str]:
    return [segment_name(k, c) for k in CITY_KEYS.values() for c in CATEGORY_LISTING_TYPES]


def is_publishable(record: PropertyRecord) -> bool:
    """Same rationale as `feed.py`'s guard: skip cards with nothing to show."""
    return bool(
        record.price is not None
        or record.rent_monthly is not None
        or record.configuration
        or record.amenities
    )


def _possession(record: PropertyRecord) -> str:
    if record.possession_date:
        return record.possession_date.strftime("%b %Y")
    return _STATUS_LABELS.get(record.possession_status, "")


def _price_display(record: PropertyRecord) -> str:
    from homz.common.parsing import format_price_inr

    if record.listing_type is ListingType.RENT:
        if record.rent_monthly is None:
            return "Price on Request"
        return f"{format_price_inr(record.rent_monthly)}/month"
    if record.price is None and record.price_max is None:
        return "Price on Request"
    if record.price is not None and record.price_max not in (None, record.price):
        return f"{format_price_inr(record.price)} - {format_price_inr(record.price_max)}"
    return format_price_inr(record.price if record.price is not None else record.price_max)


def to_listing_feed_record(record: PropertyRecord) -> dict[str, Any]:
    """One warehouse property in the shape a category+city feed file serves."""
    gallery, interior, master_plan = _split_images(record)
    is_rent = record.listing_type is ListingType.RENT
    return {
        "title": record.title or record.project_name or "",
        "location": _location(record),
        "price": _price_display(record),
        "priceValue": None if is_rent else record.price,
        "rentMonthly": record.rent_monthly if is_rent else None,
        "size": f"{int(record.area_sqft)} sq.ft" if record.area_sqft else "",
        "areaValue": record.area_sqft,
        "configuration": record.configuration or "",
        "bedrooms": record.bedrooms,
        "propertyType": record.property_type.value,
        "listingType": record.listing_type.value,
        "isCommercial": record.is_commercial,
        "reraId": record.rera_number or "",
        "projectStatus": _STATUS_LABELS.get(record.possession_status, ""),
        "possession": _possession(record),
        "builderDescription": record.builder_name or record.developer_name or "",
        "aboutProject": _about(record),
        "amenities": _amenities(record),
        "specifications": [{"heading": k, "value": v} for k, v in record.specifications.items()],
        "images": gallery,
        "interiorImages": interior,
        "masterPlan": master_plan,
        "landmarks": _landmarks(record),
        "listingUrl": record.listing_url,
        "updatedAt": _iso(record.scraped_at),
    }


def build_response(
    segment: str, records: list[PropertyRecord], *, page: int = 1, limit: int = 500
) -> dict[str, Any]:
    """Same envelope as `feed.py`'s `build_response` — the front end reads both alike."""
    start = max(page - 1, 0) * limit
    window = records[start : start + limit]
    return {
        "success": True,
        "city": segment,
        "page": page,
        "limit": limit,
        "total": len(records),
        "results": [to_listing_feed_record(r) for r in window],
    }


def partition(
    records: list[PropertyRecord], *, publishable_only: bool = True
) -> tuple[dict[str, list[PropertyRecord]], int]:
    """Bucket warehouse properties into city+category segments.

    Returns the buckets and the number of records withheld (unknown listing
    type, no front-end city segment, or not publishable), mirroring
    `feed.py`'s `partition()`.
    """
    out: dict[str, list[PropertyRecord]] = {s: [] for s in all_segments()}
    withheld = 0
    for record in records:
        key = CITY_KEYS.get(record.location.city)
        if key is None:
            continue  # ghaziabad/sohna have no front-end segment
        category = category_of(record.listing_type)
        if category is None:
            withheld += 1
            continue
        if publishable_only and not is_publishable(record):
            withheld += 1
            continue
        out[segment_name(key, category)].append(record)
    return out, withheld


# ---------------------------------------------------------------------------
# warehouse -> records
# ---------------------------------------------------------------------------

_PROPERTY_FIELDS = frozenset(PropertyRecord.model_fields)


def record_from_doc(doc: dict[str, Any]) -> PropertyRecord | None:
    from pydantic import ValidationError

    try:
        return PropertyRecord.model_validate({k: v for k, v in doc.items() if k in _PROPERTY_FIELDS})
    except ValidationError:
        return None


async def load_properties(db: Any) -> list[PropertyRecord]:
    """Every active, non-duplicate, categorizable property, newest first.

    Queried per (city, category) rather than one unfiltered scan, so each
    query hits the existing `ix_filter_primary` compound index
    (`city, listing_type, property_type, price` — `db/documents.py`) instead
    of pulling the whole collection into Python to group there. This runs
    once a day as part of `export feed`, not per frontend request.

    Unsorted, deliberately: a `scraped_at` sort isn't covered by that index,
    so Mongo falls back to an in-memory sort that exceeds its 32MB limit once
    a segment has more than a few thousand candidates — and partitioning
    doesn't care about order.
    """
    from homz.db import documents as D

    out: list[PropertyRecord] = []
    for city in CITY_KEYS:
        for types in CATEGORY_LISTING_TYPES.values():
            cursor = db[D.PROPERTIES].find(
                {
                    "city": city.value,
                    "listing_type": {"$in": [t.value for t in types]},
                    "is_active": True,
                    "canonical_id": None,
                }
            )
            async for doc in cursor:
                record = record_from_doc(doc)
                if record is not None:
                    out.append(record)
    return out
