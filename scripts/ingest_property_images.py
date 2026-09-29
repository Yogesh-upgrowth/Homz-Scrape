"""Fetch every property's photos, strip the portal watermark, brand, store.

Images only. Listing text is left alone — this walks the properties already
in Mongo, takes the image URLs each one already carries, and replaces the
pictures. Nothing is re-scraped from the portals' HTML, so the run costs one
request per *image* rather than one per listing plus its images.

Per image: download, remove the portal's watermark, apply EXIF orientation,
resize to `image_max_edge`, composite the homzrealtor mark, re-encode to
WebP, and write the bytes into MongoDB.

## Where the bytes go

Processed images are uploaded to Vercel Blob and MongoDB stores their URLs.
Storing the bytes as BSON was measured at ~40 GB for the full corpus, which
needs an Atlas M30 at roughly $390/month; the same images on Blob cost cents
and leave the documents small enough for the free tier. It is also what the
frontend expects — `lib/listings/media.ts` resolves a listing's gallery from
a manifest of URLs.

Blob paths are the SHA-256 of the processed bytes, so a photo shared between
properties uploads once. SquareYards reuses project photos across every unit:
450,465 references collapse to 128,228 uploads.

Set `HOMZ_BLOB_READ_WRITE_TOKEN` to enable this. Without it the script falls
back to embedding bytes in MongoDB, which is fine for a small test batch and
will exhaust a free tier on anything larger.

## Resumability

Progress is an `_id` watermark per source in `data/checkpoints/`, and
properties are walked in `_id` order. Already-ingested properties are skipped
unless `--force`, so an interrupted run resumes where it stopped and a repeat
run costs nothing.

Usage:
    python scripts/ingest_property_images.py --limit 100          # both sources
    python scripts/ingest_property_images.py --source squareyards --limit 50
    python scripts/ingest_property_images.py --reset --limit 100  # start over
    python scripts/ingest_property_images.py                      # everything
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402
from bson import Binary  # noqa: E402

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.images.blobstore import BlobStore, url_for  # noqa: E402
from homz.images.ingest import _process_bytes  # noqa: E402
from homz.images.urls import is_photo  # noqa: E402
from homz.images.watermark import calibrated_sizes, has_calibration  # noqa: E402
from homz.settings import settings  # noqa: E402

COLLECTION = "property_images"
CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "data" / "checkpoints"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REFERERS = {
    "magicbricks": "https://www.magicbricks.com/",
    "squareyards": "https://www.squareyards.com/",
}
SOURCES = ("magicbricks", "squareyards")

#: Leave headroom under BSON's 16 MB document ceiling for the metadata and
#: for Mongo's own overhead. Images past this are recorded by URL but not
#: stored, which is visible in the report rather than silent.
MAX_DOC_BYTES = 12 * 1024 * 1024
#: Images fetched concurrently within one property.
PER_PROPERTY_CONCURRENCY = 6
#: Atlas enforces its cap on dataSize + indexSize — the *uncompressed* logical
#: size, not the compressed bytes on disk. Hitting it does not fail gracefully:
#: every write is refused and the run spins, erroring per property, until it is
#: stopped by hand. So check it as we go and finish cleanly with room to spare.
ATLAS_CAP_MB = 512


async def _quota_free_mb(db) -> float:
    st = await db.command("dbStats", scale=1024 * 1024)
    return ATLAS_CAP_MB - (st.get("dataSize", 0) + st.get("indexSize", 0))

BASE_FILTER = {
    "property_type": "apartment",
    "is_active": True,
    "listing_type": {"$in": ["sale", "resale", "rent"]},
}


def _checkpoint(source: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"property_images_{source}.json"


def _load_last_id(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")).get("last_id")
    except (OSError, ValueError):
        return None


def _save_last_id(path: Path, last_id: str) -> None:
    path.write_text(json.dumps({"last_id": last_id}), encoding="utf-8")


def _property_name(doc: dict) -> str:
    for key in ("project_name", "society_name", "title"):
        if doc.get(key):
            return str(doc[key])
    return str(doc["_id"])


def _location(doc: dict) -> str:
    parts = [doc.get("sector"), doc.get("locality"), doc.get("city")]
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(str(p))
    return " / ".join(out)


class Stats:
    def __init__(self) -> None:
        self.properties = 0
        self.skipped = 0
        self.images_seen = 0
        self.stored = 0
        self.dewatermarked = 0
        self.no_mark = 0
        self.junk = 0
        self.failed = 0
        self.deduped = 0
        self.dropped_oversize = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.started = time.monotonic()
        #: url -> reason, so a retry pass has something to work from. A run
        #: that only counts failures cannot fix them.
        self.failures: dict[str, str] = {}
        #: Set when the run stopped itself at the quota floor rather than
        #: finishing its limit.
        self.quota_stop = False

    def note_failure(self, url: str, reason: str) -> None:
        if len(self.failures) < 20000:
            self.failures[url] = reason

    def line(self, total: int) -> str:
        el = (time.monotonic() - self.started) / 60
        rate = self.properties / max(el, 1e-9)
        return (f"[{self.properties}/{total}] stored={self.stored} dedup={self.deduped} "
                f"dewm={self.dewatermarked} nomark={self.no_mark} junk={self.junk} "
                f"fail={self.failed} {self.bytes_out / 1e6:.0f}MB "
                f"| {el:.1f}m ({rate:.1f} props/min)")


async def _fetch_one(client, url: str, source: str, idx: int, stats: Stats, sem,
                     blob: BlobStore | None = None):
    """Download and process one image. Returns the Mongo sub-document or None."""
    if not is_photo(url):
        stats.junk += 1
        return None

    # Already processed this exact photo? Reuse it without touching the
    # network. 72% of SquareYards references are repeats of a project photo
    # shared across units, so this is most of the run's work avoided.
    if blob is not None and blob.enabled:
        known = blob.lookup_url(url)
        if known:
            stats.deduped += 1
            stats.images_seen += 1
            return {"source_url": url, "idx": idx, **known}

    async with sem:
        try:
            resp = await client.get(
                url, headers={"User-Agent": UA, "Referer": REFERERS.get(source, "")})
        except httpx.HTTPError as exc:
            stats.failed += 1
            stats.note_failure(url, f"network:{type(exc).__name__}")
            return None
    if resp.status_code != 200:
        stats.failed += 1
        stats.note_failure(url, f"http_{resp.status_code}")
        return None
    raw = resp.content
    if len(raw) > settings.image_max_bytes:
        stats.failed += 1
        stats.note_failure(url, "too_large")
        return None

    try:
        encoded, w, h, dewm = await asyncio.to_thread(_process_bytes, raw, source, url)
    except Exception as exc:  # noqa: BLE001 - any decoder failure is a skip
        stats.failed += 1
        stats.note_failure(url, f"decode:{type(exc).__name__}")
        return None

    if min(w, h) < settings.image_min_edge:
        stats.junk += 1
        return None

    stats.images_seen += 1
    stats.bytes_in += len(raw)
    if dewm:
        stats.dewatermarked += 1
    elif dewm is None:
        stats.no_mark += 1

    record = {
        "source_url": url, "idx": idx,
        "bytes": len(encoded), "width": w, "height": h,
        "watermark_removed": dewm,
    }
    if blob is not None and blob.enabled:
        try:
            path, _digest, was_new = await blob.put(client, encoded)
        except Exception as exc:  # noqa: BLE001 - one failed upload is not fatal
            stats.failed += 1
            stats.note_failure(url, f"blob:{type(exc).__name__}")
            print(f"  [blob] {type(exc).__name__}: {str(exc)[:120]}")
            return None
        record["path"] = path
        blob.remember_url(url, record)
        if not was_new:
            stats.deduped += 1
    else:
        # No Blob configured: keep the bytes inline so a small test batch
        # still works end to end.
        record["url"] = url
        record["data"] = Binary(encoded)
    return record


async def _ingest_property(client, db, doc: dict, stats: Stats, dry_run: bool,
                           blob: BlobStore | None = None) -> None:
    source = doc["source"]
    urls = [i["url"] for i in (doc.get("images") or []) if i.get("url")]
    urls = list(dict.fromkeys(urls))
    if not urls:
        stats.skipped += 1
        return

    sem = asyncio.Semaphore(PER_PROPERTY_CONCURRENCY)
    results = await asyncio.gather(*[
        _fetch_one(client, u, source, i, stats, sem, blob)
        for i, u in enumerate(urls, start=1)
    ])
    images = [r for r in results if r]
    url_set = set(urls)
    failed_here = [
        {"url": u, "reason": r}
        for u, r in stats.failures.items() if u in url_set
    ]

    if not images:
        # Every image failed. Record that rather than returning silently:
        # these are precisely the properties `--retry-failed` exists for, and
        # a property that leaves no document behind can never be found again.
        stats.skipped += 1
        if failed_here and not dry_run:
            await db[COLLECTION].update_one(
                {"_id": doc["_id"]},
                {"$set": {"property_id": doc["_id"], "source": source,
                          "source_id": doc.get("source_id"),
                          "name": _property_name(doc), "location": _location(doc),
                          "listing_url": doc.get("listing_url"),
                          "images": [], "image_count": 0, "total_bytes": 0,
                          "failed_images": failed_here,
                          "updated_at": datetime.now(UTC)}},
                upsert=True)
        return

    # The BSON ceiling only binds when the bytes are inline; with Blob the
    # document holds URLs and cannot approach it.
    inline = any("data" in i for i in images)
    kept, total = [], 0
    for img in images:
        if inline and total + img["bytes"] > MAX_DOC_BYTES:
            stats.dropped_oversize += 1
            continue
        kept.append(img)
        total += img["bytes"]
    if not kept:
        stats.skipped += 1
        return

    stats.stored += len(kept)
    stats.bytes_out += total
    if dry_run:
        return

    # Point the source document at the hosted copies too. The API reads
    # `properties.images[].blob_url` and falls back to the portal URL, so
    # writing it here is what actually flips a listing over to our images —
    # the `property_images` collection is the archive, not the read path.
    hosted = {i["source_url"]: url_for(i["path"])
              for i in kept if i.get("source_url") and i.get("path")}
    if hosted:
        patched = []
        for img in (doc.get("images") or []):
            img = dict(img)
            if img.get("url") in hosted:
                img["blob_url"] = hosted[img["url"]]
            patched.append(img)
        await db[D.PROPERTIES].update_one(
            {"_id": doc["_id"]}, {"$set": {"images": patched}})

    await db[COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "property_id": doc["_id"], "source": source,
            "source_id": doc.get("source_id"),
            "name": _property_name(doc), "location": _location(doc),
            "listing_url": doc.get("listing_url"),
            "images": kept, "image_count": len(kept), "total_bytes": total,
            "failed_images": failed_here,
            "updated_at": datetime.now(UTC),
        }},
        upsert=True,
    )


async def run_source(source: str, limit: int | None, force: bool, dry_run: bool,
                     stats: Stats, blob: BlobStore | None = None,
                     min_free_mb: float = 0.0) -> None:
    db = get_database()
    ckpt = _checkpoint(source)
    last_id = None if force else _load_last_id(ckpt)
    query = {**BASE_FILTER, "source": source, "images.0": {"$exists": True}}
    total = await db[D.PROPERTIES].count_documents(query)
    print(f"\n===== {source}: {total} properties with images =====")
    if last_id:
        print(f"[resume] continuing after _id={last_id}")

    # Count this source's own progress. `stats.properties` accumulates across
    # sources, so comparing it to a per-source limit made the second source
    # exit before doing anything.
    done = 0

    projection = {"_id": 1, "source": 1, "source_id": 1, "images": 1, "listing_url": 1,
                  "project_name": 1, "society_name": 1, "title": 1,
                  "sector": 1, "locality": 1, "city": 1}

    async with httpx.AsyncClient(
        timeout=settings.request_timeout, follow_redirects=True,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
    ) as client:
        while True:
            if limit and done >= limit:
                break
            q = dict(query)
            if last_id:
                q["_id"] = {"$gt": last_id}
            batch = await (db[D.PROPERTIES].find(q, projection)
                           .sort("_id", 1).limit(50).to_list(length=None))
            if not batch:
                break

            for doc in batch:
                if limit and done >= limit:
                    break
                if min_free_mb and done % 25 == 0:
                    free = await _quota_free_mb(db)
                    if free <= min_free_mb:
                        print(f"  [quota] {free:.0f} MB free — stopping at the "
                              f"{min_free_mb:.0f} MB floor, checkpoint saved")
                        stats.quota_stop = True
                        break
                if not force and await db[COLLECTION].find_one(
                        {"_id": doc["_id"]}, {"_id": 1}):
                    last_id = doc["_id"]
                    continue
                stats.properties += 1
                done += 1
                try:
                    await _ingest_property(client, db, doc, stats, dry_run, blob)
                except Exception as exc:  # noqa: BLE001 - one bad listing is not fatal
                    stats.failed += 1
                    print(f"  [error] {doc['_id']}: {type(exc).__name__}: {str(exc)[:140]}")
                last_id = doc["_id"]
                if done % 10 == 0:
                    print("  " + stats.line(limit or total))

            if not dry_run and last_id:
                _save_last_id(ckpt, last_id)
            if getattr(stats, "quota_stop", False):
                break


async def run_retry(sources, limit, dry_run, stats, blob) -> None:
    """Reprocess only the properties whose images failed earlier.

    Failures are mostly transient — a timeout, a rate-limited CDN — but some
    are permanent, because a portal removed the photo. Re-running the whole
    corpus to catch a 1% failure rate would cost another full pass, so this
    walks just the affected properties.
    """
    db = get_database()
    query = {"failed_images.0": {"$exists": True}, "source": {"$in": sources}}
    total = await db[COLLECTION].count_documents(query)
    print(f"\n===== retry: {total} properties with recorded failures =====")
    if not total:
        return

    projection = {"_id": 1, "source": 1, "source_id": 1, "images": 1, "listing_url": 1,
                  "project_name": 1, "society_name": 1, "title": 1,
                  "sector": 1, "locality": 1, "city": 1}
    ids = [d["_id"] async for d in db[COLLECTION].find(query, {"_id": 1}).limit(limit or 10**9)]

    async with httpx.AsyncClient(
        timeout=settings.request_timeout, follow_redirects=True,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
    ) as client:
        for pid in ids:
            doc = await db[D.PROPERTIES].find_one({"_id": pid}, projection)
            if not doc:
                stats.skipped += 1
                continue
            stats.properties += 1
            try:
                await _ingest_property(client, db, doc, stats, dry_run, blob)
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                print(f"  [error] {pid}: {type(exc).__name__}: {str(exc)[:120]}")
            if stats.properties % 25 == 0:
                print("  " + stats.line(len(ids)))


async def main(sources, limit, force, dry_run, reset, retry_failed=False,
               min_free_mb=0.0) -> None:
    for s in SOURCES:
        if reset:
            _checkpoint(s).unlink(missing_ok=True)
    if reset:
        print("[reset] checkpoints cleared")

    for s in sources:
        if not has_calibration(s):
            print(f"WARNING: no watermark calibration for {s} — its images would be "
                  f"stored with the portal mark intact. Run scripts/calibrate_marks.py.")
            return
    for s in sources:
        print(f"calibration {s}: {len(calibrated_sizes(s))} sizes")
    print(f"branding={'on' if settings.image_brand else 'OFF'} "
          f"max_edge={settings.image_max_edge} webp_q={settings.image_webp_quality}")

    blob = BlobStore()
    if blob.enabled:
        print(f"storage: Vercel Blob (prefix '{settings.blob_prefix}') "
              f"— MongoDB stores URLs")
        # Seed dedupe from previous runs so shared photos are not paid for twice.
        await blob.warm_from_mongo(get_database(), COLLECTION)
    else:
        print("storage: MongoDB inline bytes (no HOMZ_BLOB_READ_WRITE_TOKEN set) "
              "— fine for a test batch, will exhaust a free tier at scale")

    stats = Stats()
    if retry_failed:
        await run_retry(sources, limit, dry_run, stats, blob)
    else:
        per_source = (limit // len(sources)) if limit else None
        for s in sources:
            if stats.quota_stop:
                break
            await run_source(s, per_source, force, dry_run, stats, blob, min_free_mb)

    db = get_database()
    docs = await db[COLLECTION].count_documents({})
    agg = db[COLLECTION].aggregate([
        {"$group": {"_id": None, "imgs": {"$sum": "$image_count"},
                    "bytes": {"$sum": "$total_bytes"}}}])
    tot = None
    async for r in agg:
        tot = r
    await close_client()

    print(f"\ndone: {stats.line(limit or stats.properties)}")
    if stats.dropped_oversize:
        print(f"  {stats.dropped_oversize} images dropped to stay under the "
              f"{MAX_DOC_BYTES // 1024 // 1024} MB per-document cap")
    if stats.bytes_in:
        print(f"  {stats.bytes_in / 1e6:.0f} MB downloaded -> "
              f"{stats.bytes_out / 1e6:.0f} MB stored "
              f"({100 * stats.bytes_out / stats.bytes_in:.0f}%)")
    print(f"\nMongoDB '{COLLECTION}': {docs} properties, "
          f"{tot['imgs'] if tot else 0} images, "
          f"{(tot['bytes'] if tot else 0) / 1e6:.1f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=[*SOURCES, "both"], default="both")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N properties, split across sources")
    ap.add_argument("--force", action="store_true",
                    help="reprocess properties already stored")
    ap.add_argument("--dry-run", action="store_true", help="process but write nothing")
    ap.add_argument("--reset", action="store_true", help="clear checkpoints first")
    ap.add_argument("--min-free-mb", type=float, default=0.0,
                    help="stop cleanly once the Atlas quota has this much left")
    ap.add_argument("--retry-failed", action="store_true",
                    help="reprocess only properties that recorded image failures")
    a = ap.parse_args()
    chosen = list(SOURCES) if a.source == "both" else [a.source]
    asyncio.run(main(chosen, a.limit, a.force, a.dry_run, a.reset,
                 a.retry_failed, a.min_free_mb))
