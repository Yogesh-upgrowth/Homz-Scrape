"""Prompts and JSON schemas for the Claude enrichment tier.

Two hard constraints shape this file:

1. **The system prompt is frozen.** Prompt caching is a prefix match, so
   anything volatile (a timestamp, the record being processed) must live in the
   user turn, never in the system prompt. One byte of drift in the prefix
   invalidates the whole cache.

2. **Output is schema-constrained.** We pass `output_config.format` with a JSON
   schema rather than asking for "JSON please" — the API enforces the shape, so
   the parser downstream never has to defend against prose.
"""

from __future__ import annotations

from homz.common.enums import REDDIT_TOPICS

# ---------------------------------------------------------------------------
# system prompts (frozen — do not interpolate anything into these)
# ---------------------------------------------------------------------------

REDDIT_SYSTEM_PROMPT = """\
You are a real-estate market analyst for Delhi NCR (Gurgaon, Noida, Greater \
Noida, Delhi, Faridabad, Ghaziabad). You read public Reddit discussions and \
extract structured market intelligence.

Your job on each item:

1. Identify the builders/developers discussed. Use the full company name as \
normally written in India (e.g. "Signature Global", "DLF", "M3M"). Only list a \
builder if the text actually refers to that developer — do not infer a builder \
from a project name you are unsure about.

2. Identify named projects or societies (e.g. "Godrej Aristocrat", "Sushant \
Lok"). Do not list generic phrases like "my society".

3. Identify sectors and localities mentioned, normalized as "Sector 82",
"Sector 150", "Dwarka Expressway", "Golf Course Road".

4. Classify sentiment toward the property/builder/area being discussed — not \
the poster's general mood. A factual question with no opinion is neutral. A \
post with genuine praise and genuine complaints is mixed.

5. Assign topics from the fixed list you are given. Assign only topics that are \
substantively discussed, not merely mentioned in passing.

6. Extract concrete, checkable claims (e.g. "possession delayed 3 years at \
project X", "maintenance is 4.5/sqft"). Claims are what the platform surfaces \
to buyers, so precision matters more than volume.

Rules:
- Report only what the text supports. Never infer a builder's reputation from \
your own background knowledge; this is an extraction task, not a judgement task.
- If the text is not about real estate, set is_relevant to false and leave the \
other fields empty.
- Indian number formats: "1.2 Cr" = 12,000,000 INR; "85 L"/"85 lakh" = 8,500,000 INR.
- Be concise. The summary is at most two sentences.\
"""

PROPERTY_SYSTEM_PROMPT = """\
You are a real-estate data analyst for Delhi NCR. You normalize messy listing \
text from Indian property portals into structured fields.

Extract only what the listing text states or clearly implies. Leave a field \
null rather than guessing. In particular:

- builder_name: the developer of the project, not the listing agent or agency.
- project_name: the named development/society, not the locality.
- amenities: normalize to canonical names ("Swimming Pool", "Power Backup", \
"Club House"); drop marketing filler ("world-class living").
- tags: short searchable labels a buyer would filter on ("gated society", \
"corner unit", "park facing", "metro nearby", "vastu compliant").
- highlights: at most three genuinely distinguishing facts about this listing.
- concerns: at most three factual caveats a buyer should verify (e.g. "no RERA \
number stated", "possession date not given", "price well above locality median").

Do not invent RERA numbers, prices, or dates. Do not write marketing copy.\
"""

BUILDER_SYSTEM_PROMPT = """\
You are a real-estate analyst assessing developer reputation in Delhi NCR from \
public discussion text.

You will receive aggregated public commentary about one builder. Produce a \
balanced reputation read:

- Weight specific, verifiable complaints (possession delays with dates, legal \
cases, quality defects) far above vague negativity.
- Weight recent commentary above old commentary.
- Note when the sample is too small to support a conclusion — say so in \
evidence_strength rather than producing a confident score from three comments.
- Never state a legal conclusion (e.g. "this builder committed fraud"). Report \
what people allege, attributed as allegations.

The scores you output feed a buyer-facing product, so an unsupported score is \
worse than a low-confidence one.\
"""

# ---------------------------------------------------------------------------
# JSON schemas — enforced server-side via output_config.format
# ---------------------------------------------------------------------------

REDDIT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_relevant": {
            "type": "boolean",
            "description": "True if this discusses real estate in Delhi NCR.",
        },
        "sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral", "mixed"],
        },
        "sentiment_score": {
            "type": "number",
            "description": "-1.0 (very negative) to 1.0 (very positive).",
        },
        "builders": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Full builder/developer names discussed.",
        },
        "projects": {"type": "array", "items": {"type": "string"}},
        "sectors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Normalized, e.g. 'Sector 82', 'Dwarka Expressway'.",
        },
        "city": {
            "type": "string",
            "enum": [
                "gurgaon", "noida", "greater_noida", "delhi", "faridabad",
                "ghaziabad", "sohna", "other_ncr", "unknown",
            ],
        },
        "topics": {
            "type": "array",
            "items": {"type": "string", "enum": list(REDDIT_TOPICS)},
        },
        "keywords": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "description": "At most two sentences."},
        "claims": {
            "type": "array",
            "description": "Specific checkable assertions made in the text.",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "subject": {"type": "string"},
                    "polarity": {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral"],
                    },
                },
                "required": ["claim", "subject", "polarity"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "is_relevant", "sentiment", "sentiment_score", "builders", "projects",
        "sectors", "city", "topics", "keywords", "summary", "claims",
    ],
    "additionalProperties": False,
}

PROPERTY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "builder_name": {"type": ["string", "null"]},
        "project_name": {"type": ["string", "null"]},
        "sector": {"type": ["string", "null"]},
        "city": {
            "type": "string",
            "enum": [
                "gurgaon", "noida", "greater_noida", "delhi", "faridabad",
                "ghaziabad", "sohna", "other_ncr", "unknown",
            ],
        },
        "property_type": {
            "type": "string",
            "enum": [
                "apartment", "builder_floor", "independent_house", "villa", "plot",
                "penthouse", "studio", "office", "retail_shop", "showroom",
                "warehouse", "co_working", "farmhouse", "serviced_apartment", "other",
            ],
        },
        "amenities": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "highlights": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "description": "At most two sentences."},
    },
    "required": [
        "builder_name", "project_name", "sector", "city", "property_type",
        "amenities", "tags", "keywords", "highlights", "concerns", "summary",
    ],
    "additionalProperties": False,
}

BUILDER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral", "mixed"],
        },
        "sentiment_score": {"type": "number"},
        "trust_score": {
            "type": "number",
            "description": "0-100. Delivery track record and transparency.",
        },
        "risk_score": {
            "type": "number",
            "description": "0-100, higher = riskier. Delays, litigation, quality issues.",
        },
        "evidence_strength": {
            "type": "string",
            "enum": ["strong", "moderate", "weak", "insufficient"],
        },
        "positive_themes": {"type": "array", "items": {"type": "string"}},
        "negative_themes": {"type": "array", "items": {"type": "string"}},
        "reputation_summary": {"type": "string"},
    },
    "required": [
        "sentiment", "sentiment_score", "trust_score", "risk_score",
        "evidence_strength", "positive_themes", "negative_themes",
        "reputation_summary",
    ],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# user-turn builders (volatile content lives here, after the cache breakpoint)
# ---------------------------------------------------------------------------


def reddit_user_prompt(*, title: str, body: str | None, comments: list[str]) -> str:
    parts = [f"POST TITLE:\n{title}"]
    if body:
        parts.append(f"POST BODY:\n{body[:6000]}")
    if comments:
        joined = "\n---\n".join(c[:1500] for c in comments[:15])
        parts.append(f"TOP COMMENTS:\n{joined}")
    return "\n\n".join(parts)


def property_user_prompt(
    *,
    title: str | None,
    description: str | None,
    location_raw: str | None,
    specs: dict[str, str] | None,
    amenities: list[str] | None,
    price_display: str | None,
) -> str:
    lines = []
    if title:
        lines.append(f"TITLE: {title}")
    if location_raw:
        lines.append(f"LOCATION: {location_raw}")
    if price_display:
        lines.append(f"PRICE: {price_display}")
    if specs:
        rendered = "; ".join(f"{k}: {v}" for k, v in list(specs.items())[:40])
        lines.append(f"SPECIFICATIONS: {rendered}")
    if amenities:
        lines.append(f"AMENITIES (raw): {', '.join(amenities[:60])}")
    if description:
        lines.append(f"DESCRIPTION:\n{description[:5000]}")
    return "\n".join(lines)


def builder_user_prompt(*, builder_name: str, snippets: list[str]) -> str:
    joined = "\n---\n".join(s[:1200] for s in snippets[:40])
    return f"BUILDER: {builder_name}\n\nPUBLIC COMMENTARY:\n{joined}"
