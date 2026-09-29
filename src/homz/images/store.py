"""Content-addressed store for downloaded listing photos.

Mirrors `homz.common.rawstore.RawStore`: the key written onto the record is a
path *relative to the archive root*, so swapping the backend for S3/R2 later
means reimplementing this class alone and leaving the schema untouched.

Layout:  data/images/<sha256[:2]>/<sha256[2:4]>/<sha256>.webp

Addressing by content hash rather than by source URL is what makes the
SquareYards duplication free: the portal reuses one project photo across every
unit in that project, so 450,465 image references collapse onto 128,228
distinct files. Two listings referencing the same photo write the same bytes
to the same key, and the second write is skipped.

Two levels of fan-out (256 x 256) keep directories small — a single flat
directory of 173k files is slow to enumerate on Windows, and the existing
raw-HTML archive already uses the same shape.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
from pathlib import Path

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

#: Source ids come straight from the portals; keep them filesystem-safe.
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


class ImageStore:
    def __init__(self, root: Path | str | None = None, *, enabled: bool | None = None) -> None:
        self.root = Path(root or settings.image_dir)
        self.enabled = settings.store_images if enabled is None else enabled

    def key_for(self, digest: str, extension: str = "webp") -> str:
        return f"_pool/{digest[:2]}/{digest[2:4]}/{digest}.{extension}"

    def path_for(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self.path_for(key).exists()

    def put(
        self,
        payload: bytes,
        *,
        extension: str = "webp",
        source: str | None = None,
        source_id: str | None = None,
        index: int | None = None,
    ) -> tuple[str, str, bool, str | None]:
        """Store bytes under their own hash, optionally mirrored per property.

        Returns `(key, sha256, was_new, property_key)`. A repeat of identical
        bytes is a no-op that still returns the existing key, which is what
        makes the whole ingest idempotent and safe to resume mid-run.
        """
        digest = hashlib.sha256(payload).hexdigest()
        key = self.key_for(digest, extension)
        target = self.path_for(key)
        was_new = True
        if target.exists() and target.stat().st_size == len(payload):
            was_new = False
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp name and rename, so an interrupted run never
            # leaves a truncated file that a later run trusts because the key
            # exists.
            tmp = target.with_suffix(target.suffix + ".part")
            try:
                tmp.write_bytes(payload)
                tmp.replace(target)
            except OSError as exc:
                log.warning("imagestore.write_failed", key=key, error=str(exc))
                tmp.unlink(missing_ok=True)
                raise

        property_key = None
        if settings.image_by_property and source and source_id is not None:
            property_key = self._link_into_property(
                target, digest, extension, source, str(source_id), index
            )
        return key, digest, was_new, property_key

    def _link_into_property(
        self, pool_path: Path, digest: str, extension: str,
        source: str, source_id: str, index: int | None,
    ) -> str | None:
        """Mirror a pooled file into `<source>/<source_id>/` as a hard link.

        A hard link, not a copy: SquareYards reuses one project photo across
        every unit in a project, so the 505,090 references in the corpus
        collapse onto 173,172 distinct files. Copying would inflate the store
        from ~10 GB to ~33 GB to say the same thing three times over. A hard
        link gives a real, browsable per-property file at the cost of a
        directory entry.

        Falls back to copying where hard links are unavailable (non-NTFS
        volumes, or a pool and tree that end up on different filesystems) —
        correctness first, disk second.
        """
        safe_id = _SAFE_ID_RE.sub("_", source_id)[:120] or "unknown"
        prefix = f"{index:02d}_" if index is not None else ""
        rel = f"{source}/{safe_id}/{prefix}{digest[:8]}.{extension}"
        link = self.root / rel
        if link.exists():
            return rel
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(pool_path, link)
        except OSError:
            try:
                shutil.copy2(pool_path, link)
            except OSError as exc:
                log.warning("imagestore.property_link_failed", rel=rel, error=str(exc))
                return None
        return rel

    def get(self, key: str) -> bytes | None:
        try:
            return self.path_for(key).read_bytes()
        except OSError:
            return None

    def stats(self) -> dict[str, int]:
        files = 0
        total = 0
        if self.root.exists():
            for p in (self.root / "_pool").rglob("*.webp"):
                files += 1
                with contextlib.suppress(OSError):
                    total += p.stat().st_size
        return {"files": files, "bytes": total}
