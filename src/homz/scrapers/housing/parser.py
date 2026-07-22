"""Housing.com parsers.

Housing is a React/Next.js app, so the hydration payload (`__NEXT_DATA__`) is
the primary extraction target — it is the same data the UI renders, in a stable
shape, and it survives CSS changes entirely. CSS selectors are only the last
fallback.

Because the payload's wrapper keys move around between releases, we search by
leaf key (`domx.find_first_key`) rather than by fixed path.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from bs4 import BeautifulSoup

from homz.common import domx
from homz.common.enums import ListingType, PossessionStatus, Source
from homz.common.geo import build_location
from homz.common.parsing import (
    classify_segment,
    clean_text,
    dedupe_preserve_order,
    is_commercial,
    is_price_on_request,
    normalize_configuration,
    parse_area,
    parse_bedrooms,
    parse_float,
    parse_floor,
    parse_int,
    parse_listing_date,
    parse_listing_type,
    parse_phone,
    parse_possession_date,
    parse_possession_status,
    parse_price,
    parse_price_per_sqft,
    parse_price_range,
    parse_property_type,
    parse_rera_number,
    parse_seller_type,
    to_sqft,
)
from homz.common.schema import ContactInfo, Image, Landmark, PropertyRecord, UnitConfiguration

BASE_URL = "https://housing.com"
IMAGE_HOSTS = ("housing.com", "imgs.housing", "assets.housing")

_ID_PATTERNS = (
    re.compile(r"/rent/([a-z0-9]{6,})", re.I),
    re.compile(r"/buy/([a-z0-9]{6,})", re.I),
    re.compile(r"/(?:in|resale)/[^/]+/([A-Za-z0-9_-]{8,})", re.I),
    re.compile(r"[?&]propertyId=([A-Za-z0-9_-]+)", re.I),
)


def extract_listing_id(url: str, payload: dict[str, Any] | None = None) -> str | None:
    if payload:
        for key in ("id", "propertyId", "listingId", "hash_id", "hashId"):
            value = domx.find_first_key(payload, key)
            if isinstance(value, str | int) and len(str(value)) >= 4:
                return str(value)
    for pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    return slug or None


# ---------------------------------------------------------------------------
# search results
# ---------------------------------------------------------------------------

_DETAIL_HREF_RE = re.compile(r"/(rent|buy|resale)/|/property/|/in/[^/]+/[A-Za-z0-9_-]{8,}", re.I)


def parse_search_results(html: str, *, base_url: str = BASE_URL) -> list[str]:
    from homz.common.parsing import absolute_url, canonical_url

    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()

    # Preferred: pull the URL list straight out of the hydration payload.
    payload = domx.next_data(soup)
    if payload:
        for candidate in domx.find_all_keys(payload, "url", "detailUrl", "propertyUrl", "seoUrl"):
            if isinstance(candidate, str) and _DETAIL_HREF_RE.search(candidate):
                url = absolute_url(base_url, candidate)
                if url:
                    key = canonical_url(url)
                    if key not in seen:
                        seen.add(key)
                        urls.append(key)

    if urls:
        return urls

    for anchor in domx.select_all(
        soup, "a[data-q='card-title']", "article a[href]", "a[href*='/rent/']", "a[href*='/buy/']"
    ):
        href = anchor.get("href")
        if not href or not _DETAIL_HREF_RE.search(href):
            continue
        url = absolute_url(base_url, href)
        if not url:
            continue
        key = canonical_url(url)
        if key not in seen:
            seen.add(key)
            urls.append(key)
    return urls


# ---------------------------------------------------------------------------
# detail page
# ---------------------------------------------------------------------------


def parse_property_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> PropertyRecord | None:
    soup = BeautifulSoup(html, "lxml")
    next_payload = domx.next_data(soup) or {}
    page_props = domx.deep_get(next_payload, "props.pageProps", {}) or next_payload
    ld = domx.json_ld_of_type(soup, "Residence", "Apartment", "Product", "House", "Offer") or {}

    source_id = extract_listing_id(url, page_props)
    if not source_id:
        return None

    def payload(*keys: str) -> Any:
        return domx.find_first_key(page_props, *keys)

    title = domx.first(
        [
            clean_text(payload("title", "propertyTitle", "displayTitle")),
            clean_text(ld.get("name")),
            domx.meta_content(soup, "og:title"),
            domx.text_of(soup, "h1"),
        ]
    )
    description = domx.first(
        [
            clean_text(payload("description", "propertyDescription", "aboutProperty")),
            clean_text(ld.get("description")),
            domx.text_of(soup, "[data-q='description']", "#description", ".description"),
            domx.meta_content(soup, "og:description"),
        ]
    )

    specs = _spec_map(soup, page_props)
    spec_lookup = {k.lower(): v for k, v in specs.items()}

    def spec(*names: str) -> str | None:
        for name in names:
            for key, value in spec_lookup.items():
                if name in key:
                    return value
        return None

    listing_type = parse_listing_type(url, title, payload("listingType", "saleType"))

    # --- price ------------------------------------------------------------
    price_numeric = _first_number(payload("price", "displayPrice", "amount", "priceValue"))
    price_text = domx.first(
        [
            clean_text(payload("priceDisplay", "formattedPrice", "priceLabel")),
            domx.text_of(soup, "[data-q='price']", "[class*='price']"),
            str(price_numeric) if price_numeric else None,
        ]
    )
    price, price_max = parse_price_range(price_text)
    if price is None and price_numeric:
        price = Decimal(str(int(price_numeric)))

    rent_monthly: Decimal | None = None
    if listing_type is ListingType.RENT:
        rent_monthly, price = price, None

    price_per_sqft = domx.first(
        [
            parse_price_per_sqft(spec("price per", "rate per")),
            _decimal(_first_number(payload("pricePerSqft", "priceSqft", "rate"))),
        ]
    )

    # --- area -------------------------------------------------------------
    area_text = domx.first(
        [
            clean_text(payload("areaDisplay", "sizeDisplay", "carpetAreaDisplay")),
            spec("super built", "built up", "carpet area", "area"),
        ]
    )
    area_value, area_unit = parse_area(area_text)
    area_sqft = to_sqft(area_value, area_unit)
    if area_sqft is None:
        numeric = _first_number(payload("area", "size", "builtUpArea", "carpetArea"))
        area_sqft = float(numeric) if numeric else None

    carpet_sqft = _area_sqft(spec("carpet area"))
    super_sqft = _area_sqft(spec("super built", "super area"))
    built_up_sqft = _area_sqft(spec("built up"))
    plot_sqft = _area_sqft(spec("plot area"))

    if price and area_sqft and not price_per_sqft:
        price_per_sqft = (price / Decimal(str(area_sqft))).quantize(Decimal("1"))

    # --- config -----------------------------------------------------------
    bedrooms = domx.first(
        [
            parse_int(str(payload("bedrooms", "bedroom", "numBedrooms") or "")),
            parse_bedrooms(title),
            parse_bedrooms(spec("bedroom", "configuration")),
        ]
    )
    config_text = domx.first([spec("configuration", "bhk"), title])
    floor_number, total_floors = parse_floor(spec("floor"))

    property_type = parse_property_type(
        clean_text(payload("propertyType", "apartmentType")), spec("property type"), title, url
    )

    possession_raw = domx.first(
        [clean_text(payload("possessionStatus", "availability")), spec("possession", "status")]
    )
    possession_status = parse_possession_status(possession_raw or "")
    if possession_status is PossessionStatus.UNKNOWN and listing_type is ListingType.RENT:
        possession_status = PossessionStatus.READY_TO_MOVE

    # --- location ---------------------------------------------------------
    location_raw = domx.first(
        [
            clean_text(payload("address", "formattedAddress", "localityName", "locality")),
            clean_text(domx.deep_get(ld, "address.streetAddress")),
            domx.text_of(soup, "[data-q='locality']", "[class*='locality']"),
        ]
    )
    latitude = parse_float(
        str(domx.first([payload("latitude", "lat"), domx.deep_get(ld, "geo.latitude")]) or "")
    )
    longitude = parse_float(
        str(
            domx.first([payload("longitude", "lng", "lon"), domx.deep_get(ld, "geo.longitude")])
            or ""
        )
    )
    location = build_location(
        location_raw, extra_texts=(title, url), latitude=latitude, longitude=longitude
    )

    # --- amenities / images / landmarks ----------------------------------
    amenities = _amenities(soup, page_props)
    images = _images(soup, page_props)
    landmarks = _landmarks(page_props)

    contact = ContactInfo(
        name=clean_text(payload("sellerName", "ownerName", "agentName")),
        seller_type=parse_seller_type(
            clean_text(payload("sellerType", "postedBy", "ownerType"))
            or domx.text_of(soup, "[data-q='posted-by']")
        ),
        company=clean_text(payload("agencyName", "companyName")),
        phone=parse_phone(clean_text(payload("phone", "contactNumber"))),
    )

    record = PropertyRecord(
        source=Source.HOUSING,
        source_id=str(source_id),
        listing_url=url,
        title=title,
        description=description,
        project_name=clean_text(payload("projectName", "societyName", "buildingName")),
        builder_name=clean_text(payload("builderName", "developerName")),
        society_name=clean_text(payload("societyName")),
        listing_type=listing_type,
        property_type=property_type,
        property_type_raw=clean_text(payload("propertyType")),
        is_commercial=is_commercial(property_type, title, url),
        configuration=normalize_configuration(config_text),
        bedrooms=bedrooms,
        bathrooms=parse_int(str(payload("bathrooms", "bathroom") or "") or spec("bathroom")),
        balconies=parse_int(str(payload("balconies") or "") or spec("balcon")),
        floor_number=floor_number,
        total_floors=total_floors,
        facing=clean_text(payload("facing")) or spec("facing"),
        furnishing=clean_text(payload("furnishing", "furnishingType")) or spec("furnish"),
        age_years=parse_float(spec("age of", "property age")),
        price=price,
        price_max=price_max,
        price_display=price_text,
        price_per_sqft=price_per_sqft,
        booking_amount=parse_price(spec("booking")),
        maintenance_charge=parse_price(
            clean_text(payload("maintenanceCharge")) or spec("maintenance")
        ),
        rent_monthly=rent_monthly,
        security_deposit=parse_price(clean_text(payload("securityDeposit")) or spec("deposit")),
        is_price_on_request=is_price_on_request(price_text),
        area_value=area_value,
        area_unit=area_unit,
        area_sqft=area_sqft,
        carpet_area_sqft=carpet_sqft,
        built_up_area_sqft=built_up_sqft,
        super_built_up_area_sqft=super_sqft,
        plot_area_sqft=plot_sqft,
        location=location,
        possession_status=possession_status,
        possession_date=parse_possession_date(possession_raw),
        possession_raw=possession_raw,
        rera_number=parse_rera_number(
            clean_text(payload("reraId", "reraNumber")) or spec("rera") or ""
        ),
        amenities=amenities,
        specifications=specs,
        images=images,
        landmarks=landmarks,
        contact=contact,
        listed_at=parse_listing_date(clean_text(payload("postedOn", "createdAt", "listingDate"))),
        listing_date_raw=clean_text(payload("postedOn", "createdAt")),
        raw_html_key=raw_html_key,
        raw={"specs": specs, "has_next_data": bool(next_payload)},
    )
    record.segment = classify_segment(record.price or record.rent_monthly, listing_type)
    record.is_luxury = record.segment.value in {"luxury", "ultra_luxury"}
    record.is_affordable = record.segment.value == "affordable"
    return record.finalize()


def parse_project_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> PropertyRecord | None:
    record = parse_property_detail(html, url, raw_html_key=raw_html_key)
    if record is None:
        return None
    soup = BeautifulSoup(html, "lxml")
    payload = domx.deep_get(domx.next_data(soup) or {}, "props.pageProps", {}) or {}

    record.listing_type = ListingType.PROJECT
    record.project_name = record.project_name or record.title
    record.unit_configurations = _unit_configs(payload)
    record.total_units = record.total_units or parse_int(
        str(domx.find_first_key(payload, "totalUnits", "unitsCount") or "")
    )
    record.project_area_acres = record.project_area_acres or parse_float(
        str(domx.find_first_key(payload, "projectArea", "landArea") or "")
    )
    return record.finalize()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(int(float(value))))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _first_number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        return parse_float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "min", "displayValue"):
            if key in value:
                return _first_number(value[key])
    return None


def _area_sqft(text: str | None) -> float | None:
    value, unit = parse_area(text)
    return to_sqft(value, unit)


def _spec_map(soup: BeautifulSoup, payload: dict[str, Any]) -> dict[str, str]:
    """Merge the payload's detail list with any DOM spec table."""
    specs: dict[str, str] = {}

    for container in domx.find_all_keys(
        payload, "details", "propertyDetails", "highlights", "factsAndFeatures"
    ):
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                label = clean_text(
                    str(item.get("label") or item.get("title") or item.get("key") or "")
                )
                value = clean_text(str(item.get("value") or item.get("text") or ""))
                if label and value:
                    specs.setdefault(label, value)
        elif isinstance(container, dict):
            for label, value in container.items():
                if isinstance(value, str | int | float):
                    cleaned_label = clean_text(str(label))
                    cleaned_value = clean_text(str(value))
                    if cleaned_label and cleaned_value:
                        specs.setdefault(cleaned_label, cleaned_value)

    dom_specs = domx.label_value_pairs(
        domx.select_one(soup, "[data-q='details']", "#details", ".property-details") or soup,
        row_selector="[data-q='detail-item'], .detail-item, tr, li",
    )
    for label, value in dom_specs.items():
        specs.setdefault(label, value)
    return specs


def _amenities(soup: BeautifulSoup, payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for container in domx.find_all_keys(payload, "amenities", "amenityList", "features"):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("label") or item.get("title")
                    if isinstance(name, str):
                        values.append(name)
    values.extend(
        domx.texts_of(soup, "[data-q='amenities'] li", "[class*='amenit'] li", "#amenities li")
    )
    return dedupe_preserve_order(values)[:120]


def _images(soup: BeautifulSoup, payload: dict[str, Any]) -> list[Image]:
    images: list[Image] = []
    seen: set[str] = set()

    for container in domx.find_all_keys(payload, "images", "photos", "media", "gallery"):
        if not isinstance(container, list):
            continue
        for item in container:
            url = None
            caption = None
            if isinstance(item, str):
                url = item
            elif isinstance(item, dict):
                url = item.get("url") or item.get("src") or item.get("imageUrl")
                caption = clean_text(str(item.get("caption") or item.get("title") or "")) or None
            if not isinstance(url, str) or not url.startswith(("http", "//")):
                continue
            if url.startswith("//"):
                url = f"https:{url}"
            key = url.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            images.append(Image(url=url, caption=caption, is_primary=not images))

    if not images:
        images = domx.extract_images(
            soup,
            base_url=BASE_URL,
            selectors=("[data-q='gallery'] img", "[class*='gallery'] img", "img"),
            allow_hosts=IMAGE_HOSTS,
        )
    return images[:40]


def _landmarks(payload: dict[str, Any]) -> list[Landmark]:
    landmarks: list[Landmark] = []
    for container in domx.find_all_keys(payload, "nearbyPlaces", "landmarks", "poi", "nearby"):
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            name = clean_text(str(item.get("name") or item.get("title") or ""))
            if not name:
                continue
            category = clean_text(
                str(item.get("category") or item.get("type") or item.get("placeType") or "other")
            )
            distance = item.get("distance") or item.get("distanceInKm")
            distance_km = _first_number(distance)
            landmarks.append(
                Landmark(
                    category=(category or "other").lower(),
                    name=name[:200],
                    distance_km=distance_km,
                    raw_distance=str(distance) if distance is not None else None,
                )
            )
    return landmarks[:60]


def _unit_configs(payload: dict[str, Any]) -> list[UnitConfiguration]:
    configs: list[UnitConfiguration] = []
    for container in domx.find_all_keys(payload, "configurations", "unitTypes", "floorPlans"):
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            config_text = clean_text(
                str(item.get("configuration") or item.get("name") or item.get("type") or "")
            )
            area = _first_number(item.get("area") or item.get("size") or item.get("carpetArea"))
            price_min = _decimal(_first_number(item.get("priceMin") or item.get("price")))
            price_max = _decimal(_first_number(item.get("priceMax")))
            if not any([config_text, area, price_min]):
                continue
            configs.append(
                UnitConfiguration(
                    configuration=normalize_configuration(config_text),
                    bedrooms=parse_bedrooms(config_text),
                    area_sqft=area,
                    price_min=price_min,
                    price_max=price_max,
                    price_display=clean_text(str(item.get("priceDisplay") or "")) or None,
                )
            )
    return configs


def build_search_url(
    *,
    city: str,
    listing_type: str = "sale",
    property_type: str | None = None,
    page: int = 1,
) -> str:
    city_slug = city.strip().lower().replace(" ", "-")
    section = "rent" if listing_type == "rent" else "buy"
    ptype = f"/{property_type}" if property_type else ""
    query = f"?page={page}" if page > 1 else ""
    return f"{BASE_URL}/in/{section}/{city_slug}{ptype}{query}"
