"""Catalogue feed export — the contract the HomzRealtor front end consumes.

These tests pin the *shape* as much as the values: the website reads this
payload directly, so a renamed key is a production outage, not a refactor.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from homz.common.enums import City, PossessionStatus, Source
from homz.common.schema import (
    Image,
    Landmark,
    Location,
    ProjectRecord,
    UnitConfiguration,
)
from homz.services import feed

BLOB = "https://gnunxcv3vxg0q9wy.public.blob.vercel-storage.com/listings/"


def make_project(**overrides) -> ProjectRecord:
    data = {
        "source": Source.SQUAREYARDS,
        "source_id": "344020",
        "project_url": "https://www.squareyards.com/eldeco-terra-and-sol-npd-344020",
        "name": "Eldeco Terra And Sol",
        "builder_name": "Eldeco",
        "location": Location(
            locality="Sector 80", sector="Sector 80", city=City.GURGAON,
            city_raw="Sector 80, Gurgaon",
        ),
        "status": PossessionStatus.NEW_LAUNCH,
        "possession_date": date(2031, 1, 1),
        "rera_number": "GGM/1048/780/2026/20",
        "price_min": Decimal("28500000"),
        "price_max": Decimal("31500000"),
        "total_units": 1214,
        "project_area_acres": 8.2,
        "configurations": [
            UnitConfiguration(configuration="3 BHK", bedrooms=3, area_sqft=1900.0,
                              price_min=Decimal("28500000"), price_display="₹ 2.85 Cr"),
            UnitConfiguration(configuration="4 BHK", bedrooms=4, area_sqft=2400.0,
                              price_min=Decimal("31500000"), price_display="₹ 3.15 Cr"),
        ],
        "amenities": ["Swimming Pool", "CCTV / Video Surveillance", "Power Backup", "Kids' Pool"],
        "specifications": {"Master Bedroom-Walls": "Oil Bound Distemper"},
        # `blob_url` is our own processed copy and is what the feed serves;
        # `url` is the portal original, kept for provenance and still what the
        # gallery/interior/master-plan split classifies on.
        "images": [
            Image(url="https://static.squareyards.com/x/project-large-image1.jpg",
                  blob_url=BLOB + "aa/11/" + "a" * 64 + ".webp"),
            Image(url="https://static.squareyards.com/x/apartment-interior-1.jpg",
                  blob_url=BLOB + "bb/22/" + "b" * 64 + ".webp"),
            Image(url="https://static.squareyards.com/x/master-plan.jpg",
                  blob_url=BLOB + "cc/33/" + "c" * 64 + ".webp"),
        ],
        "landmarks": [Landmark(category="school", name="DPS", raw_distance="2.49 KM")],
        "construction_updates": ["Tower A slab cast"],
        "description": "Para one.\n\nPara two.",
        "scraped_at": datetime(2026, 8, 1, 8, 46, tzinfo=UTC),
    }
    data.update(overrides)
    return ProjectRecord(**data)


class TestFeedContract:
    """Key names are load-bearing — the front end indexes them directly."""

    def test_envelope_matches_the_live_api(self) -> None:
        payload = feed.build_response("ggnResidentialProjects", [make_project()])
        assert set(payload) == {"success", "city", "page", "limit", "total", "results"}
        assert payload["success"] is True
        assert payload["city"] == "ggnResidentialProjects"
        assert payload["total"] == 1

    def test_record_carries_every_field_the_site_reads(self) -> None:
        record = feed.to_feed_record(make_project())
        # The exact key set the legacy feed served, which the app destructures.
        for key in (
            "projectTitle", "price", "size", "BHKType", "reraId", "projectStatus",
            "possession", "numberOfUnits", "totalArea", "builderDescription",
            "aboutProject", "amenities", "priceList", "flats", "landmarks",
            "specifications", "recentUpdates", "images", "interiorImages", "masterPlan",
        ):
            assert key in record, f"missing {key}"

    def test_emits_freshness_the_legacy_feed_lacked(self) -> None:
        record = feed.to_feed_record(make_project())
        # Scrape time, not export time — re-exporting stale rows must not look fresh.
        assert record["updatedAt"] == "2026-08-01T08:46:00+00:00"

    def test_pagination_windows_results(self) -> None:
        projects = [make_project(source_id=str(i), name=f"P{i}") for i in range(5)]
        page2 = feed.build_response("ggnResidentialProjects", projects, page=2, limit=2)
        assert page2["total"] == 5
        assert [r["projectTitle"] for r in page2["results"]] == ["P2", "P3"]


class TestFieldMapping:
    def test_price_range_is_human_readable(self) -> None:
        assert feed.to_feed_record(make_project())["price"] == "2.85 Cr - 3.15 Cr"

    def test_price_on_request_when_unpriced(self) -> None:
        record = feed.to_feed_record(make_project(price_min=None, price_max=None))
        assert record["price"] == "Price on Request"

    def test_bhk_types_collapse_to_one_label(self) -> None:
        assert feed.to_feed_record(make_project())["BHKType"] == "3, 4 BHK"

    def test_size_is_the_configuration_range(self) -> None:
        assert feed.to_feed_record(make_project())["size"] == "1900 to 2400"

    def test_amenities_are_grouped_by_category(self) -> None:
        grouped = feed.to_feed_record(make_project())["amenities"]
        by_name = {g["category"]: g["amenities"] for g in grouped}
        assert "Swimming Pool" in by_name["Sports"]
        assert "CCTV / Video Surveillance" in by_name["Safety"]
        assert "Power Backup" in by_name["Convenience"]

    def test_images_split_into_gallery_interior_and_master_plan(self) -> None:
        """The split classifies on the portal URL but serves our own copy."""
        record = feed.to_feed_record(make_project())
        assert record["images"] == [BLOB + "aa/11/" + "a" * 64 + ".webp"]
        assert record["interiorImages"] == [BLOB + "bb/22/" + "b" * 64 + ".webp"]
        assert record["masterPlan"] == {"image": BLOB + "cc/33/" + "c" * 64 + ".webp"}

    def test_description_becomes_paragraph_list(self) -> None:
        assert feed.to_feed_record(make_project())["aboutProject"] == ["Para one.", "Para two."]

    def test_specifications_become_heading_value_rows(self) -> None:
        assert feed.to_feed_record(make_project())["specifications"] == [
            {"heading": "Master Bedroom-Walls", "value": "Oil Bound Distemper"}
        ]

    def test_landmarks_group_by_category(self) -> None:
        assert feed.to_feed_record(make_project())["landmarks"] == {
            "school": [{"name": "DPS", "distance": "2.49 KM"}]
        }


class TestLocationLine:
    """`location` is read in ~18 places in the app, and SquareYards leaks its
    meta description into the parsed locality on PDPs without an address
    block — so this must never render marketing copy as a place."""

    def test_composes_sector_and_city(self) -> None:
        assert feed.to_feed_record(make_project())["location"] == "Sector 80, Gurgaon"

    def test_does_not_repeat_a_component(self) -> None:
        project = make_project(
            location=Location(locality="Sector 70A", sector="Sector 70A", city=City.GURGAON)
        )
        assert feed.to_feed_record(project)["location"] == "Sector 70A, Gurgaon"

    def test_rejects_meta_description_prose(self) -> None:
        project = make_project(
            location=Location(
                locality="Explore Ycon Platinum Heights Pataudi",
                raw="Explore Ycon Platinum Heights Pataudi, Gurgaon is New Launch Project.",
                city=City.GURGAON,
            )
        )
        assert feed.to_feed_record(project)["location"] == "Gurgaon"

    def test_greater_noida_reads_naturally(self) -> None:
        project = make_project(location=Location(sector="Sector 1", city=City.GREATER_NOIDA))
        assert feed.to_feed_record(project)["location"] == "Sector 1, Greater Noida"


class TestPartitioning:
    def test_residential_and_commercial_split(self) -> None:
        residential = make_project()
        commercial = make_project(
            source_id="999", name="M3M Capital Financial Center",
            configurations=[UnitConfiguration(configuration="Office Space", area_sqft=900.0)],
        )
        buckets, withheld = feed.partition([residential, commercial])
        assert [p.name for p in buckets["ggnResidentialProjects"]] == ["Eldeco Terra And Sol"]
        assert [p.name for p in buckets["ggnCommercialProjects"]] == [
            "M3M Capital Financial Center"
        ]
        assert withheld == 0

    def test_stub_projects_are_withheld_not_published(self) -> None:
        # SquareYards publishes registered-but-unannounced projects with no
        # price, configurations or amenities; they would render as blank cards.
        stub = make_project(
            source_id="344229", name="DLF Sector 63, Gurgaon",
            price_min=None, price_max=None, configurations=[], amenities=[],
        )
        buckets, withheld = feed.partition([stub])
        assert withheld == 1
        assert all(not v for v in buckets.values())

    def test_stub_is_kept_when_filtering_is_off(self) -> None:
        stub = make_project(price_min=None, price_max=None, configurations=[], amenities=[])
        buckets, withheld = feed.partition([stub], publishable_only=False)
        assert withheld == 0
        assert len(buckets["ggnResidentialProjects"]) == 1

    def test_cities_without_a_segment_are_dropped(self) -> None:
        ghaziabad = make_project(location=Location(sector="Sector 1", city=City.GHAZIABAD))
        buckets, withheld = feed.partition([ghaziabad])
        assert all(not v for v in buckets.values())
        assert withheld == 0  # dropped for having no segment, not for being thin

    def test_every_segment_key_exists_even_when_empty(self) -> None:
        buckets, _ = feed.partition([])
        assert set(buckets) == set(feed.all_segments())
        assert len(buckets) == 10


class TestWarehouseReadback:
    def test_filters_denormalized_extras_the_model_forbids(self) -> None:
        # Stored docs carry `_id`, `city`, `builder_id`, … which ProjectRecord
        # rejects outright (extra="forbid").
        doc = make_project().model_dump()
        doc.update({"_id": "squareyards:344020", "city": "gurgaon", "builder_id": "b1",
                    "normalized_name": "eldeco terra and sol", "updated_at": datetime.now(UTC)})
        record = feed.record_from_doc(doc)
        assert record is not None
        assert record.name == "Eldeco Terra And Sol"

    def test_undecodable_document_is_skipped_not_raised(self) -> None:
        assert feed.record_from_doc({"_id": "x", "name": "no source or url"}) is None


class TestUnprocessedImagery:
    """Listings whose photos we have not processed yet.

    Their `images[].url` still points at the portal, and those files have the
    portal's watermark burnt into the pixels — serving one would put a
    competitor's brand on a HomzRealtor page. So they are dropped, the
    "images coming soon" placeholder stands in, and the listing sorts to the
    back of its segment.
    """

    PLACEHOLDER = "listings/3d/29/" + "3" * 64 + ".webp"

    def _with_placeholder(self, monkeypatch) -> str:
        """Configure the placeholder and return the URL the feed should emit."""
        from homz.images.blobstore import url_for
        from homz.settings import settings

        monkeypatch.setattr(settings, "placeholder_path", self.PLACEHOLDER)
        return url_for(self.PLACEHOLDER)

    def test_portal_only_images_are_never_served(self, monkeypatch) -> None:
        from homz.settings import settings

        monkeypatch.setattr(settings, "placeholder_path", "")
        record = feed.to_feed_record(make_project(images=[
            Image(url="https://static.squareyards.com/x/project-large-image1.jpg"),
        ]))
        assert record["images"] == []

    def test_placeholder_stands_in_for_the_gallery(self, monkeypatch) -> None:
        expected = self._with_placeholder(monkeypatch)
        record = feed.to_feed_record(make_project(images=[
            Image(url="https://static.squareyards.com/x/project-large-image1.jpg"),
        ]))
        assert record["images"] == [expected]

    def test_partially_processed_listing_shows_only_its_own(self, monkeypatch) -> None:
        self._with_placeholder(monkeypatch)
        own = BLOB + "aa/11/" + "a" * 64 + ".webp"
        record = feed.to_feed_record(make_project(images=[
            Image(url="https://static.squareyards.com/x/one.jpg"),
            Image(url="https://static.squareyards.com/x/two.jpg", blob_url=own),
        ]))
        assert record["images"] == [own]

    def test_has_own_images_tracks_blob_url_not_url(self) -> None:
        assert feed.has_own_images(make_project())
        assert not feed.has_own_images(make_project(images=[
            Image(url="https://static.squareyards.com/x/one.jpg"),
        ]))
        assert not feed.has_own_images(make_project(images=[]))

    def test_a_processed_master_plan_alone_is_not_a_gallery(self) -> None:
        """It renders the placeholder, so it must sort with the placeholders."""
        master_only = make_project(images=[
            Image(url="https://static.squareyards.com/x/master-plan.jpg",
                  blob_url=BLOB + "cc/33/" + "c" * 64 + ".webp"),
        ])
        assert not feed.has_own_images(master_only)

    def test_partition_sorts_placeholder_listings_last(self) -> None:
        processed = make_project(source_id="1", name="Processed")
        pending = make_project(source_id="2", name="Pending", images=[
            Image(url="https://static.squareyards.com/x/one.jpg"),
        ])
        buckets, _ = feed.partition([pending, processed], publishable_only=False)
        names = [r.name for r in buckets["ggnResidentialProjects"]]
        assert names == ["Processed", "Pending"]

    def test_partition_ordering_is_stable_within_each_group(self) -> None:
        # Two processed projects keep the order they arrived in, so the sort
        # only moves placeholder listings and does not reshuffle the rest.
        first = make_project(source_id="1", name="First")
        second = make_project(source_id="2", name="Second")
        buckets, _ = feed.partition([first, second], publishable_only=False)
        assert [r.name for r in buckets["ggnResidentialProjects"]] == ["First", "Second"]


class TestPublishedCities:
    """Only cities with processed imagery are served.

    The rest would be page after page of "images coming soon", which reads as
    a broken catalogue rather than a thin one. Segment keys still exist, since
    the front end requests every city and a 404 is worse than an empty list.
    """

    def _delhi(self, **kw):
        return make_project(
            location=Location(locality="Dwarka", sector="Sector 19", city=City.DELHI,
                              city_raw="Dwarka, Delhi"),
            **kw,
        )

    def test_unpublished_city_records_are_withheld(self) -> None:
        buckets, withheld = feed.partition([self._delhi()], publishable_only=False)
        assert buckets["delhiResidentialProjects"] == []
        assert withheld == 1

    def test_the_segment_key_still_exists(self) -> None:
        buckets, _ = feed.partition([self._delhi()], publishable_only=False)
        assert "delhiResidentialProjects" in buckets

    def test_gurgaon_is_unaffected(self) -> None:
        buckets, withheld = feed.partition([make_project()], publishable_only=False)
        assert len(buckets["ggnResidentialProjects"]) == 1
        assert withheld == 0

    def test_an_empty_setting_publishes_every_city(self, monkeypatch) -> None:
        from homz.settings import settings

        monkeypatch.setattr(settings, "feed_cities", [])
        buckets, withheld = feed.partition([self._delhi()], publishable_only=False)
        assert len(buckets["delhiResidentialProjects"]) == 1
        assert withheld == 0
