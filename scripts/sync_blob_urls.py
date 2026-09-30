"""Point `properties.images[].blob_url` at the copies already on Blob.

`property_images` is the archive of what was processed; `properties` is what
the API reads. Ingestion writes both, but anything ingested before that
write-back existed — or any property whose source document was rewritten by a
later scrape — has images on Blob that nothing serves.

This reconciles the two from data already in MongoDB. No image is downloaded
and nothing is uploaded, so it is cheap to run as often as needed and safe to
run while ingestion is still going.

Matching is on the portal URL (`images[].source_url` in the archive against
`images[].url` in the source document), which is stable and is exactly the
key the ingest wrote.

`--projects` does the same for the `projects` catalogue, with one difference:
no archive document is keyed by a project id, because the ingest only ever
walked `properties`. But SquareYards serves the same project photography on
every unit inside that project, so most project images are already on Blob
under some listing's record. That pass therefore matches against *every*
processed URL rather than one document's, and links what is already there —
no download, no upload, no new archive documents.

Usage:
    python scripts/sync_blob_urls.py --dry-run
    python scripts/sync_blob_urls.py
    python scripts/sync_blob_urls.py --projects
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
from homz.images.blobstore import url_for  # noqa: E402
from homz.images.watermark import mark_survived  # noqa: E402

COLLECTION = "property_images"


async def sync_projects(db, dry_run: bool) -> None:
    """Link `projects.images[].blob_url` against every processed URL we hold.

    Projects that match nothing keep no blob_url and will render the
    "images coming soon" placeholder, which is the intended outcome: their
    only images are the portal's, watermark and all.
    """
    hosted: dict[str, str] = {}
    async for archive in db[COLLECTION].find(
            {}, {"images.source_url": 1, "images.path": 1,
                 "images.watermark_removed": 1}):
        source = str(archive["_id"]).split(":")[0]
        for img in archive.get("images", []):
            if (img.get("source_url") and img.get("path")
                    and not mark_survived(img, source)):
                hosted[img["source_url"]] = url_for(img["path"])
    print(f"{len(hosted)} distinct portal URLs already processed")

    scanned = updated = images_linked = 0
    async for proj in db[D.PROJECTS].find({}, {"images": 1}):
        scanned += 1
        patched, changed = [], 0
        for img in proj.get("images") or []:
            img = dict(img)
            blob_url = hosted.get(img.get("url"))
            if blob_url and img.get("blob_url") != blob_url:
                img["blob_url"] = blob_url
                changed += 1
            patched.append(img)
        if not changed:
            continue
        images_linked += changed
        updated += 1
        if not dry_run:
            await db[D.PROJECTS].update_one(
                {"_id": proj["_id"]}, {"$set": {"images": patched}})

    total = await db[D.PROJECTS].count_documents({"images.blob_url": {"$type": "string"}})
    print("")
    print(f"scanned {scanned} projects")
    print(f"  linked  : {updated} projects, {images_linked} images"
          f"{' (dry run - nothing written)' if dry_run else ''}")
    print(f"projects now serving Blob URLs: {total} "
          f"({scanned - total} will show the placeholder)")


async def main(dry_run: bool, limit: int | None, projects: bool = False) -> None:
    db = get_database()
    if projects:
        await sync_projects(db, dry_run)
        await close_client()
        return

    scanned = updated = images_linked = skipped = 0

    cursor = db[COLLECTION].find(
        {}, {"images.source_url": 1, "images.path": 1, "images.watermark_removed": 1})
    if limit:
        cursor = cursor.limit(limit)

    async for archive in cursor:
        scanned += 1
        # An image whose watermark survived is deliberately not served (see
        # scripts/suppress_marked_images.py). Linking it here would hand it
        # straight back, so this pass has to honour the same rule -- otherwise
        # every sync would silently undo the suppression.
        source = str(archive["_id"]).split(":")[0]
        hosted = {
            i["source_url"]: url_for(i["path"])
            for i in archive.get("images", [])
            if i.get("source_url") and i.get("path") and not mark_survived(i, source)
        }
        if not hosted:
            skipped += 1
            continue

        prop = await db[D.PROPERTIES].find_one({"_id": archive["_id"]}, {"images": 1})
        if not prop:
            skipped += 1
            continue

        patched, changed = [], 0
        for img in prop.get("images") or []:
            img = dict(img)
            blob_url = hosted.get(img.get("url"))
            if blob_url and img.get("blob_url") != blob_url:
                img["blob_url"] = blob_url
                changed += 1
            patched.append(img)

        if not changed:
            continue
        images_linked += changed
        updated += 1
        if not dry_run:
            await db[D.PROPERTIES].update_one(
                {"_id": archive["_id"]}, {"$set": {"images": patched}})
        if updated % 200 == 0:
            print(f"  {updated} properties linked ({images_linked} images)")

    total = await db[D.PROPERTIES].count_documents(
        {"images.blob_url": {"$type": "string"}})
    await close_client()

    print(f"\nscanned {scanned} archived properties")
    print(f"  linked  : {updated} properties, {images_linked} images"
          f"{' (dry run — nothing written)' if dry_run else ''}")
    print(f"  skipped : {skipped} (no archive images, or source document gone)")
    print(f"properties now serving Blob URLs: {total}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--projects", action="store_true",
                    help="link the projects catalogue instead of properties")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run, args.limit, args.projects))
