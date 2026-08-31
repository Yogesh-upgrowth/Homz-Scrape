"""Haryana RERA public registry — reference data, not a listing source.

See COMPLIANCE.md §2 "Recommended additional sources (open by design)": HARERA
is government-published with no anti-scraping posture, and is the authoritative
source for `rera_number` that the other portals only echo (and sometimes get
wrong). This module scrapes `haryanarera.gov.in`'s registered-projects table
into the `hrera_registry` collection; `homz.enrichment.rera_matching` then
fuzzy-matches it against existing `properties`/`projects` documents.

This does not subclass `BaseScraper`: that contract is discover() yielding
listing URLs -> parse_detail() yielding one `ScrapedRecord` per page. HRERA has
no per-project detail URL to discover — it's one paginated reference table, and
its output is a lookup row, not a `PropertyRecord`/`ProjectRecord`. It still
goes through `common.http.Fetcher` for the same robots/rate-limit/retry/block
handling every other source gets.
"""
