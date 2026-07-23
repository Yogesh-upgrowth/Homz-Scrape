"""Unit tests for the common parser.

These are the highest-value tests in the repo: portal selectors change, but a
regression in price/area parsing silently corrupts every downstream number —
scores, trends, yields — without throwing anything.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from homz.common.enums import AreaUnit, City, ListingType, PossessionStatus, PropertyType, Segment
from homz.common.geo import build_location, detect_micro_market, parse_sector
from homz.common.parsing import (
    canonical_url,
    classify_segment,
    clean_text,
    format_price_inr,
    normalize_configuration,
    normalize_name,
    parse_area,
    parse_bedrooms,
    parse_floor,
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
    to_sqft,
)


class TestPrice:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("₹ 1.25 Cr", Decimal("12500000")),
            ("1.25 Crore", Decimal("12500000")),
            ("Rs 2 Cr", Decimal("20000000")),
            ("85 Lac", Decimal("8500000")),
            ("85 Lakh", Decimal("8500000")),
            ("₹85 L", Decimal("8500000")),
            ("Rs. 45,00,000", Decimal("4500000")),
            ("45000", Decimal("45000")),
            ("₹ 32,000/month", Decimal("32000")),
        ],
    )
    def test_indian_formats(self, text: str, expected: Decimal) -> None:
        assert parse_price(text) == expected

    def test_price_on_request_is_none(self) -> None:
        assert parse_price("Price on Request") is None
        assert parse_price("Call for price") is None

    def test_unit_after_number_only(self) -> None:
        # "Sector 82" must not turn a later unit word into the multiplier for 3.
        assert parse_price("3 BHK in Sector 82 for 1.2 Cr") == Decimal("12000000")

    def test_range_borrows_right_hand_unit(self) -> None:
        low, high = parse_price_range("1.2 - 2.4 Cr")
        assert low == Decimal("12000000")
        assert high == Decimal("24000000")

    def test_range_with_both_units(self) -> None:
        low, high = parse_price_range("₹85 Lac - ₹1.2 Cr")
        assert low == Decimal("8500000")
        assert high == Decimal("12000000")

    def test_price_per_sqft_requires_marker(self) -> None:
        assert parse_price_per_sqft("₹12,500 per sq.ft") == Decimal("12500")
        # A bare price is not a rate — must not be mistaken for one.
        assert parse_price_per_sqft("₹12,500") is None

    def test_format_round_trip(self) -> None:
        assert format_price_inr(Decimal("12500000")) == "1.25 Cr"
        assert format_price_inr(Decimal("8500000")) == "85 L"
        assert format_price_inr(Decimal("45000")) == "45,000"


class TestArea:
    @pytest.mark.parametrize(
        ("text", "value", "unit"),
        [
            ("1,250 sq.ft.", 1250.0, AreaUnit.SQFT),
            ("1250 sqft", 1250.0, AreaUnit.SQFT),
            ("145 sq yards", 145.0, AreaUnit.SQYD),
            ("200 gaj", 200.0, AreaUnit.SQYD),
            ("120 sq m", 120.0, AreaUnit.SQM),
            ("2.5 Acres", 2.5, AreaUnit.ACRE),
            ("1200", 1200.0, AreaUnit.SQFT),  # bare number defaults to sqft
        ],
    )
    def test_parse(self, text: str, value: float, unit: AreaUnit) -> None:
        assert parse_area(text) == (value, unit)

    def test_conversion_to_sqft(self) -> None:
        assert to_sqft(145.0, AreaUnit.SQYD) == 1305.0
        assert to_sqft(1.0, AreaUnit.ACRE) == 43560.0
        assert to_sqft(100.0, AreaUnit.SQM) == 1076.39


class TestConfiguration:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("3 BHK", 3),
            ("3BHK", 3),
            ("2 bhk apartment", 2),
            ("1 RK", 1),
            ("4 Bedroom", 4),
            ("Studio Apartment", 0),
        ],
    )
    def test_bedrooms(self, text: str, expected: int) -> None:
        assert parse_bedrooms(text) == expected

    def test_normalization(self) -> None:
        assert normalize_configuration("3bhk+study") == "3 BHK + Study"
        assert normalize_configuration("2 BHK") == "2 BHK"
        assert normalize_configuration("studio") == "Studio"

    def test_floor(self) -> None:
        assert parse_floor("5 out of 14") == (5, 14)
        assert parse_floor("Ground Floor of 4") == (0, 4)
        assert parse_floor("12th of 25 Floors") == (12, 25)


class TestPossession:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Ready to Move", PossessionStatus.READY_TO_MOVE),
            ("Under Construction", PossessionStatus.UNDER_CONSTRUCTION),
            ("New Launch", PossessionStatus.NEW_LAUNCH),
            ("Immediate Possession", PossessionStatus.READY_TO_MOVE),
        ],
    )
    def test_status(self, text: str, expected: PossessionStatus) -> None:
        assert parse_possession_status(text) == expected

    def test_future_date_implies_under_construction(self) -> None:
        future = date.today().year + 3
        assert parse_possession_status(f"Possession by Dec {future}") == (
            PossessionStatus.UNDER_CONSTRUCTION
        )

    def test_date_parsing(self) -> None:
        assert parse_possession_date("Dec 2026") == date(2026, 12, 1)
        assert parse_possession_date("December '27") == date(2027, 12, 1)
        assert parse_possession_date("2028") == date(2028, 1, 1)


class TestClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Independent Builder Floor", PropertyType.BUILDER_FLOOR),
            ("3 BHK Villa", PropertyType.VILLA),
            ("Residential Plot", PropertyType.PLOT),
            ("Office Space in Cyber City", PropertyType.OFFICE),
            ("2 BHK Flat", PropertyType.APARTMENT),
            ("Penthouse for sale", PropertyType.PENTHOUSE),
        ],
    )
    def test_property_type(self, text: str, expected: PropertyType) -> None:
        assert parse_property_type(text) == expected

    def test_builder_floor_beats_apartment(self) -> None:
        # "Builder Floor Apartment" contains both keywords; the more specific
        # pattern has to win or every builder floor is misfiled.
        assert parse_property_type("Builder Floor Apartment") == PropertyType.BUILDER_FLOOR

    def test_listing_type(self) -> None:
        assert parse_listing_type("Flat for Rent in Gurgaon") == ListingType.RENT
        assert parse_listing_type("3 BHK for Sale") == ListingType.SALE
        assert parse_listing_type("New Launch Project") == ListingType.NEW_LAUNCH

    def test_segments(self) -> None:
        assert classify_segment(Decimal("3500000"), ListingType.SALE) == Segment.AFFORDABLE
        assert classify_segment(Decimal("90000000"), ListingType.SALE) == Segment.LUXURY
        # Rent uses a different band entirely.
        assert classify_segment(Decimal("20000"), ListingType.RENT) == Segment.AFFORDABLE
        assert classify_segment(Decimal("250000"), ListingType.RENT) == Segment.LUXURY


class TestRera:
    def test_haryana(self) -> None:
        text = "RERA: RC/REP/HARERA/GGM/812/544/2024/45"
        assert "HARERA" in parse_rera_number(text)

    def test_up(self) -> None:
        assert parse_rera_number("UPRERAPRJ123456") == "UPRERAPRJ123456"

    def test_absent(self) -> None:
        assert parse_rera_number("no registration mentioned") is None


class TestMisc:
    def test_clean_text_collapses_whitespace(self) -> None:
        assert clean_text("  3   BHK\n\n Flat  ") == "3 BHK Flat"
        assert clean_text("") is None

    def test_normalize_name_strips_suffixes(self) -> None:
        assert normalize_name("M3M India Pvt. Ltd.") == normalize_name("M3M INDIA")
        assert normalize_name("Godrej Properties Limited") == "godrej"

    def test_phone_normalized(self) -> None:
        assert parse_phone("Call 98765 43210") == "+919876543210"
        assert parse_phone("+91-9876543210") == "+919876543210"
        assert parse_phone("12345") is None

    def test_canonical_url_drops_tracking(self) -> None:
        assert canonical_url(
            "https://Example.com/prop/123/?utm_source=x&page=2&gclid=abc"
        ) == "https://example.com/prop/123?page=2"

    def test_relative_dates(self) -> None:
        now = datetime(2026, 7, 22, tzinfo=UTC)
        parsed = parse_listing_date("3 days ago", now=now)
        assert parsed is not None and parsed.date() == date(2026, 7, 19)
        assert parse_listing_date("Today", now=now) == now

    def test_fuzzy_date_rejects_sector_numbers(self) -> None:
        # "Sector 45" must not become a date via fuzzy parsing.
        assert parse_listing_date("Sector 45") is None


class TestGeo:
    def test_sector_normalization(self) -> None:
        assert parse_sector("Sector 82, Gurgaon") == "Sector 82"
        assert parse_sector("Sec-102 Dwarka Expressway") == "Sector 102"
        assert parse_sector("sector 37c") == "Sector 37C"

    def test_greater_noida_before_noida(self) -> None:
        location = build_location("Sector 1, Greater Noida West")
        assert location.city == City.GREATER_NOIDA

    def test_micro_market_from_text(self) -> None:
        assert detect_micro_market("Property on Dwarka Expressway") == "Dwarka Expressway"
        assert detect_micro_market("Near SPR road", city=City.GURGAON) == (
            "Southern Peripheral Road"
        )

    def test_micro_market_from_sector_map(self) -> None:
        location = build_location("Sector 102, Gurgaon")
        assert location.city == City.GURGAON
        assert location.micro_market == "Dwarka Expressway"

    def test_geo_rejects_out_of_ncr_coordinates(self) -> None:
        # (0, 0) is the classic garbage coordinate on Indian portals.
        location = build_location("Sector 82, Gurgaon", latitude=0.0, longitude=0.0)
        assert location.geo is None

        valid = build_location("Sector 82, Gurgaon", latitude=28.39, longitude=76.98)
        assert valid.geo is not None and valid.geo.latitude == 28.39

    def test_state_inferred_from_city(self) -> None:
        assert build_location("Sector 150, Noida").state == "Uttar Pradesh"
        assert build_location("Sector 82, Gurgaon").state == "Haryana"

    def test_city_falls_back_to_micro_market(self) -> None:
        # "Dwarka Expressway" names no city but is unambiguously Gurgaon;
        # without this these records drop out of every city-filtered query.
        from homz.common.geo import detect_city

        assert detect_city("prices on Dwarka Expressway are rising") == City.GURGAON
