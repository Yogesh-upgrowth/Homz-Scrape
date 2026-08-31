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
from homz.common.enums import ListingType, PossessionStatus, SellerType, Source
from homz.common.geo import build_location
from homz.common.parsing import (
    absolute_url,
    classify_segment,
    clean_text,
    dedupe_preserve_order,
    is_commercial,
    is_price_on_request,
    normalize_configuration,
    parse_area,
    parse_area_sqft,
    parse_bedrooms,
    parse_float,
    parse_floor,
    parse_int,
    parse_possession_date,
    parse_possession_status,
    parse_price,
    parse_price_range,
    parse_property_type,
    parse_rera_number,
    to_sqft,
)
from homz.common.schema import (
    ContactInfo,
    Image,
    Landmark,
    ProjectRecord,
    PropertyRecord,
    UnitConfiguration,
)

BASE_URL = "https://www.squareyards.com"
IMAGE_HOSTS = ("static.squareyards.com", "squareyards.com")

_RESIZE_QUERY_RE = re.compile(r"[?&]aio=[^&]*")


def _strip_resize(url: str) -> str:
    """Drop squareyards' own `?aio=w-N;h-N;crop;` resize/crop directive.

    Verified live against a real listing photo: the `h-438` variant this
    directive produces is a *cropped* 603x438, while the same path with the
    query dropped serves the uncropped 603x800 original — that crop, stretched
    back out to card size by the frontend, is what "blurry" turns out to mean.
    """
    return _RESIZE_QUERY_RE.sub("", url)


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


_RENTAL_URL_RE = re.compile(r"squareyards\.com/rental-[a-z0-9-]+/\d+", re.I)


def parse_jsonld_rental_urls(html: str) -> list[str]:
    """Individual rental-listing URLs from a `/rent/property-for-rent-in-*`
    search page's JSON-LD.

    Unlike project listing pages, card anchors here only exist once the
    page's JS runs, but every card still emits a residence block
    (`Apartment`/`House`/...) plus a paired `RentAction` server-side, both
    carrying the same canonical listing `url` — matching on that url pattern
    picks up either block without needing to enumerate every `@type` schema.org
    uses for a rented residence.
    """
    from homz.common.parsing import canonical_url

    urls: list[str] = []
    seen: set[str] = set()
    for node in _iter_jsonld(html):
        url = node.get("url")
        if not isinstance(url, str) or not _RENTAL_URL_RE.search(url):
            continue
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        urls.append(key)
    return urls


def build_rent_search_url(city: str, *, page: int = 1, category: str = "property-for-rent") -> str:
    """`/rent/property-for-rent-in-{city}` — NOT `/{city}/property-for-rent`
    (that 404s; squareyards' own 404 page links to the working pattern) and
    NOT `/rental/search?...` (robots.txt disallows `/rental/search*` and
    several of its query params outright, so that endpoint is off-limits
    regardless of what it renders live).

    `category` swaps in a type-scoped variant of the same page shape, e.g.
    `apartments-for-rent` -> `/rent/apartments-for-rent-in-{city}` — verified
    live to carry its own independent listing count (14,338+ for Gurgaon
    apartments vs. 34,882+ for the unscoped page) and its own "Popular
    Localities" widget, so `parse_locality_links` works against it unchanged.
    """
    city_slug = city.strip().lower().replace(" ", "-")
    url = f"{BASE_URL}/rent/{category}-in-{city_slug}"
    return f"{url}?page={page}" if page > 1 else url


_RESALE_URL_RE = re.compile(r"squareyards\.com/resale-[a-z0-9-]+/\d+", re.I)


def parse_resale_urls(html: str) -> list[str]:
    """Individual resale-listing URLs from a `/sale/property-for-sale-in-*`
    search page's JSON-LD `ItemList`.

    Unlike the rent page's per-card JSON-LD, this page publishes one
    `ItemList` block whose `itemListElement` mixes individual
    `/resale-{slug}/{id}` listings with builder-project urls — the latter are
    already covered by the existing sitemap+listing project discovery, so
    filter down to just the resale shape.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for node in _iter_jsonld(html):
        items = node.get("itemListElement")
        if not isinstance(items, list):
            continue
        for item in items:
            url = item.get("url") if isinstance(item, dict) else None
            if not isinstance(url, str) or not _RESALE_URL_RE.search(url) or url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def build_sale_search_url(city: str, *, page: int = 1, category: str = "property-for-sale") -> str:
    """`/sale/property-for-sale-in-{city}` — the resale-listing analogue of
    `build_rent_search_url`; NOT `/resale/search?...`, which robots.txt
    disallows the same way it disallows `/rental/search*`.

    `category` swaps in a type-scoped variant, e.g. `apartments-for-sale` ->
    `/sale/apartments-for-sale-in-{city}` (verified live: 16,232+ for Gurgaon
    apartments, its own independent count and "Popular Localities" widget)."""
    city_slug = city.strip().lower().replace(" ", "-")
    url = f"{BASE_URL}/sale/{category}-in-{city_slug}"
    return f"{url}?page={page}" if page > 1 else url


_LOCALITY_LINK_RE = re.compile(
    r'href="(https://www\.squareyards\.com/(?:rent|sale)/[a-z-]+-in-[a-z0-9-]+)"',
    re.I,
)


def parse_locality_links(html: str, *, city: str, base_url: str) -> list[str]:
    """"Popular Localities" links off a city-wide rent or sale search page.

    Each is the same crawlable `/rent/property-for-rent-in-*` (or
    `/sale/property-for-sale-in-*`) URL shape as the city page passed in via
    `base_url`, just scoped to one sector/micro-market (e.g.
    `...-in-sector-56-gurgaon`). A citywide sample of a few hundred listings
    out of tens of thousands can easily land zero for any one sector — paging
    through these too is how a sector-scoped filter ends up with real
    listings instead of whatever the firehose sample happened to include.

    Real bug this guards against: the same page also has a "nearby cities"
    widget (its FAQ section links "Property for Rent in Delhi", "...in
    Noida", etc.) using this *exact* URL shape with no locality segment at
    all — indistinguishable from a locality link by pattern alone. Requiring
    the slug to end with `-{city}` excludes those other cities' bare pages
    (and the current city's own bare page) without excluding a genuine
    locality whose slug happens to end in the city name twice, e.g.
    `central-gurgaon-gurgaon`.

    Second real bug hit live: the *sale* page's own "Popular Localities"
    widget also links its `/rent/property-for-rent-in-gurgaon` counterpart
    (a mode-switch link) — matches `_LOCALITY_LINK_RE` and ends with
    `-gurgaon` just like a real locality, so it must be excluded by requiring
    the matched url to share `base_url`'s own `/rent/...-in-` or
    `/sale/...-in-` prefix, not just any prefix the regex allows.
    """
    city_slug = city.strip().lower().replace(" ", "-")
    suffix = f"-{city_slug}"
    base_url = base_url.rstrip("/")
    prefix = base_url.rsplit("-in-", 1)[0] + "-in-"
    seen: set[str] = {base_url}
    urls: list[str] = []
    for match in _LOCALITY_LINK_RE.finditer(html):
        url = match.group(1).rstrip("/")
        if url in seen or not url.startswith(prefix) or not url.lower().endswith(suffix):
            continue
        seen.add(url)
        urls.append(url)
    return urls


# ---------------------------------------------------------------------------
# project detail (PDP)
# ---------------------------------------------------------------------------


def _extract_project_images(soup: BeautifulSoup, *, base_url: str) -> list[Image]:
    """Prefer the page's schema.org `ImageGallery` block over scraping `<img>` tags.

    SquareYards' server-rendered gallery `<img>`s carry a `?aio=w-N;h-N;crop;`
    resize directive capped well below the source resolution (a 931x350 cover
    shot, 192x168 "peek" thumbnails for the rest — that's what "blurry" turns
    out to mean once stretched to fill a card), and the same page also runs a
    "similar projects" carousel through plain `<img>` tags for *other*
    projects entirely. The `ImageGallery` JSON-LD block that every PDP also
    emits lists this project's own photos only, as original URLs with no
    resize suffix at all — strictly better resolution and provenance, so it
    wins whenever present.
    """
    gallery = domx.json_ld_of_type(soup, "ImageGallery")
    entries = gallery.get("image") if isinstance(gallery, dict) else None
    if isinstance(entries, list):
        images: list[Image] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = absolute_url(base_url, entry.get("contentUrl"))
            if not url:
                continue
            key = url.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            images.append(
                Image(url=url, caption=clean_text(entry.get("caption")), is_primary=not images)
            )
        if images:
            return images[:40]

    # Fallback: a bare "img" selector would also sweep in every thumbnail
    # from the "similar projects" carousel (other projects' photos, not this
    # one's) — exclude that subtree by DOM position, not by URL shape, since
    # a lone flagship thumbnail for *this* project (when it has no full
    # gallery uploaded) lives at an identical-looking path and would
    # otherwise be excluded right along with the carousel, leaving no image
    # at all instead of the one low-res photo the project actually has.
    fallback = domx.extract_images(
        soup,
        base_url=base_url,
        selectors=("img:not([class*='project-tile'] img)",),
        allow_hosts=IMAGE_HOSTS,
    )
    for image in fallback:
        image.url = _strip_resize(image.url)
    return fallback


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
        # data-reraid is taken as-is from the page, unvalidated — SquareYards
        # sometimes puts the *Registration Certificate Number* there instead
        # of the RERA Project ID (e.g. "GGM/1062/794/2026/34", missing the
        # "HARERA" marker _RERA_PATTERNS looks for), which then gets stored
        # as rera_number with no way to tell it apart from a real one.
        # Verified live 2026-08-31 by cross-checking against the actual
        # HRERA registry (see homz.scrapers.hrera). Routing the attribute
        # through parse_rera_number() rejects that shape instead of trusting
        # it outright, while still accepting a well-formed value (including
        # the full "RC/REP/HARERA/..." certificate format this codebase has
        # always treated as valid — see tests/test_scrapers.py).
        rera_number=domx.first(
            [
                parse_rera_number(domx.attr_of(soup, "data-reraid", ".accordion-header[data-reraid]")),
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
        images=_extract_project_images(soup, base_url=BASE_URL),
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
# individual listing — rent (`/rental-{slug}/{id}`) or resale
# (`/resale-{slug}/{id}`) — distinct from a project — one unit, one owner or
# agent, its own price/floor/furnishing, not a range across configurations
# ---------------------------------------------------------------------------

_LISTING_ID_RE = re.compile(r"/(\d+)/?(?:[?#].*)?$")


def _unit_info(soup: BeautifulSoup) -> dict[str, str]:
    """`.unit-info-list li` rows look like
    `<span class="span">Label<strong>Value</strong></span>` — label and value
    share one text node, so split on the `<strong>` rather than using
    `label_value_pairs()`, which expects them in separate elements."""
    out: dict[str, str] = {}
    for li in domx.select_all(soup, ".unit-info-list li"):
        span = li.select_one(".span")
        strong = span.find("strong") if span else None
        if not span or not strong:
            continue
        value = clean_text(strong.get_text(" "))
        label = clean_text(span.get_text(" ").replace(strong.get_text(" "), "", 1))
        if label and value:
            out[label.lower()] = value
    return out


def _extract_listing_images(soup: BeautifulSoup, *, base_url: str) -> list[Image]:
    """Individual listing PDPs carry no `ImageGallery` JSON-LD (unlike project
    PDPs) — stay scoped to the cover photo and gallery-launcher thumbnail
    (both marked `load-gallery`, either on the `<img>` itself or a wrapping
    `<div>`), which skips the unrelated "3D Virtual Tour" thumbnail, and
    strip the `?aio=...` resize query the same way project images do."""
    images = domx.extract_images(
        soup,
        base_url=base_url,
        selectors=("img[class*='gallery'], [class*='gallery'] img",),
        allow_hosts=IMAGE_HOSTS,
    )
    for image in images:
        image.url = _strip_resize(image.url)
    return images


def _parse_individual_listing(
    html: str, url: str, *, is_rent: bool, raw_html_key: str | None = None
) -> PropertyRecord | None:
    """Shared by `parse_rental_detail` and `parse_resale_detail` — rent and
    resale PDPs are the same template family (same `unit-info-list`, same
    `data-button="view-number"` attribute set), differing only in which
    price field the listing's amount belongs in and the RENT/SALE default."""
    match = _LISTING_ID_RE.search(url)
    if not match:
        return None
    source_id = match.group(1)

    soup = BeautifulSoup(html, "lxml")
    button = domx.select_one(soup, "[data-button='view-number']", "[data-button='contact']")
    attrs = button.attrs if button else {}

    title = domx.first(
        [
            attrs.get("propertytitle"),
            domx.text_of(soup, ".listing-title strong", ".listing-title"),
            domx.meta_content(soup, "og:title"),
        ]
    )
    if not title:
        return None

    property_type_raw = attrs.get("propertytype")
    property_type = parse_property_type(property_type_raw, title, url)
    commercial = is_commercial(property_type, title, url)
    # Same rule as elsewhere in this codebase: commercial is its own top-level
    # feed segment and must win over the plain residential Rent/Sale bucket.
    listing_type = ListingType.COMMERCIAL if commercial else (
        ListingType.RENT if is_rent else ListingType.SALE
    )

    info = _unit_info(soup)
    # unitType is "N/A" for plots/land — no BHK to report, `parse_bedrooms`
    # and `normalize_configuration` both already treat that as "no match".
    config_text = domx.first([attrs.get("unittype"), title])
    bedrooms = parse_bedrooms(config_text) or parse_int(info.get("bedroom"))
    floor_number, total_floors = parse_floor(info.get("floor"))

    price_text = attrs.get("totalprice") or attrs.get("priceamount")
    price_value = parse_price(price_text) if price_text else None
    if price_value is None:
        price_value, _ = parse_price_range(
            domx.text_of(soup, ".listing-price", "[class*='listing-price']")
        )
    rent_monthly = price_value if is_rent else None
    price = None if is_rent else price_value

    area_sqft = parse_area_sqft(attrs.get("area")) or parse_area_sqft(info.get("area"))

    location_raw = domx.first(
        [attrs.get("location"), domx.text_of(soup, ".listing-loction", ".listing-title")]
    )
    latitude = parse_float(domx.attr_of(soup, "data-lat", "[data-lat]"))
    longitude = parse_float(domx.attr_of(soup, "data-long", "[data-long]"))
    location = build_location(
        location_raw, extra_texts=(title, url), latitude=latitude, longitude=longitude
    )

    amenities = dedupe_preserve_order(
        domx.texts_of(soup, ".amenities-list li", "[class*='amenities'] li")
    )

    agent_name = clean_text(attrs.get("username"))
    user_type = (attrs.get("usertype") or "").lower()
    seller_type = (
        SellerType.OWNER
        if "owner" in user_type
        else SellerType.BUILDER
        if "builder" in user_type
        else SellerType.AGENT
        if agent_name
        else SellerType.UNKNOWN
    )
    contact = ContactInfo(
        name=agent_name, seller_type=seller_type, company=clean_text(attrs.get("brokerlocation"))
    )

    # "area" is dropped here: its value node also wraps the sqft/sqm/sqyard
    # unit-switcher dropdown, so `_unit_info()`'s text-diff comes out full of
    # unit-list noise; `attrs["area"]` above is the clean source for it.
    specifications = {k.title(): v for k, v in info.items() if k != "area"}
    if attrs.get("depositamount"):
        specifications["Deposit Amount"] = clean_text(attrs["depositamount"])

    record = PropertyRecord(
        source=Source.SQUAREYARDS,
        source_id=source_id,
        listing_url=url,
        title=title,
        description=domx.meta_content(soup, "og:description"),
        project_name=clean_text(attrs.get("projectname")),
        listing_type=listing_type,
        property_type=property_type,
        property_type_raw=property_type_raw,
        is_commercial=commercial,
        configuration=normalize_configuration(config_text),
        bedrooms=bedrooms,
        bathrooms=parse_int(info.get("bath")),
        floor_number=floor_number,
        total_floors=total_floors,
        furnishing=clean_text(info.get("furnishing status")),
        price=price,
        rent_monthly=rent_monthly,
        is_price_on_request=price_value is None,
        area_sqft=area_sqft,
        location=location,
        amenities=amenities,
        specifications=specifications,
        images=_extract_listing_images(soup, base_url=BASE_URL),
        contact=contact,
        raw_html_key=raw_html_key,
        raw={"unit_info": info},
    )
    record.segment = classify_segment(price_value, listing_type)
    record.is_luxury = record.segment.value in {"luxury", "ultra_luxury"}
    record.is_affordable = record.segment.value == "affordable"
    return record.finalize()


def parse_rental_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> PropertyRecord | None:
    """`/rental-{slug}/{id}` — one specific unit posted by an owner or agent."""
    return _parse_individual_listing(html, url, is_rent=True, raw_html_key=raw_html_key)


def parse_resale_detail(
    html: str, url: str, *, raw_html_key: str | None = None
) -> PropertyRecord | None:
    """`/resale-{slug}/{id}` — an individual owner/broker sale listing,
    distinct from a builder's new-launch project page."""
    return _parse_individual_listing(html, url, is_rent=False, raw_html_key=raw_html_key)


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
