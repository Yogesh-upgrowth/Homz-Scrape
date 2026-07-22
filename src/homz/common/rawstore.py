"""Raw HTML/JSON archive.

Every fetched page is gzipped to disk under a content-addressed key. When a
parser starts returning nulls because a portal changed its markup, you replay
the stored payload through the new parser instead of re-crawling.

Layout:  data/raw/<source>/<YYYY>/<MM>/<DD>/<sha1[:2]>/<sha1>.html.gz
The key stored on the record is the path relative to the archive root, so the
backend can be swapped for S3 without touching the schema.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)


class RawStore:
    def __init__(self, root: Path | str | None = None, *, enabled: bool | None = None) -> None:
        self.root = Path(root or settings.raw_html_dir)
        self.enabled = settings.store_raw_html if enabled is None else enabled

    # -- write --------------------------------------------------------------

    def put(
        self,
        *,
        source: str,
        url: str,
        content: str | bytes,
        extension: str = "html",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Store a payload; returns the archive key (or None when disabled)."""
        if not self.enabled:
            return None

        payload = content.encode("utf-8", errors="replace") if isinstance(content, str) else content
        digest = hashlib.sha1(url.encode("utf-8") + b"|" + payload).hexdigest()
        now = datetime.now(UTC)
        rel = Path(
            source,
            f"{now.year:04d}",
            f"{now.month:02d}",
            f"{now.day:02d}",
            digest[:2],
            f"{digest}.{extension}.gz",
        )
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with gzip.open(target, "wb", compresslevel=6) as fh:
                fh.write(payload)
            if metadata:
                meta = {"url": url, "fetched_at": now.isoformat(), **metadata}
                target.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:
            log.warning("rawstore.write_failed", url=url, error=str(exc))
            return None

        return str(rel)

    # -- read ---------------------------------------------------------------

    def get(self, key: str) -> bytes | None:
        path = self.root / key
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            log.warning("rawstore.read_failed", key=key, error=str(exc))
            return None

    def get_text(self, key: str, encoding: str = "utf-8") -> str | None:
        data = self.get(key)
        return data.decode(encoding, errors="replace") if data is not None else None

    # -- maintenance --------------------------------------------------------

    def prune(self, older_than_days: int | None = None) -> int:
        """Delete day-partitions older than the retention window."""
        days = older_than_days if older_than_days is not None else settings.raw_html_retention_days
        if days <= 0 or not self.root.exists():
            return 0

        cutoff = (datetime.now(UTC) - timedelta(days=days)).date()
        removed = 0
        for source_dir in self.root.iterdir():
            if not source_dir.is_dir():
                continue
            for year_dir in source_dir.iterdir():
                for month_dir in _safe_iterdir(year_dir):
                    for day_dir in _safe_iterdir(month_dir):
                        try:
                            partition = datetime(
                                int(year_dir.name), int(month_dir.name), int(day_dir.name)
                            ).date()
                        except ValueError:
                            continue
                        if partition < cutoff:
                            shutil.rmtree(day_dir, ignore_errors=True)
                            removed += 1
        if removed:
            log.info("rawstore.pruned", partitions=removed, retention_days=days)
        return removed

    def usage_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


def _safe_iterdir(path: Path):
    if path.is_dir():
        yield from (p for p in path.iterdir() if p.is_dir())
