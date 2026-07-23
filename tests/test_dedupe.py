"""Dedupe and normalization tests.

Dedupe errors are expensive in both directions: a missed duplicate inflates
supply counts and skews medians; a false merge hides a real listing. These
tests pin both sides.
"""

from __future__ import annotations

from decimal import Decimal

from homz.common.dedupe import (
    blocking_key,
    choose_canonical,
    find_duplicates,
    hamming,
    simhash,
    similarity,
)
from homz.common.enums import ListingType, PropertyType, Source
from homz.common.geo import build_location
from homz.common.schema import Image, PropertyRecord


def make_property(
    *,
    source: Source = Source.MAGICBRICKS,
    source_id: str = "1",
    title: str = "3 BHK Flat in Godrej Aristocrat, Sector 49 Gurgaon",
    project: str | None = "Godrej Aristocrat",
    price: str | None = "35000000",
    area: float | None = 2100.0,
    config: str | None = "3 BHK",
    location_raw: str = "Sector 49, Gurgaon",
    images: list[str] | None = None,
    description: str | None = None,
) -> PropertyRecord:
    record = PropertyRecord(
        source=source,
        source_id=source_id,
        listing_url=f"https://example.com/{source.value}/{source_id}",
        title=title,
        description=description,
        project_name=project,
        listing_type=ListingType.SALE,
        property_type=PropertyType.APARTMENT,
        configuration=config,
        bedrooms=3,
        price=Decimal(price) if price else None,
        area_sqft=area,
        location=build_location(location_raw),
        images=[Image(url=u) for u in (images or [])],
    )
    return record.finalize()


class TestFingerprints:
    def test_content_hash_is_stable(self) -> None:
        a, b = make_property(), make_property()
        assert a.content_hash == b.content_hash

    def test_content_hash_changes_with_price(self) -> None:
        a = make_property(price="35000000")
        b = make_property(price="34000000")
        assert a.content_hash != b.content_hash

    def test_content_hash_ignores_source_id(self) -> None:
        # Same listing syndicated under two ids still hashes the same content,
        # which is what lets the incremental skip work across re-listings.
        a = make_property(source_id="1")
        b = make_property(source_id="2")
        assert a.content_hash == b.content_hash

    def test_dedupe_key_buckets_close_values(self) -> None:
        a = make_property(area=2100.0, price="35000000")
        b = make_property(area=2110.0, price="35050000")  # within one bucket
        assert a.dedupe_key == b.dedupe_key

    def test_dedupe_key_separates_distinct_units(self) -> None:
        a = make_property(area=2100.0)
        b = make_property(area=1400.0, config="2 BHK")
        assert a.dedupe_key != b.dedupe_key


class TestSimhash:
    def test_identical_text(self) -> None:
        assert simhash("3 BHK in Godrej Aristocrat") == simhash("3 BHK in Godrej Aristocrat")

    def test_near_text_is_close(self) -> None:
        a = simhash("3 BHK Flat for sale in Godrej Aristocrat Sector 49")
        b = simhash("3 BHK Apartment for sale in Godrej Aristocrat Sector 49")
        assert hamming(a, b) <= 12

    def test_unrelated_text_is_far(self) -> None:
        a = simhash("3 BHK Flat in Godrej Aristocrat Gurgaon")
        b = simhash("Commercial warehouse for lease in Bhiwandi Maharashtra")
        assert hamming(a, b) > 12

    def test_deterministic_across_calls(self) -> None:
        # Must not depend on PYTHONHASHSEED — dedupe runs across processes.
        assert simhash("stable") == simhash("stable")


class TestSimilarity:
    def test_same_listing_two_portals(self) -> None:
        mb = make_property(source=Source.MAGICBRICKS, source_id="mb1")
        hs = make_property(source=Source.HOUSING, source_id="hs1")
        score, reason = similarity(mb, hs)
        assert score >= 0.75, reason

    def test_different_units_same_project(self) -> None:
        a = make_property(source_id="a", area=2100.0, price="35000000", config="3 BHK")
        b = make_property(
            source=Source.HOUSING,
            source_id="b",
            area=1400.0,
            price="24000000",
            config="2 BHK",
            title="2 BHK Flat in Godrej Aristocrat, Sector 49 Gurgaon",
        )
        score, _ = similarity(a, b)
        assert score < 0.75

    def test_shared_image_is_conclusive(self) -> None:
        shared = "https://cdn.example.com/photo-abc.jpg"
        a = make_property(source_id="a", images=[shared], price="35000000")
        b = make_property(
            source=Source.HOUSING,
            source_id="b",
            images=[shared],
            price="41000000",  # price differs, image does not
            title="Luxury residence available",
            project=None,
        )
        score, reason = similarity(a, b)
        assert score >= 0.95
        assert "shared image" in reason

    def test_same_source_id_short_circuits(self) -> None:
        a = make_property(source_id="x")
        b = make_property(source_id="x")
        score, reason = similarity(a, b)
        assert score == 1.0
        assert reason == "same source id"


class TestBlocking:
    def test_blocking_key_groups_comparable_records(self) -> None:
        a = make_property(source_id="a")
        b = make_property(source=Source.HOUSING, source_id="b")
        assert blocking_key(a) == blocking_key(b)

    def test_blocking_key_separates_cities(self) -> None:
        a = make_property(location_raw="Sector 49, Gurgaon")
        b = make_property(location_raw="Sector 49, Noida")
        assert blocking_key(a) != blocking_key(b)

    def test_find_duplicates_across_sources(self) -> None:
        records = [
            make_property(source=Source.MAGICBRICKS, source_id="mb1"),
            make_property(source=Source.HOUSING, source_id="hs1"),
            make_property(
                source=Source.SQUAREYARDS,
                source_id="sy1",
                title="Plot in Sector 99 Noida",
                project="Some Other Project",
                location_raw="Sector 99, Noida",
                area=900.0,
                price="9000000",
                config="Plot",
            ),
        ]
        matches = find_duplicates(records)
        assert len(matches) == 1
        assert {matches[0].left, matches[0].right} == {"magicbricks:mb1", "housing:hs1"}


class TestCanonicalSelection:
    def test_richer_record_wins(self) -> None:
        sparse = make_property(source_id="sparse", project=None, description=None)
        rich = make_property(
            source=Source.HOUSING,
            source_id="rich",
            description="A detailed description of the property " * 5,
            images=["https://cdn/1.jpg", "https://cdn/2.jpg", "https://cdn/3.jpg"],
        )
        rich.rera_number = "HARERA/GGM/123/2024"
        rich.amenities = ["Swimming Pool", "Gym", "Club House", "Power Backup", "Lift"]

        assert choose_canonical([sparse, rich]) is rich

    def test_completeness_beats_recency(self) -> None:
        from datetime import UTC, datetime, timedelta

        old_rich = make_property(
            source_id="old",
            description="Detailed listing text " * 20,
            images=["https://cdn/1.jpg", "https://cdn/2.jpg"],
        )
        old_rich.scraped_at = datetime.now(UTC) - timedelta(days=7)
        old_rich.rera_number = "HARERA/GGM/999/2024"

        new_sparse = make_property(
            source=Source.HOUSING, source_id="new", project=None, description=None
        )
        assert choose_canonical([old_rich, new_sparse]) is old_rich
