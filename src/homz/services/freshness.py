"""Freshness lifecycle for published records.

The warehouse already tracks `first_seen_at` / `last_seen_at` / `is_active` /
`delisted_at` per listing. This module turns them into the three additive feed
fields (`firstSeenAt`, `lastSeenAt`, `removedAt`) and holds the two safeguards
that stop a bad crawl from emptying the site:

* `delist_skip_reason` — refuse to delist when too little of the active set was
  seen recently (a blocked, failed or never-run crawl).
* `segment_regressions` — refuse to overwrite the published feed when a segment
  would shrink below a fraction of what is already published.

Pure functions only, so both rules are unit-testable without Mongo.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Removed listings stay in the feed (with `removedAt` set) this long, then drop.
REMOVED_RETENTION_DAYS = 30

#: A run that saw less than this share of the active set is not trusted to
#: declare anything removed, nor to replace the published feed.
MIN_COVERAGE = 0.8

#: Segments smaller than this are too small for a ratio to mean anything.
_MIN_SEGMENT_FOR_RATIO = 50

_BAD_RUN_STATUSES = frozenset({"blocked", "failed"})
_TOTAL_RE = re.compile(rb'"total"\s*:\s*(\d+)')


def as_utc(value: Any) -> datetime | None:
    """Mongo hands back naive UTC datetimes unless the client is tz-aware."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def iso_date(value: Any) -> str | None:
    dt = as_utc(value)
    return dt.date().isoformat() if dt else None


def removed_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=REMOVED_RETENTION_DAYS)


def lifecycle_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """The warehouse-side facts the feed needs, lifted off a Mongo document.

    `removed_at` is only set for an inactive document. A legacy inactive row
    with no `delisted_at` falls back to its last sighting, which keeps it
    inside the 30-day window only if it was seen recently.
    """
    inactive = doc.get("is_active") is False
    removed = None
    if inactive:
        removed = doc.get("delisted_at") or doc.get("last_seen_at") or doc.get("scraped_at")
    return {
        "first_seen_at": doc.get("first_seen_at") or doc.get("created_at"),
        "last_seen_at": doc.get("last_seen_at"),
        "removed_at": removed,
    }


def feed_lifecycle(lifecycle: dict[str, Any], scraped_at: Any) -> dict[str, Any]:
    """`firstSeenAt`, `lastSeenAt`, `removedAt` and the `updatedAt` they imply.

    `updatedAt` is the later of the last detail scrape and the last sighting:
    a listing re-confirmed live on the portal today is fresh today, even if its
    content did not change. Dates (not datetimes) for the three new fields, so
    repeated exports within one day produce an identical file.
    """
    scraped = as_utc(scraped_at)
    last_seen = as_utc(lifecycle.get("last_seen_at"))
    first_seen = as_utc(lifecycle.get("first_seen_at"))
    newest = max((d for d in (scraped, last_seen) if d), default=None)
    return {
        "firstSeenAt": iso_date(first_seen or scraped),
        "lastSeenAt": iso_date(last_seen or scraped),
        "removedAt": iso_date(lifecycle.get("removed_at")),
        "updatedAt": newest.isoformat() if newest else None,
    }


def delist_skip_reason(
    *,
    active_total: int,
    fresh: int,
    latest_run_status: str | None,
    min_coverage: float = MIN_COVERAGE,
) -> str | None:
    """Why delisting must not run for a source, or None if it may.

    `fresh` is how many active listings were seen inside the stale window. If
    that is under `min_coverage` of the active set the crawl is not healthy
    (blocked, failed, or simply not run), and flipping everything else to
    inactive would empty the published feed.
    """
    if latest_run_status in _BAD_RUN_STATUSES:
        return f"latest scrape run was {latest_run_status}"
    if active_total <= 0:
        return None
    coverage = fresh / active_total
    if coverage < min_coverage:
        return (
            f"only {fresh}/{active_total} active records ({coverage:.0%}) were seen "
            f"in the stale window; need {min_coverage:.0%}"
        )
    return None


def read_segment_total(path: Path) -> int | None:
    """`total` from an exported segment file without parsing 80 MB of JSON.

    The envelope puts `total` before `results`, so it is in the first bytes.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(2048)
    except OSError:
        return None
    match = _TOTAL_RE.search(head)
    return int(match.group(1)) if match else None


def segment_regressions(
    previous: dict[str, int | None],
    new: dict[str, int],
    *,
    min_ratio: float = MIN_COVERAGE,
) -> list[str]:
    """Segments whose new row count fell below `min_ratio` of the published one."""
    problems: list[str] = []
    for segment, before in previous.items():
        if before is None or before < _MIN_SEGMENT_FOR_RATIO:
            continue
        after = new.get(segment, 0)
        if after < before * min_ratio:
            problems.append(f"{segment}: {before} -> {after} ({after / before:.0%} of published)")
    return problems
