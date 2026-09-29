"""Download → de-watermark → normalize → store, for one listing's photos.

Ordering matters and is not arbitrary:

1. **Classify** the URL first, so junk costs zero network (see `urls.py`).
2. **Download** the original bytes.
3. **De-watermark before resizing.** The watermark calibration is indexed by
   the portal's *served* pixel dimensions; resizing first would destroy the
   anchor and the alpha map's pixel alignment, making the mark unremovable.
4. **Apply EXIF orientation**, then resize. SquareYards `reviewrating` photos
   are phone uploads that carry an orientation tag the portal never baked in —
   ignore it and half the user-submitted interiors are stored sideways.
5. **Re-encode to WebP** and store by content hash.

Re-encoding is not optional at this corpus size: originals run from 26 KB
crops to 2.8 MB full-resolution phone shots, totalling ~90 GB, which no free
storage tier can hold. Bounding the long edge at 1920px and encoding WebP q82
brings that to single-digit GB with no visible loss at display sizes.

Concurrency is per-listing and bounded; the shared `RateLimiter` is *not*
used here because image CDNs (`img.staticmb.com`, `static.squareyards.com`)
are separate infrastructure from the HTML origins and are built to serve
assets in parallel. They are still capped by `image_concurrency` so a backfill
cannot saturate the link.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field

import httpx

from homz.common.schema import Image
from homz.images.blobstore import BlobStore
from homz.images.brand import apply_branding
from homz.images.store import ImageStore
from homz.images.urls import ImageKind, classify
from homz.images.watermark import remove_watermark
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

_REFERERS = {
    "magicbricks": "https://www.magicbricks.com/",
    "squareyards": "https://www.squareyards.com/",
}

#: Permanent failures — a re-run should not retry these.
_PERMANENT = {"junk", "too_small", "decode_failed", "http_404", "http_403", "http_410"}


@dataclass
class IngestStats:
    considered: int = 0
    skipped_junk: int = 0
    already_stored: int = 0
    downloaded: int = 0
    stored_new: int = 0
    dewatermarked: int = 0
    failed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def note_error(self, reason: str) -> None:
        self.failed += 1
        self.errors[reason] = self.errors.get(reason, 0) + 1

    def merge(self, other: IngestStats) -> None:
        for f in ("considered", "skipped_junk", "already_stored", "downloaded",
                  "stored_new", "dewatermarked", "failed", "bytes_in", "bytes_out"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        for k, v in other.errors.items():
            self.errors[k] = self.errors.get(k, 0) + v


def _process_bytes(payload: bytes, source: str, url: str | None = None) -> tuple[bytes, int, int, bool | None]:
    """Decode → de-watermark → orient → resize → WebP. Returns (bytes, w, h, dewm)."""
    import numpy as np
    from PIL import Image as PILImage
    from PIL import ImageOps

    with PILImage.open(io.BytesIO(payload)) as im:
        im.load()
        # De-watermark on the AS-SERVED pixel grid, before any geometry change.
        dewm: bool | None = None
        if settings.image_dewatermark:
            rgb = im.convert("RGB")
            result = remove_watermark(np.asarray(rgb), source, url)
            dewm = result.removed
            if result.removed:
                im = PILImage.fromarray(result.image)

        im = ImageOps.exif_transpose(im) or im
        im = im.convert("RGB")

        w, h = im.size
        longest = max(w, h)
        if longest > settings.image_max_edge:
            scale = settings.image_max_edge / longest
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           PILImage.LANCZOS)

        # Brand AFTER the resize. Branding first would scale the badge by
        # the portal's arbitrary served width (269px to 2000px across the
        # corpus), giving narrow crops an illegible speck and wide ones a
        # banner.
        im = apply_branding(im)

        out = io.BytesIO()
        im.save(out, format="WEBP", quality=settings.image_webp_quality, method=4)
        return out.getvalue(), im.size[0], im.size[1], dewm


async def _fetch_one(
    client: httpx.AsyncClient,
    image: Image,
    *,
    source: str,
    store: ImageStore,
    stats: IngestStats,
    sem: asyncio.Semaphore,
    source_id: str | None = None,
    index: int | None = None,
    blob: BlobStore | None = None,
) -> Image:
    stats.considered += 1

    kind, reason = classify(image.url)
    if kind is ImageKind.JUNK:
        stats.skipped_junk += 1
        image.fetch_error = f"junk:{reason}"
        return image

    # Already ingested in a previous run — verified against the filesystem, so
    # a half-finished run that recorded a key it never wrote self-heals.
    if image.storage_key and store.exists(image.storage_key):
        stats.already_stored += 1
        return image

    if image.fetch_error and image.fetch_error.split(":")[0] in _PERMANENT:
        return image

    async with sem:
        try:
            resp = await client.get(image.url)
        except httpx.HTTPError as exc:
            stats.note_error(type(exc).__name__)
            image.fetch_error = f"network:{type(exc).__name__}"
            return image

    if resp.status_code != 200:
        stats.note_error(f"http_{resp.status_code}")
        image.fetch_error = f"http_{resp.status_code}"
        return image

    payload = resp.content
    if len(payload) > settings.image_max_bytes:
        stats.note_error("too_large")
        image.fetch_error = "too_large"
        return image

    stats.downloaded += 1
    stats.bytes_in += len(payload)

    try:
        # Pillow work is CPU-bound and releases the GIL poorly; keep it off the
        # event loop so downloads for the rest of the listing keep flowing.
        encoded, w, h, dewm = await asyncio.to_thread(_process_bytes, payload, source, image.url)
    except Exception as exc:  # noqa: BLE001 - any decoder failure is just a skip
        stats.note_error("decode_failed")
        image.fetch_error = f"decode_failed:{type(exc).__name__}"
        return image

    if min(w, h) < settings.image_min_edge:
        stats.note_error("too_small")
        image.fetch_error = "too_small"
        return image

    try:
        key, digest, was_new, property_key = await asyncio.to_thread(
            lambda: store.put(encoded, source=source, source_id=source_id, index=index)
        )
    except OSError as exc:
        stats.note_error("store_failed")
        image.fetch_error = f"store_failed:{type(exc).__name__}"
        return image

    if was_new:
        stats.stored_new += 1
        stats.bytes_out += len(encoded)
    if dewm:
        stats.dewatermarked += 1

    # Publish to Blob when configured, so a scrape and the backfill leave
    # images in the same place and the site has a URL it can actually serve.
    if blob is not None and blob.enabled:
        try:
            image.blob_url, _d, _new = await blob.put(client, encoded)
        except Exception as exc:  # noqa: BLE001 - hosting is best-effort
            log.warning("ingest.blob_upload_failed",
                        url=image.url, error=f"{type(exc).__name__}: {str(exc)[:160]}")

    image.storage_key = key
    image.property_key = property_key
    image.sha256 = digest
    image.bytes = len(encoded)
    image.width, image.height = w, h
    image.watermark_removed = dewm
    image.fetch_error = None
    return image


async def ingest_images(
    images: list[Image],
    *,
    source: str,
    store: ImageStore | None = None,
    client: httpx.AsyncClient | None = None,
    source_id: str | None = None,
    blob: BlobStore | None = None,
) -> tuple[list[Image], IngestStats]:
    """Download and store every photo for one listing. Mutates and returns them."""
    stats = IngestStats()
    if not images or not settings.store_images:
        return images, stats

    store = store or ImageStore()
    blob = blob if blob is not None else BlobStore()
    sem = asyncio.Semaphore(settings.image_concurrency)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": _REFERERS.get(source, ""),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    owns_client = client is None
    client = client or httpx.AsyncClient(
        headers=headers, timeout=settings.request_timeout, follow_redirects=True
    )
    try:
        results = await asyncio.gather(*[
            _fetch_one(client, img, source=source, store=store, stats=stats,
                       sem=sem, source_id=source_id, index=i, blob=blob)
            for i, img in enumerate(images, start=1)
        ])
    finally:
        if owns_client:
            await client.aclose()

    return list(results), stats
