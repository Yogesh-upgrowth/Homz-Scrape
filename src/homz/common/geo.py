"""Delhi NCR gazetteer: city / sector / micro-market resolution.

This is deliberately rule-based rather than an LLM call — it runs on every
record, has to be deterministic, and NCR's naming is regular enough that
regex + a curated alias table beats a model here. The LLM layer only handles
what this cannot: free-text Reddit prose.
"""

from __future__ import annotations

import re

from homz.common.enums import City

# ---------------------------------------------------------------------------
# City aliases
# ---------------------------------------------------------------------------

_CITY_ALIASES: tuple[tuple[re.Pattern[str], City], ...] = (
    (re.compile(r"\bgreater\s*noida\b|\bgr\.?\s*noida\b|\bgnoida\b", re.I), City.GREATER_NOIDA),
    (re.compile(r"\bgurgaon\b|\bgurugram\b|\bggn\b|\bggm\b", re.I), City.GURGAON),
    (re.compile(r"\bnoida\b", re.I), City.NOIDA),
    (re.compile(r"\bfaridabad\b|\bfbd\b", re.I), City.FARIDABAD),
    (re.compile(r"\bghaziabad\b|\bgzb\b|\bindirapuram\b|\bvaishali\b|\bkaushambi\b", re.I),
     City.GHAZIABAD),
    (re.compile(r"\bsohna\b", re.I), City.SOHNA),
    (re.compile(r"\bnew\s*delhi\b|\bdelhi\b|\bdwarka\b(?!\s*express)|\brohini\b|\bsaket\b"
                r"|\bvasant\s*(kunj|vihar)\b|\bgreater\s*kailash\b|\bhauz\s*khas\b", re.I),
     City.DELHI),
    (re.compile(r"\bmanesar\b|\bbahadurgarh\b|\bsonipat\b|\bpalwal\b|\bmeerut\b|\bncr\b", re.I),
     City.OTHER_NCR),
)


def detect_city(*texts: str | None) -> City:
    """Resolve a city from free text.

    Order matters: 'Greater Noida' must be tested before 'Noida'.

    When no city is named outright, a micro-market mention is used as a
    fallback — "prices on Dwarka Expressway" is unambiguously Gurgaon to a
    local reader, and losing those records to `unknown` is a real recall gap.
    """
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return City.UNKNOWN
    for pattern, city in _CITY_ALIASES:
        if pattern.search(blob):
            return city
    for _name, pattern, market_city in _MICRO_MARKETS:
        if pattern.search(blob):
            return market_city
    return City.UNKNOWN


# ---------------------------------------------------------------------------
# Sector
# ---------------------------------------------------------------------------

# Matches: "Sector 82", "Sec-102", "sector 37C", "Sector-1A", "Sec 150 Noida"
_SECTOR_RE = re.compile(r"\b(?:sector|sec)[\s\-\.]*(\d{1,3}\s*[A-Da-d]?)\b", re.I)
_BLOCK_RE = re.compile(r"\b(?:block)[\s\-]*([A-Z]{1,2}\d?)\b", re.I)


def parse_sector(*texts: str | None) -> str | None:
    """Normalize any sector spelling to the canonical 'Sector 82A' form."""
    for text in texts:
        if not text:
            continue
        match = _SECTOR_RE.search(text)
        if match:
            token = re.sub(r"\s+", "", match.group(1)).upper()
            return f"Sector {token}"
    return None


def parse_block(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = _BLOCK_RE.search(text)
        if match:
            return f"Block {match.group(1).upper()}"
    return None


# ---------------------------------------------------------------------------
# Micro-markets — the corridors buyers and Reddit actually talk about
# ---------------------------------------------------------------------------

_MICRO_MARKETS: tuple[tuple[str, re.Pattern[str], City], ...] = (
    (
        "Dwarka Expressway",
        re.compile(r"dwarka\s*expressway|\bnpr\b|northern\s*peripheral\s*road|\bdxp\b", re.I),
        City.GURGAON,
    ),
    (
        "Golf Course Road",
        re.compile(r"golf\s*course\s*road(?!\s*ext)|\bgcr\b", re.I),
        City.GURGAON,
    ),
    (
        "Golf Course Extension Road",
        re.compile(r"golf\s*course\s*(ext(ension)?)\s*road|\bgcer\b", re.I),
        City.GURGAON,
    ),
    ("Sohna Road", re.compile(r"sohna\s*road", re.I), City.GURGAON),
    (
        "Southern Peripheral Road",
        re.compile(r"southern\s*peripheral\s*road|\bspr\b", re.I),
        City.GURGAON,
    ),
    (
        "New Gurgaon",
        re.compile(r"new\s*gurgaon|new\s*gurugram", re.I),
        City.GURGAON,
    ),
    ("MG Road", re.compile(r"\bmg\s*road\b", re.I), City.GURGAON),
    ("Cyber City", re.compile(r"cyber\s*city|cyber\s*hub|dlf\s*phase", re.I), City.GURGAON),
    (
        "Noida Expressway",
        re.compile(r"noida\s*(greater\s*noida\s*)?expressway|\bnoida\s*expy\b", re.I),
        City.NOIDA,
    ),
    ("Yamuna Expressway", re.compile(r"yamuna\s*expressway|\byeida\b", re.I), City.GREATER_NOIDA),
    ("Greater Noida West", re.compile(r"greater\s*noida\s*west|noida\s*extension", re.I),
     City.GREATER_NOIDA),
    ("Dwarka", re.compile(r"\bdwarka\b(?!\s*express)", re.I), City.DELHI),
    ("Neharpar", re.compile(r"neharpar|greater\s*faridabad", re.I), City.FARIDABAD),
    ("NH-24 / NH-9", re.compile(r"\bnh[\s\-]?(24|9)\b|raj\s*nagar\s*extension", re.I),
     City.GHAZIABAD),
    ("Indirapuram", re.compile(r"indirapuram", re.I), City.GHAZIABAD),
)

# Sector → micro-market, for the corridors where the mapping is well known.
_SECTOR_MICRO_MARKET: dict[tuple[City, str], str] = {}


def _seed_sector_map() -> None:
    dwarka_expy = [
        "Sector 37C", "Sector 37D", "Sector 88", "Sector 88A", "Sector 88B", "Sector 89",
        "Sector 90", "Sector 91", "Sector 92", "Sector 93", "Sector 95", "Sector 99",
        "Sector 102", "Sector 103", "Sector 104", "Sector 106", "Sector 107", "Sector 108",
        "Sector 109", "Sector 110", "Sector 110A", "Sector 111", "Sector 112", "Sector 113",
        "Sector 114",
    ]
    new_gurgaon = [
        "Sector 76", "Sector 77", "Sector 78", "Sector 79", "Sector 80", "Sector 81",
        "Sector 82", "Sector 82A", "Sector 83", "Sector 84", "Sector 85", "Sector 86",
        "Sector 92", "Sector 95A", "Sector 37D",
    ]
    spr = ["Sector 68", "Sector 69", "Sector 70", "Sector 70A", "Sector 71", "Sector 72",
           "Sector 74", "Sector 75", "Sector 61", "Sector 62", "Sector 63", "Sector 65"]
    gcer = ["Sector 55", "Sector 56", "Sector 57", "Sector 58", "Sector 59", "Sector 60",
            "Sector 65", "Sector 66", "Sector 67"]
    noida_expy = ["Sector 128", "Sector 132", "Sector 134", "Sector 135", "Sector 137",
                  "Sector 143", "Sector 150", "Sector 151", "Sector 152", "Sector 168"]
    gn_west = ["Sector 1", "Sector 2", "Sector 3", "Sector 4", "Sector 16B", "Sector 10",
               "Sector 12", "Sector 16C"]

    for sectors, market, city in (
        (dwarka_expy, "Dwarka Expressway", City.GURGAON),
        (new_gurgaon, "New Gurgaon", City.GURGAON),
        (spr, "Southern Peripheral Road", City.GURGAON),
        (gcer, "Golf Course Extension Road", City.GURGAON),
        (noida_expy, "Noida Expressway", City.NOIDA),
        (gn_west, "Greater Noida West", City.GREATER_NOIDA),
    ):
        for sector in sectors:
            _SECTOR_MICRO_MARKET.setdefault((city, sector), market)


_seed_sector_map()


def detect_micro_market(
    *texts: str | None, city: City | None = None, sector: str | None = None
) -> str | None:
    """Explicit corridor mention wins; otherwise fall back to the sector map."""
    blob = " ".join(t for t in texts if t)
    if blob.strip():
        for name, pattern, market_city in _MICRO_MARKETS:
            if pattern.search(blob) and (city in (None, City.UNKNOWN) or city == market_city):
                return name
    if city and sector:
        return _SECTOR_MICRO_MARKET.get((city, sector))
    return None


# ---------------------------------------------------------------------------
# Locality extraction
# ---------------------------------------------------------------------------

_LOCALITY_NOISE = re.compile(
    r"\b(for\s+sale|for\s+rent|property|properties|apartment|flat|villa|plot|"
    r"in|at|near|,|india|haryana|uttar\s*pradesh|delhi\s*ncr)\b",
    re.I,
)


def parse_locality(raw: str | None, *, city: City | None = None) -> str | None:
    """Best-effort locality from a portal's breadcrumb/address string.

    "3 BHK Flat in Sector 82, Gurgaon" → "Sector 82"
    "Sushant Lok Phase 1, Gurgaon"     → "Sushant Lok Phase 1"
    """
    if not raw:
        return None
    sector = parse_sector(raw)
    if sector:
        return sector

    parts = [p.strip() for p in re.split(r"[,|·•]", raw) if p.strip()]
    candidates = []
    for part in parts:
        if detect_city(part) != City.UNKNOWN:
            continue
        cleaned = _LOCALITY_NOISE.sub(" ", part)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–")
        if len(cleaned) >= 3 and not cleaned.isdigit():
            candidates.append(cleaned)
    return candidates[0] if candidates else None


def build_location(
    raw: str | None,
    *,
    extra_texts: tuple[str | None, ...] = (),
    latitude: float | None = None,
    longitude: float | None = None,
):
    """One-call location builder used by every property parser."""
    from homz.common.schema import GeoPoint, Location

    texts = (raw, *extra_texts)
    city = detect_city(*texts)
    sector = parse_sector(*texts)
    locality = parse_locality(raw, city=city) or sector
    micro = detect_micro_market(*texts, city=city, sector=sector)

    geo = None
    # NCR bounding box — reject obviously bogus coordinates (0,0 is common).
    if (
        latitude is not None
        and longitude is not None
        and 27.0 <= latitude <= 29.5
        and 76.0 <= longitude <= 78.5
    ):
        geo = GeoPoint(latitude=latitude, longitude=longitude)

    state = {
        City.GURGAON: "Haryana",
        City.FARIDABAD: "Haryana",
        City.SOHNA: "Haryana",
        City.NOIDA: "Uttar Pradesh",
        City.GREATER_NOIDA: "Uttar Pradesh",
        City.GHAZIABAD: "Uttar Pradesh",
        City.DELHI: "Delhi",
    }.get(city)

    pincode_match = re.search(r"\b(1[0-9]{5})\b", raw or "")

    return Location(
        raw=raw,
        locality=locality,
        sector=sector,
        city=city,
        city_raw=raw,
        state=state,
        pincode=pincode_match.group(1) if pincode_match else None,
        micro_market=micro,
        geo=geo,
    )
