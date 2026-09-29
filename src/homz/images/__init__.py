"""Listing photo ingestion: download, de-watermark, normalize, store."""

from homz.images.ingest import IngestStats, ingest_images
from homz.images.store import ImageStore
from homz.images.urls import ImageKind, classify, is_photo
from homz.images.watermark import remove_watermark

__all__ = [
    "ImageKind",
    "ImageStore",
    "IngestStats",
    "classify",
    "ingest_images",
    "is_photo",
    "remove_watermark",
]
