"""Feed freshness: firstSeenAt / lastSeenAt / removedAt, and the two safeguards.

The point of this module is that a date in the feed means "the portal was seen
listing this on that day" — and that a bad crawl can never empty the site.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.test_feed import make_project
from tests.test_listings_feed import make_property

from homz.common.base import BaseScraper, ScrapeJob
from homz.common.enums import Source
from homz.common.schema import Image
from homz.etl.refresh import RefreshResult, build_job, carry_over_images
from homz.scrapers.magicbricks.scraper import MagicBricksScraper
from homz.scrapers.squareyards.scraper import SquareYardsScraper
from homz.services import feed as project_feed
from homz.services import listings_feed
from homz.services.freshness import (
    delist_skip_reason,
    feed_lifecycle,
    iso_date,
    lifecycle_from_doc,
    read_segment_total,
    removed_cutoff,
    segment_regressions,
)

AUG_4 = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
OCT_22 = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)


class TestFeedLifecycle:
    def test_sighting_after_the_scrape_makes_the_record_fresh(self) -> None:
        out = feed_lifecycle({"first_seen_at": AUG_4, "last_seen_at": OCT_22}, AUG_4)
        assert out == {
            "firstSeenAt": "2026-08-04",
            "lastSeenAt": "2026-10-22",
            "removedAt": None,
            "updatedAt": OCT_22.isoformat(),
        }

    def test_never_seen_since_scrape_stays_at_the_scrape_date(self) -> None:
        # No sighting recorded: the record must not look fresher than it is.
        out = feed_lifecycle({}, AUG_4)
        assert out["updatedAt"] == AUG_4.isoformat()
        assert out["lastSeenAt"] == "2026-08-04"
        assert out["firstSeenAt"] == "2026-08-04"

    def test_removed_record_carries_removed_at(self) -> None:
        out = feed_lifecycle({"last_seen_at": AUG_4, "removed_at": OCT_22}, AUG_4)
        assert out["removedAt"] == "2026-10-22"

    def test_naive_mongo_datetimes_are_treated_as_utc(self) -> None:
        naive = datetime(2026, 10, 22, 6, 30)
        out = feed_lifecycle({"last_seen_at": naive}, AUG_4)
        assert out["updatedAt"] == OCT_22.isoformat()

    def test_no_dates_at_all(self) -> None:
        assert feed_lifecycle({}, None) == {
            "firstSeenAt": None, "lastSeenAt": None, "removedAt": None, "updatedAt": None,
        }

    def test_dates_are_day_precision_so_same_day_exports_are_identical(self) -> None:
        a = feed_lifecycle({"last_seen_at": OCT_22}, AUG_4)
        b = feed_lifecycle({"last_seen_at": OCT_22 + timedelta(hours=5)}, AUG_4)
        assert a["lastSeenAt"] == b["lastSeenAt"]
        assert iso_date("not a date") is None


class TestLifecycleFromDoc:
    def test_active_document_has_no_removed_at(self) -> None:
        doc = {"is_active": True, "first_seen_at": AUG_4, "last_seen_at": OCT_22}
        assert lifecycle_from_doc(doc) == {
            "first_seen_at": AUG_4, "last_seen_at": OCT_22, "removed_at": None,
        }

    def test_inactive_document_reports_when_it_was_delisted(self) -> None:
        doc = {"is_active": False, "delisted_at": OCT_22, "last_seen_at": AUG_4}
        assert lifecycle_from_doc(doc)["removed_at"] == OCT_22

    def test_legacy_inactive_document_falls_back_to_last_sighting(self) -> None:
        assert lifecycle_from_doc({"is_active": False, "last_seen_at": AUG_4})["removed_at"] == AUG_4

    def test_first_seen_falls_back_to_created_at(self) -> None:
        assert lifecycle_from_doc({"created_at": AUG_4})["first_seen_at"] == AUG_4

    def test_removed_window_is_thirty_days(self) -> None:
        now = datetime(2026, 10, 22, tzinfo=UTC)
        assert removed_cutoff(now) == datetime(2026, 9, 22, tzinfo=UTC)


class TestFeedRecords:
    def test_listing_record_gets_the_three_new_fields(self) -> None:
        record = make_property()
        record.raw["_lifecycle"] = {"first_seen_at": AUG_4, "last_seen_at": OCT_22}
        out = listings_feed.to_listing_feed_record(record)
        assert out["firstSeenAt"] == "2026-08-04"
        assert out["lastSeenAt"] == "2026-10-22"
        assert out["removedAt"] is None
        assert out["updatedAt"] == OCT_22.isoformat()

    def test_listing_without_lifecycle_keeps_its_scrape_date(self) -> None:
        # Backwards compatible: a record read without freshness data behaves as before.
        out = listings_feed.to_listing_feed_record(make_property())
        assert out["updatedAt"] == "2026-08-04T09:00:00+00:00"
        assert out["removedAt"] is None

    def test_removed_listing_is_marked(self) -> None:
        record = make_property()
        record.raw["_lifecycle"] = {"last_seen_at": AUG_4, "removed_at": OCT_22}
        assert listings_feed.to_listing_feed_record(record)["removedAt"] == "2026-10-22"

    def test_project_record_gets_the_three_new_fields(self) -> None:
        record = make_project()
        record.raw["_lifecycle"] = {"first_seen_at": AUG_4, "last_seen_at": OCT_22}
        out = project_feed.to_feed_record(record)
        assert out["lastSeenAt"] == "2026-10-22"
        assert out["firstSeenAt"] == "2026-08-04"
        assert out["removedAt"] is None
        assert out["updatedAt"] == OCT_22.isoformat()

    def test_existing_fields_are_untouched(self) -> None:
        # Additive only: nothing the site already reads may disappear.
        record = make_property()
        before = set(listings_feed.to_listing_feed_record(record))
        record.raw["_lifecycle"] = {"last_seen_at": OCT_22}
        after = listings_feed.to_listing_feed_record(record)
        assert before <= set(after)
        assert set(after) - before == set()  # the keys are always present, lifecycle or not


class TestDelistSafeguard:
    def test_refuses_when_nothing_was_seen_recently(self) -> None:
        # Today's situation: every record last seen in August.
        reason = delist_skip_reason(active_total=20957, fresh=0, latest_run_status=None)
        assert reason and "0/20957" in reason

    def test_refuses_below_eighty_percent(self) -> None:
        assert delist_skip_reason(active_total=1000, fresh=790, latest_run_status="success")

    def test_allows_a_healthy_crawl(self) -> None:
        assert delist_skip_reason(active_total=1000, fresh=800, latest_run_status="success") is None

    @pytest.mark.parametrize("status", ["blocked", "failed"])
    def test_refuses_after_a_bad_run_even_with_good_coverage(self, status: str) -> None:
        reason = delist_skip_reason(active_total=1000, fresh=1000, latest_run_status=status)
        assert reason and status in reason

    def test_partial_run_is_not_a_bad_run(self) -> None:
        assert delist_skip_reason(active_total=10, fresh=10, latest_run_status="partial") is None

    def test_empty_warehouse_has_nothing_to_protect(self) -> None:
        assert delist_skip_reason(active_total=0, fresh=0, latest_run_status=None) is None


class TestExportSafeguard:
    def test_reads_total_from_the_head_of_a_big_file(self, tmp_path) -> None:
        path = tmp_path / "ggnSaleProperties.json"
        path.write_text(
            '{"success":true,"city":"ggnSaleProperties","page":1,"limit":20957,'
            '"total":20957,"results":[' + '{"id":"x","total":1},' * 500 + "{}]}"
        )
        assert read_segment_total(path) == 20957

    def test_missing_or_unreadable_file_is_none(self, tmp_path) -> None:
        assert read_segment_total(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        assert read_segment_total(bad) is None

    def test_flags_a_segment_that_collapsed(self) -> None:
        problems = segment_regressions({"ggnSaleProperties": 20957}, {"ggnSaleProperties": 0})
        assert len(problems) == 1 and "ggnSaleProperties" in problems[0]

    def test_flags_below_eighty_percent_but_not_at_it(self) -> None:
        assert segment_regressions({"s": 1000}, {"s": 799})
        assert not segment_regressions({"s": 1000}, {"s": 800})

    def test_growth_and_tiny_segments_never_block(self) -> None:
        assert not segment_regressions({"s": 1000, "tiny": 10, "none": None}, {"s": 1500})

    def test_a_segment_missing_from_the_new_export_counts_as_zero(self) -> None:
        assert segment_regressions({"ggnRentProperties": 12945}, {})


class TestImageCarryOver:
    def processed(self, name: str) -> dict:
        return Image(
            url=f"https://img.staticmb.com/{name}.jpg",
            blob_url=f"https://blob.example/{name}.webp",
            storage_key=f"_pool/{name}.webp",
            watermark_removed=True,
        ).model_dump()

    def test_rescrape_keeps_already_branded_photos(self) -> None:
        incoming = [Image(url="https://img.staticmb.com/a.jpg")]
        merged, resolved = carry_over_images(incoming, [self.processed("a")])
        assert merged[0].blob_url == "https://blob.example/a.webp"
        assert resolved is True

    def test_new_photo_needs_ingestion(self) -> None:
        incoming = [
            Image(url="https://img.staticmb.com/a.jpg"),
            Image(url="https://img.staticmb.com/b.jpg"),
        ]
        merged, resolved = carry_over_images(incoming, [self.processed("a")])
        assert merged[0].blob_url and merged[1].blob_url is None
        assert resolved is False

    def test_a_photo_the_portal_dropped_is_dropped(self) -> None:
        merged, _ = carry_over_images(
            [Image(url="https://img.staticmb.com/a.jpg")],
            [self.processed("a"), self.processed("gone")],
        )
        assert [i.url for i in merged] == ["https://img.staticmb.com/a.jpg"]

    def test_permanent_failure_is_not_retried(self) -> None:
        dead = Image(url="https://img.staticmb.com/z.jpg", fetch_error="http_404").model_dump()
        merged, resolved = carry_over_images([Image(url=dead["url"])], [dead])
        assert merged[0].fetch_error == "http_404" and resolved is True

    def test_no_existing_document(self) -> None:
        merged, resolved = carry_over_images([Image(url="https://x/a.jpg")], None)
        assert merged[0].blob_url is None and resolved is False

    def test_no_images_is_resolved(self) -> None:
        assert carry_over_images([], None) == ([], True)


class TestSightingIds:
    def test_magicbricks_id_matches_the_stored_source_id(self) -> None:
        url = "https://www.magicbricks.com/propertyDetails/3-BHK-Flat-for-Sale-pdpid-4d423835383138393537"
        assert MagicBricksScraper().source_ids_for_url(url) == ["4d423835383138393537"]

    def test_squareyards_project_maps_to_project_and_projection(self) -> None:
        url = "https://www.squareyards.com/gurgaon-residential-property/dlf-privana/12345/project"
        assert SquareYardsScraper().source_ids_for_url(url) == ["12345", "project:12345"]

    def test_squareyards_unit_maps_to_its_listing_id(self) -> None:
        url = "https://www.squareyards.com/rental-3-bhk-flat-in-sector-60/987654"
        ids = SquareYardsScraper().source_ids_for_url(url)
        assert len(ids) <= 1 and all("project:" not in i for i in ids)

    def test_default_scraper_gives_no_sighting_credit(self) -> None:
        class Bare(BaseScraper):
            source = Source.MAGICBRICKS
            base_url = "https://example.com"

            async def discover(self, job, state):  # pragma: no cover
                yield ""

            async def parse_detail(self, result, job):  # pragma: no cover
                return []

        assert Bare().source_ids_for_url("https://example.com/x") == []


class TestRunJobRecordsSightings:
    async def test_every_discovered_id_is_recorded_even_when_unchanged(self) -> None:
        urls = [
            "https://www.magicbricks.com/propertyDetails/a-pdpid-4d42aaaa",
            "https://www.magicbricks.com/propertyDetails/b-pdpid-4d42bbbb",
        ]

        class Stub(MagicBricksScraper):
            async def discover(self, job, state):
                for url in urls:
                    yield url

            async def fetch_detail(self, url, job):
                raise RuntimeError("no network in tests")

        async with Stub() as scraper:
            _, report = await scraper.run_job(ScrapeJob(name="t", max_items=10))
        assert report.seen_ids == {"4d42aaaa", "4d42bbbb"}
        assert report.as_dict()["seen"] == 2


class TestRefreshJobs:
    def test_magicbricks_sweeps_the_full_page_range(self) -> None:
        job = build_job("magicbricks", city="gurgaon", listing_type="rent",
                        property_type=None, max_pages=None)
        assert (job.max_pages, job.listing_type, job.incremental) == (100, "rent", False)

    def test_squareyards_rent_uses_the_rentals_job(self) -> None:
        job = build_job("squareyards", city="gurgaon", listing_type="rent",
                        property_type=None, max_pages=None)
        assert job.name == "rentals" and job.listing_type == "rent"

    def test_other_sources_are_refused(self) -> None:
        with pytest.raises(ValueError):
            build_job("reddit", city="gurgaon", listing_type=None,
                      property_type=None, max_pages=None)

    def test_coverage_is_share_of_active_records_confirmed(self) -> None:
        result = RefreshResult(source="magicbricks", job="refresh:x")
        assert result.coverage is None
        result.known_active, result.touched = 1000, 280
        assert result.coverage == 0.28
        assert result.as_dict()["coverage"] == 0.28
