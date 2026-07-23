"""Rule-based extraction: builders, projects, sectors, topics, keywords.

This is the cheap deterministic tier of the enrichment pipeline. It runs on
100% of records at ingest time and costs nothing, so the warehouse is queryable
by builder/sector/topic even when the LLM tier is disabled or backlogged.

The LLM tier (see `llm.py`) only handles what rules genuinely cannot: nuanced
sentiment, summarisation, and entities outside the curated gazetteer.

Gazetteer maintenance: `NCR_BUILDERS` is the list of developers that actually
transact in NCR. Adding a name here immediately improves recall across every
historical Reddit post on the next enrichment pass.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from homz.common.enums import City, Sentiment
from homz.common.geo import detect_city
from homz.common.parsing import clean_text, normalize_name

# ---------------------------------------------------------------------------
# gazetteers
# ---------------------------------------------------------------------------

#: canonical name → alias patterns (lowercase, matched as whole words)
NCR_BUILDERS: dict[str, tuple[str, ...]] = {
    "DLF": ("dlf",),
    "M3M": ("m3m",),
    "Godrej Properties": ("godrej",),
    "Sobha": ("sobha",),
    "Signature Global": ("signature global", "signatureglobal", "signature glbl"),
    "Emaar India": ("emaar",),
    "Tata Housing": ("tata housing", "tata realty"),
    "Adani Realty": ("adani realty", "adani housing"),
    "Puravankara": ("puravankara", "purva"),
    "Central Park": ("central park",),
    "Vatika": ("vatika",),
    "Ireo": ("ireo",),
    "Unitech": ("unitech",),
    "Ansal API": ("ansal",),
    "BPTP": ("bptp",),
    "Raheja Developers": ("raheja",),
    "Experion Developers": ("experion",),
    "Elan Group": ("elan group", "elan "),
    "Smartworld Developers": ("smartworld", "smart world"),
    "Whiteland": ("whiteland",),
    "Trevoc": ("trevoc",),
    "AIPL": ("aipl",),
    "Conscient": ("conscient",),
    "Silverglades": ("silverglades", "silver glades"),
    "Anant Raj": ("anant raj",),
    "Pioneer Urban": ("pioneer urban",),
    "Mahindra Lifespaces": ("mahindra lifespace", "mahindra life"),
    "Shapoorji Pallonji": ("shapoorji", "joyville"),
    "ATS Infrastructure": ("ats infra", "ats greens", "ats "),
    "Gaursons": ("gaursons", "gaur "),
    "Supertech": ("supertech",),
    "Amrapali": ("amrapali",),
    "Jaypee Group": ("jaypee", "jaiprakash"),
    "Prateek Group": ("prateek",),
    "Eldeco": ("eldeco",),
    "Nirala World": ("nirala",),
    "Migsun": ("migsun",),
    "Ajnara": ("ajnara",),
    "Paras Buildtech": ("paras buildtech", "paras "),
    "County Group": ("county group",),
    "Lodha": ("lodha",),
    "Max Estates": ("max estates",),
    "Bharti Realty": ("bharti realty",),
    "Krisumi": ("krisumi",),
    "Trump Towers / Tribeca": ("tribeca", "trump tower"),
    "Sushma Group": ("sushma",),
    "Omaxe": ("omaxe",),
    "Ambience Group": ("ambience",),
    "Bestech": ("bestech",),
    "Orris Infrastructure": ("orris",),
    "SS Group": ("ss group",),
    "Suncity Projects": ("suncity",),
    "Hines": ("hines",),
    "Birla Estates": ("birla estates",),
    "Prestige Group": ("prestige group", "prestige estates"),
    "Runwal": ("runwal",),
    "Rise Infraventures": ("rise infra",),
    "Aarize": ("aarize",),
    "Navraj": ("navraj",),
    "Indiabulls Real Estate": ("indiabulls",),
    "Assotech": ("assotech",),
    "Mapsko": ("mapsko",),
    "Ramprastha": ("ramprastha",),
    "Tulip Infratech": ("tulip infratech", "tulip violet", "tulip white"),
}

#: Well-known NCR project/society names worth detecting verbatim.
KNOWN_PROJECTS: tuple[str, ...] = (
    "DLF Camellias", "DLF Magnolias", "DLF Aralias", "DLF Crest", "DLF Ultima",
    "DLF Privana", "DLF Arbour", "DLF Park Place", "DLF Belaire",
    "M3M Golf Estate", "M3M Merlin", "M3M Skywalk", "M3M Crown", "M3M Antalya Hills",
    "Godrej Aristocrat", "Godrej Meridien", "Godrej Air", "Godrej Habitat",
    "Godrej Woods", "Godrej Nurture", "Godrej Zenith",
    "Sobha City", "Sobha International City",
    "Signature Global City", "Signature Global Titanium", "Signature Global Deluxe",
    "Emaar Digi Homes", "Emaar Palm Heights", "Emaar Urban Oasis", "Emaar Marbella",
    "Central Park Flower Valley", "Central Park Resorts",
    "Vatika India Next", "Vatika Sovereign Park", "Vatika Seven Elements",
    "Ireo Victory Valley", "Ireo Grand Arch", "Ireo Skyon", "Ireo Uptown",
    "Tata Primanti", "Tata La Vida", "Tata Raisina Residency",
    "Experion Windchants", "Experion Elements", "Experion The Heartsong",
    "Elan The Presidential", "Elan Empire", "Elan Miracle",
    "Smartworld One DXP", "Smartworld Orchard", "Smartworld Gems",
    "Whiteland Blissville", "Whiteland Aspen",
    "Krisumi Waterfall Residences",
    "Max Estates 128", "Max Estates Estate 361",
    "ATS Marigold", "ATS Kocoon", "ATS Triumph", "ATS Tourmaline",
    "ATS Pristine", "ATS Greens Village", "ATS Knightsbridge",
    "Gaur City", "Gaur Saundaryam", "Gaur Yamuna City", "Gaur Runway Suites",
    "Supertech Eco Village", "Supertech Capetown", "Supertech Emerald Court",
    "Jaypee Greens", "Jaypee Wish Town", "Jaypee Klassic",
    "Prateek Grand City", "Prateek Edifice", "Prateek Stylome",
    "Nirala Estate", "Nirala Aspire",
    "Ace Divino", "Ace City", "Ace Parkway",
    "Eldeco Live By The Greens", "Eldeco Accolade",
    "Paras Dews", "Paras Quartier", "Paras Floret",
    "Sushant Lok", "Nirvana Country", "Palam Vihar", "South City",
    "Ambience Caitriona", "Ambience Creacions",
    "Bestech Park View", "Bestech Altura",
    "SS The Leaf", "SS Almeria", "SS Linden Floors",
    "Suncity Platinum Towers", "Suncity Avenue",
    "Mahindra Luminare",
    "Adani Samsara", "Adani Brahma Oyster",
    "Puri Diplomatic Greens", "Puri Emerald Bay",
    "Anant Raj Estate", "Anant Raj Ashok Estate",
    "Trevoc Royal Residences",
    "Aarize The Tessoro",
    "Navraj The Antalyas",
    "Birla Navya",
    "Lodha Bellagio",
    "Tulip Violet", "Tulip White", "Tulip Purple",
    "Ramprastha City", "Ramprastha Edge Towers",
    "Mapsko Casa Bella", "Mapsko Royale Ville",
    "Orris Aster Court", "Orris Carnation",
    "Assotech Blith",
    "Migsun Wynn", "Migsun Roof",
    "Ajnara Le Garden", "Ajnara Daffodil",
    "Amrapali Silicon City", "Amrapali Dream Valley",
    "Prestige Sonnet",
    "Indiabulls Enigma", "Indiabulls Centrum Park",
    "Conscient Hines Elevate",
    "Silverglades Legacy", "Silverglades Hightown",
    "Pioneer Presidia", "Pioneer Araya",
    "Unitech Uniworld", "Unitech Escape", "Unitech Harmony",
    "BPTP Amstoria", "BPTP Astaire Gardens", "BPTP Terra", "BPTP Freedom Park Life",
    "Raheja Revanta", "Raheja Vedaanta", "Raheja Shilas",
    "Omaxe The Forest Spa", "Omaxe Celebration Mall",
    "Joyville Gurugram",
    "AIPL Joy Street", "AIPL The Peaceful Homes",
    "Central Park Bellevue",
    "Sare Homes", "Emerald Hills", "Malibu Towne",
)


def _project_regex(name: str) -> re.Pattern[str]:
    """Tolerate hyphen/space/multi-space variants: "DLF Camellias",
    "DLF-Camellias" and "DLF  Camellias" all match one pattern."""
    parts = [re.escape(part) for part in name.split()]
    return re.compile(r"\b" + r"[-\s]+".join(parts) + r"\b", re.I)


_PROJECT_PATTERNS = tuple((name, _project_regex(name)) for name in KNOWN_PROJECTS)

_BUILDER_PATTERNS = tuple(
    (canonical, re.compile(r"|".join(rf"\b{re.escape(a)}\b" for a in aliases), re.I))
    for canonical, aliases in NCR_BUILDERS.items()
)

# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------

TOPIC_PATTERNS: dict[str, re.Pattern[str]] = {
    "buying": re.compile(r"\bbuy(ing)?\b|\bpurchas\w+|\bshould i buy\b|\bbooking\b", re.I),
    "selling": re.compile(r"\bsell(ing)?\b|\bexit\b|\boffload\b", re.I),
    "renting": re.compile(r"\brent(al|ing)?\b|\btenant\b|\blandlord\b|\blease\b", re.I),
    "builder_reputation": re.compile(
        r"builder\s*(reputation|track record|history|quality)|reliable builder", re.I
    ),
    "builder_fraud": re.compile(
        r"fraud|cheat(ed|ing)?|scam|duped|misappropriat\w+|siphon\w*|ponzi|"
        r"fake (promise|commitment)|eow complaint",
        re.I,
    ),
    "project_review": re.compile(r"project review|review of|worth (buying|investing)", re.I),
    "construction_delay": re.compile(
        r"delay(ed|s)?\b|behind schedule|stalled|slow (construction|progress)|not completed", re.I
    ),
    "possession_issue": re.compile(
        r"possession\s*(delay|issue|pending|date)|no possession|handover", re.I
    ),
    "maintenance_issue": re.compile(
        r"maintenance\s*(charge|issue|cost|fee)|cam charges|society dues|rwa", re.I
    ),
    "society_review": re.compile(r"society\b|apartment complex|rwa|gated community", re.I),
    "broker_experience": re.compile(r"\bbroker\b|\bagent\b|\bdealer\b|brokerage", re.I),
    "investment_advice": re.compile(
        r"invest(ment|ing)?\b|appreciation|capital gain|good (buy|investment)", re.I
    ),
    "sector_recommendation": re.compile(
        r"which sector|best sector|sector \d+ (or|vs)|recommend.*(sector|area)", re.I
    ),
    "roi": re.compile(r"\broi\b|return on investment|returns\b", re.I),
    "rental_yield": re.compile(r"rental yield|yield\b|rent to price", re.I),
    "legal_issue": re.compile(
        r"legal\b|litigation|court case|nclt|stay order|dispute|title (issue|clear)", re.I
    ),
    "rera": re.compile(r"\brera\b|harera|uprera|regulatory authority", re.I),
    "home_loan": re.compile(
        r"home ?loan|\bemi\b|interest rate|pre[- ]?payment|loan (approval|sanction)|lap\b", re.I
    ),
    "hidden_charges": re.compile(
        r"hidden charge|extra charge|\bedc\b|\bidc\b|plc\b|preferential location|"
        r"club (charge|membership)|ifms",
        re.I,
    ),
    "registration": re.compile(r"registr(y|ation)|conveyance deed|sale deed|mutation", re.I),
    "stamp_duty": re.compile(r"stamp duty|circle rate", re.I),
    "property_tax": re.compile(r"property tax|house tax|mcg tax", re.I),
    "infrastructure": re.compile(
        r"infrastructure|road (work|widening)|sewage|water supply|power cut|flooding|"
        r"waterlogging",
        re.I,
    ),
    "metro": re.compile(r"\bmetro\b|rapid metro|rrts|namo bharat", re.I),
    "dwarka_expressway": re.compile(r"dwarka expressway|\bnpr\b|northern peripheral", re.I),
    "spr_road": re.compile(r"\bspr\b|southern peripheral", re.I),
    "golf_course_road": re.compile(r"golf course (ext(ension)?\s*)?road|\bgcr\b", re.I),
    "new_gurgaon": re.compile(r"new gurgaon|new gurugram", re.I),
    "sohna_road": re.compile(r"sohna road|sohna\b", re.I),
    "noida_expressway": re.compile(r"noida (greater noida )?expressway|yamuna expressway", re.I),
    "market_outlook": re.compile(
        r"market (trend|outlook|crash|bubble|correction)|price (rise|drop|trend)|"
        r"appreciat\w+|oversupply",
        re.I,
    ),
}

# ---------------------------------------------------------------------------
# lexicon sentiment (fallback when the LLM tier is off)
# ---------------------------------------------------------------------------

_POSITIVE_TERMS = (
    "excellent", "great", "good", "happy", "satisfied", "recommend", "smooth",
    "on time", "timely", "quality", "worth", "best", "love", "impressed",
    "transparent", "hassle free", "no issues", "delivered", "appreciat",
    "premium", "well maintained", "responsive",
)
_NEGATIVE_TERMS = (
    "fraud", "scam", "cheat", "worst", "terrible", "horrible", "avoid", "regret",
    "delay", "delayed", "poor", "bad", "issue", "problem", "leak", "seepage",
    "harass", "litigation", "stuck", "pathetic", "overpriced", "misleading",
    "never buy", "don't buy", "dont buy", "stay away", "unprofessional",
    "no response", "cracks", "substandard",
)
_NEGATORS = re.compile(r"\b(not|no|never|isn'?t|wasn'?t|aren'?t|don'?t|didn'?t)\b", re.I)

_STOPWORDS = frozenset(
    # A prose block reads better than a 90-element list literal here, and it is
    # evaluated once at import.
    """a an the and or but if of in on at to for with without from by is are was were be been
    being have has had do does did will would shall should can could may might must this that
    these those i you he she it we they my your his her its our their me him them as so than
    then there here what which who whom how why when where all any both each few more most other
    some such only own same too very just also into over under about after before again further
    once
    """.split()  # noqa: SIM905 - readability over a 90-element literal
)


@dataclass
class ExtractedEntities:
    builders: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    city: City = City.UNKNOWN
    keywords: list[str] = field(default_factory=list)


def extract_builders(text: str | None) -> list[str]:
    if not text:
        return []
    found = [canonical for canonical, pattern in _BUILDER_PATTERNS if pattern.search(text)]
    return sorted(set(found))


def extract_projects(text: str | None) -> list[str]:
    if not text:
        return []
    found = [name for name, pattern in _PROJECT_PATTERNS if pattern.search(text)]
    return sorted(set(found))


def extract_sectors(text: str | None, *, limit: int = 10) -> list[str]:
    """All sector mentions, not just the first — a comparison post names several."""
    if not text:
        return []
    matches = re.findall(r"\b(?:sector|sec)[\s\-\.]*(\d{1,3}\s*[A-Da-d]?)\b", text, re.I)
    out: list[str] = []
    for token in matches:
        suffix = re.sub(r"\s+", "", token).upper()
        normalized = "Sector " + suffix
        if normalized not in out:
            out.append(normalized)
    return out[:limit]


def extract_keywords(text: str | None, *, limit: int = 15) -> list[str]:
    """Frequency-ranked content words, domain terms boosted."""
    if not text:
        return []
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{2,}", text.lower())
    counts = Counter(t for t in tokens if t not in _STOPWORDS and len(t) > 3)
    domain_boost = {
        "possession", "builder", "rera", "maintenance", "resale", "carpet",
        "registry", "loan", "yield", "sector", "society", "broker", "delay",
        "expressway", "metro", "investment", "rental", "construction",
    }
    for term in domain_boost:
        if term in counts:
            counts[term] += 5
    return [word for word, _ in counts.most_common(limit)]


def extract_topics(text: str | None, *, limit: int = 12) -> list[str]:
    if not text:
        return []
    scored: list[tuple[str, int]] = []
    for topic, pattern in TOPIC_PATTERNS.items():
        hits = len(pattern.findall(text))
        if hits:
            scored.append((topic, hits))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [topic for topic, _ in scored[:limit]]


def extract_entities(text: str | None) -> ExtractedEntities:
    text = clean_text(text)
    if not text:
        return ExtractedEntities()
    return ExtractedEntities(
        builders=extract_builders(text),
        projects=extract_projects(text),
        sectors=extract_sectors(text),
        city=detect_city(text),
        keywords=extract_keywords(text),
    )


def lexicon_sentiment(text: str | None) -> tuple[Sentiment, float]:
    """Fallback sentiment: term counts with simple negation handling.

    Returns (label, score) where score ∈ [-1, 1]. Not as good as the LLM pass —
    it exists so every row has *some* sentiment even with LLM disabled.
    """
    if not text:
        return Sentiment.NEUTRAL, 0.0
    lowered = text.lower()

    positive = 0
    negative = 0
    for term in _POSITIVE_TERMS:
        for match in re.finditer(re.escape(term), lowered):
            window = lowered[max(0, match.start() - 30) : match.start()]
            if _NEGATORS.search(window):
                negative += 1
            else:
                positive += 1
    for term in _NEGATIVE_TERMS:
        negative += len(re.findall(re.escape(term), lowered))

    total = positive + negative
    if total == 0:
        return Sentiment.NEUTRAL, 0.0

    score = round((positive - negative) / total, 3)
    if positive and negative and abs(score) < 0.2:
        return Sentiment.MIXED, score
    if score > 0.15:
        return Sentiment.POSITIVE, score
    if score < -0.15:
        return Sentiment.NEGATIVE, score
    return Sentiment.NEUTRAL, score


def canonical_builder(name: str | None) -> str | None:
    """Map a free-text builder mention onto the gazetteer's canonical name."""
    if not name:
        return None
    for canonical, pattern in _BUILDER_PATTERNS:
        if pattern.search(name):
            return canonical
    return clean_text(name)


def builder_match_key(name: str | None) -> str | None:
    return normalize_name(canonical_builder(name))
