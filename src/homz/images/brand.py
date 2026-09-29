"""Apply the homzrealtor watermark to a stored photo.

Runs *after* the portal's own mark has been removed and after the image has
been resized. Both orderings matter:

* **After de-watermarking**, obviously — otherwise the inversion in
  `watermark.py` would be fed our own overlay and try to solve for it.
* **After resizing.** Branding before the resize scales the badge by the
  portal's arbitrary served width (which ranges from 269px to 2000px across
  the corpus), so a narrow crop ends up with an illegible speck and a wide
  one with a banner. Sizing against the *final* dimensions keeps the mark
  visually constant everywhere.

The logo (`assets/homzrealtor-logo.png`, taken from homzrealtor.com) is a
white wordmark authored for a dark site header, so it disappears against pale
walls and bright skies — which is most of a property gallery. Every placement
therefore composites a soft dark shadow underneath it. That shadow is what
makes the mark legible on light photos, not decoration.

The logo master is only 128x44, so it is never upscaled past
`_MAX_LOGO_WIDTH`; beyond that it turns visibly soft. On a 1920px image the
mark lands at ~9% of the width.
"""

from __future__ import annotations

import functools
from pathlib import Path

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "homzrealtor-logo.png"

#: Logo width as a fraction of the image width, then clamped. Wider than a
#: corner badge would be: centred, the mark reads as a deliberate overlay
#: rather than a sticker, and it also sits where both portals put their own
#: marks, so whatever faint trace survives removal falls under it.
_LOGO_WIDTH_RATIO = 0.26
_MIN_LOGO_WIDTH = 96
_MAX_LOGO_WIDTH = 460
#: Margin from the image edge, as a fraction of the short edge. Only used to
#: keep the mark off the very edge on extreme aspect ratios.
_MARGIN_RATIO = 0.022
_MIN_MARGIN = 8
#: Opacity of the composited mark. Lower than a corner badge: centred over the
#: subject it has to read as a watermark, not obscure the room behind it.
_LOGO_ALPHA = 0.55
_SHADOW_ALPHA = 0.45
_SHADOW_BLUR = 2.0
_SHADOW_OFFSET = 1


@functools.lru_cache(maxsize=1)
def _load_logo():
    """The RGBA master, loaded once per process."""
    from PIL import Image

    if not LOGO_PATH.exists():
        log.warning("brand.logo_missing", path=str(LOGO_PATH))
        return None
    try:
        with Image.open(LOGO_PATH) as im:
            return im.convert("RGBA").copy()
    except Exception as exc:  # noqa: BLE001 - branding must never break ingest
        log.warning("brand.logo_unreadable", error=str(exc)[:200])
        return None


@functools.lru_cache(maxsize=64)
def _scaled_logo(width: int):
    """Logo resized to `width`, with its drop shadow baked in.

    Cached per width: the corpus collapses onto a handful of output sizes, so
    this runs a few dozen times across a 170k-image run rather than 170k.
    """
    from PIL import Image, ImageFilter

    logo = _load_logo()
    if logo is None:
        return None

    ratio = width / logo.width
    height = max(1, round(logo.height * ratio))
    scaled = logo.resize((width, height), Image.LANCZOS)

    # Shadow = the logo's own alpha, blurred and darkened, offset a touch.
    pad = int(_SHADOW_BLUR * 3) + _SHADOW_OFFSET
    canvas = Image.new("RGBA", (width + pad * 2, height + pad * 2), (0, 0, 0, 0))

    alpha = scaled.split()[-1]
    shadow = Image.new("RGBA", scaled.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda v: int(v * _SHADOW_ALPHA)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(_SHADOW_BLUR))
    canvas.alpha_composite(shadow, (pad + _SHADOW_OFFSET, pad + _SHADOW_OFFSET))

    if _LOGO_ALPHA < 1.0:
        scaled.putalpha(scaled.split()[-1].point(lambda v: int(v * _LOGO_ALPHA)))
    canvas.alpha_composite(scaled, (pad, pad))
    return canvas


def apply_branding(image):
    """Composite the homzrealtor mark in the centre. Takes and returns a PIL RGB image.

    Centred rather than tucked in a corner: it is the position a viewer reads
    as ownership, it is harder to crop off, and it coincides with where both
    portals place their own marks, so any residue left by the de-watermarker
    ends up beneath it.

    Returns the image untouched when branding is disabled, the logo is
    unavailable, or the frame is too small to carry a legible mark — a
    thumbnail with a badge over a third of it is worse than an unbranded one.
    """
    if not settings.image_brand:
        return image

    width, height = image.size
    if min(width, height) < settings.image_brand_min_edge:
        return image

    target = int(width * _LOGO_WIDTH_RATIO)
    target = max(_MIN_LOGO_WIDTH, min(_MAX_LOGO_WIDTH, target))

    mark = _scaled_logo(target)
    if mark is None:
        return image
    if mark.width >= width or mark.height >= height:
        return image

    margin = max(_MIN_MARGIN, int(min(width, height) * _MARGIN_RATIO))
    x = (width - mark.width) // 2
    y = (height - mark.height) // 2
    x = min(max(margin, x), width - mark.width - margin)
    y = min(max(margin, y), height - mark.height - margin)

    out = image.convert("RGBA")
    out.alpha_composite(mark, (max(0, x), max(0, y)))
    return out.convert("RGB")
