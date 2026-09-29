"""Re-scrape every already-known apartment and ingest its photos.

Unlike `apartment_backfill.py`, which *discovers* new listings, this walks the
apartments already in Mongo and refreshes each one in place: re-fetch the
detail page live, re-parse it with the current parsers, download every photo,
strip the MagicBricks watermark, and store the files content-addressed under
`data/images/`.

## Why re-fetch rather than replay archived HTML

Replaying `raw_html_key` through the parsers is free and fast, but it pins
content to whatever the page said on the day it was first scraped — prices,
availability and gallery contents all drift. The images have to be fetched
over the network regardless, so a live re-fetch costs one extra request per
listing on top of work that was already network-bound.

## Preserving what has already been downloaded

The parsers emit fresh `Image` objects with no `storage_key`, so writing them
back verbatim would discard every previously stored file and re-download the
corpus on each run. `_merge_image_state` carries the storage fields across
from the existing document, matched on URL, before ingest runs — so a second
pass over an already-processed listing downloads nothing.

## Resumability

Progress is a single `_id` watermark in `data/checkpoints/`, and listings are
walked in `_id` order. A kill at any point loses at most the in-flight batch;
a restart resumes immediately after the last committed one. Combined with the
content-addressed store (a repeat write of identical bytes is a no-op), the
whole script is safe to run, interrupt and re-run at will.

Usage:
    python scripts/rescrape_apartments_with_images.py --source magicbricks --limit 50 --dry-run
    python scripts/rescrape_apartments_with_images.py --source magicbricks --rps 0.8
    python scripts/rescrape_apartments_with_images.py --source squareyards --rps 0.7
    python scripts/rescrape_apartments_with_images.py --source magicbricks --images-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402

from homz.common.base import ScrapeJob  # noqa: E402
from homz.common.schema import Image, PropertyRecord  # noqa: E402
from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.etl.pipeline import load_records  # noqa: E402
from homz.images import ImageStore, IngestStats, ingest_images  # noqa: E402
from homz.images.watermark import calibrated_sizes, has_calibration  # noqa: E402
from homz.settings import settings  # noqa: E402

CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "data" / "checkpoints"
BATCH_SIZE = 200
COMMIT_EVERY = 20

BASE_FILTER = {
    "property_type": "apartment",
    "is_active": True,
    "listing_type": {"$in": ["sale", "resale", "rent"]},
}


def _checkpoint(source: str, tag: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"rescrape_images_{source}_{tag}_progress.json"


def _load_last_id(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")).get("last_id")
    except (OSError, ValueError):
        return None


def _save_last_id(path: Path, last_id: str) -> None:
    path.write_text(json.dumps({"last_id": last_id}), encoding="utf-8")


_STORAGE_FIELDS = ("storage_key", "property_key", "sha256", "bytes",
                   "watermark_removed", "fetch_error")


def _merge_image_state(fresh: list[Image], existing: list[dict]) -> list[Image]:
    """Carry storage fields from the stored document onto freshly parsed images.

    Without this every run re-downloads the entire corpus, because the parser
    has no idea a file already exists on disk. Matched on the portal URL,
    which is stable and is exactly what `Image.url` still holds after ingest.

    Photos that were already downloaded but are *absent* from the fresh parse
    are retained rather than dropped. MagicBricks builds its gallery
    client-side, so the server-rendered HTML the scraper sees can legitimately
    come back short on one fetch and complete on the next; overwriting blindly
    would discard files we already hold, silently and irreversibly. Only
    entries carrying a `storage_key` are kept this way, which is precisely the
    set that cost a download — junk URLs never get one, so the parser's junk
    filter still takes effect on re-scrape.
    """
    by_url = {img.get("url"): img for img in existing if img.get("url")}
    for image in fresh:
        prior = by_url.get(image.url)
        if not prior:
            continue
        for field in _STORAGE_FIELDS:
            setattr(image, field, prior.get(field))

    # Never let a parse that found nothing erase a gallery we already have.
    # A portal changing its gallery markup makes the selectors return zero
    # matches while the page still renders photos normally (exactly what
    # SquareYards' listing extractor does today: the URLs are present in the
    # HTML but no selector matches them). Writing that result back would
    # destroy the stored URLs across the corpus in a single pass, with no way
    # to recover them short of re-crawling everything.
    if not fresh and existing:
        return [Image(**prior) for prior in existing]

    fresh_urls = {image.url for image in fresh}
    retained = [
        Image(**prior)
        for url, prior in by_url.items()
        if url not in fresh_urls and prior.get("storage_key")
    ]
    return fresh + retained


class Totals:
    def __init__(self) -> None:
        self.listings = 0
        self.refetched = 0
        self.fetch_failed = 0
        self.parse_failed = 0
        self.inserted = 0
        self.updated = 0
        self.images = IngestStats()
        self.started = time.monotonic()

    def line(self) -> str:
        el = (time.monotonic() - self.started) / 3600
        i = self.images
        rate = self.listings / max(el * 60, 1e-9)
        return (
            f"listings={self.listings} refetched={self.refetched} "
            f"fetch_fail={self.fetch_failed} parse_fail={self.parse_failed} "
            f"updated={self.updated} | img: dl={i.downloaded} new={i.stored_new} "
            f"dewm={i.dewatermarked} junk={i.skipped_junk} fail={i.failed} "
            f"{i.bytes_out / 1e9:.2f}GB | {el:.2f}h ({rate:.1f}/min)"
        )


async def _scraper_for(source: str, rps: float | None):
    if source == "magicbricks":
        from homz.scrapers.magicbricks.scraper import MagicBricksScraper

        scraper = MagicBricksScraper()
    elif source == "squareyards":
        from homz.scrapers.squareyards.scraper import SquareYardsScraper

        scraper = SquareYardsScraper()
    else:
        raise SystemExit(f"unknown source: {source}")
    if rps:
        # Per-process only — the scheduled incremental jobs keep the default.
        for host in getattr(scraper, "hosts", ()) or ():
            scraper.limiter.set_host_rate(host, rps)
    return scraper


async def main(
    source: str, rps: float | None, limit: int | None, dry_run: bool, images_only: bool
) -> None:
    if not has_calibration(source):
        print("WARNING: no watermark calibration found — images will be stored "
              "with watermarks intact. Run scripts/calibrate_watermark.py first.")
    else:
        print(f"watermark calibration: {len(calibrated_sizes(source))} sizes for {source}")

    mode = "images-only (no re-fetch)" if images_only else "live re-fetch + images"
    print(f"mode: {mode}  source={source}  dry_run={dry_run}")
    print(f"image store: {settings.image_dir}  max_edge={settings.image_max_edge} "
          f"webp_q={settings.image_webp_quality}")

    db = get_database()
    store = ImageStore()
    totals = Totals()
    ckpt = _checkpoint(source, "imgonly" if images_only else "full")
    last_id = _load_last_id(ckpt)
    if last_id:
        print(f"[resume] continuing after _id={last_id}")

    query = {**BASE_FILTER, "source": source}
    total_docs = await db[D.PROPERTIES].count_documents(query)
    print(f"{total_docs} {source} apartments in scope\n")

    scraper = None
    job = None
    if not images_only:
        scraper = await _scraper_for(source, rps)
        job = ScrapeJob(name="rescrape-images", city="gurgaon",
                        listing_type="sale", max_items=10**9)
        await scraper.__aenter__()

    # One shared client for image CDNs across the whole run — connection reuse
    # matters at ~170k requests.
    img_client = httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://www.{source}.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        timeout=settings.request_timeout,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=settings.image_concurrency * 2,
                            max_keepalive_connections=settings.image_concurrency),
    )

    buffer: list[PropertyRecord] = []

    async def flush() -> None:
        nonlocal buffer
        if not buffer or dry_run:
            buffer = []
            return
        res = await load_records(buffer)
        totals.inserted += res.inserted
        totals.updated += res.updated
        buffer = []

    try:
        while True:
            q = dict(query)
            if last_id:
                q["_id"] = {"$gt": last_id}
            batch = await (
                db[D.PROPERTIES]
                .find(q, projection={"_id": 1, "source_id": 1, "listing_url": 1, "images": 1})
                .sort("_id", 1)
                .limit(BATCH_SIZE)
                .to_list(length=None)
            )
            if not batch:
                break

            for doc in batch:
                if limit and totals.listings >= limit:
                    break
                totals.listings += 1
                existing = doc.get("images") or []

                record = None
                if not images_only and doc.get("listing_url"):
                    try:
                        fetched = await scraper.fetch_detail(doc["listing_url"], job)
                        records = await scraper.parse_detail(fetched, job)
                        totals.refetched += 1
                        record = next(
                            (r for r in records if isinstance(r, PropertyRecord)), None
                        )
                        if record is None:
                            totals.parse_failed += 1
                    except Exception as exc:  # noqa: BLE001
                        totals.fetch_failed += 1
                        print(f"[fetch-fail] {doc['_id']}: {type(exc).__name__}: "
                              f"{str(exc)[:120]}")

                if record is not None:
                    images = _merge_image_state(list(record.images), existing)
                else:
                    # images-only, or the re-fetch failed: fall back to the
                    # images already recorded so the run still makes progress.
                    images = [Image(**img) for img in existing]

                images, stats = await ingest_images(
                    images, source=source, store=store, client=img_client,
                    source_id=doc.get("source_id"),
                )
                totals.images.merge(stats)

                if dry_run:
                    continue

                if record is not None:
                    record.images = images
                    buffer.append(record)
                    if len(buffer) >= COMMIT_EVERY:
                        await flush()
                else:
                    # No fresh record to upsert — patch the images array alone.
                    await db[D.PROPERTIES].update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"images": [i.model_dump(mode="python") for i in images]}},
                    )
                    totals.updated += 1

            await flush()
            last_id = batch[-1]["_id"]
            if not dry_run:
                _save_last_id(ckpt, last_id)
            print(f"[{totals.listings}/{total_docs}] {totals.line()}")

            if limit and totals.listings >= limit:
                break
    finally:
        await flush()
        await img_client.aclose()
        if scraper is not None:
            await scraper.__aexit__(None, None, None)
        await close_client()

    print(f"\ndone: {totals.line()}")
    if totals.images.errors:
        print("image errors:", dict(sorted(
            totals.images.errors.items(), key=lambda kv: -kv[1])[:10]))
    st = store.stats()
    print(f"store now holds {st['files']} files, {st['bytes'] / 1e9:.2f} GB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["magicbricks", "squareyards"])
    ap.add_argument("--rps", type=float, default=None, help="per-host rate for this process")
    ap.add_argument("--limit", type=int, default=None, help="stop after N listings")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and process, write nothing to Mongo")
    ap.add_argument("--images-only", action="store_true",
                    help="skip the page re-fetch; ingest the images already recorded")
    args = ap.parse_args()
    asyncio.run(main(args.source, args.rps, args.limit, args.dry_run, args.images_only))
