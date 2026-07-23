"""Derived scores: investment, risk, location, builder trust.

These are **deterministic formulas over observed data**, not model opinions.
That matters for a buyer-facing product: a score has to be explainable ("this
project scored 41 on risk because possession has slipped and there is no RERA
number on the listing"), reproducible across runs, and stable when the LLM tier
is unavailable.

The LLM tier contributes *inputs* (sentiment, extracted claims). It never
produces the final number.

Every function returns a `Score` carrying the value plus the component
breakdown, so the API can show its working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from homz.common.enums import PossessionStatus, Segment

# All scores are 0-100.
_MIN, _MAX = 0.0, 100.0


def _clamp(value: float) -> float:
    return max(_MIN, min(_MAX, value))


@dataclass
class Score:
    value: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 2),
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# location score
# ---------------------------------------------------------------------------

#: Corridors with committed infrastructure score higher. Reviewed quarterly —
#: this is a market judgement, so it is data, not logic.
_MICRO_MARKET_WEIGHTS: dict[str, float] = {
    "Golf Course Road": 92.0,
    "Cyber City": 90.0,
    "Golf Course Extension Road": 84.0,
    "Dwarka Expressway": 82.0,
    "Noida Expressway": 80.0,
    "Southern Peripheral Road": 76.0,
    "MG Road": 74.0,
    "New Gurgaon": 70.0,
    "Sohna Road": 68.0,
    "Greater Noida West": 62.0,
    "Dwarka": 72.0,
    "Yamuna Expressway": 58.0,
    "Indirapuram": 62.0,
    "Neharpar": 55.0,
    "NH-24 / NH-9": 58.0,
}

_LANDMARK_WEIGHTS = {
    "metro": 12.0,
    "transport": 6.0,
    "school": 8.0,
    "hospital": 8.0,
    "mall": 6.0,
    "business": 5.0,
}


def location_score(
    *,
    micro_market: str | None,
    landmarks: list[dict[str, Any]] | None = None,
    has_coordinates: bool = False,
    locality_listing_count: int | None = None,
) -> Score:
    """Connectivity + social infrastructure + market depth."""
    components: dict[str, float] = {}
    notes: list[str] = []

    base = _MICRO_MARKET_WEIGHTS.get(micro_market or "", 55.0)
    components["micro_market"] = base
    if micro_market is None:
        notes.append("no micro-market resolved; using NCR baseline")

    # Landmarks: reward *variety* of nearby amenity types, capped, and give
    # extra credit for anything within 2 km.
    landmark_points = 0.0
    seen_categories: set[str] = set()
    for landmark in landmarks or []:
        category = str(landmark.get("category", "")).lower()
        weight = _LANDMARK_WEIGHTS.get(category)
        if weight is None or category in seen_categories:
            continue
        seen_categories.add(category)
        distance = landmark.get("distance_km")
        proximity = 1.0
        if isinstance(distance, int | float):
            proximity = 1.0 if distance <= 2 else (0.6 if distance <= 5 else 0.3)
        landmark_points += weight * proximity
    landmark_points = min(landmark_points, 30.0)
    components["landmarks"] = landmark_points
    if not landmarks:
        notes.append("no landmark data on this listing")

    depth = 0.0
    if locality_listing_count:
        # A locality with real transaction volume is easier to exit.
        depth = min(10.0, locality_listing_count / 20.0)
    components["market_depth"] = depth

    geo_bonus = 3.0 if has_coordinates else 0.0
    components["geo_precision"] = geo_bonus

    # Base carries most of the weight; the rest are adjustments.
    value = _clamp(base * 0.6 + landmark_points + depth + geo_bonus)
    return Score(value=value, components=components, notes=notes)


# ---------------------------------------------------------------------------
# risk score  (higher = riskier)
# ---------------------------------------------------------------------------


def risk_score(
    *,
    possession_status: PossessionStatus,
    possession_date: date | None = None,
    rera_number: str | None = None,
    builder_trust: float | None = None,
    price_per_sqft: Decimal | float | None = None,
    locality_median_ppsf: Decimal | float | None = None,
    negative_mentions: int = 0,
    total_mentions: int = 0,
    listing_age_days: int | None = None,
) -> Score:
    components: dict[str, float] = {}
    notes: list[str] = []

    # 1. Construction stage — under-construction carries delivery risk.
    stage_risk = {
        PossessionStatus.READY_TO_MOVE: 5.0,
        PossessionStatus.COMPLETED: 5.0,
        PossessionStatus.UNDER_CONSTRUCTION: 25.0,
        PossessionStatus.NEW_LAUNCH: 32.0,
        PossessionStatus.UPCOMING: 38.0,
        PossessionStatus.UNKNOWN: 20.0,
    }[possession_status]
    components["construction_stage"] = stage_risk

    # 2. A possession date already in the past on an unfinished project is the
    #    single most reliable distress signal we can compute.
    slip_risk = 0.0
    if possession_date and possession_status in {
        PossessionStatus.UNDER_CONSTRUCTION,
        PossessionStatus.NEW_LAUNCH,
        PossessionStatus.UPCOMING,
    }:
        months_late = (date.today() - possession_date).days / 30.0
        if months_late > 0:
            slip_risk = min(25.0, months_late * 1.5)
            notes.append(f"possession date passed ~{int(months_late)} months ago")
    components["possession_slip"] = slip_risk

    # 3. RERA registration.
    rera_risk = 0.0 if rera_number else 12.0
    if not rera_number:
        notes.append("no RERA number found on the listing")
    components["rera"] = rera_risk

    # 4. Builder track record (inverse of trust).
    builder_risk = 15.0 if builder_trust is None else (100.0 - builder_trust) * 0.25
    if builder_trust is None:
        notes.append("builder trust unknown; using neutral prior")
    components["builder"] = builder_risk

    # 5. Price dislocation vs the locality median — both directions are a
    #    flag. Far above median = overpaying; far below = something is wrong.
    price_risk = 0.0
    if price_per_sqft and locality_median_ppsf:
        try:
            ratio = float(price_per_sqft) / float(locality_median_ppsf)
        except ZeroDivisionError:
            ratio = 1.0
        if ratio > 1.35:
            price_risk = min(15.0, (ratio - 1.35) * 40)
            notes.append(f"priced {int((ratio - 1) * 100)}% above locality median")
        elif ratio < 0.65:
            price_risk = min(15.0, (0.65 - ratio) * 40)
            notes.append(f"priced {int((1 - ratio) * 100)}% below locality median — verify title")
    components["price_dislocation"] = price_risk

    # 6. Public sentiment.
    sentiment_risk = 0.0
    if total_mentions >= 3:
        negative_ratio = negative_mentions / total_mentions
        sentiment_risk = negative_ratio * 15.0
        if negative_ratio > 0.5:
            notes.append(f"{negative_mentions}/{total_mentions} public mentions are negative")
    components["public_sentiment"] = sentiment_risk

    # 7. A listing that has sat unsold for a long time.
    stale_risk = 0.0
    if listing_age_days and listing_age_days > 180:
        stale_risk = min(8.0, (listing_age_days - 180) / 60.0)
        notes.append(f"listed for {listing_age_days} days")
    components["staleness"] = stale_risk

    return Score(value=_clamp(sum(components.values())), components=components, notes=notes)


# ---------------------------------------------------------------------------
# investment score
# ---------------------------------------------------------------------------


def investment_score(
    *,
    location: float,
    risk: float,
    rental_yield_pct: float | None = None,
    price_per_sqft: Decimal | float | None = None,
    locality_median_ppsf: Decimal | float | None = None,
    price_trend_pct: float | None = None,
    possession_status: PossessionStatus = PossessionStatus.UNKNOWN,
    segment: Segment = Segment.UNKNOWN,
    builder_trust: float | None = None,
) -> Score:
    """Blend of location quality, entry price, yield, momentum and risk."""
    components: dict[str, float] = {}
    notes: list[str] = []

    components["location"] = location * 0.30

    # Buying below the locality median is the clearest edge available.
    value_points = 12.0
    if price_per_sqft and locality_median_ppsf:
        try:
            ratio = float(price_per_sqft) / float(locality_median_ppsf)
            value_points = _clamp(25.0 * (2 - ratio)) if ratio > 0 else 12.0
            value_points = min(value_points, 25.0)
            if ratio < 0.9:
                notes.append("priced below locality median")
        except ZeroDivisionError:
            pass
    else:
        notes.append("no locality benchmark available; value component is neutral")
    components["relative_value"] = value_points

    # Rental yield. NCR residential typically runs 2-4%; 4%+ is strong.
    yield_points = 6.0
    if rental_yield_pct is not None:
        yield_points = min(15.0, max(0.0, rental_yield_pct * 3.5))
        if rental_yield_pct >= 4:
            notes.append(f"strong rental yield ({rental_yield_pct:.1f}%)")
    components["rental_yield"] = yield_points

    momentum = 5.0
    if price_trend_pct is not None:
        momentum = _clamp(5.0 + price_trend_pct * 0.8)
        momentum = min(momentum, 15.0)
        if price_trend_pct > 8:
            notes.append(f"locality prices up {price_trend_pct:.1f}% year-on-year")
    components["price_momentum"] = momentum

    # Under-construction carries upside if the builder is credible.
    stage_points = {
        PossessionStatus.NEW_LAUNCH: 8.0,
        PossessionStatus.UNDER_CONSTRUCTION: 7.0,
        PossessionStatus.UPCOMING: 6.0,
        PossessionStatus.READY_TO_MOVE: 5.0,
        PossessionStatus.COMPLETED: 4.0,
        PossessionStatus.UNKNOWN: 4.0,
    }[possession_status]
    if builder_trust is not None and builder_trust < 40 and stage_points > 5:
        stage_points *= 0.5
        notes.append("early-stage upside discounted for weak builder record")
    components["stage_upside"] = stage_points

    segment_points = {
        Segment.AFFORDABLE: 7.0,
        Segment.MID: 8.0,
        Segment.PREMIUM: 6.0,
        Segment.LUXURY: 4.0,
        Segment.ULTRA_LUXURY: 2.0,
        Segment.UNKNOWN: 4.0,
    }[segment]
    components["segment_liquidity"] = segment_points

    # Risk is a direct deduction, not a multiplier — keeps it explainable.
    components["risk_penalty"] = -(risk * 0.30)

    return Score(value=_clamp(sum(components.values())), components=components, notes=notes)


# ---------------------------------------------------------------------------
# builder trust score
# ---------------------------------------------------------------------------


def builder_trust_score(
    *,
    completed_projects: int | None = None,
    ongoing_projects: int | None = None,
    total_projects: int | None = None,
    established_year: int | None = None,
    rating: float | None = None,
    rating_count: int | None = None,
    positive_mentions: int = 0,
    negative_mentions: int = 0,
    delay_mentions: int = 0,
    fraud_mentions: int = 0,
    llm_trust: float | None = None,
) -> Score:
    """Delivery record + longevity + public sentiment.

    Starts from a neutral 50 rather than 0 — absence of evidence is not
    evidence of untrustworthiness, and a new builder should not be scored the
    same as one with documented fraud allegations.
    """
    components: dict[str, float] = {"base": 50.0}
    notes: list[str] = []

    # Delivery record: completions are the strongest positive signal.
    delivery = 0.0
    if completed_projects:
        delivery = min(20.0, completed_projects * 1.5)
        if total_projects and total_projects > 0:
            completion_ratio = completed_projects / total_projects
            delivery *= 0.5 + completion_ratio * 0.5
            notes.append(f"{completed_projects}/{total_projects} projects delivered")
    elif ongoing_projects:
        notes.append("only ongoing projects on record; no delivery history yet")
    components["delivery_record"] = delivery

    longevity = 0.0
    if established_year and 1900 < established_year <= date.today().year:
        years = date.today().year - established_year
        longevity = min(10.0, years * 0.4)
        notes.append(f"operating for {years} years")
    components["longevity"] = longevity

    # Portal ratings, discounted when the sample is thin.
    rating_points = 0.0
    if rating is not None:
        confidence = min(1.0, (rating_count or 0) / 50.0)
        rating_points = (rating - 3.0) * 5.0 * max(confidence, 0.2)
        if (rating_count or 0) < 10:
            notes.append("portal rating based on a small sample")
    components["portal_rating"] = rating_points

    # Public discussion.
    total_mentions = positive_mentions + negative_mentions
    sentiment_points = 0.0
    if total_mentions >= 3:
        net = (positive_mentions - negative_mentions) / total_mentions
        sentiment_points = net * 12.0
    components["public_sentiment"] = sentiment_points

    # Specific, checkable complaint categories carry more weight than tone.
    penalty = -min(15.0, delay_mentions * 2.0) - min(20.0, fraud_mentions * 4.0)
    if delay_mentions:
        notes.append(f"{delay_mentions} public reports of delays")
    if fraud_mentions:
        notes.append(f"{fraud_mentions} public allegations of malpractice (unverified)")
    components["complaint_penalty"] = penalty

    value = _clamp(sum(components.values()))

    # Blend in the LLM's read, but never let it dominate the hard signals.
    if llm_trust is not None:
        value = _clamp(value * 0.7 + llm_trust * 0.3)
        components["llm_blend"] = llm_trust

    if total_mentions < 3 and not completed_projects:
        notes.append("low-confidence score: little public data on this builder")

    return Score(value=value, components=components, notes=notes)


def confidence_label(sample_size: int) -> str:
    if sample_size >= 30:
        return "strong"
    if sample_size >= 10:
        return "moderate"
    if sample_size >= 3:
        return "weak"
    return "insufficient"
