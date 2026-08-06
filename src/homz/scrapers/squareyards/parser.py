"""SquareYards parsers.

Selectors here are ported from the Puppeteer scrapers already in this repo
(`gurgaonPDPScraper.js` and siblings), which were validated against live PDPs —
so this parser starts from known-good ground truth rather than guesses:

    .price-box                              → price
    .unit-status-box .status                → project status / possession / units
    .unit .bhk-type                         → configuration
    .accordion-header[data-reraid]          → RERA id
    #amenities .amenities-list-box li span  → amenities
    #priceList tbody tr                     → per-config price table
    #mapLandmarks .near-distance-box        → landmarks (data-attribute = category)
    #specifications .specification-table    → specification rows
    #recentUpdates ... .details p           → construction updates

Each has a generic fallback so a class rename degrades rather than breaks.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

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
    parse_int,
    parse_possession_date,
    parse_possession_status,
    parse_price_range,
    parse_property_type,
    parse_rera_number,
    to_sqft,
)
from homz.common.schema import (
    Landmark,
    ProjectRecord,
    PropertyRecord,
    UnitConfiguration,
)

BASE_URL = "https://www.squareyards.com"
IMAGE_HOSTS = ("static.squareyards.com", "squareyards.com")

_ID_PATTERNS = (
    re.compile(r"/([a-z0-9-]+)-(\d{4,})(?:/|$)", re.I),
    re.compile(r"[?&]projectId=(\d+)", re.I),
    # `/{city}-residential-property/{slug}/{id}/project` — the id is its own
    # path segment here, not hyphen-suffixed, so the first pattern misses it
    # and silently falls back to the literal segment "project" for every URL
    # of this shape (the majority of them), collapsing them onto one _id.
    re.compile(r"/(\d+)/project(?:/|$)", re.I),
)


def extract_project_id(url: str) -> str | None:
    for pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.groups()[-1]
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    return slug or None


# ---------------------------------------------------------------------------
# listing pages
# ---------------------------------------------------------------------------


def _iter_jsonld(html: str):
    """Yield every JSON-LD object embedded in the page, flattening @graph/lists."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if isinstance(node.get("@graph"), list):
                    stack.extend(node["@graph"])


def parse_jsonld_project_urls(html: str) -> list[str]:
    """Project URLs from the page's schema.org `Product` blocks.

    Listing pages render their card anchors client-side, but every card is also
    published as JSON-LD for search engines — server-side, in the initial HTML.
    Reading that is both cheaper and sturdier than driving a browser to
    materialise anchors the page already describes.
    """
    from homz.common.parsing import canonical_url

    urls: list[str] = []
    seen: set[str] = set()
    for node in _iter_jsonld(html):
        if node.get("@type") != "Product":
            continue
        url = node.get("url") or (node.get("offers") or {}).get("url")
        if not isinstance(url, str) or "squareyards.com" not in url:
            continue
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        urls.append(key)
    return urls


def parse_project_cards(html: str, *, base_url: str = BASE_URL) -> list[str]:
    """Project detail URLs from a listing/hot-selling page.

    JSON-LD first (present in the server-rendered HTML), then card anchors —
    which only exist once the page's JS has run.
    """
    from homz.common.parsing import absolute_url, canonical_url

    urls = parse_jsonld_project_urls(html)
    seen: set[str] = set(urls)

    soup = BeautifulSoup(html, "lxml")
    for anchor in domx.select_all(
        soup,
        ".project-card .heading-body a.projectDetailUrl",
        "a.projectDetailUrl",
        ".listing-card-box a[href]",
        ".project-card a[href]",
    ):
        href = anchor.get("href")
        url = absolute_url(base_url, href)
        if not url:
            continue
        key = canonical_url(url)
        if key in seen or key.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(key)
        urls.append(key)
    return urls


# ---------------------------------------------------------------------------
# project detail (PDP)
# ---------------------------------------------------------------------------


def parse_project_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> ProjectRecord | None:
    soup = BeautifulSoup(html, "lxml")
    source_id = extract_project_id(url)
    if not source_id:
        return None

    title = domx.first(
        [
            _heading_first_line(soup),
            domx.meta_content(soup, "og:title"),
        ]
    )
    if not title:
        return None

    status_map = _status_box(soup)
    price_text = domx.text_of(soup, ".price-box", "[class*='price-box']", "[class*='price']")
    price_min, price_max = parse_price_range(price_text)

    possession_raw = status_map.get("possession starting from")
    status = parse_possession_status(status_map.get("project status") or possession_raw or "")

    about = " ".join(domx.texts_of(soup, "#aboutProject p", "[id*='aboutProject'] p"))
    builder_description = domx.text_of(soup, "#aboutBuilder .content-box p", "#aboutBuilder p")
    builder_name = domx.first(
        [
            domx.text_of(soup, "#aboutBuilder h2", "#aboutBuilder h3", "[class*='builder-name']"),
            _builder_from_breadcrumb(soup),
        ]
    )

    location_raw = domx.first(
        [
            domx.text_of(soup, ".location-box", "[class*='project-location']", ".address"),
            domx.meta_content(soup, "og:description"),
        ]
    )
    latitude = parse_float(domx.attr_of(soup, "data-lat", "[data-lat]"))
    longitude = parse_float(domx.attr_of(soup, "data-lng", "[data-lng]", "[data-long]"))
    location = build_location(
        location_raw, extra_texts=(title, url), latitude=latitude, longitude=longitude
    )

    configurations = parse_price_list(soup)
    specifications = parse_specifications(soup)
    amenities = parse_amenities(soup)
    landmarks = parse_landmarks(soup)
    updates = domx.texts_of(
        soup,
        "#recentUpdates .recent-updates-box article .details p",
        "#recentUpdates p",
        "[class*='recent-update'] p",
    )

    price_per_sqft = None
    if price_min and configurations:
        areas = [c.area_sqft for c in configurations if c.area_sqft]
        if areas:
            price_per_sqft = (price_min / Decimal(str(min(areas)))).quantize(Decimal("1"))

    record = ProjectRecord(
        source=Source.SQUAREYARDS,
        source_id=str(source_id),
        project_url=url,
        name=title,
        builder_name=builder_name,
        location=location,
        status=status,
        possession_date=parse_possession_date(possession_raw),
        rera_number=domx.first(
            [
                domx.attr_of(soup, "data-reraid", ".accordion-header[data-reraid]"),
                parse_rera_number(html[:300_000]),
            ]
        ),
        price_min=price_min,
        price_max=price_max,
        price_per_sqft=price_per_sqft,
        total_units=parse_int(status_map.get("number of units")),
        project_area_acres=_acres(status_map.get("total area")),
        configurations=configurations,
        amenities=amenities,
        specifications=specifications,
        images=domx.extract_images(
            soup,
            base_url=BASE_URL,
            selectors=("img", "[class*='gallery'] img"),
            allow_hosts=IMAGE_HOSTS,
        ),
        landmarks=landmarks,
        construction_updates=updates[:40],
        description=clean_text(about) or clean_text(builder_description),
        raw_html_key=raw_html_key,
        raw={"status_box": status_map, "builder_description": builder_description},
    )
    return record


def project_to_property(record: ProjectRecord) -> PropertyRecord:
    """Project pages also belong in `properties` so they show up in unified
    search alongside individual listings."""
    property_type = parse_property_type(record.name, record.project_url)
    commercial = is_commercial(property_type, record.name)
    # Commercial is its own top-level category by design (see
    # docs/listings-feed-contract.md in the export layer) — a commercial
    # project must win over the new-launch/project split, or it silently
    # lands under Sale instead, same bug as MagicBricks had.
    listing_type = (
        ListingType.COMMERCIAL
        if commercial
        else ListingType.NEW_LAUNCH
        if record.status in {PossessionStatus.NEW_LAUNCH, PossessionStatus.UPCOMING}
        else ListingType.PROJECT
    )
    smallest = min(
        (c for c in record.configurations if c.area_sqft), key=lambda c: c.area_sqft, default=None
    )

    prop = PropertyRecord(
        source=record.source,
        source_id=f"project:{record.source_id}",
        listing_url=record.project_url,
        title=record.name,
        description=record.description,
        project_name=record.name,
        builder_name=record.builder_name,
        developer_name=record.builder_name,
        listing_type=listing_type,
        property_type=property_type,
        is_commercial=commercial,
        configuration=smallest.configuration if smallest else None,
        bedrooms=smallest.bedrooms if smallest else None,
        price=record.price_min,
        price_max=record.price_max,
        price_per_sqft=record.price_per_sqft,
        is_price_on_request=record.price_min is None,
        area_sqft=smallest.area_sqft if smallest else None,
        location=record.location,
        possession_status=record.status,
        possession_date=record.possession_date,
        rera_number=record.rera_number,
        total_units=record.total_units,
        project_area_acres=record.project_area_acres,
        launch_date=record.launch_date,
        amenities=record.amenities,
        specifications=record.specifications,
        unit_configurations=record.configurations,
        images=record.images,
        landmarks=record.landmarks,
        raw_html_key=record.raw_html_key,
        raw=record.raw,
        scraped_at=record.scraped_at,
    )
    prop.segment = classify_segment(prop.price, listing_type)
    prop.is_luxury = prop.segment.value in {"luxury", "ultra_luxury"}
    prop.is_affordable = prop.segment.value == "affordable"
    return prop.finalize()


# ---------------------------------------------------------------------------
# section parsers (each independently testable)
# ---------------------------------------------------------------------------


def _status_box(soup: BeautifulSoup) -> dict[str, str]:
    """`.unit-status-box .status` → {"project status": "Under Construction", ...}."""
    out: dict[str, str] = {}
    for block in domx.select_all(
        soup, ".unit-status-box .status", ".status-box .status", "[class*='status-box'] .status"
    ):
        label = clean_text(domx.text_of(block, "span"))
        value = clean_text(domx.text_of(block, "strong"))
        if label and value:
            out[label.lower().rstrip(":")] = value
    return out


def parse_amenities(soup: BeautifulSoup) -> list[str]:
    values = domx.texts_of(
        soup,
        "#amenities .amenities-list-box ul li span",
        "#amenities li span",
        "#amenities li",
        "[class*='amenities'] li",
    )
    # The amenities modal groups by category in accordions.
    for item in domx.select_all(soup, ".accordion-item"):
        values.extend(
            clean_text(span.get_text(" ")) or ""
            for span in domx.select_all(item, ".accordion-body span")
        )
    return dedupe_preserve_order([v for v in values if v])[:150]


def parse_specifications(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for row in domx.select_all(
        soup, "#specifications .specification-table tbody tr", "#specifications tr"
    ):
        heading = clean_text(domx.text_of(row, ".specification-heading", "th", "td:first-child"))
        value = clean_text(domx.text_of(row, ".specification-value", "td:last-child"))
        if heading and value and heading != value:
            specs[heading] = value
    return specs


def parse_price_list(soup: BeautifulSoup) -> list[UnitConfiguration]:
    """`#priceList tbody tr` → one UnitConfiguration per row.

    Size is read from `.unit-value[data-sqft]` when present because the visible
    text may be in sq. yards while the attribute is always sqft.
    """
    configs: list[UnitConfiguration] = []
    for row in domx.select_all(soup, "#priceList tbody tr", "[id*='priceList'] tbody tr"):
        config_text = clean_text(domx.text_of(row, "td span", "td:first-child"))
        unit_value = domx.select_one(row, ".unit-value")
        area_sqft: float | None = None
        if unit_value is not None:
            data_sqft = unit_value.get("data-sqft")
            if data_sqft:
                area_sqft = parse_float(str(data_sqft))
            if area_sqft is None:
                value, unit = parse_area(unit_value.get_text(" "))
                area_sqft = to_sqft(value, unit)

        price_text = clean_text(
            domx.text_of(row, "td:nth-child(2) strong", "td:nth-child(2)", "td:last-child")
        )
        price_min, price_max = parse_price_range(price_text)

        if not any([config_text, area_sqft, price_min]):
            continue
        configs.append(
            UnitConfiguration(
                configuration=normalize_configuration(config_text),
                bedrooms=parse_bedrooms(config_text),
                area_sqft=area_sqft,
                price_min=price_min,
                price_max=price_max,
                price_display=price_text,
            )
        )
    return configs


_LANDMARK_CATEGORY_MAP = {
    "metro": "metro",
    "transport": "transport",
    "bus": "transport",
    "railway": "transport",
    "airport": "transport",
    "school": "school",
    "education": "school",
    "college": "school",
    "hospital": "hospital",
    "healthcare": "hospital",
    "mall": "mall",
    "shopping": "mall",
    "restaurant": "food",
    "food": "food",
    "business": "business",
    "hotel": "hotel",
}


def parse_landmarks(soup: BeautifulSoup) -> list[Landmark]:
    """`#mapLandmarks .near-distance-box[data-attribute]` → landmarks.

    The category lives in the container's `data-attribute`; each row has a
    `.distance-title` and a `.distance span`.
    """
    landmarks: list[Landmark] = []
    for box in domx.select_all(
        soup, "#mapLandmarks .near-distance-box", ".near-distance-box", "[data-attribute]"
    ):
        raw_category = (box.get("data-attribute") or "other").lower()
        category = next(
            (v for k, v in _LANDMARK_CATEGORY_MAP.items() if k in raw_category), raw_category
        )
        for row in domx.select_all(box, "tbody tr", "li"):
            name = clean_text(domx.text_of(row, ".distance-title"))
            distance_text = clean_text(domx.text_of(row, ".distance span", ".distance"))
            if not name:
                continue
            landmarks.append(
                Landmark(
                    category=category[:40],
                    name=name[:200],
                    distance_km=_km(distance_text),
                    raw_distance=distance_text,
                )
            )
    return landmarks[:120]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _heading_first_line(soup: BeautifulSoup) -> str | None:
    """SquareYards packs "<project>\\n<locality>" into one h1.

    The split has to happen on the *raw* node text — `clean_text` collapses the
    newline into a space, after which the locality is indistinguishable from
    the project name.
    """
    heading = domx.select_one(soup, "h1")
    if heading is None:
        return None
    for line in heading.get_text("\n").split("\n"):
        cleaned = clean_text(line)
        if cleaned:
            return cleaned
    return None


def _km(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"([\d.]+)\s*(km|m|meters?|kms?)\b", text, re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value if unit.startswith("k") else round(value / 1000, 3)


def _acres(text: str | None) -> float | None:
    if not text:
        return None
    value, unit = parse_area(text)
    if value is None:
        return None
    from homz.common.enums import AreaUnit

    if unit is AreaUnit.ACRE:
        return value
    sqft = to_sqft(value, unit)
    return round(sqft / 43560.0, 4) if sqft else None


def _builder_from_breadcrumb(soup: BeautifulSoup) -> str | None:
    crumbs = domx.texts_of(soup, ".breadcrumb li", "[class*='breadcrumb'] a")
    for crumb in crumbs:
        if re.search(r"builder|developer", crumb, re.I):
            return clean_text(re.sub(r"builders?|developers?", "", crumb, flags=re.I))
    return None


def build_city_url(city: str, *, listing_type: str = "sale") -> str:
    """City listing URL.

    Verified live: the pattern is `/new-projects-in-{city}`, not
    `/{city}/new-projects` — the latter 404s. Confirmed against Gurgaon,
    where the working URL yields 36 project cards.
    """
    city_slug = city.strip().lower().replace(" ", "-")
    if listing_type == "rent":
        return f"{BASE_URL}/{city_slug}/property-for-rent"
    return f"{BASE_URL}/new-projects-in-{city_slug}"


def is_price_on_request_page(html: str) -> bool:
    return is_price_on_request(html[:5000])


def project_price_summary(record: ProjectRecord) -> str | None:
    from homz.common.parsing import format_price_inr

    if record.price_min is None:
        return None
    low = format_price_inr(record.price_min)
    if record.price_max and record.price_max != record.price_min:
        return f"{low} - {format_price_inr(record.price_max)}"
    return low
