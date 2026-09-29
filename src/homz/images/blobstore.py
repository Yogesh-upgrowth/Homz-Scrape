"""Vercel Blob as the image backend, with content-hash deduplication.

MongoDB holds the URL; Blob holds the bytes. That split is what makes the
whole corpus affordable: 505,090 images are ~40 GB, which needs an Atlas M30
at roughly $390/month to store as BSON, against ~$0.40/month on Blob — while
the documents shrink to a URL each and the free Atlas tier keeps working.

It is also what the frontend already expects. `lib/listings/media.ts` in the
HomzRealtor repo resolves a listing's images from a manifest of *URLs* and
falls back to the portal's CDN when it has none, so populating that manifest
is the whole integration — there is no renderer change to make.

## Deduplication

The blob path is the SHA-256 of the processed bytes, so the same photo stored
for two properties writes once and both reference it. SquareYards reuses one
project photo across every unit in a project: 450,465 references collapse to
128,228 files, cutting storage and upload operations more than threefold.

`put()` is skipped entirely when the digest is already known, which matters
for cost (uploads are billed per operation) and for the Pro rate limit of
4,500 advanced operations per minute.

## Rate limiting

Uploads are throttled below that ceiling. Exceeding it fails the request
rather than queueing, so a backfill that ignores it would start losing images
partway through a long run.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import httpx

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

_API = "https://blob.vercel-storage.com"
#: Public base for this store. Kept out of every stored record: repeating it
#: 180,000 times cost ~10 MB of an Atlas quota that writes were blocked on.
#: Records hold the pathname; the URL is rebuilt from it on read.
PUBLIC_BASE = "https://gnunxcv3vxg0q9wy.public.blob.vercel-storage.com/"


def url_for(path: str) -> str:
    """Full public URL for a stored blob pathname."""
    return path if path.startswith("http") else PUBLIC_BASE + path.lstrip("/")


def sha_of(path: str) -> str:
    """The content hash, which IS the filename — no need to store it twice."""
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
#: Pro allows 4,500 advanced operations/minute. Stay comfortably under it —
#: the ceiling is enforced by rejection, not by queueing.
_MAX_UPLOADS_PER_MIN = 3000


class BlobStore:
    """Uploads processed images to Vercel Blob, once per distinct content."""

    def __init__(self, token: str | None = None, *, prefix: str = "listings") -> None:
        self.token = token or settings.blob_read_write_token
        self.prefix = prefix.strip("/")
        #: digest -> url, for content already uploaded in this process.
        self._seen: dict[str, str] = {}
        #: source url -> full image record, so a photo already processed is
        #: never downloaded again. This is the difference between fetching
        #: 450,465 SquareYards images and 128,228 of them: the portal reuses
        #: one project photo across every unit, and hashing to spot that only
        #: helps *after* paying for the download.
        self._by_url: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._stamps: list[float] = []

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def path_for(self, digest: str, extension: str = "webp") -> str:
        # Two levels of fan-out keeps any one prefix listable.
        return f"{self.prefix}/{digest[:2]}/{digest[2:4]}/{digest}.{extension}"

    async def _throttle(self) -> None:
        """Block until another upload fits inside the per-minute budget."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._stamps = [t for t in self._stamps if now - t < 60.0]
                if len(self._stamps) < _MAX_UPLOADS_PER_MIN:
                    self._stamps.append(now)
                    return
                wait = 60.0 - (now - self._stamps[0]) + 0.05
            await asyncio.sleep(max(wait, 0.05))

    async def put(
        self,
        client: httpx.AsyncClient,
        payload: bytes,
        *,
        content_type: str = "image/webp",
        extension: str = "webp",
    ) -> tuple[str, str, bool]:
        """Store bytes. Returns `(pathname, sha256, was_new)`.

        Identical bytes seen earlier in this run return the known URL without
        touching the network, which is the dedupe in practice.
        """
        digest = hashlib.sha256(payload).hexdigest()
        path = self.path_for(digest, extension)
        async with self._lock:
            known = self._seen.get(digest)
        if known:
            return known, digest, False

        await self._throttle()
        resp = await client.put(
            f"{_API}/{path}",
            content=payload,
            headers={
                "authorization": f"Bearer {self.token}",
                "x-api-version": "7",
                "x-content-type": content_type,
                # The digest already makes the path unique, so a collision is
                # the same bytes. Overwriting is correct and keeps the run
                # idempotent across restarts.
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1",
                "x-cache-control-max-age": "31536000",
            },
            timeout=60.0,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"blob put {resp.status_code}: {resp.text[:200]}")
        async with self._lock:
            self._seen[digest] = path
        return path, digest, True

    async def list_paths(self, client: httpx.AsyncClient, prefix: str | None = None):
        """Every stored pathname under `prefix`, paginated. Yields dicts.

        Blob's list endpoint caps a page at 1,000, so a store of ~100,000
        files is ~100 round trips. Each is a "simple" operation, far cheaper
        than the advanced ones `put` is throttled against, but the throttle
        still applies so a sweep cannot eat the budget a concurrent ingest
        needs.
        """
        cursor = None
        while True:
            await self._throttle()
            params = {"limit": "1000", "prefix": prefix or self.prefix}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                f"{_API}/", params=params,
                headers={"authorization": f"Bearer {self.token}",
                         "x-api-version": "7"},
                timeout=60.0)
            if resp.status_code >= 400:
                raise RuntimeError(f"blob list {resp.status_code}: {resp.text[:200]}")
            body = resp.json()
            for blob in body.get("blobs", []):
                yield blob
            cursor = body.get("cursor")
            if not body.get("hasMore") or not cursor:
                return

    async def delete(self, client: httpx.AsyncClient, urls: list[str]) -> None:
        """Permanently remove blobs by URL. There is no undo.

        Batched, because the endpoint takes a list and one call per file
        would spend the operation budget for no reason.
        """
        if not urls:
            return
        await self._throttle()
        resp = await client.post(
            f"{_API}/delete",
            json={"urls": urls},
            headers={"authorization": f"Bearer {self.token}",
                     "x-api-version": "7"},
            timeout=60.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"blob delete {resp.status_code}: {resp.text[:200]}")

    def lookup_url(self, source_url: str) -> dict | None:
        """The stored record for a source URL already processed, if known.

        Carries the real dimensions and byte count, not just the URL: a
        reused image must look identical in Mongo to a freshly processed one,
        or the frontend gets zero-sized images for the majority of the
        SquareYards catalogue.
        """
        return self._by_url.get(source_url)

    def remember_url(self, source_url: str, record: dict) -> None:
        self._by_url[source_url] = {
            "path": record["path"],
            "bytes": record.get("bytes", 0),
            "width": record.get("width", 0), "height": record.get("height", 0),
            "watermark_removed": record.get("watermark_removed"),
        }

    async def warm_from_mongo(self, db, collection: str = "property_images",
                              *, url_memo: bool = True) -> int:
        """Seed the dedupe tables from what previous runs already did.

        Without this a resumed run re-downloads and re-uploads every shared
        photo, which on SquareYards means paying for the same file dozens of
        times over and spending most of the run on redundant transfers.

        `url_memo=False` seeds only the content table, not the URL one. That
        is what a *repair* run needs: the URL memo answers "we already have a
        file for this photo", which is precisely the answer to refuse when the
        point of the run is that the file we have is wrong. The content table
        still applies, so any image whose reprocessing happens to produce
        identical bytes costs no upload.
        """
        n = 0
        cursor = db[collection].aggregate([
            {"$unwind": "$images"},
            {"$match": {"images.path": {"$ne": None}}},
            {"$project": {"_id": 0, "path": "$images.path",
                          "src": "$images.source_url", "bytes": "$images.bytes",
                          "width": "$images.width", "height": "$images.height",
                          "wm": "$images.watermark_removed"}},
        ])
        async for row in cursor:
            path = row["path"]
            digest = sha_of(path)
            self._seen[digest] = path
            if url_memo and row.get("src"):
                self._by_url[row["src"]] = {
                    "path": path,
                    "bytes": row.get("bytes") or 0,
                    "width": row.get("width") or 0,
                    "height": row.get("height") or 0,
                    "watermark_removed": row.get("wm"),
                }
            n += 1
        if n:
            log.info("blobstore.warmed", known=n, urls=len(self._by_url))
        return n
