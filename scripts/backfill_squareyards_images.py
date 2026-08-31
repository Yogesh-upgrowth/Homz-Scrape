"""One-off backfill: re-derive `images` for already-scraped SquareYards
projects/properties using the fixed domx.extract_images() filter.

Context: every SquareYards project page put its own sitewide logo and
amenity-icon sprites in plain <img> tags, on an allow-listed host with a real
image extension, so the old filter let them all through — every project's
images[0] ended up being the same squareyards.com logo. See domx.py's
_CHROME_PATH_RE fix. That fix only changes behavior for pages scraped *after*
it lands; this script repairs what's already in Mongo by replaying the
archived raw HTML (data/raw/squareyards/...) through the fixed parser — no
re-scraping, no network calls.

Updates only the `images` field, via a targeted $set, on both the source
`projects` doc and its mirrored `properties` doc (source_id=f"project:{id}"),
never touching enrichment fields or other scrape-derived fields.

Usage:
    python scripts/backfill_squareyards_images.py --dry-run   # report only
    python scripts/backfill_squareyards_images.py             # apply
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.common.rawstore import RawStore  # noqa: E402
from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.scrapers.squareyards import parser as sy  # noqa: E402


async def main(dry_run: bool) -> None:
    db = get_database()
    store = RawStore()

    cursor = db[D.PROJECTS].find(
        {"source": "squareyards", "raw_html_key": {"$ne": None}},
        projection={"_id": 1, "source_id": 1, "project_url": 1, "raw_html_key": 1, "images": 1},
    )
    # Per-doc raw HTML read + lxml reparse is slow enough across thousands of
    # projects to blow past Mongo's default 10-minute server-side cursor
    # timeout between `getMore`s (hit live: CursorNotFound) — and this Atlas
    # tier disallows `no_cursor_timeout=True` (also hit live). The projected
    # fields are small, so just materialize them all up front instead of
    # holding a cursor open across the slow part.
    docs = await cursor.to_list(length=None)

    total = 0
    changed = 0
    missing_html = 0
    parse_failed = 0
    properties_updated = 0

    for doc in docs:
        total += 1
        try:
            html = store.get_text(doc["raw_html_key"])
        except (OSError, MemoryError, UnicodeError) as exc:
            # Transient (seen: sporadic MemoryError under lxml's cumulative
            # memory use across thousands of parses in one process, not a
            # corrupt file — re-running this idempotent script later picks
            # these back up once they no longer hit it).
            parse_failed += 1
            print(f"FAIL  {doc['source_id']}: {type(exc).__name__} reading raw html")
            continue
        if not html:
            missing_html += 1
            print(f"SKIP  missing raw html: {doc['source_id']} ({doc['raw_html_key']})")
            continue

        try:
            parsed = sy.parse_project_detail(html, doc["project_url"], raw_html_key=doc["raw_html_key"])
        except Exception as exc:  # noqa: BLE001
            parse_failed += 1
            print(f"FAIL  {doc['source_id']}: {type(exc).__name__}: {exc}")
            continue
        if parsed is None:
            parse_failed += 1
            print(f"FAIL  {doc['source_id']}: parser returned None")
            continue

        new_images = [img.model_dump(mode="python") for img in parsed.images]
        old_urls = [i.get("url") for i in (doc.get("images") or [])]
        new_urls = [i["url"] for i in new_images]
        if new_urls == old_urls:
            continue

        changed += 1
        print(
            f"{'WOULD FIX' if dry_run else 'FIX'}  {doc['source_id']}: "
            f"{len(old_urls)} -> {len(new_urls)} images "
            f"(was: {old_urls[0] if old_urls else None})"
        )

        if dry_run:
            continue

        await db[D.PROJECTS].update_one({"_id": doc["_id"]}, {"$set": {"images": new_images}})
        result = await db[D.PROPERTIES].update_one(
            {"source": "squareyards", "source_id": f"project:{doc['source_id']}"},
            {"$set": {"images": new_images}},
        )
        if result.matched_count:
            properties_updated += 1

        if total % 200 == 0:
            gc.collect()
            print(f"... {total} scanned, {changed} changed so far")

    print(
        f"\ndone: {total} scanned, {changed} changed, {missing_html} missing raw html, "
        f"{parse_failed} parse failures, {properties_updated} mirrored property docs updated"
        f"{' (dry run — nothing written)' if dry_run else ''}"
    )
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
