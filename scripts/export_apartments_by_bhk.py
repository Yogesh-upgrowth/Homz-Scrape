"""Export every apartment listing as one JSON file, grouped by BHK/RK configuration.

Scope:

- `property_type == "apartment"`
- `is_active == True` (excludes delisted/stale listings)
- `listing_type in (sale, resale, rent)` — excludes builder-*project*
  overview rows, which carry multiple unit configurations rather than one
  clean bedroom count

Deliberately includes records with `canonical_id` set (i.e. already linked as
a duplicate of another listing) — unlike the search API and the pre-built
frontend feed (`search/query.py`, `services/listings_feed.py`), which filter
those out. This export is meant to hold every scraped apartment record.

`bedrooms` alone can't tell a "1 RK" from a "1 BHK" — `parse_bedrooms()`
(homz/common/parsing.py) extracts the same integer for both, the label only
survives in the `configuration` string (e.g. "1 RK" vs "1 BHK"). So each
bedroom count is split into its own bhk/rk bucket by matching `configuration`
against `\brk\b`, rather than bucketing on `bedrooms` alone.

Output shape is a flat object keyed by bucket, e.g.:

    {"1 bhk": [...], "1 rk": [...], "2 bhk": [...], "2 rk": [...], ..., "unknown": [...]}

Full raw Mongo documents are written (not a curated subset), which makes the
file large (~200MB+) — so it's streamed straight to disk one document at a
time rather than built as a Python list and dumped in one `json.dumps()`
call, keeping peak memory to roughly one document at a time regardless of
total size.

Usage:
    python scripts/export_apartments_by_bhk.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "exports" / "apartments_by_bhk.json"
LOG_EVERY = 2000

BASE_FILTER = {
    "property_type": "apartment",
    "is_active": True,
    "listing_type": {"$in": ["sale", "resale", "rent"]},
}

# Order controls the order buckets are written in; "unknown" (bedrooms is
# null or outside this range) always goes last.
BUCKET_BEDROOMS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

# "1 RK" and "1 BHK" both parse to bedrooms=1 — this is what tells them apart.
RK_MATCH = {"configuration": {"$regex": r"\brk\b", "$options": "i"}}
NOT_RK_MATCH = {"configuration": {"$not": {"$regex": r"\brk\b", "$options": "i"}}}


def _json_default(value):
    """Mirrors `homz.services.feed._default` — inlined since this is a
    standalone script, same as every other file in `scripts/`."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


async def _write_bucket(fh, coll, mongo_filter, label: str) -> int:
    count = 0
    cursor = coll.find(mongo_filter)
    async for doc in cursor:
        if count:
            fh.write(",")
        fh.write(json.dumps(doc, default=_json_default))
        count += 1
        if count % LOG_EVERY == 0:
            print(f"  {label}: {count} written so far...")
    return count


async def main() -> None:
    started = time.monotonic()
    db = get_database()
    coll = db[D.PROPERTIES]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    total = await coll.count_documents(BASE_FILTER)
    print(f"Exporting {total} apartments (deduped, live, individual listings) -> {OUTPUT_PATH}")

    summary: dict[str, int] = {}

    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        fh.write("{")
        first_bucket = True

        for n in BUCKET_BEDROOMS:
            for suffix, extra_filter in (("bhk", NOT_RK_MATCH), ("rk", RK_MATCH)):
                label = f"{n} {suffix}"
                if not first_bucket:
                    fh.write(",")
                first_bucket = False
                fh.write(json.dumps(label) + ":[")
                count = await _write_bucket(
                    fh, coll, {**BASE_FILTER, "bedrooms": n, **extra_filter}, label
                )
                fh.write("]")
                summary[label] = count
                print(f"{label}: {count}")

        # Anything not already emitted (null bedrooms, or a value outside
        # BUCKET_BEDROOMS) falls into "unknown".
        if not first_bucket:
            fh.write(",")
        fh.write('"unknown":[')
        count = await _write_bucket(
            fh, coll, {**BASE_FILTER, "bedrooms": {"$nin": BUCKET_BEDROOMS}}, "unknown"
        )
        fh.write("]")
        summary["unknown"] = count
        print(f"unknown: {count}")

        fh.write("}")

    elapsed = time.monotonic() - started
    size_mb = OUTPUT_PATH.stat().st_size / 1_000_000
    grand_total = sum(summary.values())

    print("--- summary ---")
    for label, count in summary.items():
        print(f"  {label}: {count}")
    print(f"  TOTAL: {grand_total} (expected {total})")
    print(f"  file size: {size_mb:.1f} MB")
    print(f"  elapsed: {elapsed:.1f}s")

    await close_client()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
