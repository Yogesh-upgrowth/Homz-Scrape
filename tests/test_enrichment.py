"""Enrichment tests: rule extraction and the deterministic scoring formulas.

The scores feed a buyer-facing product, so their behaviour has to be pinned:
a change that quietly makes every under-construction project look safe is not
something a type checker catches.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from homz.common.enums import City, PossessionStatus, Segment, Sentiment
from homz.enrichment.extractors import (
    canonical_builder,
    extract_builders,
    extract_entities,
    extract_projects,
    extract_sectors,
    extract_topics,
    lexicon_sentiment,
)
from homz.enrichment.scoring import (
    builder_trust_score,
    investment_score,
    location_score,
    risk_score,
)


class TestEntityExtraction:
    def test_builders_by_alias(self) -> None:
        assert "DLF" in extract_builders("Looking at DLF Privana vs M3M Crown")
        assert "M3M" in extract_builders("Looking at DLF Privana vs M3M Crown")

    def test_builder_alias_normalization(self) -> None:
        assert "Signature Global" in extract_builders("signatureglobal city sector 37D")
        assert canonical_builder("godrej properties ltd") == "Godrej Properties"

    def test_projects(self) -> None:
        found = extract_projects("Anyone bought in Godrej Aristocrat or DLF Privana?")
        assert "Godrej Aristocrat" in found
        assert "DLF Privana" in found

    def test_project_tolerates_hyphen_and_spacing(self) -> None:
        assert "Godrej Aristocrat" in extract_projects("Godrej-Aristocrat is overpriced")
        assert "Godrej Aristocrat" in extract_projects("Godrej  Aristocrat  review")

    def test_sectors_multiple(self) -> None:
        sectors = extract_sectors("Sector 82 vs Sec-102 vs sector 37C — which is better?")
        assert sectors == ["Sector 82", "Sector 102", "Sector 37C"]

    def test_full_extraction(self) -> None:
        text = (
            "Bought a 3BHK in Sector 102 on Dwarka Expressway from Signature Global. "
            "Possession delayed by 2 years, RERA complaint filed."
        )
        entities = extract_entities(text)
        assert "Signature Global" in entities.builders
        assert "Sector 102" in entities.sectors
        assert entities.city == City.GURGAON
        assert entities.keywords


class TestTopics:
    def test_fraud_and_delay(self) -> None:
        topics = extract_topics(
            "This builder cheated us — possession delayed 3 years, filed a RERA complaint."
        )
        assert "builder_fraud" in topics
        assert "construction_delay" in topics or "possession_issue" in topics
        assert "rera" in topics

    def test_corridor_topics(self) -> None:
        assert "dwarka_expressway" in extract_topics("Prices on Dwarka Expressway are rising")
        assert "spr_road" in extract_topics("SPR connectivity has improved a lot")

    def test_financial_topics(self) -> None:
        topics = extract_topics(
            "What's the stamp duty and registration charge? Also EDC/IDC hidden charges?"
        )
        assert "stamp_duty" in topics
        assert "hidden_charges" in topics

    def test_ordered_by_strength(self) -> None:
        topics = extract_topics("rent rent rent rental tenant landlord. Also one buy mention.")
        assert topics[0] == "renting"

    def test_empty_input(self) -> None:
        assert extract_topics(None) == []
        assert extract_topics("") == []


class TestLexiconSentiment:
    def test_positive(self) -> None:
        label, score = lexicon_sentiment("Excellent quality, delivered on time, highly recommend")
        assert label == Sentiment.POSITIVE
        assert score > 0

    def test_negative(self) -> None:
        label, score = lexicon_sentiment("Worst builder, total fraud, avoid — possession delayed")
        assert label == Sentiment.NEGATIVE
        assert score < 0

    def test_neutral_on_factual_question(self) -> None:
        label, _ = lexicon_sentiment("What is the current circle rate in sector 82?")
        assert label == Sentiment.NEUTRAL

    def test_negation_flips_positive_terms(self) -> None:
        _, negated = lexicon_sentiment("The construction quality is not good")
        _, plain = lexicon_sentiment("The construction quality is good")
        assert negated < plain


class TestLocationScore:
    def test_prime_corridor_beats_unknown(self) -> None:
        prime = location_score(micro_market="Golf Course Road")
        unknown = location_score(micro_market=None)
        assert prime.value > unknown.value

    def test_landmarks_add_value(self) -> None:
        bare = location_score(micro_market="New Gurgaon")
        with_poi = location_score(
            micro_market="New Gurgaon",
            landmarks=[
                {"category": "metro", "distance_km": 1.2},
                {"category": "school", "distance_km": 0.8},
                {"category": "hospital", "distance_km": 3.0},
            ],
        )
        assert with_poi.value > bare.value

    def test_duplicate_categories_do_not_stack(self) -> None:
        one = location_score(
            micro_market="New Gurgaon", landmarks=[{"category": "metro", "distance_km": 1.0}]
        )
        many = location_score(
            micro_market="New Gurgaon",
            landmarks=[{"category": "metro", "distance_km": 1.0}] * 5,
        )
        assert one.value == many.value

    def test_bounded(self) -> None:
        maxed = location_score(
            micro_market="Golf Course Road",
            landmarks=[{"category": c, "distance_km": 0.5}
                       for c in ("metro", "school", "hospital", "mall", "business", "transport")],
            has_coordinates=True,
            locality_listing_count=10_000,
        )
        assert 0 <= maxed.value <= 100


class TestRiskScore:
    def test_ready_to_move_is_safer_than_new_launch(self) -> None:
        ready = risk_score(
            possession_status=PossessionStatus.READY_TO_MOVE, rera_number="HARERA/1"
        )
        launch = risk_score(
            possession_status=PossessionStatus.NEW_LAUNCH, rera_number="HARERA/1"
        )
        assert ready.value < launch.value

    def test_missing_rera_increases_risk(self) -> None:
        with_rera = risk_score(
            possession_status=PossessionStatus.UNDER_CONSTRUCTION, rera_number="HARERA/1"
        )
        without = risk_score(possession_status=PossessionStatus.UNDER_CONSTRUCTION)
        assert without.value > with_rera.value
        assert any("RERA" in note for note in without.notes)

    def test_passed_possession_date_flags(self) -> None:
        late = risk_score(
            possession_status=PossessionStatus.UNDER_CONSTRUCTION,
            possession_date=date.today() - timedelta(days=800),
            rera_number="HARERA/1",
        )
        assert late.components["possession_slip"] > 0
        assert any("possession date passed" in note for note in late.notes)

    def test_price_dislocation_both_directions(self) -> None:
        overpriced = risk_score(
            possession_status=PossessionStatus.READY_TO_MOVE,
            rera_number="X",
            price_per_sqft=Decimal("20000"),
            locality_median_ppsf=Decimal("12000"),
        )
        underpriced = risk_score(
            possession_status=PossessionStatus.READY_TO_MOVE,
            rera_number="X",
            price_per_sqft=Decimal("5000"),
            locality_median_ppsf=Decimal("12000"),
        )
        assert overpriced.components["price_dislocation"] > 0
        assert underpriced.components["price_dislocation"] > 0

    def test_bounded(self) -> None:
        worst = risk_score(
            possession_status=PossessionStatus.UPCOMING,
            possession_date=date.today() - timedelta(days=3000),
            rera_number=None,
            builder_trust=0.0,
            price_per_sqft=Decimal("50000"),
            locality_median_ppsf=Decimal("10000"),
            negative_mentions=20,
            total_mentions=20,
            listing_age_days=2000,
        )
        assert 0 <= worst.value <= 100


class TestInvestmentScore:
    def test_risk_penalises(self) -> None:
        low_risk = investment_score(location=80, risk=10)
        high_risk = investment_score(location=80, risk=90)
        assert low_risk.value > high_risk.value

    def test_below_median_price_helps(self) -> None:
        cheap = investment_score(
            location=70, risk=30,
            price_per_sqft=Decimal("9000"), locality_median_ppsf=Decimal("12000"),
        )
        expensive = investment_score(
            location=70, risk=30,
            price_per_sqft=Decimal("16000"), locality_median_ppsf=Decimal("12000"),
        )
        assert cheap.value > expensive.value

    def test_weak_builder_discounts_early_stage_upside(self) -> None:
        strong = investment_score(
            location=70, risk=30,
            possession_status=PossessionStatus.NEW_LAUNCH, builder_trust=85.0,
        )
        weak = investment_score(
            location=70, risk=30,
            possession_status=PossessionStatus.NEW_LAUNCH, builder_trust=20.0,
        )
        assert strong.components["stage_upside"] > weak.components["stage_upside"]
        assert any("weak builder" in note for note in weak.notes)

    def test_bounded(self) -> None:
        assert 0 <= investment_score(location=100, risk=0, rental_yield_pct=10).value <= 100
        assert 0 <= investment_score(location=0, risk=100, segment=Segment.ULTRA_LUXURY).value <= 100


class TestBuilderTrust:
    def test_neutral_prior_for_unknown_builder(self) -> None:
        score = builder_trust_score()
        assert 40 <= score.value <= 60
        assert any("low-confidence" in note for note in score.notes)

    def test_delivery_record_helps(self) -> None:
        proven = builder_trust_score(completed_projects=25, total_projects=30, established_year=1990)
        assert proven.value > builder_trust_score().value

    def test_fraud_allegations_hurt_more_than_tone(self) -> None:
        tone_only = builder_trust_score(positive_mentions=0, negative_mentions=10)
        with_fraud = builder_trust_score(
            positive_mentions=0, negative_mentions=10, fraud_mentions=5
        )
        assert with_fraud.value < tone_only.value
        assert any("allegations" in note for note in with_fraud.notes)

    def test_thin_rating_sample_is_discounted(self) -> None:
        thin = builder_trust_score(rating=5.0, rating_count=2)
        thick = builder_trust_score(rating=5.0, rating_count=500)
        assert thick.value > thin.value

    def test_llm_blend_does_not_dominate(self) -> None:
        # A generous LLM read must not erase a hard-evidence penalty.
        hard_negative = builder_trust_score(fraud_mentions=5, delay_mentions=8)
        blended = builder_trust_score(fraud_mentions=5, delay_mentions=8, llm_trust=95.0)
        assert blended.value < 80.0
        assert blended.value > hard_negative.value

    def test_bounded(self) -> None:
        assert 0 <= builder_trust_score(fraud_mentions=100, delay_mentions=100).value <= 100
        assert 0 <= builder_trust_score(completed_projects=500, rating=5.0,
                                        rating_count=9999).value <= 100


class TestBuilderInference:
    """Most listings name the project but not the developer; without this the
    builders table stays empty and builder-trust scoring has nothing to score."""

    def test_infers_builder_from_project_name(self) -> None:
        from homz.db.repository import infer_builder_from_project

        assert infer_builder_from_project("Godrej Aristocrat") == "Godrej Properties"
        assert infer_builder_from_project("M3M Crown") == "M3M"
        assert infer_builder_from_project("DLF Privana South") == "DLF"

    def test_returns_none_for_unknown_project(self) -> None:
        from homz.db.repository import infer_builder_from_project

        assert infer_builder_from_project("Green Valley Residency") is None
        assert infer_builder_from_project(None) is None

    def test_ambiguous_name_is_not_guessed(self) -> None:
        from homz.db.repository import infer_builder_from_project

        # Two developers in one string — attributing it to either would be wrong.
        assert infer_builder_from_project("DLF vs M3M comparison tower") is None
