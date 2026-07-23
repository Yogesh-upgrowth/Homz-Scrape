"""Parser tests against synthetic fixtures.

These use hand-built HTML that mirrors each portal's real structure. When a
portal changes its markup, replay a stored payload from `data/raw/<source>/…`
through the same parser to confirm the fix — that is the whole reason raw HTML
is archived.
"""

from __future__ import annotations

from decimal import Decimal

from homz.common import domx
from homz.common.captcha import BlockKind, detect_block
from homz.common.enums import City, ListingType, PossessionStatus, PropertyType
from homz.scrapers.magicbricks import parser as mb
from homz.scrapers.reddit import parser as reddit
from homz.scrapers.squareyards import parser as sy

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

MB_DETAIL_HTML = """
<html><head>
  <meta property="og:title" content="3 BHK Flat for Sale in Sector 102, Gurgaon"/>
  <script type="application/ld+json">
  {"@type":"Residence","name":"3 BHK Flat in Godrej Aristocrat",
   "description":"Spacious 3 BHK with park view.",
   "address":{"streetAddress":"Sector 102, Dwarka Expressway, Gurgaon"},
   "geo":{"latitude":28.5021,"longitude":76.9856}}
  </script>
</head><body>
  <h1 class="mb-ldp__dtls__title">3 BHK Flat for Sale in Sector 102</h1>
  <div class="mb-ldp__dtls__price">₹ 2.35 Cr</div>
  <ul class="mb-ldp__dtls__body__list">
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Super Area</div>
      <div class="mb-ldp__dtls__body__list--value">1,850 sqft</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Carpet Area</div>
      <div class="mb-ldp__dtls__body__list--value">1,250 sqft</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Bedrooms</div>
      <div class="mb-ldp__dtls__body__list--value">3 BHK</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Bathrooms</div>
      <div class="mb-ldp__dtls__body__list--value">3</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Floor</div>
      <div class="mb-ldp__dtls__body__list--value">12 out of 24</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Status</div>
      <div class="mb-ldp__dtls__body__list--value">Under Construction</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">Project</div>
      <div class="mb-ldp__dtls__body__list--value">Godrej Aristocrat</div></li>
    <li class="mb-ldp__dtls__body__list--item">
      <div class="mb-ldp__dtls__body__list--label">RERA</div>
      <div class="mb-ldp__dtls__body__list--value">UPRERAPRJ998877</div></li>
  </ul>
  <div class="mb-ldp__amenities">
    <ul><li>Swimming Pool</li><li>Gymnasium</li><li>Power Backup</li></ul>
  </div>
  <div class="mb-ldp__gallery">
    <img src="https://img.staticmb.com/photo1.jpg" alt="Living room"/>
    <img data-src="https://img.staticmb.com/photo2.jpg" alt="Bedroom"/>
    <img src="https://img.staticmb.com/placeholder.png" alt="ignore me"/>
  </div>
  <ul class="mb-ldp__nearby__list">
    <li class="mb-ldp__nearby__list--item">Sector 101 Metro Station 1.2 km</li>
    <li class="mb-ldp__nearby__list--item">DPS School 800 m</li>
  </ul>
</body></html>
"""

MB_SEARCH_HTML = """
<html><body><div class="mb-srp__list">
  <div class="mb-srp__card">
    <a class="mb-srp__card--title"
       href="/propertyDetails/3-BHK-in-Sector-102-pdpid-4d4235373">3 BHK</a>
  </div>
  <div class="mb-srp__card">
    <a class="mb-srp__card--title"
       href="https://www.magicbricks.com/propertyDetails/2-BHK-pdpid-9z9z9z9?utm_source=srp">2 BHK</a>
  </div>
</div></body></html>
"""

SY_PDP_HTML = """
<html><body>
  <h1>Godrej Aristocrat\nSector 49, Gurgaon</h1>
  <div class="price-box">₹ 3.10 Cr - 5.25 Cr</div>
  <div class="unit-status-box">
    <div class="status"><span>Project Status</span><strong>Under Construction</strong></div>
    <div class="status"><span>Possession Starting From</span><strong>Dec 2028</strong></div>
    <div class="status"><span>Number of Units</span><strong>444</strong></div>
    <div class="status"><span>Total area</span><strong>9.19 Acres</strong></div>
  </div>
  <div id="aboutProject"><p>A premium residential development.</p></div>
  <div class="accordion-header" data-reraid="RC/REP/HARERA/GGM/812/544/2024/45"></div>
  <div id="amenities"><div class="amenities-list-box">
    <ul><li><span>Swimming Pool</span></li><li><span>Club House</span></li></ul>
  </div></div>
  <table id="priceList"><tbody>
    <tr><td><span>3 BHK</span></td><td><strong>₹ 3.10 Cr</strong></td>
        <td class="unit-value" data-sqft="2100">233 sq yd</td></tr>
    <tr><td><span>4 BHK</span></td><td><strong>₹ 5.25 Cr</strong></td>
        <td class="unit-value" data-sqft="3200">355 sq yd</td></tr>
  </tbody></table>
  <div id="mapLandmarks">
    <div class="near-distance-box" data-attribute="Metro">
      <table><tbody>
        <tr><td class="distance-title">Huda City Centre</td>
            <td class="distance"><span>6.5 km</span></td></tr>
      </tbody></table>
    </div>
    <div class="near-distance-box" data-attribute="School">
      <table><tbody>
        <tr><td class="distance-title">Scottish High</td>
            <td class="distance"><span>2.1 km</span></td></tr>
      </tbody></table>
    </div>
  </div>
  <div id="specifications"><table class="specification-table"><tbody>
    <tr><td class="specification-heading">Flooring</td>
        <td class="specification-value">Italian Marble</td></tr>
  </tbody></table></div>
  <div id="recentUpdates"><div class="recent-updates-box"><article>
    <div class="details"><p>Tower C slab work completed.</p></div>
  </article></div></div>
  <img src="https://static.squareyards.com/project/hero.jpg" alt="Hero"/>
</body></html>
"""


# ---------------------------------------------------------------------------
# MagicBricks
# ---------------------------------------------------------------------------


class TestMagicBricksParser:
    def test_listing_id_from_url(self) -> None:
        url = "https://www.magicbricks.com/propertyDetails/3-BHK-pdpid-4d4235373"
        assert mb.extract_listing_id(url) == "4d4235373"

    def test_search_results_deduplicated_and_absolute(self) -> None:
        urls = mb.parse_search_results(MB_SEARCH_HTML)
        assert urls
        assert all(u.startswith("https://www.magicbricks.com/") for u in urls)
        # utm_source must be stripped by canonicalization.
        assert not any("utm_source" in u for u in urls)

    def test_detail_extraction(self) -> None:
        record = mb.parse_property_detail(
            MB_DETAIL_HTML,
            "https://www.magicbricks.com/propertyDetails/3-BHK-pdpid-4d4235373",
        )
        assert record is not None
        assert record.source_id == "4d4235373"
        assert record.price == Decimal("23500000")
        assert record.area_sqft == 1850.0
        assert record.carpet_area_sqft == 1250.0
        assert record.bedrooms == 3
        assert record.bathrooms == 3
        assert record.floor_number == 12
        assert record.total_floors == 24
        assert record.property_type == PropertyType.APARTMENT
        assert record.possession_status == PossessionStatus.UNDER_CONSTRUCTION
        assert record.project_name == "Godrej Aristocrat"
        assert record.rera_number == "UPRERAPRJ998877"

    def test_price_per_sqft_derived_when_absent(self) -> None:
        record = mb.parse_property_detail(MB_DETAIL_HTML, "https://x/pdpid-1")
        # 23,500,000 / 1850 ≈ 12,703
        assert record.price_per_sqft == Decimal("12703")

    def test_location_and_micro_market(self) -> None:
        record = mb.parse_property_detail(MB_DETAIL_HTML, "https://x/pdpid-1")
        assert record.location.city == City.GURGAON
        assert record.location.sector == "Sector 102"
        assert record.location.micro_market == "Dwarka Expressway"
        assert record.location.geo is not None

    def test_images_skip_placeholders_and_read_lazy_attrs(self) -> None:
        record = mb.parse_property_detail(MB_DETAIL_HTML, "https://x/pdpid-1")
        urls = [i.url for i in record.images]
        assert "https://img.staticmb.com/photo1.jpg" in urls
        assert "https://img.staticmb.com/photo2.jpg" in urls  # from data-src
        assert not any("placeholder" in u for u in urls)

    def test_landmarks_categorised(self) -> None:
        record = mb.parse_property_detail(MB_DETAIL_HTML, "https://x/pdpid-1")
        categories = {lm.category for lm in record.landmarks}
        assert "metro" in categories
        assert "school" in categories
        metro = next(lm for lm in record.landmarks if lm.category == "metro")
        assert metro.distance_km == 1.2

    def test_finalize_sets_hashes(self) -> None:
        record = mb.parse_property_detail(MB_DETAIL_HTML, "https://x/pdpid-1")
        assert record.content_hash and record.dedupe_key

    def test_search_url_builder(self) -> None:
        url = mb.build_search_url(city="Gurgaon", listing_type="rent", page=3)
        assert "for-rent-in-gurgaon" in url
        assert url.endswith("page=3")


# ---------------------------------------------------------------------------
# SquareYards
# ---------------------------------------------------------------------------


class TestSquareYardsParser:
    def test_project_detail(self) -> None:
        record = sy.parse_project_detail(
            SY_PDP_HTML, "https://www.squareyards.com/gurgaon/godrej-aristocrat-123456"
        )
        assert record is not None
        assert record.name == "Godrej Aristocrat"
        assert record.source_id == "123456"
        assert record.price_min == Decimal("31000000")
        assert record.price_max == Decimal("52500000")
        assert record.status == PossessionStatus.UNDER_CONSTRUCTION
        assert record.total_units == 444
        assert record.rera_number == "RC/REP/HARERA/GGM/812/544/2024/45"

    def test_project_area_converted_to_acres(self) -> None:
        record = sy.parse_project_detail(SY_PDP_HTML, "https://x/p-123456")
        assert record.project_area_acres == 9.19

    def test_price_list_prefers_data_sqft_over_display_unit(self) -> None:
        # Display text is sq yards; data-sqft is authoritative.
        record = sy.parse_project_detail(SY_PDP_HTML, "https://x/p-123456")
        areas = sorted(c.area_sqft for c in record.configurations if c.area_sqft)
        assert areas == [2100.0, 3200.0]

    def test_landmarks_use_data_attribute_category(self) -> None:
        record = sy.parse_project_detail(SY_PDP_HTML, "https://x/p-123456")
        by_category = {lm.category: lm for lm in record.landmarks}
        assert "metro" in by_category
        assert by_category["metro"].distance_km == 6.5
        assert "school" in by_category

    def test_specifications_and_updates(self) -> None:
        record = sy.parse_project_detail(SY_PDP_HTML, "https://x/p-123456")
        assert record.specifications.get("Flooring") == "Italian Marble"
        assert any("Tower C" in u for u in record.construction_updates)

    def test_project_to_property_projection(self) -> None:
        project = sy.parse_project_detail(SY_PDP_HTML, "https://x/p-123456")
        prop = sy.project_to_property(project)
        assert prop.source_id == "project:123456"
        assert prop.listing_type == ListingType.PROJECT
        assert prop.project_name == "Godrej Aristocrat"
        # Entry price/area come from the smallest configuration.
        assert prop.area_sqft == 2100.0
        assert prop.price == Decimal("31000000")
        assert prop.content_hash


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------


class TestRedditParser:
    def test_relevance_filters_noise(self) -> None:
        assert reddit.is_relevant("Best builder in Sector 102 for a 3BHK flat?", None)
        assert not reddit.is_relevant("Where to get good momos in Gurgaon?", None)

    def test_parse_post(self) -> None:
        payload = {
            "data": {
                "id": "1abc2de",
                "subreddit": "gurgaon",
                "title": "Signature Global possession delayed — anyone else?",
                "selftext": "Booked in Sector 37D. RERA complaint filed.",
                "author": "someuser",
                "created_utc": 1_750_000_000,
                "score": 42,
                "upvote_ratio": 0.93,
                "num_comments": 17,
                "permalink": "/r/gurgaon/comments/1abc2de/x/",
                "url": "https://reddit.com/r/gurgaon/comments/1abc2de/x/",
                "is_self": True,
            }
        }
        record = reddit.parse_post(payload)
        assert record is not None
        assert record.source_id == "1abc2de"
        assert record.score == 42
        assert record.permalink.startswith("https://www.reddit.com/")
        assert "Signature Global" in record.detected_builders
        assert "Sector 37D" in record.detected_sectors
        assert record.detected_city == City.GURGAON
        assert "rera" in record.topics

    def test_deleted_author_becomes_none(self) -> None:
        payload = {"data": {"id": "x1", "subreddit": "gurgaon", "title": "t",
                            "author": "[deleted]", "permalink": "/p/"}}
        assert reddit.parse_post(payload).author is None

    def test_comment_tree_flattened_and_filtered(self) -> None:
        listing = [
            {"kind": "Listing", "data": {"children": []}},
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t1",
                            "data": {
                                "id": "c1", "body": "Same here, 2 year delay.", "score": 15,
                                "author": "a", "created_utc": 1_750_000_100,
                                "replies": {
                                    "data": {
                                        "children": [
                                            {"kind": "t1", "data": {
                                                "id": "c2", "body": "Filed with HARERA too.",
                                                "score": 8, "author": "b"}},
                                        ]
                                    }
                                },
                            },
                        },
                        {"kind": "t1", "data": {"id": "c3", "body": "[deleted]", "score": 5}},
                        {"kind": "t1", "data": {"id": "c4", "body": "downvoted", "score": -3}},
                    ]
                },
            },
        ]
        comments = reddit.parse_comments(listing, "1abc2de")
        ids = [c.comment_id for c in comments]
        assert ids == ["c1", "c2"]          # sorted by score, filtered
        assert comments[1].depth == 1       # nested reply keeps its depth

    def test_full_text_truncates(self) -> None:
        record = reddit.parse_post(
            {"data": {"id": "x", "subreddit": "gurgaon", "title": "t" * 100,
                      "selftext": "b" * 50_000, "permalink": "/p/"}}
        )
        assert len(record.full_text(max_chars=1000)) <= 1000


# ---------------------------------------------------------------------------
# shared extraction + block detection
# ---------------------------------------------------------------------------


class TestDomx:
    def test_json_ld_flattens_graph(self) -> None:
        from bs4 import BeautifulSoup

        html = """<script type="application/ld+json">
        {"@graph":[{"@type":"Organization","name":"X"},{"@type":"Residence","name":"Y"}]}
        </script>"""
        soup = BeautifulSoup(html, "lxml")
        assert len(domx.json_ld(soup)) == 2
        assert domx.json_ld_of_type(soup, "Residence")["name"] == "Y"

    def test_find_first_key_survives_restructure(self) -> None:
        payload = {"props": {"pageProps": {"deeply": {"nested": {"propertyId": "abc123"}}}}}
        assert domx.find_first_key(payload, "propertyId") == "abc123"

    def test_deep_get(self) -> None:
        payload = {"a": {"b": [{"c": 7}]}}
        assert domx.deep_get(payload, "a.b.0.c") == 7
        assert domx.deep_get(payload, "a.x.y", default="fallback") == "fallback"

    def test_window_state_balanced_braces(self) -> None:
        from bs4 import BeautifulSoup

        html = """<script>window.__INITIAL_STATE__ = {"a":{"b":"}"},"c":1};</script>"""
        soup = BeautifulSoup(html, "lxml")
        state = domx.window_state(soup, "window.__INITIAL_STATE__")
        assert state == {"a": {"b": "}"}, "c": 1}


class TestBlockDetection:
    def test_captcha(self) -> None:
        signal = detect_block(
            status_code=200, body="<html><body>Please complete the g-recaptcha</body></html>"
        )
        assert signal.kind is BlockKind.CAPTCHA
        assert not signal.is_retryable

    def test_cloudflare_wall(self) -> None:
        signal = detect_block(status_code=403, body="<title>Just a moment...</title>")
        assert signal.is_blocked

    def test_rate_limit_reads_retry_after(self) -> None:
        signal = detect_block(status_code=429, body="", headers={"Retry-After": "120"})
        assert signal.kind is BlockKind.RATE_LIMITED
        assert signal.retry_after == 120.0
        assert signal.is_retryable

    def test_clean_page_passes(self) -> None:
        body = "<html><body>" + ("<div>listing content</div>" * 100) + "</body></html>"
        assert not detect_block(status_code=200, body=body).is_blocked

    def test_tiny_shell_flagged(self) -> None:
        signal = detect_block(status_code=200, body="<html><body></body></html>")
        assert signal.kind is BlockKind.EMPTY_SHELL


class TestScrapeJobKey:
    def test_params_are_part_of_the_key(self) -> None:
        """scrape_state is keyed on (source, job) — two Reddit jobs differing
        only by subreddit must not share one cursor row."""
        from homz.common.base import ScrapeJob

        a = ScrapeJob(name="subreddit", params={"subreddit": "gurgaon"})
        b = ScrapeJob(name="subreddit", params={"subreddit": "noida"})
        assert a.key != b.key
        assert "gurgaon" in a.key


class TestSearchUrlBuilders:
    """URL patterns verified against the live sites.

    The first version of build_search_url guessed a pattern that 404'd, and
    the run still reported success. Both are pinned here.
    """

    def test_magicbricks_sale(self) -> None:
        url = mb.build_search_url(city="Gurgaon", listing_type="sale")
        assert url == "https://www.magicbricks.com/property-for-sale-in-gurgaon-pppfs"

    def test_magicbricks_rent_uses_a_different_suffix(self) -> None:
        # -pppfr, not -pppfs. Using pppfs for rent 404s.
        url = mb.build_search_url(city="Gurgaon", listing_type="rent")
        assert url == "https://www.magicbricks.com/property-for-rent-in-gurgaon-pppfr"

    def test_magicbricks_delhi_is_slugged_new_delhi(self) -> None:
        url = mb.build_search_url(city="delhi", listing_type="sale")
        assert "new-delhi" in url

    def test_magicbricks_gurugram_normalises_to_gurgaon(self) -> None:
        assert mb.build_search_url(city="Gurugram") == mb.build_search_url(city="Gurgaon")

    def test_magicbricks_pagination(self) -> None:
        assert mb.build_search_url(city="noida", page=3).endswith("-pppfs?page=3")
        assert "?page=" not in mb.build_search_url(city="noida", page=1)


class TestJobStatus:
    """A job that produced nothing must not report success."""

    def _report(self, **kwargs):
        from homz.common.base import ScrapeReport

        report = ScrapeReport(source="magicbricks", job="t")
        for key, value in kwargs.items():
            setattr(report, key, value)
        return report

    def _finalize(self, report):
        # Mirror the status resolution in BaseScraper.run_job.
        from homz.common.enums import JobStatus

        if report.errors and report.parsed:
            return JobStatus.PARTIAL
        if report.errors and not report.parsed:
            return JobStatus.FAILED
        if report.discovered == 0:
            return JobStatus.FAILED
        if report.parsed == 0 and report.skipped_known == 0:
            return JobStatus.FAILED
        return JobStatus.SUCCESS

    def test_discovering_nothing_is_a_failure(self) -> None:
        from homz.common.enums import JobStatus

        # The exact shape of the MagicBricks 404 bug.
        assert self._finalize(self._report(discovered=0, parsed=0)) is JobStatus.FAILED

    def test_parsing_nothing_from_candidates_is_a_failure(self) -> None:
        from homz.common.enums import JobStatus

        assert self._finalize(
            self._report(discovered=30, fetched=30, parsed=0)
        ) is JobStatus.FAILED

    def test_all_known_is_success_not_failure(self) -> None:
        """An incremental run where everything was already seen parsed 0 new
        records — that is a healthy no-op, not an outage."""
        from homz.common.enums import JobStatus

        assert self._finalize(
            self._report(discovered=30, fetched=30, parsed=0, skipped_known=30)
        ) is JobStatus.SUCCESS

    def test_normal_run_is_success(self) -> None:
        from homz.common.enums import JobStatus

        assert self._finalize(
            self._report(discovered=30, fetched=30, parsed=28)
        ) is JobStatus.SUCCESS

    def test_squareyards_city_url(self) -> None:
        # Verified live: /new-projects-in-{city}; the /{city}/new-projects
        # form 404s.
        assert sy.build_city_url("Gurgaon") == (
            "https://www.squareyards.com/new-projects-in-gurgaon"
        )
        assert sy.build_city_url("Greater Noida") == (
            "https://www.squareyards.com/new-projects-in-greater-noida"
        )
