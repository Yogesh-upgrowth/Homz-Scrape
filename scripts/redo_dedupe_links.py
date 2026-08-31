"""One-off backfill: re-validate every existing `property_duplicates` link
against the corrected `homz.common.dedupe.similarity()`.

Context: the old rule treated *any* single shared image URL as conclusive
proof of duplication (`score = max(score, 0.95)`). Verified live against
this warehouse: that one rule alone was behind 16,295 of 16,706 duplicate
links ever recorded (97.5%) — and confirmed real examples merged completely
unrelated properties (e.g. a "Gail CGHS" listing linked as a duplicate of an
unrelated "Huda CGHS" listing) that happened to share a recommendation-
carousel photo or a generic platform placeholder image, not a real photo of
either unit. `similarity()` now requires 2+ distinct shared images before
that signal counts, and `scripts/backfill_magicbricks_images.py` (run first)
strips the carousel/placeholder contamination out of MagicBricks' `images`
field at the source.

This script doesn't hunt for brand-new duplicate pairs — it only re-checks
links that already exist, dropping the ones that no longer qualify (a
duplicate_id's `canonical_id` gets unset, and its `property_duplicates` row
is removed) and refreshing the score/reason on the ones that still do.

Usage:
    python scripts/redo_dedupe_links.py --dry-run   # report only
    python scripts/redo_dedupe_links.py             # apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.common.dedupe import similarity  # noqa: E402
from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.services.listings_feed import record_from_doc  # noqa: E402

BATCH_SIZE = 500
PROGRESS_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoints" / (
    "redo_dedupe_links_progress.json"
)


def _load_last_id():
    if not PROGRESS_PATH.exists():
        return None
    return json.loads(PROGRESS_PATH.read_text(encoding="utf-8-sig")).get("last_id")


def _save_last_id(last_id) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps({"last_id": str(last_id)}), encoding="utf-8")


async def main(dry_run: bool) -> None:
    db = get_database()
    props = db[D.PROPERTIES]
    dupes = db[D.PROPERTY_DUPLICATES]

    last_id = _load_last_id()
    if last_id:
        from bson import ObjectId

        last_id = ObjectId(last_id)
        print(f"[resume] starting after _id={last_id}", flush=True)

    total = 0
    dropped = 0
    kept = 0
    missing_docs = 0
    unparseable = 0

    while True:
        query: dict = {}
        if last_id:
            query["_id"] = {"$gt": last_id}
        batch = await dupes.find(query).sort("_id", 1).limit(BATCH_SIZE).to_list(length=None)
        if not batch:
            break

        for link in batch:
            total += 1
            canonical_doc = await props.find_one({"_id": link["canonical_id"]})
            duplicate_doc = await props.find_one({"_id": link["duplicate_id"]})
            if canonical_doc is None or duplicate_doc is None:
                missing_docs += 1
                if not dry_run:
                    await dupes.delete_one({"_id": link["_id"]})
                continue

            canonical = record_from_doc(canonical_doc)
            duplicate = record_from_doc(duplicate_doc)
            if canonical is None or duplicate is None:
                unparseable += 1
                continue

            new_score, new_reason = similarity(canonical, duplicate)
            if new_score < 0.75:
                dropped += 1
                print(
                    f"DROP  {link['duplicate_id']} <- {link['canonical_id']} "
                    f"(was {link['score']:.2f} '{link['reason']}', now {new_score:.2f})"
                )
                if not dry_run:
                    await props.update_one(
                        {"_id": link["duplicate_id"]}, {"$unset": {"canonical_id": ""}}
                    )
                    await dupes.delete_one({"_id": link["_id"]})
            else:
                kept += 1
                if not dry_run and (new_score != link["score"] or new_reason != link["reason"]):
                    await dupes.update_one(
                        {"_id": link["_id"]},
                        {"$set": {"score": new_score, "reason": new_reason[:500]}},
                    )

        last_id = batch[-1]["_id"]
        if not dry_run:
            _save_last_id(last_id)
        print(f"... {total} checked, {kept} kept, {dropped} dropped so far")

    if not dry_run and dropped:
        # duplicate_count on a canonical is a running counter incremented at
        # link time — recompute it from what's actually still linked rather
        # than trying to decrement it exactly right per drop above.
        print("recomputing duplicate_count on affected canonicals...")
        async for row in props.aggregate(
            [
                {"$match": {"canonical_id": {"$ne": None}}},
                {"$group": {"_id": "$canonical_id", "count": {"$sum": 1}}},
            ]
        ):
            await props.update_one({"_id": row["_id"]}, {"$set": {"duplicate_count": row["count"]}})
        # Canonicals that no longer have any duplicate at all.
        await props.update_many(
            {"duplicate_count": {"$gt": 0}, "_id": {"$nin": await dupes.distinct("canonical_id")}},
            {"$set": {"duplicate_count": 0}},
        )

    print(
        f"\ndone: {total} links checked, {kept} kept, {dropped} dropped, "
        f"{missing_docs} pointed at missing docs, {unparseable} unparseable"
        f"{' (dry run — nothing written)' if dry_run else ''}"
    )
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
