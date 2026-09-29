"""Shrink the image records so the backfill fits the Atlas quota.

Atlas enforces its cap on `dataSize + indexSize` — the *uncompressed* logical
size — not the compressed bytes on disk. At 510 MB of 512 MB writes are
blocked, and the remaining 16,832 properties need roughly 100 MB more than
exists. These records carry that much in redundancy:

* **`sha256` is the blob filename.** The URL ends in `<sha256>.webp`, so the
  field restates 64 characters already present. Dropped; derive it from the
  path when needed.
* **`content_type` is always `image/webp`.** The pipeline emits nothing else.
  Dropped; assume it.
* **The URL repeats its host 180,000 times.** Only the pathname varies, so
  store that and rebuild the URL from one constant base.
* **`blob_url` on `properties` duplicates the mapping** `property_images`
  already holds, at ~1.9 KB per property. Removed; the read path joins
  instead (see `homz.services.feed`).

Together these reclaim ~55 MB and cut per-property cost from 6.1 KB to about
2.5 KB, which is what makes the rest of the corpus fit.

Usage:
    python scripts/compact_image_records.py --dry-run
    python scripts/compact_image_records.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402

COLLECTION = "property_images"
BLOB_BASE = "https://gnunxcv3vxg0q9wy.public.blob.vercel-storage.com/"


async def _stats(db) -> float:
    st = await db.command("dbStats", scale=1024 * 1024)
    return st.get("dataSize", 0) + st.get("indexSize", 0)


async def main(dry_run: bool, drop_source_url: bool = False) -> None:
    db = get_database()
    before = await _stats(db)
    print(f"before: {before:.0f} MB of 512\n")

    # --- 1. drop blob_url from properties -------------------------------
    n = await db[D.PROPERTIES].count_documents({"images.blob_url": {"$type": "string"}})
    print(f"1. removing blob_url from {n} properties "
          f"(duplicates what {COLLECTION} already stores)")
    if not dry_run and n:
        await db[D.PROPERTIES].update_many(
            {"images.blob_url": {"$type": "string"}},
            {"$unset": {"images.$[].blob_url": ""}})
        print(f"   done — {await _stats(db):.0f} MB")

    # --- 1b. optional: drop source_url ----------------------------------
    # ~168 B per image record, and it duplicates properties.images[].url.
    # Kept by default because the dedupe memo uses it to skip re-downloading
    # a photo shared between properties — without it a resumed run re-fetches
    # every shared SquareYards project shot. Only worth dropping when the
    # quota is the binding constraint and the corpus is essentially done.
    if drop_source_url:
        n = await db[COLLECTION].count_documents({"images.source_url": {"$exists": True}})
        print(f"\n1b. dropping source_url from {n} archive documents "
              f"(~35 MB; disables download-skip dedupe on future runs)")
        if not dry_run and n:
            await db[COLLECTION].update_many(
                {"images.source_url": {"$exists": True}},
                {"$unset": {"images.$[].source_url": ""}})
            print(f"    done — {await _stats(db):.0f} MB")

    # --- 2. slim each image record --------------------------------------
    total = await db[COLLECTION].count_documents({})
    print(f"\n2. slimming image records on {total} properties")
    done = changed = 0
    async for doc in db[COLLECTION].find({}, {"images": 1}):
        done += 1
        images, touched = [], False
        for img in doc.get("images", []):
            out = dict(img)
            url = out.get("url") or ""
            if url.startswith(BLOB_BASE):
                out["path"] = url[len(BLOB_BASE):]
                out.pop("url", None)
                touched = True
            for field in ("sha256", "content_type"):
                if field in out:
                    out.pop(field)
                    touched = True
            images.append(out)
        if touched:
            changed += 1
            if not dry_run:
                await db[COLLECTION].update_one(
                    {"_id": doc["_id"]}, {"$set": {"images": images}})
        if done % 2000 == 0:
            print(f"   {done}/{total} scanned, {changed} rewritten "
                  f"({await _stats(db):.0f} MB)")

    after = await _stats(db)
    print(f"\nafter : {after:.0f} MB  (reclaimed {before - after:.0f} MB)"
          f"{'  [dry run — nothing written]' if dry_run else ''}")
    print(f"headroom: {512 - after:.0f} MB")
    remaining = 30814 - total
    if after < 512 and remaining > 0:
        per = after and (await db.command("collStats", COLLECTION,
                                          scale=1024))["size"] / max(total, 1)
        print(f"remaining {remaining} properties need ~{per * remaining / 1024:.0f} MB "
              f"at {per:.1f} KB each")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--drop-source-url", action="store_true",
                    help="also drop source_url (~35 MB) — last-resort space")
    a = ap.parse_args()
    asyncio.run(main(a.dry_run, a.drop_source_url))
