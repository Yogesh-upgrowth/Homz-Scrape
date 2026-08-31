"""One-off backfill: re-derive `images` for already-scraped MagicBricks
properties using the fixed gallery selector + crop upscaling.

Context: MagicBricks renamed its detail-page gallery markup from
`mb-ldp__gallery` to `mb-ldp__premium-dtls__photo*` at some point, so that
selector (and its `[class*='gallery']` fallback) silently matched nothing
and every image fell through to a bare "img" sweep. Separately, the
gallery's secondary photos reference a small `Photo_h300_w450` crop while
the exact same photo id is also reachable at `Photo_h600_w900` (verified
live: a real, ~3.5x larger file, not just recompression) — see
`parser._extract_property_images`/`_upscale_photo`. That fix only changes
behavior for pages scraped *after* it lands; this script repairs what's
already in Mongo by replaying the archived raw HTML through the fixed
parser — no re-scraping, no network calls.

Updates only the `images` field via a targeted $set. MagicBricks project
pages map onto the same PropertyRecord shape (no separate `projects`
collection entries for this source), so only `properties` needs touching.

Fetches and checkpoints in small `_id`-ordered batches rather than
materializing the whole ~21k-doc result set in one shot. A single
long-lived cursor over that many docs turned out to be fragile against
this environment's intermittent network stalls — one bad moment loses the
entire run's progress. Small batches mean a stall only costs one batch: on
restart, `data/checkpoints/magicbricks_image_backfill_progress.json` picks
up right after the last `_id` actually completed, no work re-done.

Usage:
    python scripts/backfill_magicbricks_images.py --dry-run   # report only
    python scripts/backfill_magicbricks_images.py             # apply
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.common.rawstore import RawStore  # noqa: E402
from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.scrapers.magicbricks import parser as mb  # noqa: E402

BATCH_SIZE = 500
PROGRESS_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoints" / (
    "magicbricks_image_backfill_progress.json"
)


def _load_last_id() -> str | None:
    # `_id` on this collection is a custom string key (e.g.
    # "magicbricks:<source_id>"), not a BSON ObjectId — compare/store it as
    # a plain string.
    if not PROGRESS_PATH.exists():
        return None
    return json.loads(PROGRESS_PATH.read_text(encoding="utf-8-sig")).get("last_id")


def _save_last_id(last_id: str) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps({"last_id": last_id}), encoding="utf-8")


async def main(dry_run: bool) -> None:
    db = get_database()
    store = RawStore()

    last_id = _load_last_id()
    if last_id:
        print(f"[resume] starting after _id={last_id}", flush=True)

    total = 0
    changed = 0
    missing_html = 0
    parse_failed = 0

    while True:
        query: dict = {"source": "magicbricks", "raw_html_key": {"$ne": None}}
        if last_id:
            query["_id"] = {"$gt": last_id}
        batch = await (
            db[D.PROPERTIES]
            .find(
                query,
                projection={"_id": 1, "source_id": 1, "listing_url": 1, "raw_html_key": 1, "images": 1},
            )
            .sort("_id", 1)
            .limit(BATCH_SIZE)
            .to_list(length=None)
        )
        if not batch:
            break

        for doc in batch:
            total += 1
            try:
                html = store.get_text(doc["raw_html_key"])
            except (OSError, MemoryError, UnicodeError) as exc:
                parse_failed += 1
                print(f"FAIL  {doc['source_id']}: {type(exc).__name__} reading raw html")
                continue
            if not html:
                missing_html += 1
                print(f"SKIP  missing raw html: {doc['source_id']} ({doc['raw_html_key']})")
                continue

            try:
                parsed = mb.parse_property_detail(
                    html, doc["listing_url"], raw_html_key=doc["raw_html_key"]
                )
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
            if new_urls != old_urls:
                changed += 1
                print(
                    f"{'WOULD FIX' if dry_run else 'FIX'}  {doc['source_id']}: "
                    f"{len(old_urls)} -> {len(new_urls)} images "
                    f"(was: {old_urls[0] if old_urls else None})"
                )
                if not dry_run:
                    await db[D.PROPERTIES].update_one(
                        {"_id": doc["_id"]}, {"$set": {"images": new_images}}
                    )

        last_id = batch[-1]["_id"]
        if not dry_run:
            _save_last_id(last_id)
        gc.collect()
        print(f"... {total} scanned, {changed} changed so far (through _id={last_id})")

    print(
        f"\ndone: {total} scanned, {changed} changed, {missing_html} missing raw html, "
        f"{parse_failed} parse failures"
        f"{' (dry run — nothing written)' if dry_run else ''}"
    )
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
