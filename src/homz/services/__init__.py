"""Application services that sit above the repository.

`ondemand` turns a search miss into a queued scrape task; `ingest` accepts the
scraped result back from a client. Together they close the
search → miss → scrape → store → hit loop.
"""

from homz.services.ingest import (
    IngestError,
    IngestResult,
    IngestService,
    check_rate_limit,
    verify_token,
)
from homz.services.ondemand import DemandFiller, FillDecision, query_fingerprint

__all__ = [
    "DemandFiller",
    "FillDecision",
    "IngestError",
    "IngestResult",
    "IngestService",
    "check_rate_limit",
    "query_fingerprint",
    "verify_token",
]
