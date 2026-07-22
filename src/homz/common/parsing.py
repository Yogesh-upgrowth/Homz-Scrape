"""The common parser: reusable text→typed-value helpers for Indian real estate.

Everything here is pure and unit-testable. Source-specific parsers select the
DOM nodes; this module interprets the strings inside them.

Handles the things that actually break naive parsers on Indian portals:
  * ₹1.25 Cr / 85 Lac / 45,00,000 / 2.5 Crore / "Price on Request"
  * 1,250 sq.ft. / 145 sq yards / 2 Acres / 120 Gaj
  * "3 BHK", "2 BHK + Study", "4BHK+Servant"
  * "Ready to Move", "Possession by Dec '26", "Under Construction"
  * "Sector 82A, Gurgaon" / "Sec-102 Dwarka Expressway"
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as date_parser

from homz.common.enums import (
    AreaUnit,
    City,
    ListingType,
    PossessionStatus,
    PropertyType,
    Segment,
    SellerType,
)

# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_NBSP = " "


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace, normalize unicode, drop empties."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).replace(_NBSP, " ")
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def slugify(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text


def normalize_name(value: str | None) -> str | None:
    """Canonical form of a builder/project name for matching.

    "M3M India Pvt. Ltd." and "M3M INDIA" collapse to the same key.
    """
    text = clean_text(value)
    if not text:
        return None
    text = text.lower()
    text = re.sub(
        r"\b(pvt\.?|private|ltd\.?|limited|llp|inc\.?|corp\.?|co\.?|"
        r"builders?|developers?|group|infra(structure)?|realty|realtors?|"
        r"propert(y|ies)|projects?|estates?|homes?|housing|constructions?|"
        r"buildtech|infratech|lifespaces?|india)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"(\d[\d,]*\.?\d*)")
_PRICE_ON_REQUEST_RE = re.compile(
    r"price\s*on\s*request|on\s*request|call\s*for\s*price|p\.?o\.?r\.?\b|ask\s*price",
    re.I,
)

_MULTIPLIERS: tuple[tuple[re.Pattern[str], Decimal], ...] = (
    (re.compile(r"\bcr(?:ore)?s?\b|\bcrs?\b", re.I), Decimal("10000000")),
    (re.compile(r"\blakh?s?\b|\blac?s?\b|\blakhs?\b|\bl\b", re.I), Decimal("100000")),
    (re.compile(r"\bthousand\b|\bk\b", re.I), Decimal("1000")),
    (re.compile(r"\bmillion\b|\bmn\b", re.I), Decimal("1000000")),
    (re.compile(r"\bbillion\b|\bbn\b", re.I), Decimal("1000000000")),
)


def is_price_on_request(text: str | None) -> bool:
    return bool(text and _PRICE_ON_REQUEST_RE.search(text))


# A price token: optional currency marker, the number, optional unit word.
# Anchoring the unit *immediately* after the number is what stops
# "3 BHK in Sector 82 for 1.2 Cr" from reading as 3 Cr.
_PRICE_TOKEN_RE = re.compile(
    r"(?P<cur>₹|rs\.?|inr)?\s*"
    r"(?P<num>\d[\d,]*\.?\d*)\s*"
    r"(?P<unit>cr(?:ore)?s?|crs?|lakh?s?|lacs?|lakhs?|thousand|million|mn|billion|bn|[lk])?\b",
    re.I,
)


def _unit_multiplier(unit: str | None) -> Decimal | None:
    if not unit:
        return None
    for pattern, multiplier in _MULTIPLIERS:
        if pattern.fullmatch(unit) or pattern.match(unit):
            return multiplier
    return None


def _price_parts(text: str | None) -> tuple[Decimal | None, bool]:
    """Return (unrounded INR amount, whether a unit word was attached).

    Rounding is deliberately *not* applied here: "1.2" in "1.2 - 2.4 Cr" has to
    survive as 1.2 until the range parser can borrow the Cr from the right-hand
    side. Quantizing first would turn it into 1.
    """
    text = clean_text(text)
    if not text or is_price_on_request(text):
        return None, False

    with_unit: Decimal | None = None
    with_currency: Decimal | None = None
    first_number: Decimal | None = None

    for match in _PRICE_TOKEN_RE.finditer(text):
        try:
            amount = Decimal(match.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        if amount <= 0:
            continue

        multiplier = _unit_multiplier(match.group("unit"))
        if multiplier is not None and with_unit is None:
            with_unit = amount * multiplier
        if match.group("cur") and with_currency is None:
            with_currency = amount
        if first_number is None:
            first_number = amount

    if with_unit is not None:
        return with_unit, True
    return (with_currency if with_currency is not None else first_number), False


def parse_price(text: str | None) -> Decimal | None:
    """Parse an Indian price string into absolute INR.

    >>> parse_price("₹ 1.25 Cr")
    Decimal('12500000')
    >>> parse_price("85 Lac")
    Decimal('8500000')
    >>> parse_price("Rs. 45,00,000")
    Decimal('4500000')
    >>> parse_price("3 BHK in Sector 82 for 1.2 Cr")
    Decimal('12000000')

    Candidate selection, best first:
      1. a number with a unit word directly attached ("1.2 Cr")
      2. a number directly preceded by a currency marker ("₹45,00,000")
      3. the first number in the string
    """
    amount, _ = _price_parts(text)
    return amount.quantize(Decimal("1")) if amount is not None else None


def parse_price_range(text: str | None) -> tuple[Decimal | None, Decimal | None]:
    """"₹1.2 Cr - 2.4 Cr" → (12000000, 24000000).

    A bare unit on the right side applies to the left side too: "1.2 - 2.4 Cr".
    """
    text = clean_text(text)
    if not text:
        return None, None
    parts = re.split(r"\s*(?:-|–|—|to)\s*", text, maxsplit=1)
    if len(parts) == 1:
        value = parse_price(text)
        return value, None

    left_raw, right_raw = parts[0], parts[1]
    left, left_has_unit = _price_parts(left_raw)
    right, _ = _price_parts(right_raw)

    # Borrow the right-hand unit when the left has none: "1.2 - 2.4 Cr".
    if left is not None and right is not None and not left_has_unit:
        for match in _PRICE_TOKEN_RE.finditer(right_raw):
            multiplier = _unit_multiplier(match.group("unit"))
            if multiplier is not None:
                left *= multiplier
                break

    return (
        left.quantize(Decimal("1")) if left is not None else None,
        right.quantize(Decimal("1")) if right is not None else None,
    )


def parse_price_per_sqft(text: str | None) -> Decimal | None:
    """"₹12,500/sq.ft" → 12500. Rejects strings without a per-area marker."""
    text = clean_text(text)
    if not text:
        return None
    if not re.search(r"(per|/)\s*(sq\.?\s*(ft|feet|yd|yard|m)|sqft|sqyd)", text, re.I):
        return None
    return parse_price(text)


def format_price_inr(value: Decimal | int | float | None) -> str | None:
    """Render INR the way Indian users read it: 1.25 Cr / 85 L / 45,000."""
    if value is None:
        return None
    amount = Decimal(str(value))
    if amount >= 10_000_000:
        return f"{(amount / 10_000_000).normalize():f} Cr".replace(".000000", "")
    if amount >= 100_000:
        return f"{(amount / 100_000).normalize():f} L".replace(".000000", "")
    return f"{int(amount):,}"


# ---------------------------------------------------------------------------
# area
# ---------------------------------------------------------------------------

_AREA_TO_SQFT: dict[AreaUnit, float] = {
    AreaUnit.SQFT: 1.0,
    AreaUnit.SQYD: 9.0,
    AreaUnit.SQM: 10.7639,
    AreaUnit.ACRE: 43560.0,
    AreaUnit.HECTARE: 107639.0,
    AreaUnit.MARLA: 272.25,
    AreaUnit.KANAL: 5445.0,
}

_AREA_UNIT_PATTERNS: tuple[tuple[re.Pattern[str], AreaUnit], ...] = (
    (re.compile(r"\bsq\.?\s*(ft|feet|foot)\b|\bsqft\b|\bft2\b|\bsft\b", re.I), AreaUnit.SQFT),
    (
        re.compile(r"\bsq\.?\s*(yd|yds|yard|yards)\b|\bsqyd\b|\bgaj\b|\bgajj?\b", re.I),
        AreaUnit.SQYD,
    ),
    (re.compile(r"\bsq\.?\s*(m|mt|mtr|meter|metre)s?\b|\bsqm\b|\bm2\b", re.I), AreaUnit.SQM),
    (re.compile(r"\bacres?\b", re.I), AreaUnit.ACRE),
    (re.compile(r"\bhectares?\b|\bha\b", re.I), AreaUnit.HECTARE),
    (re.compile(r"\bmarlas?\b", re.I), AreaUnit.MARLA),
    (re.compile(r"\bkanals?\b", re.I), AreaUnit.KANAL),
)


def parse_area(text: str | None) -> tuple[float | None, AreaUnit | None]:
    """"1,250 sq.ft." → (1250.0, SQFT). Defaults to sqft when the unit is absent."""
    text = clean_text(text)
    if not text:
        return None, None
    match = _NUMBER_RE.search(text)
    if not match:
        return None, None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None, None
    if value <= 0:
        return None, None

    for pattern, unit in _AREA_UNIT_PATTERNS:
        if pattern.search(text):
            return value, unit
    return value, AreaUnit.SQFT


def to_sqft(value: float | None, unit: AreaUnit | None) -> float | None:
    if value is None:
        return None
    factor = _AREA_TO_SQFT.get(unit or AreaUnit.SQFT, 1.0)
    return round(value * factor, 2)


def parse_area_sqft(text: str | None) -> float | None:
    value, unit = parse_area(text)
    return to_sqft(value, unit)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

_BHK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+\s*\d+\s*)?\s*(bhk|bed|bedroom|rk)\b", re.I)
_STUDIO_RE = re.compile(r"\bstudio\b", re.I)


def parse_bedrooms(text: str | None) -> int | None:
    """"3 BHK" → 3, "1 RK" → 1, "Studio Apartment" → 0."""
    text = clean_text(text)
    if not text:
        return None
    match = _BHK_RE.search(text)
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return None
    if _STUDIO_RE.search(text):
        return 0
    return None


def normalize_configuration(text: str | None) -> str | None:
    """"3bhk+study" → "3 BHK + Study"."""
    text = clean_text(text)
    if not text:
        return None
    beds = parse_bedrooms(text)
    if beds is None:
        return text
    if beds == 0:
        return "Studio"
    label = "RK" if re.search(r"\brk\b", text, re.I) else "BHK"
    base = f"{beds} {label}"
    extras = []
    for token, pretty in (
        (r"\bstudy\b", "Study"),
        (r"\bservant\b|\bsq\b", "Servant"),
        (r"\bpooja\b|\bpuja\b", "Pooja"),
        (r"\butility\b", "Utility"),
    ):
        if re.search(token, text, re.I):
            extras.append(pretty)
    return f"{base} + {' + '.join(extras)}" if extras else base


def parse_int(text: str | None) -> int | None:
    text = clean_text(text)
    if not text:
        return None
    match = re.search(r"(\d[\d,]*)", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_float(text: str | None) -> float | None:
    text = clean_text(text)
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_floor(text: str | None) -> tuple[int | None, int | None]:
    """"5 out of 14" / "5th of 14 Floors" → (5, 14). Ground → 0."""
    text = clean_text(text)
    if not text:
        return None, None
    lowered = text.lower()
    numbers = [int(n) for n in re.findall(r"\d+", lowered)]
    floor: int | None = None
    total: int | None = None
    if re.search(r"\bground\b|\bgf\b", lowered):
        floor = 0
        total = numbers[0] if numbers else None
    elif re.search(r"\bbasement\b", lowered):
        floor = -1
        total = numbers[0] if numbers else None
    elif numbers:
        floor = numbers[0]
        if len(numbers) > 1:
            total = numbers[1]
    return floor, total


# ---------------------------------------------------------------------------
# possession / dates
# ---------------------------------------------------------------------------

_POSSESSION_PATTERNS: tuple[tuple[re.Pattern[str], PossessionStatus], ...] = (
    (
        re.compile(r"ready\s*to\s*move|ready\s*possession|immediate\s*possession|rtm\b", re.I),
        PossessionStatus.READY_TO_MOVE,
    ),
    (re.compile(r"under\s*construction|u/?c\b|ongoing", re.I), PossessionStatus.UNDER_CONSTRUCTION),
    (re.compile(r"new\s*launch|pre\s*launch|newly\s*launched", re.I), PossessionStatus.NEW_LAUNCH),
    (re.compile(r"upcoming|announced", re.I), PossessionStatus.UPCOMING),
    (re.compile(r"completed|delivered|occupied", re.I), PossessionStatus.COMPLETED),
)

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_MONTH_YEAR_RE = re.compile(rf"({_MONTHS})[\s,'`-]*(\d{{2,4}})", re.I)
_YEAR_ONLY_RE = re.compile(r"\b(20\d{2})\b")


def parse_possession_status(text: str | None) -> PossessionStatus:
    text = clean_text(text)
    if not text:
        return PossessionStatus.UNKNOWN
    for pattern, status in _POSSESSION_PATTERNS:
        if pattern.search(text):
            return status
    # A future possession date implies under construction.
    parsed = parse_possession_date(text)
    if parsed:
        return (
            PossessionStatus.UNDER_CONSTRUCTION
            if parsed > date.today()
            else PossessionStatus.COMPLETED
        )
    return PossessionStatus.UNKNOWN


def parse_possession_date(text: str | None) -> date | None:
    """"Dec '26" / "December 2026" / "2027" → a date (1st of the month)."""
    text = clean_text(text)
    if not text:
        return None
    match = _MONTH_YEAR_RE.search(text)
    if match:
        month_token, year_token = match.group(1), match.group(2)
        year = int(year_token)
        if year < 100:
            year += 2000
        try:
            return date_parser.parse(f"{month_token} 1 {year}").date()
        except (ValueError, OverflowError):
            return None
    match = _YEAR_ONLY_RE.search(text)
    if match:
        return date(int(match.group(1)), 1, 1)
    return None


_REL_DATE_RE = re.compile(
    r"(\d+)\s*(min(?:ute)?s?|hours?|hrs?|days?|weeks?|months?|years?)\s*ago", re.I
)


def parse_listing_date(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Portals mix "2 days ago", "Posted on 12 Jan 2026" and ISO strings."""
    text = clean_text(text)
    if not text:
        return None
    now = now or datetime.now(UTC)

    if re.search(r"\btoday\b|\bjust now\b", text, re.I):
        return now
    if re.search(r"\byesterday\b", text, re.I):
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - _timedelta_days(1)

    match = _REL_DATE_RE.search(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        days = {
            "min": 0,
            "hour": 0,
            "hr": 0,
            "day": 1,
            "week": 7,
            "month": 30,
            "year": 365,
        }
        for key, factor in days.items():
            if unit.startswith(key):
                if factor == 0:
                    return now
                return now - _timedelta_days(amount * factor)

    try:
        parsed = date_parser.parse(text, fuzzy=True, dayfirst=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Guard against fuzzy parsing turning "Sector 45" into a date.
    if not (2000 <= parsed.year <= now.year + 10):
        return None
    return parsed


def _timedelta_days(days: float):
    from datetime import timedelta

    return timedelta(days=days)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

_PROPERTY_TYPE_PATTERNS: tuple[tuple[re.Pattern[str], PropertyType], ...] = (
    (re.compile(r"builder\s*floor|independent\s*floor", re.I), PropertyType.BUILDER_FLOOR),
    (re.compile(r"\bvilla\b|row\s*house|town\s*house", re.I), PropertyType.VILLA),
    (re.compile(r"farm\s*house", re.I), PropertyType.FARMHOUSE),
    (re.compile(r"penthouse", re.I), PropertyType.PENTHOUSE),
    (re.compile(r"studio", re.I), PropertyType.STUDIO),
    (
        re.compile(r"independent\s*house|\bkothi\b|\bbungalow\b", re.I),
        PropertyType.INDEPENDENT_HOUSE,
    ),
    (re.compile(r"\bplot\b|\bland\b|residential\s*plot", re.I), PropertyType.PLOT),
    (re.compile(r"\boffice\b|office\s*space|\bit\s*space\b", re.I), PropertyType.OFFICE),
    (re.compile(r"\bshop\b|retail|\bsco\b", re.I), PropertyType.RETAIL_SHOP),
    (re.compile(r"showroom", re.I), PropertyType.SHOWROOM),
    (re.compile(r"warehouse|godown|industrial", re.I), PropertyType.WAREHOUSE),
    (re.compile(r"co\s*-?\s*working", re.I), PropertyType.CO_WORKING),
    (re.compile(r"service[d]?\s*apartment", re.I), PropertyType.SERVICED_APARTMENT),
    (re.compile(r"apartment|\bflat\b|multistorey", re.I), PropertyType.APARTMENT),
)

_COMMERCIAL_TYPES = {
    PropertyType.OFFICE,
    PropertyType.RETAIL_SHOP,
    PropertyType.SHOWROOM,
    PropertyType.WAREHOUSE,
    PropertyType.CO_WORKING,
}


def parse_property_type(*texts: str | None) -> PropertyType:
    """First match across all provided strings wins (most specific pattern first)."""
    blob = " ".join(clean_text(t) or "" for t in texts)
    if not blob:
        return PropertyType.OTHER
    for pattern, ptype in _PROPERTY_TYPE_PATTERNS:
        if pattern.search(blob):
            return ptype
    return PropertyType.OTHER


def is_commercial(ptype: PropertyType, *texts: str | None) -> bool:
    if ptype in _COMMERCIAL_TYPES:
        return True
    blob = " ".join(clean_text(t) or "" for t in texts)
    return bool(re.search(r"\bcommercial\b|\boffice space\b|\bretail\b", blob, re.I))


def parse_listing_type(*texts: str | None) -> ListingType:
    blob = " ".join(clean_text(t) or "" for t in texts).lower()
    if not blob:
        return ListingType.UNKNOWN
    if re.search(r"\bpg\b|paying\s*guest|\bhostel\b", blob):
        return ListingType.PG
    if re.search(r"for\s*rent|\brent(al)?\b|\blease\b|/rent", blob):
        return ListingType.RENT
    if re.search(r"new\s*launch|pre\s*-?launch", blob):
        return ListingType.NEW_LAUNCH
    if re.search(r"\bresale\b", blob):
        return ListingType.RESALE
    if re.search(r"for\s*sale|\bbuy\b|/sale|\bsell\b", blob):
        return ListingType.SALE
    if re.search(r"\bproject\b", blob):
        return ListingType.PROJECT
    return ListingType.UNKNOWN


def parse_seller_type(text: str | None) -> SellerType:
    text = (clean_text(text) or "").lower()
    if not text:
        return SellerType.UNKNOWN
    if "owner" in text:
        return SellerType.OWNER
    if re.search(r"builder|developer", text):
        return SellerType.BUILDER
    if re.search(r"agent|broker|dealer|consultant|realtor", text):
        return SellerType.AGENT
    return SellerType.UNKNOWN


# Segment thresholds are for NCR sale prices (INR). Rents use a scaled band.
_SALE_SEGMENTS: tuple[tuple[Decimal, Segment], ...] = (
    (Decimal("4500000"), Segment.AFFORDABLE),  # < 45 L
    (Decimal("15000000"), Segment.MID),  # 45 L – 1.5 Cr
    (Decimal("40000000"), Segment.PREMIUM),  # 1.5 – 4 Cr
    (Decimal("100000000"), Segment.LUXURY),  # 4 – 10 Cr
)


def classify_segment(price: Decimal | None, listing_type: ListingType) -> Segment:
    if price is None or price <= 0:
        return Segment.UNKNOWN
    if listing_type == ListingType.RENT:
        # Monthly rent bands.
        if price < 25_000:
            return Segment.AFFORDABLE
        if price < 75_000:
            return Segment.MID
        if price < 200_000:
            return Segment.PREMIUM
        if price < 500_000:
            return Segment.LUXURY
        return Segment.ULTRA_LUXURY
    for threshold, segment in _SALE_SEGMENTS:
        if price < threshold:
            return segment
    return Segment.ULTRA_LUXURY


# ---------------------------------------------------------------------------
# RERA
# ---------------------------------------------------------------------------

# Haryana: "RC/REP/HARERA/GGM/812/544/2024/45", UP: "UPRERAPRJ123456"
_RERA_PATTERNS = (
    re.compile(r"\b(?:RC/)?(?:REP/)?HARERA[/\-][A-Z0-9/\-]{6,40}", re.I),
    re.compile(r"\bUPRERA(?:PRJ|AGT)?[A-Z0-9]{4,20}\b", re.I),
    re.compile(r"\bDLRERA[A-Z0-9/\-]{4,30}\b", re.I),
    re.compile(r"\b\d{1,4}\s*OF\s*20\d{2}\b", re.I),
)


def parse_rera_number(text: str | None) -> str | None:
    text = clean_text(text)
    if not text:
        return None
    for pattern in _RERA_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).upper().strip(" .,")
    return None


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"(?:\+91[\-\s]?)?[6-9]\d{9}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def parse_phone(text: str | None) -> str | None:
    """Extract a publicly listed Indian mobile number, normalized to +91XXXXXXXXXX."""
    text = clean_text(text)
    if not text:
        return None
    match = _PHONE_RE.search(text.replace(" ", "").replace("-", ""))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))[-10:]
    return f"+91{digits}" if len(digits) == 10 else None


def parse_email(text: str | None) -> str | None:
    text = clean_text(text)
    if not text:
        return None
    match = _EMAIL_RE.search(text)
    return match.group(0).lower() if match else None


def absolute_url(base: str, href: str | None) -> str | None:
    from urllib.parse import urljoin, urlsplit, urlunsplit

    href = clean_text(href)
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    joined = urljoin(base, href)
    parts = urlsplit(joined)
    if parts.scheme not in {"http", "https"}:
        return None
    # Drop the fragment; keep the query (portals encode filters there).
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def canonical_url(url: str) -> str:
    """Strip tracking params so the same listing hashes to one key."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    drop_prefixes = ("utm_", "gclid", "fbclid", "_ga", "mc_")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(drop_prefixes)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = clean_text(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def coalesce(*values):
    """First non-None, non-empty value."""
    for value in values:
        if value is not None and value != "" and value != []:
            return value
    return None


def city_from_text(text: str | None) -> City:
    """Thin re-export so parsers only need one import. See `homz.common.geo`."""
    from homz.common.geo import detect_city

    return detect_city(text)
