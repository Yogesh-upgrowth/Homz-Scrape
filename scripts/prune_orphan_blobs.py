"""Delete Blob files no MongoDB record points at any more.

A repair run (`ingest_property_images.py --force`) re-processes a photo and
stores the result under a new content hash, because the bytes changed. The
file the old hash named is still there, and after `sync_blob_urls.py` has
repointed the read path, nothing references it. This finds those and, with
`--delete`, removes them.

Referenced means referenced by *anything* we serve from:

  * `property_images.images[].path`   -- the archive of what was processed
  * `properties.images[].blob_url`    -- what the listings feed reads
  * `projects.images[].blob_url`      -- what the projects feed reads
  * `settings.placeholder_path`       -- the "images coming soon" card,
                                         which no archive document mentions

Missing any one of those would delete a live image, so the set is built from
all of them and the run refuses to proceed if the archive comes back empty --
an empty reference set would otherwise mean "delete everything".

Deleting from Blob is irreversible: the bytes are gone and the only way back
is re-scraping the portal. So this is a dry run unless `--delete` is passed,
and it prints what it would remove either way.

Usage:
    python scripts/prune_orphan_blobs.py                # report only
    python scripts/prune_orphan_blobs.py --delete
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.images.blobstore import PUBLIC_BASE, BlobStore, url_for  # noqa: E402
from homz.settings import settings  # noqa: E402

COLLECTION = "property_images"
#: Below this the reference set is assumed broken rather than the store
#: assumed empty. 99,303 files were live when this was written.
MIN_EXPECTED_REFS = 10_000


def _as_path(value: str) -> str:
    return value[len(PUBLIC_BASE):] if value.startswith(PUBLIC_BASE) else value


async def referenced(db) -> set[str]:
    """Every blob path anything still points at."""
    keep: set[str] = set()

    async for d in db[COLLECTION].find({}, {"images.path": 1}):
        keep.update(i["path"] for i in d.get("images", []) if i.get("path"))
    print(f"  {len(keep):7d} from {COLLECTION}")

    before = len(keep)
    for coll in (D.PROPERTIES, D.PROJECTS):
        async for d in db[coll].find({"images.blob_url": {"$type": "string"}},
                                     {"images.blob_url": 1}):
            keep.update(_as_path(i["blob_url"]) for i in d.get("images", [])
                        if i.get("blob_url"))
        print(f"  {len(keep) - before:7d} more from {coll}")
        before = len(keep)

    if settings.placeholder_path:
        keep.add(_as_path(settings.placeholder_path))
        print("        1 placeholder")
    return keep


async def main(delete: bool, batch: int) -> None:
    db = get_database()
    print("building the reference set")
    keep = await referenced(db)
    await close_client()
    print(f"  {len(keep)} distinct paths referenced\n")

    if len(keep) < MIN_EXPECTED_REFS:
        print(f"REFUSING: only {len(keep)} references found, expected "
              f">{MIN_EXPECTED_REFS}. That looks like a failed read, not an "
              f"empty store, and acting on it would delete live images.")
        return

    blob = BlobStore()
    if not blob.enabled:
        print("no HOMZ_BLOB_READ_WRITE_TOKEN set")
        return

    seen = orphans = 0
    orphan_bytes = 0
    pending: list[str] = []
    deleted = 0
    async with httpx.AsyncClient(timeout=60) as client:
        async for b in blob.list_paths(client):
            seen += 1
            path = b.get("pathname") or ""
            if path in keep:
                continue
            orphans += 1
            orphan_bytes += int(b.get("size") or 0)
            if orphans <= 5:
                print(f"  orphan: {url_for(path)}")
            if delete:
                pending.append(b.get("url") or url_for(path))
                if len(pending) >= batch:
                    await blob.delete(client, pending)
                    deleted += len(pending)
                    pending = []
                    print(f"  deleted {deleted}")
            if seen % 20000 == 0:
                print(f"  scanned {seen} files, {orphans} orphaned")
        if delete and pending:
            await blob.delete(client, pending)
            deleted += len(pending)

    print(f"\nfiles in store : {seen}")
    print(f"referenced     : {seen - orphans}")
    print(f"orphaned       : {orphans}  ({orphan_bytes / 1024 / 1024:.0f} MB)")
    if delete:
        print(f"DELETED        : {deleted}")
    else:
        print("\nnothing deleted — re-run with --delete to remove them")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true",
                    help="actually remove them (irreversible)")
    ap.add_argument("--batch", type=int, default=200,
                    help="URLs per delete call")
    a = ap.parse_args()
    asyncio.run(main(a.delete, a.batch))
