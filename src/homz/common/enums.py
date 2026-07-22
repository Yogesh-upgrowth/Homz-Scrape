"""Controlled vocabularies shared by every source.

Source-specific parsers map their raw strings into these enums so the warehouse
never sees "Under Constr.", "under-construction" and "UC" as three things.
"""

from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    MAGICBRICKS = "magicbricks"
    HOUSING = "housing"
    SQUAREYARDS = "squareyards"
    REDDIT = "reddit"


class ListingType(StrEnum):
    """What the record is offering."""

    SALE = "sale"
    RENT = "rent"
    RESALE = "resale"
    NEW_LAUNCH = "new_launch"
    PROJECT = "project"  # a builder project page, not a single unit
    COMMERCIAL = "commercial"
    PG = "pg"
    UNKNOWN = "unknown"


class PropertyType(StrEnum):
    APARTMENT = "apartment"
    BUILDER_FLOOR = "builder_floor"
    INDEPENDENT_HOUSE = "independent_house"
    VILLA = "villa"
    PLOT = "plot"
    PENTHOUSE = "penthouse"
    STUDIO = "studio"
    OFFICE = "office"
    RETAIL_SHOP = "retail_shop"
    SHOWROOM = "showroom"
    WAREHOUSE = "warehouse"
    CO_WORKING = "co_working"
    FARMHOUSE = "farmhouse"
    SERVICED_APARTMENT = "serviced_apartment"
    OTHER = "other"


class PossessionStatus(StrEnum):
    READY_TO_MOVE = "ready_to_move"
    UNDER_CONSTRUCTION = "under_construction"
    NEW_LAUNCH = "new_launch"
    UPCOMING = "upcoming"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class Segment(StrEnum):
    """Price segment — derived, not scraped."""

    AFFORDABLE = "affordable"
    MID = "mid"
    PREMIUM = "premium"
    LUXURY = "luxury"
    ULTRA_LUXURY = "ultra_luxury"
    UNKNOWN = "unknown"


class AreaUnit(StrEnum):
    SQFT = "sqft"
    SQYD = "sqyd"
    SQM = "sqm"
    ACRE = "acre"
    HECTARE = "hectare"
    MARLA = "marla"
    KANAL = "kanal"


class SellerType(StrEnum):
    OWNER = "owner"
    AGENT = "agent"
    BUILDER = "builder"
    UNKNOWN = "unknown"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class City(StrEnum):
    GURGAON = "gurgaon"
    NOIDA = "noida"
    GREATER_NOIDA = "greater_noida"
    DELHI = "delhi"
    FARIDABAD = "faridabad"
    GHAZIABAD = "ghaziabad"
    SOHNA = "sohna"
    OTHER_NCR = "other_ncr"
    UNKNOWN = "unknown"


class TrendMetric(StrEnum):
    AVG_PRICE_PER_SQFT = "avg_price_per_sqft"
    AVG_RENT = "avg_rent"
    RENTAL_YIELD = "rental_yield"
    LISTING_SUPPLY = "listing_supply"
    DEMAND_INDEX = "demand_index"
    NEW_LAUNCH_COUNT = "new_launch_count"
    PRICE_CHANGE_PCT = "price_change_pct"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


# --- Topic taxonomy used by the Reddit + enrichment pipelines ---------------
REDDIT_TOPICS: tuple[str, ...] = (
    "buying",
    "selling",
    "renting",
    "builder_reputation",
    "builder_fraud",
    "project_review",
    "construction_delay",
    "possession_issue",
    "maintenance_issue",
    "society_review",
    "broker_experience",
    "investment_advice",
    "sector_recommendation",
    "roi",
    "rental_yield",
    "legal_issue",
    "rera",
    "home_loan",
    "hidden_charges",
    "registration",
    "stamp_duty",
    "property_tax",
    "infrastructure",
    "metro",
    "dwarka_expressway",
    "spr_road",
    "golf_course_road",
    "new_gurgaon",
    "sohna_road",
    "noida_expressway",
    "market_outlook",
)
