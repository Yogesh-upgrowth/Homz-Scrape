"""Category listing feeds (Sale/Rent/Pg/Commercial) — the contract the
front end's category tabs consume.

Mirrors `tests/test_feed.py`'s approach: pin the *shape* as much as the
values, since a renamed key breaks a live page, not just a test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from homz.common.enums import City, ListingType, PossessionStatus, PropertyType, Source
from homz.common.schema import Image, Landmark, Location, PropertyRecord
from homz.services import listings_feed


def make_property(**overrides) -> PropertyRecord:
    data = {
        "source": Source.MAGICBRICKS,
        "source_id": "4d423835383138393537",
        "listing_url": "https://www.magicbricks.com/propertyDetails/3-BHK-x-pdpid-1",
        "title": "3 BHK Flat for Sale in IREO Skyon",
        "project_name": "IREO Skyon",
        "builder_name": "IREO",
        "listing_type": ListingType.RESALE,
        "property_type": PropertyType.APARTMENT,
        "configuration": "3 BHK",
        "bedrooms": 3,
        "price": Decimal("46500000"),
        "area_sqft": 2045.0,
        "location": Location(
            locality="Sector 60", sector="Sector 60", city=City.GURGAON,
            city_raw="Sector 60, Gurgaon",
        ),
        "possession_status": PossessionStatus.READY_TO_MOVE,
        "rera_number": "GGM/1001/2020/1",
        "amenities": ["Swimming Pool", "CCTV / Video Surveillance", "Power Backup"],
        "specifications": {"Flooring": "Vitrified Tiles"},
        "images": [
            Image(url="https://img.staticmb.com/x/gallery1.jpg"),
            Image(url="https://img.staticmb.com/x/apartment-interior-1.jpg"),
        ],
        "landmarks": [Landmark(category="school", name="DPS", raw_distance="1.2 KM")],
        "description": "Spacious flat.\n\nPark facing.",
        "scraped_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return PropertyRecord(**data)


class TestFeedContract:
    def test_envelope_matches_the_projects_feed(self) -> None:
        payload = listings_feed.build_response("ggnSaleProperties", [make_property()])
        assert set(payload) == {"success", "city", "page", "limit", "total", "results"}
        assert payload["success"] is True
        assert payload["total"] == 1

    def test_record_carries_every_field_the_site_needs(self) -> None:
        record = listings_feed.to_listing_feed_record(make_property())
        for key in (
            "id", "title", "location", "price", "priceValue", "rentMonthly", "size",
            "areaValue", "configuration", "bedrooms", "propertyType", "listingType",
            "isCommercial", "reraId", "projectStatus", "possession", "builderDescription",
            "aboutProject", "amenities", "specifications", "images", "interiorImages",
            "masterPlan", "landmarks", "listingUrl", "updatedAt", "investmentScore",
            "riskScore", "locationScore", "aiSummary",
        ):
            assert key in record, f"missing {key}"

    def test_id_is_the_natural_key(self) -> None:
        record = listings_feed.to_listing_feed_record(make_property())
        assert record["id"] == "magicbricks:4d423835383138393537"

    def test_scores_default_to_none_before_enrichment(self) -> None:
        record = listings_feed.to_listing_feed_record(make_property())
        assert record["investmentScore"] is None
        assert record["riskScore"] is None
        assert record["locationScore"] is None
        assert record["aiSummary"] is None

    def test_scores_surface_once_enrichment_has_run(self) -> None:
        # investment_score/risk_score/location_score/ai_summary live on the
        # Mongo doc, not the PropertyRecord schema — load_properties() stashes
        # them in record.raw["_enrichment"] since record_from_doc() would
        # otherwise drop them; this pins that hand-off.
        enriched = make_property()
        enriched.raw["_enrichment"] = {
            "investment_score": 72.5, "risk_score": 18.0, "location_score": 65.0,
            "ai_summary": "A well-connected 3 BHK resale unit in IREO Skyon.",
        }
        record = listings_feed.to_listing_feed_record(enriched)
        assert record["investmentScore"] == 72.5
        assert record["riskScore"] == 18.0
        assert record["locationScore"] == 65.0
        assert record["aiSummary"] == "A well-connected 3 BHK resale unit in IREO Skyon."

    def test_emits_freshness(self) -> None:
        record = listings_feed.to_listing_feed_record(make_property())
        assert record["updatedAt"] == "2026-08-04T09:00:00+00:00"

    def test_pagination_windows_results(self) -> None:
        props = [make_property(source_id=str(i), title=f"P{i}") for i in range(5)]
        page2 = listings_feed.build_response("ggnSaleProperties", props, page=2, limit=2)
        assert page2["total"] == 5
        assert [r["title"] for r in page2["results"]] == ["P2", "P3"]


class TestFieldMapping:
    def test_raw_filter_fields_are_not_just_display_strings(self) -> None:
        # Unlike the legacy Projects feed, this contract must support
        # client-side range/facet filtering, so raw numerics are required.
        record = listings_feed.to_listing_feed_record(make_property())
        assert record["priceValue"] == Decimal("46500000")
        assert record["areaValue"] == 2045.0
        assert record["propertyType"] == "apartment"
        assert record["listingType"] == "resale"

    def test_sale_price_is_human_readable(self) -> None:
        assert listings_feed.to_listing_feed_record(make_property())["price"] == "4.65 Cr"

    def test_rent_shows_monthly_not_a_sale_price(self) -> None:
        rent = make_property(
            listing_type=ListingType.RENT, price=None, rent_monthly=Decimal("35000")
        )
        record = listings_feed.to_listing_feed_record(rent)
        assert record["price"] == "35,000/month"
        assert record["priceValue"] is None
        assert record["rentMonthly"] == Decimal("35000")

    def test_price_on_request_when_unpriced(self) -> None:
        record = listings_feed.to_listing_feed_record(make_property(price=None, price_max=None))
        assert record["price"] == "Price on Request"

    def test_amenities_are_grouped_by_category(self) -> None:
        grouped = listings_feed.to_listing_feed_record(make_property())["amenities"]
        by_name = {g["category"]: g["amenities"] for g in grouped}
        assert "Swimming Pool" in by_name["Sports"]
        assert "Power Backup" in by_name["Convenience"]

    def test_images_split_into_gallery_and_interior(self) -> None:
        record = listings_feed.to_listing_feed_record(make_property())
        assert record["images"] == ["https://img.staticmb.com/x/gallery1.jpg"]
        assert record["interiorImages"] == [
            "https://img.staticmb.com/x/apartment-interior-1.jpg"
        ]

    def test_description_becomes_paragraph_list(self) -> None:
        assert listings_feed.to_listing_feed_record(make_property())["aboutProject"] == [
            "Spacious flat.", "Park facing.",
        ]


class TestPartitioning:
    def test_sale_resale_and_new_launch_share_one_bucket(self) -> None:
        resale = make_property(source_id="1", listing_type=ListingType.RESALE)
        new_launch = make_property(source_id="2", listing_type=ListingType.NEW_LAUNCH)
        plain_sale = make_property(source_id="3", listing_type=ListingType.SALE)
        buckets, withheld = listings_feed.partition([resale, new_launch, plain_sale])
        assert len(buckets["ggnSaleProperties"]) == 3
        assert withheld == 0

    def test_rent_and_pg_and_commercial_get_their_own_bucket(self) -> None:
        rent = make_property(source_id="1", listing_type=ListingType.RENT)
        pg = make_property(source_id="2", listing_type=ListingType.PG)
        commercial = make_property(
            source_id="3", listing_type=ListingType.COMMERCIAL,
            property_type=PropertyType.OFFICE,
        )
        buckets, withheld = listings_feed.partition([rent, pg, commercial])
        assert len(buckets["ggnRentProperties"]) == 1
        assert len(buckets["ggnPgProperties"]) == 1
        assert len(buckets["ggnCommercialProperties"]) == 1
        assert withheld == 0

    def test_commercial_bucket_is_by_listing_type_not_property_type_or_flag(self) -> None:
        # A commercial-flagged apartment resale must NOT land in Commercial —
        # that axis was explicitly rejected in favor of listing_type.
        flagged_but_not_commercial_type = make_property(
            listing_type=ListingType.RESALE, is_commercial=True,
        )
        buckets, _ = listings_feed.partition([flagged_but_not_commercial_type])
        assert len(buckets["ggnCommercialProperties"]) == 0
        assert len(buckets["ggnSaleProperties"]) == 1

    def test_unknown_listing_type_is_withheld(self) -> None:
        unknown = make_property(listing_type=ListingType.UNKNOWN)
        buckets, withheld = listings_feed.partition([unknown])
        assert withheld == 1
        assert all(not v for v in buckets.values())

    def test_stub_listings_are_withheld_not_published(self) -> None:
        stub = make_property(price=None, price_max=None, configuration=None, amenities=[])
        buckets, withheld = listings_feed.partition([stub])
        assert withheld == 1
        assert all(not v for v in buckets.values())

    def test_cities_without_a_segment_are_dropped(self) -> None:
        ghaziabad = make_property(location=Location(sector="Sector 1", city=City.GHAZIABAD))
        buckets, withheld = listings_feed.partition([ghaziabad])
        assert all(not v for v in buckets.values())
        assert withheld == 0  # dropped for having no segment, not for being thin

    def test_every_segment_key_exists_even_when_empty(self) -> None:
        buckets, _ = listings_feed.partition([])
        assert set(buckets) == set(listings_feed.all_segments())
        assert len(buckets) == 20  # 5 cities x 4 categories


class TestWarehouseReadback:
    def test_filters_denormalized_extras_the_model_forbids(self) -> None:
        doc = make_property().model_dump()
        doc.update({
            "_id": "magicbricks:4d423835383138393537", "city": "gurgaon",
            "builder_id": "b1", "canonical_id": None, "is_active": True,
        })
        record = listings_feed.record_from_doc(doc)
        assert record is not None
        assert record.title == "3 BHK Flat for Sale in IREO Skyon"

    def test_undecodable_document_is_skipped_not_raised(self) -> None:
        assert listings_feed.record_from_doc({"_id": "x", "title": "no source or url"}) is None
