"""Catalogue feed export — the bridge to the HomzRealtor front end.

The website reads `…/api/data?city={segment}&page=&limit=` and expects records
in the shape the original Puppeteer scripts happened to emit (`projectTitle`,
`BHKType`, `priceList`, grouped `amenities`, …). The warehouse stores the
normalized `ProjectRecord` instead, so nothing downstream could consume it —
the scraped data and the live site were two disconnected worlds.

This module translates one into the other, so the site's existing contract is
served from real, freshly-scraped rows without the front end changing a line.

Two deliberate choices:

* **`updatedAt` is always emitted.** The legacy feed omitted it, so the app
  (and `app/sitemap.ts`, which wants `lastModified`) had no way to tell fresh
  data from a year-old snapshot. It is the scrape timestamp, not export time —
  a re-export of stale rows must not look fresh.
* **`location` is always emitted.** The front end reads it in ~18 places but
  the legacy feed never sent it, so those call sites silently fell back.
"""

from __future__ import annotations

import re
from collections import OrderedDict, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from homz.common.enums import City, PossessionStatus
from homz.common.parsing import format_price_inr
from homz.common.schema import ProjectRecord

# Front-end city keys (see `CITY_KEYS` in lib/scraping/homzbackend.ts).
CITY_KEYS: dict[City, str] = {
    City.GURGAON: "ggn",
    City.DELHI: "delhi",
    City.FARIDABAD: "faridabad",
    City.GREATER_NOIDA: "gNoida",
    City.NOIDA: "noida",
}

CATEGORIES = ("Residential", "Commercial")

# Amenity buckets, mirroring how SquareYards groups them on the PDP. Order is
# meaningful: the first pattern to match wins, so "Kids' Pool" lands in Sports
# rather than Kids by virtue of ordering below.
_AMENITY_GROUPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Sports", re.compile(r"gym|swim|pool|court|football|cricket|jogging|cycling|yoga|skating|golf", re.I)),
    ("Safety", re.compile(r"security|cctv|surveillance|fire|intercom|guard|access control", re.I)),
    ("Convenience", re.compile(r"lift|elevat|power backup|water|parking|atm|shopping|laundry|maintenance|waste|sewage|rain", re.I)),
    ("Leisure", re.compile(r"club|party|theatre|lounge|cafe|restaurant|amphi|library|spa|sauna|banquet", re.I)),
    ("Environment", re.compile(r"park|garden|green|landscap|open space|terrace|lawn", re.I)),
    ("Kids", re.compile(r"kid|child|play|creche|sand pit", re.I)),
)
_AMENITY_FALLBACK = "Others"

_STATUS_LABELS: dict[PossessionStatus, str] = {
    PossessionStatus.READY_TO_MOVE: "Ready to Move",
    PossessionStatus.UNDER_CONSTRUCTION: "Under Construction",
    PossessionStatus.NEW_LAUNCH: "New Launch",
    PossessionStatus.UPCOMING: "Upcoming",
    PossessionStatus.COMPLETED: "Completed",
}

_INTERIOR_RE = re.compile(r"interior|apartment-interior|room|kitchen|bedroom|bathroom", re.I)
_MASTER_PLAN_RE = re.compile(r"master-?plan|layout|site-?plan", re.I)


def segment_name(city_key: str, category: str) -> str:
    """("ggn", "Residential") -> "ggnResidentialProjects"."""
    return f"{city_key}{category}Projects"


def all_segments() -> list[str]:
    return [segment_name(k, c) for k in CITY_KEYS.values() for c in CATEGORIES]


def _price_display(record: ProjectRecord) -> str:
    if record.price_min is None and record.price_max is None:
        return "Price on Request"
    if record.price_min is not None and record.price_max not in (None, record.price_min):
        return f"{format_price_inr(record.price_min)} - {format_price_inr(record.price_max)}"
    return format_price_inr(record.price_min if record.price_min is not None else record.price_max)


def _possession(record: ProjectRecord) -> str:
    if record.possession_date:
        return record.possession_date.strftime("%b %Y")
    return _STATUS_LABELS.get(record.status, "")


def city_label(city: City) -> str:
    """City enum -> display name ("greater_noida" -> "Greater Noida")."""
    return city.value.replace("_", " ").title()


# Marketing copy that leaks into the locality field on PDPs without a clean
# address block — "Explore Ycon Platinum Heights Pataudi, Gurgaon is New
# Launch Project. …" is a meta description, not a place.
_PROSE_MARKERS = re.compile(
    r"\b(explore|view|know|check|discover|offers?|project|floor plans?|reviews?|"
    r"amenities|rera|details|status|price)\b",
    re.I,
)


def _looks_like_place(value: str) -> bool:
    return (
        len(value) <= 40
        and len(value.split()) <= 4
        and not _PROSE_MARKERS.search(value)
    )


def _location(record: ProjectRecord) -> str:
    """A human "Sector 80, Gurgaon" line.

    Deliberately composed from the *parsed* fields rather than `location.raw`
    or `city_raw`: on PDPs where SquareYards omits a clean address block, both
    of those fall back to the page's meta description, which would otherwise
    be rendered to users as the project's location.
    """
    loc = record.location
    parts = [loc.sector or loc.sub_locality, loc.locality]

    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = (part or "").strip().strip(",")
        if not cleaned or cleaned.lower() in seen or not _looks_like_place(cleaned):
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)

    if loc.city is not City.UNKNOWN:
        label = city_label(loc.city)
        if label.lower() not in seen:
            out.append(label)
    return ", ".join(out)


def _bhk_type(record: ProjectRecord) -> str:
    labels = [c.configuration for c in record.configurations if c.configuration]
    if not labels:
        return ""
    # "1 BHK", "2 BHK" -> "1, 2 BHK"
    numbers = [m.group(1) for lb in labels if (m := re.match(r"\s*(\d+)\s*BHK", lb, re.I))]
    if numbers and len(numbers) == len(labels):
        uniq = list(OrderedDict.fromkeys(numbers))
        return f"{', '.join(uniq)} BHK"
    return ", ".join(OrderedDict.fromkeys(labels))


def _size_range(record: ProjectRecord) -> str:
    areas = [c.area_sqft for c in record.configurations if c.area_sqft]
    if not areas:
        return ""
    lo, hi = min(areas), max(areas)
    fmt = lambda v: str(int(v)) if float(v).is_integer() else f"{v:.0f}"  # noqa: E731
    return fmt(lo) if lo == hi else f"{fmt(lo)} to {fmt(hi)}"


def _price_list(record: ProjectRecord) -> list[dict[str, str]]:
    rows = []
    for cfg in record.configurations:
        if not cfg.configuration:
            continue
        price = cfg.price_display or (
            format_price_inr(cfg.price_min) if cfg.price_min is not None else "Price on Request"
        )
        rows.append(
            {
                "bhkType": cfg.configuration,
                "size": str(int(cfg.area_sqft)) if cfg.area_sqft else "",
                "price": price,
            }
        )
    return rows


def _flats(record: ProjectRecord, images: list[str]) -> dict[str, list[dict[str, str]]]:
    hero = images[0] if images else ""
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for cfg in record.configurations:
        if not cfg.configuration:
            continue
        grouped[cfg.configuration].append(
            {
                "size": f"{int(cfg.area_sqft)} sq.ft" if cfg.area_sqft else "",
                "price": cfg.price_display
                or (format_price_inr(cfg.price_min) if cfg.price_min is not None else ""),
                "image": hero,
            }
        )
    return dict(grouped)


def _amenities(record: ProjectRecord) -> list[dict[str, Any]]:
    buckets: dict[str, list[str]] = OrderedDict()
    for amenity in record.amenities:
        label = next(
            (name for name, pattern in _AMENITY_GROUPS if pattern.search(amenity)),
            _AMENITY_FALLBACK,
        )
        buckets.setdefault(label, []).append(amenity)
    return [{"category": name, "amenities": items} for name, items in buckets.items()]


def _landmarks(record: ProjectRecord) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for lm in record.landmarks:
        grouped[lm.category].append(
            {
                "name": lm.name,
                "distance": lm.raw_distance
                or (f"{lm.distance_km} KM" if lm.distance_km is not None else ""),
            }
        )
    return dict(grouped)


def _split_images(record: ProjectRecord) -> tuple[list[str], list[str], dict[str, str]]:
    gallery, interior, master = [], [], {}
    for img in record.images:
        url = img.url
        if _MASTER_PLAN_RE.search(url) and not master:
            master = {"image": url}
        elif _INTERIOR_RE.search(url):
            interior.append(url)
        else:
            gallery.append(url)
    # A project with only interior shots should still render a gallery.
    if not gallery and interior:
        gallery, interior = interior, []
    return gallery, interior, master


def _about(record: ProjectRecord) -> list[str]:
    """`aboutProject` is a list of paragraphs in the feed contract."""
    if not record.description:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\r\n\r\n", record.description) if p.strip()]
    return paragraphs or [record.description.strip()]


def to_feed_record(record: ProjectRecord) -> dict[str, Any]:
    """One warehouse project in the shape the website's `/api/data` serves."""
    gallery, interior, master_plan = _split_images(record)
    return {
        "projectTitle": record.name,
        "location": _location(record),
        "price": _price_display(record),
        "size": _size_range(record),
        "BHKType": _bhk_type(record),
        "reraId": record.rera_number or "",
        "projectStatus": _STATUS_LABELS.get(record.status, ""),
        "possession": _possession(record),
        "numberOfUnits": str(record.total_units) if record.total_units else "",
        "totalArea": f"{record.project_area_acres} Acres" if record.project_area_acres else "",
        "builderDescription": record.builder_name or "",
        "aboutProject": _about(record),
        "amenities": _amenities(record),
        "priceList": _price_list(record),
        "flats": _flats(record, gallery),
        "landmarks": _landmarks(record),
        "specifications": [{"heading": k, "value": v} for k, v in record.specifications.items()],
        "recentUpdates": list(record.construction_updates),
        "images": gallery,
        "interiorImages": interior,
        "masterPlan": master_plan,
        "projectUrl": record.project_url,
        # Freshness the legacy feed never carried — see the module docstring.
        "updatedAt": _iso(record.scraped_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def build_response(
    segment: str, records: list[ProjectRecord], *, page: int = 1, limit: int = 500
) -> dict[str, Any]:
    """The full `/api/data` envelope for one city segment."""
    start = max(page - 1, 0) * limit
    window = records[start : start + limit]
    return {
        "success": True,
        "city": segment,
        "page": page,
        "limit": limit,
        "total": len(records),
        "results": [to_feed_record(r) for r in window],
    }


def _is_commercial(record: ProjectRecord) -> bool:
    from homz.common.enums import PropertyType
    from homz.common.parsing import is_commercial

    blob = " ".join(
        filter(None, [record.name, record.description, *(c.configuration or "" for c in record.configurations)])
    )
    return is_commercial(PropertyType.OTHER, blob)


def is_publishable(record: ProjectRecord) -> bool:
    """Does this project have enough substance to be worth a card on the site?

    SquareYards publishes stub pages for registered-but-unannounced projects
    ("DLF Sector 63, Gurgaon" — 48KB, no price, no configurations, no
    amenities). They are legitimately in the warehouse, and may fill in later
    once the builder announces, but rendering them is a blank card with a
    "Price on Request" label and nothing else.
    """
    return bool(
        record.price_min is not None
        or record.price_max is not None
        or record.configurations
        or record.amenities
    )


def partition(
    records: list[ProjectRecord], *, publishable_only: bool = True
) -> tuple[dict[str, list[ProjectRecord]], int]:
    """Bucket warehouse projects into the front end's city+category segments.

    Returns the buckets and the number of records withheld, so callers can
    report what was dropped rather than silently shrinking the feed.
    """
    out: dict[str, list[ProjectRecord]] = {s: [] for s in all_segments()}
    withheld = 0
    for record in records:
        key = CITY_KEYS.get(record.location.city)
        if key is None:
            continue  # ghaziabad/sohna have no front-end segment
        if publishable_only and not is_publishable(record):
            withheld += 1
            continue
        category = "Commercial" if _is_commercial(record) else "Residential"
        out[segment_name(key, category)].append(record)
    return out, withheld


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


# ---------------------------------------------------------------------------
# warehouse -> records
# ---------------------------------------------------------------------------

# Stored project documents carry denormalized extras (`_id`, `city`, `sector`,
# `builder_id`, …) that `ProjectRecord` declares `extra="forbid"` against, so
# read-back filters down to the model's own fields.
_PROJECT_FIELDS = frozenset(ProjectRecord.model_fields)


def record_from_doc(doc: dict[str, Any]) -> ProjectRecord | None:
    from pydantic import ValidationError

    try:
        return ProjectRecord.model_validate({k: v for k, v in doc.items() if k in _PROJECT_FIELDS})
    except ValidationError:
        return None


async def load_projects(db: Any, *, city: str | None = None) -> list[ProjectRecord]:
    """Every project in the warehouse, newest scrape first."""
    from homz.db import documents as D

    query: dict[str, Any] = {}
    if city:
        query["city"] = city
    cursor = db[D.PROJECTS].find(query).sort("scraped_at", -1)
    out: list[ProjectRecord] = []
    async for doc in cursor:
        record = record_from_doc(doc)
        if record is not None:
            out.append(record)
    return out
