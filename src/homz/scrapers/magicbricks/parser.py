"""MagicBricks parsers — pure `HTML → normalized record` functions.

No network calls here, so every function is testable against a stored raw-HTML
fixture from `data/raw/magicbricks/...`.

Extraction ladder (see `homz.common.domx`): JSON-LD → embedded state →
OpenGraph → CSS. MagicBricks emits `Residence`/`Product` JSON-LD on most detail
pages, which survives their frequent CSS refactors.
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
    parse_email,
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
from homz.common.schema import (
    ContactInfo,
    Landmark,
    PropertyRecord,
    UnitConfiguration,
)

BASE_URL = "https://www.magicbricks.com"
IMAGE_HOSTS = ("magicbricks.com", "mbimg", "img.staticmb.com")

# Property photos live at `.../cropped_images/ver2/{hash}/Photo_h{H}_w{W}/
# {stem}_{H}_{W}.{ext}` — several fixed, pre-signed crop sizes under the same
# hash-keyed path. Guessing an arbitrary size 401s ("Un-authorized access",
# verified live), but h600_w900 is verified live as a real, fetchable size
# for any photo id on this same path (41.5KB actual photo data vs 11.7KB for
# the h300_w450 crop of the *same* shot) — every detail page's own cover
# photo already uses it, this just applies that same already-legitimate size
# to every other photo instead of leaving them at whatever smaller crop the
# thumbnail strip happens to reference.
_MB_PHOTO_CROP_RE = re.compile(
    r"(/cropped_images/ver2/[^/]+/)Photo_h(\d+)_w(\d+)/([^/?#]+?)_\d+_\d+(\.\w+)"
)

# Project-banner photos live at `.../mbimages/project/Photo_h{H}_w{W}/{rest}
# _{H}_{W}.{ext}` — a *different* CDN path from the per-listing photos above,
# with its own separate set of pre-signed crop sizes. Requesting h600_w900
# here 404s ("Original image not found", verified live) since that size was
# never signed for this path. But dropping the crop folder AND the trailing
# `_{H}_{W}` suffix entirely serves the uncropped original (verified live:
# 300-650KB actual photo data vs ~25KB for the h300_w450/h310_w462/h240_w0
# crops that are the only sizes this path actually signs).
_MB_PROJECT_CROP_RE = re.compile(r"(mbimages/project/)Photo_h\d+_w\d+/(.+?)_\d+_\d+(\.\w+)$")


def _own_photo_digits(source_id: str | None) -> str | None:
    """`source_id` is hex-encoded ASCII of MagicBricks' own listing id (e.g.
    "4d423835363939303139" decodes to "MB85699019") — and that same numeric
    tail ("85699019") is exactly the id embedded at the front of that
    listing's own `cropped_images` photo filenames (".../85699019_1_....jpg").
    This recovers it so `_extract_property_images` can tell this listing's
    real photos apart from a *different* listing's photo that leaked in via
    a "similar/owner properties" carousel sharing the same URL shape (see
    that function's docstring)."""
    if not source_id:
        return None
    try:
        decoded = bytes.fromhex(source_id).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    digits = re.sub(r"\D", "", decoded)
    return digits or None


def _upscale_photo(url: str) -> str:
    match = _MB_PHOTO_CROP_RE.search(url)
    if match:
        prefix, h, w, stem, ext = match.groups()
        if int(h) < 600 or int(w) < 900:
            url = _MB_PHOTO_CROP_RE.sub(rf"\g<1>Photo_h600_w900/{stem}_600_900{ext}", url)
    match = _MB_PROJECT_CROP_RE.search(url)
    if match:
        prefix, rest, ext = match.groups()
        url = _MB_PROJECT_CROP_RE.sub(rf"\g<1>{rest}{ext}", url)
    return url

_ID_PATTERNS = (
    re.compile(r"pdpid[-_]?([A-Za-z0-9]+)", re.I),
    re.compile(r"-pdpid-([A-Za-z0-9]+)", re.I),
    re.compile(r"/propertyDetails/[^?]*?-(\d{6,})", re.I),
    re.compile(r"[?&]id=([A-Za-z0-9]+)", re.I),
)


def extract_listing_id(url: str) -> str | None:
    for pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    # Last resort: the final path slug is stable enough to key on.
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    return slug or None


# ---------------------------------------------------------------------------
# search results → detail URLs
# ---------------------------------------------------------------------------

_CARD_LINK_SELECTORS = (
    "a.mb-srp__card--title",
    ".mb-srp__list .mb-srp__card a[href*='propertyDetails']",
    "a[href*='/propertyDetails/']",
    "a[href*='-pdpid-']",
)


def parse_search_results(html: str, *, base_url: str = BASE_URL) -> list[str]:
    """Detail-page URLs from a search-results page, de-duplicated in order."""
    from homz.common.parsing import absolute_url, canonical_url

    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()

    for anchor in domx.select_all(soup, *_CARD_LINK_SELECTORS):
        href = anchor.get("href")
        url = absolute_url(base_url, href)
        if not url or ("propertyDetails" not in url and "pdpid" not in url):
            continue
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        urls.append(key)

    return urls


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    if domx.select_one(soup, "a[rel='next']", ".mb-srp__pagination--next:not(.disabled)"):
        return True
    # MagicBricks lazy-loads pages; presence of any card implies more may exist.
    return bool(domx.select_all(soup, ".mb-srp__card"))


# ---------------------------------------------------------------------------
# detail page
# ---------------------------------------------------------------------------


def _extract_property_images(soup: BeautifulSoup, source_id: str | None = None) -> list:
    """`.mb-ldp__gallery` no longer exists on the live site — MagicBricks
    renamed its detail-page gallery markup to `mb-ldp__premium-dtls__photo*`
    at some point, so that selector (and its `[class*='gallery']` fallback)
    silently matched nothing and every image fell through to a bare "img"
    sweep instead. Try the current real gallery class first, keep the old
    ones as fallbacks in case it changes again, and upscale whatever crop
    size the thumbnail strip happened to reference.

    The bare "img" fallback is unscoped, though, and on current pages the
    actual photo gallery is populated client-side (its markup isn't in the
    static HTML we fetch at all — confirmed live: none of `.photoGallery
    img`/`.sliderImages img`/etc. match anything in the raw response), so
    every one of the three scoped selectors above now comes up empty and
    every page falls through to the bare sweep. That sweeps in whatever *is*
    server-rendered instead: a "similar projects nearby" carousel
    (`mbimages/project/...` banner photos — always some other project, never
    this listing) and "properties you may also like"/"owner-listed
    properties" carousels (real `cropped_images/...` property photos, but
    for a *different* listing). Both got treated as this listing's own
    images, feeding bogus "shared image" duplicate links in
    `homz.common.dedupe` between totally unrelated properties that merely
    appeared on the same recommendation widget together.

    So the carousel filtering below applies only once we're actually in that
    unscoped bare-sweep tier — a properly-scoped selector (e.g.
    `.mb-ldp__gallery`) can legitimately return a `mbimages/project/...`
    banner as this listing's own photo (a project with no unit photos yet
    falls back to its project banner), and that case is left untouched.
    """
    scoped_selectors = (
        "[class*='premium-dtls__photo'] img, img[class*='premium-dtls__photo']",
        ".mb-ldp__gallery img",
        "[class*='gallery'] img",
    )
    images = domx.extract_images(
        soup, base_url=BASE_URL, selectors=scoped_selectors, allow_hosts=IMAGE_HOSTS
    )
    used_bare_sweep = not images
    if used_bare_sweep:
        images = domx.extract_images(
            soup, base_url=BASE_URL, selectors=("img",), allow_hosts=IMAGE_HOSTS
        )

    own_digits = _own_photo_digits(source_id)
    deduped = []
    seen: set[str] = set()
    for image in images:
        if used_bare_sweep:
            if _MB_PROJECT_CROP_RE.search(image.url):
                continue  # recommended-project banner, never this listing's own
            photo_match = _MB_PHOTO_CROP_RE.search(image.url)
            if photo_match and own_digits and not photo_match.group(4).startswith(own_digits):
                continue  # a *different* listing's photo, same URL shape
        image.url = _upscale_photo(image.url)
        # Upscaling can collapse two distinct pre-upscale URLs onto the same
        # photo (e.g. the hero already at h600_w900 and a thumbnail-strip
        # copy of that same shot at h300_w450) — dedup again post-upscale.
        key = image.url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(image)
    return deduped


def parse_property_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> PropertyRecord | None:
    soup = BeautifulSoup(html, "lxml")
    source_id = extract_listing_id(url)
    if not source_id:
        return None

    ld = domx.json_ld_of_type(
        soup, "Residence", "Apartment", "House", "Product", "SingleFamilyResidence", "Offer"
    ) or {}
    state = domx.window_state(soup, "window.__INITIAL_STATE__", "window.pdpData") or {}

    title = domx.first(
        [
            clean_text(ld.get("name")),
            domx.meta_content(soup, "og:title"),
            domx.text_of(soup, "h1.mb-ldp__dtls__title", "h1"),
        ]
    )
    description = domx.first(
        [
            clean_text(ld.get("description")),
            domx.text_of(soup, "#aboutPropertyText", ".mb-ldp__more-dtls__body", ".description"),
            domx.meta_content(soup, "og:description", "description"),
        ]
    )

    # --- specs table is the richest single source on an MB detail page ------
    specs = domx.label_value_pairs(
        domx.select_one(
            soup,
            ".mb-ldp__dtls__body__list",
            ".mb-ldp__more-dtls__list",
            "#propertyDetailTable",
            ".p-details",
        )
        or soup,
        row_selector=".mb-ldp__dtls__body__list--item, .mb-ldp__more-dtls__list--item, tr, li",
        label_selector=".mb-ldp__dtls__body__list--label, .mb-ldp__more-dtls__list--label, th, .lbl",
        value_selector=".mb-ldp__dtls__body__list--value, .mb-ldp__more-dtls__list--value, td, .val",
    )
    spec_lookup = {k.lower(): v for k, v in specs.items()}

    def spec(*names: str) -> str | None:
        for name in names:
            for key, value in spec_lookup.items():
                if name in key:
                    return value
        return None

    # --- price ------------------------------------------------------------
    price_text = domx.first(
        [
            domx.text_of(soup, ".mb-ldp__dtls__price", ".mb-ldp__price", "[class*='price']"),
            _ld_price_text(ld),
            spec("price"),
        ]
    )
    listing_type = parse_listing_type(url, title, price_text, spec("transaction type"))
    price, price_max = parse_price_range(price_text)
    on_request = is_price_on_request(price_text)

    rent_monthly: Decimal | None = None
    if listing_type is ListingType.RENT:
        rent_monthly, price = price, None

    price_per_sqft = domx.first(
        [
            parse_price_per_sqft(domx.text_of(soup, ".mb-ldp__dtls__price--sqft", ".rate-sqft")),
            parse_price_per_sqft(spec("price per", "rate")),
        ]
    )

    # --- area -------------------------------------------------------------
    area_text = domx.first(
        [
            spec("super area", "super built"),
            spec("carpet area"),
            spec("plot area", "area"),
            domx.text_of(soup, ".mb-ldp__dtls__body__summary--area", "[class*='area']"),
        ]
    )
    area_value, area_unit = parse_area(area_text)
    area_sqft = to_sqft(area_value, area_unit)
    carpet_sqft = _area_sqft(spec("carpet area"))
    super_sqft = _area_sqft(spec("super area", "super built"))
    built_up_sqft = _area_sqft(spec("built up area", "built-up"))
    plot_sqft = _area_sqft(spec("plot area"))
    area_sqft = area_sqft or super_sqft or built_up_sqft or carpet_sqft or plot_sqft

    if price and area_sqft and not price_per_sqft:
        price_per_sqft = (price / Decimal(str(area_sqft))).quantize(Decimal("1"))

    # --- configuration ----------------------------------------------------
    config_text = domx.first([spec("bedrooms", "configuration", "bhk"), title])
    bedrooms = parse_bedrooms(config_text) or parse_bedrooms(title)
    floor_number, total_floors = parse_floor(spec("floor"))

    property_type = parse_property_type(spec("property type"), title, url)
    # `parse_listing_type()` has no commercial branch — it only ever returns
    # PG/RENT/NEW_LAUNCH/RESALE/SALE/PROJECT/UNKNOWN, so an office-for-rent
    # listing would otherwise land under residential Rent. Commercial is its
    # own top-level category (by design, not by is_commercial flag alone —
    # see docs/listings-feed-contract.md), so it must win over everything
    # except PG, which is never commercial in practice.
    commercial = is_commercial(property_type, title, url)
    if commercial and listing_type is not ListingType.PG:
        listing_type = ListingType.COMMERCIAL
    possession_raw = spec("possession", "status", "availability")
    possession_status = parse_possession_status(possession_raw or "")
    if possession_status is PossessionStatus.UNKNOWN and listing_type is ListingType.RENT:
        possession_status = PossessionStatus.READY_TO_MOVE

    # --- location ---------------------------------------------------------
    location_raw = domx.first(
        [
            clean_text(domx.deep_get(ld, "address.streetAddress")),
            domx.text_of(soup, ".mb-ldp__dtls__title--loc", ".mb-ldp__location"),
            _address_from_title(title),
            spec("locality", "address"),
        ]
    )
    latitude = parse_float(
        str(
            domx.first(
                [domx.deep_get(ld, "geo.latitude"), domx.find_first_key(state, "latitude", "lat")]
            )
            or ""
        )
    )
    longitude = parse_float(
        str(
            domx.first(
                [
                    domx.deep_get(ld, "geo.longitude"),
                    domx.find_first_key(state, "longitude", "lng", "lon"),
                ]
            )
            or ""
        )
    )
    location = build_location(
        location_raw,
        extra_texts=(title, url),
        latitude=latitude,
        longitude=longitude,
    )

    # --- amenities & landmarks -------------------------------------------
    amenities = dedupe_preserve_order(
        domx.texts_of(
            soup,
            "#amenities li",
            ".mb-ldp__amenities li",
            ".mb-ldp__amenities__list--item",
            "[class*='amenities'] li",
        )
    )
    landmarks = _parse_landmarks(soup)

    # --- contact ----------------------------------------------------------
    agent_name = domx.text_of(soup, ".mb-ldp__pdtl__name", ".agent-name", "[class*='owner-name']")
    contact = ContactInfo(
        name=agent_name,
        seller_type=parse_seller_type(
            " ".join(
                filter(
                    None,
                    [
                        spec("posted by", "listed by"),
                        domx.text_of(soup, ".mb-ldp__pdtl__tag", ".posted-by"),
                    ],
                )
            )
        ),
        company=domx.text_of(soup, ".mb-ldp__pdtl__company", ".agency-name"),
        phone=parse_phone(domx.text_of(soup, "[href^='tel:']", ".contact-number")),
        email=parse_email(domx.text_of(soup, "[href^='mailto:']")),
    )

    project_name = domx.first(
        [
            spec("project", "society"),
            domx.text_of(soup, ".mb-ldp__dtls__title--project", "[class*='project-name']"),
        ]
    )
    builder_name = domx.first(
        [spec("builder", "developer"), domx.text_of(soup, "[class*='builder-name']")]
    )

    listed_at = parse_listing_date(spec("posted on", "listed on")) or parse_listing_date(
        domx.text_of(soup, ".mb-ldp__posted", "[class*='posted-on']")
    )

    record = PropertyRecord(
        source=Source.MAGICBRICKS,
        source_id=source_id,
        listing_url=url,
        title=title,
        description=description,
        project_name=project_name,
        builder_name=builder_name,
        society_name=spec("society"),
        listing_type=listing_type,
        property_type=property_type,
        property_type_raw=spec("property type"),
        is_commercial=commercial,
        configuration=normalize_configuration(config_text),
        bedrooms=bedrooms,
        bathrooms=parse_int(spec("bathroom")),
        balconies=parse_int(spec("balcon")),
        floor_number=floor_number,
        total_floors=total_floors,
        facing=clean_text(spec("facing")),
        furnishing=clean_text(spec("furnish")),
        age_years=parse_float(spec("age of construction", "property age")),
        price=price,
        price_max=price_max,
        price_display=price_text,
        price_per_sqft=price_per_sqft,
        booking_amount=parse_price(spec("booking amount")),
        maintenance_charge=parse_price(spec("maintenance")),
        rent_monthly=rent_monthly,
        security_deposit=parse_price(spec("security deposit", "deposit")),
        is_price_on_request=on_request,
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
        rera_number=parse_rera_number(spec("rera") or html[:200_000]),
        amenities=amenities,
        specifications=specs,
        images=_extract_property_images(soup, source_id),
        landmarks=landmarks,
        contact=contact,
        listed_at=listed_at,
        listing_date_raw=spec("posted on", "listed on"),
        raw_html_key=raw_html_key,
        raw={"json_ld": ld, "specs": specs},
    )
    record.segment = classify_segment(record.price or record.rent_monthly, listing_type)
    record.is_luxury = record.segment.value in {"luxury", "ultra_luxury"}
    record.is_affordable = record.segment.value == "affordable"
    return record.finalize()


# ---------------------------------------------------------------------------
# project (new-launch) pages
# ---------------------------------------------------------------------------


def parse_project_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> PropertyRecord | None:
    """MagicBricks project pages map onto PropertyRecord with
    `listing_type=project`; the ETL splits them into `projects`."""
    record = parse_property_detail(html, url, raw_html_key=raw_html_key)
    if record is None:
        return None

    soup = BeautifulSoup(html, "lxml")
    record.listing_type = ListingType.PROJECT
    record.project_name = record.project_name or record.title
    record.unit_configurations = _parse_unit_table(soup)
    record.total_units = record.total_units or parse_int(
        domx.text_of(soup, "[class*='total-units']")
    )
    record.project_area_acres = record.project_area_acres or parse_float(
        domx.text_of(soup, "[class*='project-area']")
    )
    if record.unit_configurations and record.price is None:
        prices = [c.price_min for c in record.unit_configurations if c.price_min]
        if prices:
            record.price = min(prices)
            record.price_max = max(
                [c.price_max or c.price_min for c in record.unit_configurations if c.price_min]
            )
    return record.finalize()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ld_price_text(ld: dict[str, Any]) -> str | None:
    for path in ("offers.price", "offers.lowPrice", "price"):
        value = domx.deep_get(ld, path)
        if value:
            return str(value)
    return None


def _area_sqft(text: str | None) -> float | None:
    value, unit = parse_area(text)
    return to_sqft(value, unit)


def _address_from_title(title: str | None) -> str | None:
    """"3 BHK Flat for Sale in Sector 82, Gurgaon" → "Sector 82, Gurgaon"."""
    if not title:
        return None
    match = re.search(r"\b(?:in|at)\s+(.+)$", title, re.I)
    return clean_text(match.group(1)) if match else None


_LANDMARK_CATEGORIES = {
    "metro": "metro",
    "railway": "transport",
    "bus": "transport",
    "airport": "transport",
    "school": "school",
    "college": "school",
    "university": "school",
    "hospital": "hospital",
    "clinic": "hospital",
    "mall": "mall",
    "shopping": "mall",
    "market": "mall",
    "restaurant": "food",
    "park": "recreation",
}


def _parse_landmarks(soup: BeautifulSoup) -> list[Landmark]:
    landmarks: list[Landmark] = []
    for block in domx.select_all(
        soup,
        ".mb-ldp__nearby__list--item",
        "[class*='nearby'] li",
        "[class*='landmark'] li",
    ):
        text = clean_text(block.get_text(" "))
        if not text:
            continue
        category = "other"
        lowered = text.lower()
        for keyword, mapped in _LANDMARK_CATEGORIES.items():
            if keyword in lowered:
                category = mapped
                break
        distance_match = re.search(r"([\d.]+)\s*(km|m)\b", lowered)
        distance_km = None
        if distance_match:
            value = float(distance_match.group(1))
            distance_km = value if distance_match.group(2) == "km" else round(value / 1000, 3)
        name = re.sub(r"[\d.]+\s*(km|m)\b.*$", "", text, flags=re.I).strip(" -–,")
        if name:
            landmarks.append(
                Landmark(
                    category=category,
                    name=name[:200],
                    distance_km=distance_km,
                    raw_distance=distance_match.group(0) if distance_match else None,
                )
            )
    return landmarks[:60]


def _parse_unit_table(soup: BeautifulSoup) -> list[UnitConfiguration]:
    configs: list[UnitConfiguration] = []
    for row in domx.select_all(
        soup, ".mb-ldp__floorplan tr", "[class*='floor-plan'] tr", "[class*='unit-table'] tr"
    ):
        cells = [clean_text(c.get_text(" ")) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 2 or any(c.lower() in {"configuration", "type"} for c in cells[:1]):
            continue
        config_text, area_text = cells[0], cells[1]
        price_text = cells[2] if len(cells) > 2 else None
        price_min, price_max = parse_price_range(price_text)
        area_value, area_unit = parse_area(area_text)
        configs.append(
            UnitConfiguration(
                configuration=normalize_configuration(config_text),
                bedrooms=parse_bedrooms(config_text),
                area_sqft=to_sqft(area_value, area_unit),
                price_min=price_min,
                price_max=price_max,
                price_display=price_text,
            )
        )
    return configs


#: MagicBricks slugs a few cities differently from their common name.
_CITY_SLUGS = {
    "delhi": "new-delhi",
    "new delhi": "new-delhi",
    "gurugram": "gurgaon",
    "greater noida": "greater-noida",
}


#: Commercial sub-types (and villa/plot, which have the same problem on the
#: residential side) live under a distinct URL *prefix* each, not a query
#: filter on the generic "/property-for-sale-..." search — that generic path
#: mixes every residential type together, so a niche category like "villa"
#: (491 of them in Gurgaon alone) is easily undersampled to near-zero by a
#: citywide crawl budget spent mostly on apartments. Verified live against
#: gurgaon; each returned real cards. "warehouse"/"offices" (plural) 404 —
#: "office-space" is correct. Residential plots have no rent variant (empty
#: category, 404s) — plots for rent are essentially nonexistent in this
#: market, so no entry for it here.
_COMMERCIAL_PREFIXES = {
    "shop": "shops",
    "office": "office-space",
    "showroom": "showroom",
    "commercial-land": "commercial-land",
    "industrial-land": "industrial-land",
    "villa": "villa",
    "plot": "residential-plots-land",
    # Kept for backward compatibility with the old (non-functional) job
    # definition — maps to the closest real category rather than 404ing.
    "commercial-real-estate": "office-space",
}


def build_search_url(
    *,
    city: str,
    listing_type: str = "sale",
    property_type: str | None = None,
    page: int = 1,
) -> str:
    """Public search URL.

    Verified live: sale and rent use *different* suffixes — `-pppfs` for sale,
    `-pppfr` for rent. Using pppfs for rent 404s, which is how the first
    version of this failed.

        sale        https://www.magicbricks.com/property-for-sale-in-gurgaon-pppfs
        rent        https://www.magicbricks.com/property-for-rent-in-gurgaon-pppfr
        commercial  https://www.magicbricks.com/shops-for-sale-in-gurgaon-pppfs
        page        …-pppfs?page=2

    Kept in the parser module so the scraper stays free of URL trivia and this
    stays unit-testable.
    """
    slug = city.strip().lower()
    slug = _CITY_SLUGS.get(slug, slug).replace(" ", "-")
    suffix = "pppfr" if listing_type == "rent" else "pppfs"
    prefix = _COMMERCIAL_PREFIXES.get(property_type or "", "property")

    path = f"/{prefix}-for-{listing_type}-in-{slug}-{suffix}"
    query = f"?page={page}" if page > 1 else ""
    return f"{BASE_URL}{path}{query}"


def build_apartment_search_url(
    city: str, *, listing_type: str = "sale", locality: str | None = None, page: int = 1
) -> str:
    """MagicBricks' dedicated "flats" category — apartments mixed in with
    the generic residential feed are easily undersampled (21,519 apartments
    is only a fraction of Gurgaon's ~25,607 total listings), and its own
    pagination caps at page 100 (page 101+ silently repeats page 1's
    results — confirmed live), so exhaustive coverage needs one request per
    locality rather than one deep paginated stream.

    Verified live: sale and rent use *different* url grammar for this one
    category specifically — `build_search_url`'s uniform
    `{prefix}-for-{listing_type}-in-{slug}-{suffix}` template only holds for
    rent here:

        sale (city)      /flats-in-gurgaon-for-sale-pppfs
        sale (locality)  /flats-in-sector-56-gurgaon-for-sale-pppfs
        rent (city)       /flats-for-rent-in-gurgaon-pppfr
        rent (locality)  /flats-for-rent-in-sector-56-gurgaon-pppfr
    """
    slug = city.strip().lower()
    slug = _CITY_SLUGS.get(slug, slug).replace(" ", "-")
    scope = f"{locality.strip().lower().replace(' ', '-')}-{slug}" if locality else slug
    suffix = "pppfr" if listing_type == "rent" else "pppfs"
    path = (
        f"/flats-for-rent-in-{scope}-{suffix}"
        if listing_type == "rent"
        else f"/flats-in-{scope}-for-sale-{suffix}"
    )
    query = f"?page={page}" if page > 1 else ""
    return f"{BASE_URL}{path}{query}"
