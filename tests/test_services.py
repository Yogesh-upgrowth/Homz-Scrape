"""Tests for the on-demand fill loop and the ingest surface.

These are the pieces where a mistake is expensive in a way types don't catch:
a broken cooldown turns search traffic into a scraping loop against a portal,
and a broken auth check lets anyone write into the warehouse.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from homz.search.query import PropertySearchQuery
from homz.services.ingest import IngestError, RateLimiter, verify_token
from homz.services.ondemand import describe_query, query_fingerprint
from homz.settings import settings


class TestQueryFingerprint:
    def test_identical_queries_share_a_fingerprint(self) -> None:
        a = PropertySearchQuery(q="godrej", city="gurgaon", bedrooms_min=3)
        b = PropertySearchQuery(q="godrej", city="gurgaon", bedrooms_min=3)
        assert query_fingerprint(a) == query_fingerprint(b)

    def test_pagination_is_not_part_of_identity(self) -> None:
        """Page 2 of a search is the same information need as page 1 — if it
        were a distinct fingerprint, paging through an empty result set would
        queue a fresh scrape task per page."""
        page1 = PropertySearchQuery(q="godrej", city="gurgaon", page=1)
        page2 = PropertySearchQuery(q="godrej", city="gurgaon", page=2, page_size=50)
        assert query_fingerprint(page1) == query_fingerprint(page2)

    def test_sort_is_not_part_of_identity(self) -> None:
        a = PropertySearchQuery(q="godrej", sort="relevance")
        b = PropertySearchQuery(q="godrej", sort="price_asc")
        assert query_fingerprint(a) == query_fingerprint(b)

    def test_different_filters_differ(self) -> None:
        a = PropertySearchQuery(city="gurgaon")
        b = PropertySearchQuery(city="noida")
        assert query_fingerprint(a) != query_fingerprint(b)

    def test_case_and_whitespace_insensitive(self) -> None:
        a = PropertySearchQuery(q="Godrej Aristocrat")
        b = PropertySearchQuery(q="  godrej aristocrat  ")
        assert query_fingerprint(a) == query_fingerprint(b)

    def test_list_order_does_not_matter(self) -> None:
        a = PropertySearchQuery(property_type=["villa", "plot"])
        b = PropertySearchQuery(property_type=["plot", "villa"])
        assert query_fingerprint(a) == query_fingerprint(b)

    def test_unfiltered_query_is_sentinel(self) -> None:
        """A bare 'everything' query returning nothing means the warehouse is
        empty — a seeding problem, not a gap to fill one query at a time."""
        assert query_fingerprint(PropertySearchQuery()) == "empty"
        assert query_fingerprint(PropertySearchQuery(page=3, sort="newest")) == "empty"

    def test_describe_query_carries_what_a_scraper_needs(self) -> None:
        query = PropertySearchQuery(
            q="godrej", city="gurgaon", bedrooms_min=3, price_max=Decimal("25000000")
        )
        described = describe_query(query)
        assert described["q"] == "godrej"
        assert described["city"] == "gurgaon"
        assert "bedrooms_min" in described
        # Pagination is deliberately excluded from the task payload.
        assert "page" not in described


class TestIngestAuth:
    def setup_method(self) -> None:
        self._original = settings.ingest_token
        settings.ingest_token = "s3cret-token"

    def teardown_method(self) -> None:
        settings.ingest_token = self._original

    def test_accepts_correct_token(self) -> None:
        assert verify_token("Bearer s3cret-token") == "client"

    def test_scheme_is_case_insensitive(self) -> None:
        assert verify_token("bearer s3cret-token") == "client"

    def test_rejects_wrong_token(self) -> None:
        with pytest.raises(IngestError) as exc:
            verify_token("Bearer wrong")
        assert exc.value.status_code == 401

    def test_rejects_missing_header(self) -> None:
        with pytest.raises(IngestError) as exc:
            verify_token(None)
        assert exc.value.status_code == 401

    def test_rejects_raw_token_without_bearer(self) -> None:
        with pytest.raises(IngestError):
            verify_token("s3cret-token")

    def test_rejects_prefix_of_the_real_token(self) -> None:
        # Guards against a comparison that stops at the first difference.
        with pytest.raises(IngestError):
            verify_token("Bearer s3cret")

    def test_disabled_when_no_token_configured(self) -> None:
        """An unset token must disable ingest, never allow it — an open write
        endpoint is worse than no ingest at all."""
        settings.ingest_token = ""
        with pytest.raises(IngestError) as exc:
            verify_token("Bearer anything")
        assert exc.value.status_code == 503


class TestRateLimiter:
    def test_allows_under_the_limit(self) -> None:
        limiter = RateLimiter(per_minute=5)
        for _ in range(5):
            limiter.check("client-a")

    def test_blocks_over_the_limit(self) -> None:
        limiter = RateLimiter(per_minute=3)
        for _ in range(3):
            limiter.check("client-a")
        with pytest.raises(IngestError) as exc:
            limiter.check("client-a")
        assert exc.value.status_code == 429

    def test_limits_are_per_client(self) -> None:
        limiter = RateLimiter(per_minute=2)
        limiter.check("a")
        limiter.check("a")
        # A second client has its own budget.
        limiter.check("b")


class TestIngestValidation:
    """Payload rejection happens before any database work, so these need no
    database — which is also why they can live in the fast unit suite."""

    @pytest.fixture(autouse=True)
    def _token(self):
        original = settings.ingest_token
        settings.ingest_token = "t"
        yield
        settings.ingest_token = original

    async def test_rejects_unsupported_source(self) -> None:
        from homz.services.ingest import IngestService

        service = IngestService(db=None)  # type: ignore[arg-type]
        with pytest.raises(IngestError, match="unsupported source"):
            await service.ingest_page(source="craigslist", url="https://x.com/a",
                                      html="x" * 500)

    async def test_rejects_non_http_url(self) -> None:
        from homz.services.ingest import IngestService

        service = IngestService(db=None)  # type: ignore[arg-type]
        with pytest.raises(IngestError, match="absolute http"):
            await service.ingest_page(source="magicbricks",
                                      url="javascript:alert(1)", html="x" * 500)

    async def test_rejects_tiny_payload(self) -> None:
        from homz.services.ingest import IngestService

        service = IngestService(db=None)  # type: ignore[arg-type]
        with pytest.raises(IngestError, match="too small"):
            await service.ingest_page(source="magicbricks",
                                      url="https://x.com/a", html="hi")

    async def test_rejects_oversized_payload(self) -> None:
        from homz.services.ingest import IngestService

        service = IngestService(db=None)  # type: ignore[arg-type]
        oversized = "x" * (settings.ingest_max_payload_bytes + 1000)
        with pytest.raises(IngestError) as exc:
            await service.ingest_page(source="magicbricks",
                                      url="https://x.com/a", html=oversized)
        assert exc.value.status_code == 413

    async def test_rejects_too_many_records(self) -> None:
        from homz.services.ingest import IngestService

        service = IngestService(db=None)  # type: ignore[arg-type]
        with pytest.raises(IngestError) as exc:
            await service.ingest_records([{"record_type": "property"}] * 501)
        assert exc.value.status_code == 413
