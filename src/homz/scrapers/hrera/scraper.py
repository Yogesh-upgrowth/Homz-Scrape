"""Fetches the Haryana RERA Gurugram registered-projects registry and upserts
it into the `hrera_registry` collection.

The whole ~1,000-row table comes back in a single response (no server-side
pagination — see `parser.py`), so this is one polite request, not a crawl.
Still goes through `common.http.Fetcher` for the same robots-gate, rate-limit,
retry and block-detection machinery every other source gets (COMPLIANCE.md).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import UpdateOne

from homz.common.http import Fetcher
from homz.db import documents as D
from homz.logging_setup import get_logger
from homz.scrapers.hrera.parser import parse_registered_projects_page

log = get_logger(__name__)

REGISTERED_PROJECTS_URL = "https://haryanarera.gov.in/admincontrol/registered_projects/2"


async def fetch_registry_rows() -> list[dict[str, Any]]:
    async with Fetcher(source="hrera") as fetcher:
        result = await fetcher.get(REGISTERED_PROJECTS_URL)
    rows = parse_registered_projects_page(result.text)
    log.info("hrera.fetched", url=REGISTERED_PROJECTS_URL, rows=len(rows))
    return rows


async def sync_registry(db: Any) -> dict[str, int]:
    """Fetch the live registry and upsert every row into `hrera_registry`.

    Keyed on `rera_number` (unique per project) so re-runs update rather than
    duplicate, and a project whose registration details changed (e.g. a new
    `registration_upto` after renewal) is reflected on the next sync.
    """
    rows = await fetch_registry_rows()
    if not rows:
        return {"fetched": 0, "upserted": 0}

    now = datetime.now(UTC)
    operations = [
        UpdateOne(
            {"_id": row["rera_number"]},
            {"$set": {**row, "scraped_at": now}},
            upsert=True,
        )
        for row in rows
    ]

    for start in range(0, len(operations), 500):
        await db[D.HRERA_REGISTRY].bulk_write(operations[start : start + 500], ordered=False)

    log.info("hrera.synced", fetched=len(rows))
    return {"fetched": len(rows), "upserted": len(rows)}
