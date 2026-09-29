"""Which image URLs are actually listing photos.

Both portals' galleries are scraped with selectors loose enough to sweep in
site furniture alongside the real photos. Measured over the live corpus
(33,151 apartments / 505,090 image references):

* `static.squareyards.com/ui-assets/...` — 362 references, and they are the
  literal Google Play and App Store download badges from the page footer.
* `img.staticmb.com/mbimages/user/...` and `.../topagent/...` — agent and
  user profile headshots, 200x200-ish, from the "posted by" block.

Deliberately **kept**, despite the suspicious path:

* `static.squareyards.com/reviewrating/...` — 46,562 references. The name
  suggests avatars, but these are full-resolution owner-uploaded photos of
  the property attached to a review (verified: 4032x3024, 2.8 MB, EXIF
  orientation unapplied). They are real content and frequently the only
  interior shots a listing has.
* `img.staticmb.com/mbimages/project/...` — builder project renders. The
  MagicBricks parser already filters these out of the *bare-sweep* tier
  (where they are a neighbouring project's banner); when a scoped selector
  returns one it is genuinely this project's own image.

`classify()` is deliberately allow-by-default: an unrecognized host is
treated as a photo, because a missed junk URL costs one wasted download
while a wrongly-dropped URL silently loses real content.
"""

from __future__ import annotations

import re
from enum import StrEnum

# Matched against the full URL, case-insensitively.
_JUNK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Footer app-store badges swept in from the page chrome.
    ("store_badge", re.compile(r"static\.squareyards\.com/ui-assets/", re.I)),
    # "Posted by" agent/user headshots.
    ("profile_photo", re.compile(r"img\.staticmb\.com/mbimages/(?:user|topagent)/", re.I)),
    # Generic site chrome that either portal may serve.
    ("site_chrome", re.compile(r"/(?:sprite|logo|icon|favicon|placeholder|no[-_]?image)", re.I)),
    # 1x1 trackers and data URIs.
    ("tracking_pixel", re.compile(r"\.(?:gif)(?:\?|$)|^data:", re.I)),
)


class ImageKind(StrEnum):
    PHOTO = "photo"
    JUNK = "junk"


def classify(url: str) -> tuple[ImageKind, str | None]:
    """Return (kind, reason). `reason` is the junk category, or None."""
    if not url or not url.strip():
        return ImageKind.JUNK, "empty"
    for reason, pattern in _JUNK_PATTERNS:
        if pattern.search(url):
            return ImageKind.JUNK, reason
    return ImageKind.PHOTO, None


def is_photo(url: str) -> bool:
    return classify(url)[0] is ImageKind.PHOTO


def filter_photos(urls: list[str]) -> list[str]:
    return [u for u in urls if is_photo(u)]
