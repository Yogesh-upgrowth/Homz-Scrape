"""Stop serving images whose portal watermark could not be removed.

Some images still carry the portal's wordmark: the de-watermarker either
could not find it or could not remove it confidently. We composite our own
mark on every stored image regardless, so those frames reach the site showing
*both* -- the portal's brand under ours, which is worse than showing no
photograph at all.

This unsets `blob_url` on those images. It does not delete anything:

* `images[].url` keeps the portal original, so provenance survives and a
  re-ingest stays idempotent.
* `property_images` keeps the full archive record, including the path, so the
  file is still on Blob and still deduplicated.

The read path already does the rest. `homz.services.feed._public_url` serves
`blob_url` and nothing else, so an image without one is simply omitted; if a
listing loses all of them it falls back to the "images coming soon"
placeholder and sorts to the end of its segment, exactly as an unprocessed
listing does.

Reversible by construction: fix the removal, re-ingest those images, and the
ingest writes `blob_url` back. `sync_blob_urls.py` will not undo it, because
it consults the same `mark_survived()` predicate.

Usage:
    python scripts/suppress_marked_images.py --dry-run
    python scripts/suppress_marked_images.py
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.images.blobstore import url_for  # noqa: E402
from homz.images.watermark import mark_survived  # noqa: E402

COLLECTION = "property_images"


async def _suppress_projects(db, dry_run: bool) -> None:
    """The same for the projects catalogue.

    No archive document is keyed by a project id -- the ingest only ever
    walked `properties` -- so the set of images to stop serving is built from
    every archived record and matched by URL, the way `sync_blob_urls.py
    --projects` links them in the first place. Missing this pass left the
    project pages serving the very renders that prompted the report, since
    one SquareYards project photo is reused across every unit in it.
    """
    bad: set[str] = set()
    async for arch in db[COLLECTION].find({}, {"images": 1}):
        source = str(arch["_id"]).split(":")[0]
        bad.update(url_for(i["path"]) for i in arch.get("images", [])
                   if i.get("path") and mark_survived(i, source))
    print("")
    print(f"{len(bad)} distinct still-marked files to withhold from projects")

    touched = images = lost_all = 0
    async for proj in db[D.PROJECTS].find(
            {"images.blob_url": {"$type": "string"}}, {"images": 1}):
        patched, dropped = [], 0
        for img in proj.get("images") or []:
            img = dict(img)
            if img.get("blob_url") in bad:
                img.pop("blob_url", None)
                dropped += 1
            patched.append(img)
        if not dropped:
            continue
        touched += 1
        images += dropped
        if not any(i.get("blob_url") for i in patched):
            lost_all += 1
        if not dry_run:
            await db[D.PROJECTS].update_one(
                {"_id": proj["_id"]}, {"$set": {"images": patched}})
    print(f"  projects changed : {touched}")
    print(f"  images suppressed: {images}")
    print(f"  now on placeholder: {lost_all}")


async def main(dry_run: bool) -> None:
    db = get_database()
    by_source = collections.Counter()
    scanned = touched = images = lost_all = 0

    async for arch in db[COLLECTION].find({}, {"images": 1}):
        scanned += 1
        source = str(arch["_id"]).split(":")[0]
        bad = {url_for(i["path"]) for i in arch.get("images", [])
               if i.get("path") and mark_survived(i, source)}
        if not bad:
            continue

        prop = await db[D.PROPERTIES].find_one({"_id": arch["_id"]}, {"images": 1})
        if not prop:
            continue

        patched, dropped = [], 0
        for img in prop.get("images") or []:
            img = dict(img)
            if img.get("blob_url") in bad:
                img.pop("blob_url", None)
                dropped += 1
            patched.append(img)
        if not dropped:
            continue

        touched += 1
        images += dropped
        by_source[source] += dropped
        if not any(i.get("blob_url") for i in patched):
            lost_all += 1
        if not dry_run:
            await db[D.PROPERTIES].update_one(
                {"_id": arch["_id"]}, {"$set": {"images": patched}})
        if touched % 2000 == 0:
            print(f"  {touched} listings, {images} images")

    await _suppress_projects(db, dry_run)

    still = await db[D.PROPERTIES].count_documents(
        {"images.blob_url": {"$type": "string"}})
    st = await db.command("dbStats", scale=1024 * 1024)
    await close_client()

    print(f"\nscanned {scanned} archived listings")
    print(f"  listings changed : {touched}")
    print(f"  images suppressed: {images}  {dict(by_source)}")
    print(f"  now on placeholder (lost every image): {lost_all}")
    print(f"listings still serving our images: {still}")
    print(f"MongoDB: {st['dataSize'] + st['indexSize']:.2f} MB of 512")
    if dry_run:
        print("\ndry run - nothing written")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
