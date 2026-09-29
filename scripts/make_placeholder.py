"""Build and publish the "images coming soon" placeholder.

Properties whose photos have not been processed yet still carry the portal's
URLs, and those images have the portal's watermark burnt into them. Serving
them would put a competitor's brand on the site, so those listings show this
placeholder instead until their own images exist.

One image, uploaded once, referenced by every such listing — the CDN caches a
single file rather than tens of thousands of hotlinks.

Usage:
    python scripts/make_placeholder.py
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from homz.images.blobstore import BlobStore, url_for  # noqa: E402

# 3:2, the aspect most listing cards render at.
SIZE = (1200, 800)
BG = (243, 244, 246)
INK = (107, 114, 128)
ACCENT = (180, 83, 31)
LOGO = Path(__file__).resolve().parent.parent / "src" / "homz" / "images" / "assets" / "homzrealtor-logo.png"
OUT = Path(__file__).resolve().parent.parent / "data" / "placeholder.webp"


def _font(size: int, bold: bool = False):
    for name in (("arialbd.ttf", "segoeuib.ttf") if bold else ("arial.ttf", "segoeui.ttf")):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def build() -> bytes:
    im = Image.new("RGB", SIZE, BG)
    d = ImageDraw.Draw(im)
    w, h = SIZE

    # A soft frame, so the placeholder reads as deliberate rather than broken.
    d.rounded_rectangle([60, 60, w - 60, h - 60], radius=18,
                        outline=(222, 226, 232), width=3)

    title = "Images coming soon"
    f1 = _font(58, bold=True)
    tw = d.textlength(title, font=f1)
    d.text(((w - tw) / 2, h / 2 - 96), title, font=f1, fill=(70, 76, 86))

    sub = "Photography for this property is being prepared"
    f2 = _font(27)
    sw = d.textlength(sub, font=f2)
    d.text(((w - sw) / 2, h / 2 - 18), sub, font=f2, fill=INK)

    d.rounded_rectangle([(w - 96) / 2, h / 2 + 34, (w + 96) / 2, h / 2 + 40],
                        radius=3, fill=ACCENT)

    if LOGO.exists():
        with Image.open(LOGO) as logo:
            logo = logo.convert("RGBA")
            target = 190
            logo = logo.resize((target, max(1, round(logo.height * target / logo.width))),
                               Image.LANCZOS)
            # The master is white, for dark headers; darken it for this light ground.
            px = logo.load()
            for y in range(logo.height):
                for x in range(logo.width):
                    r, g, b, a = px[x, y]
                    if a:
                        px[x, y] = (96, 102, 112, a)
            im.paste(logo, ((w - logo.width) // 2, int(h / 2 + 86)), logo)

    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=88, method=5)
    return buf.getvalue()


async def main() -> None:
    payload = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(payload)
    print(f"built {OUT} ({len(payload) / 1024:.0f} KB, {SIZE[0]}x{SIZE[1]})")

    blob = BlobStore()
    if not blob.enabled:
        print("no HOMZ_BLOB_READ_WRITE_TOKEN — built locally, not published")
        return
    async with httpx.AsyncClient(timeout=60) as client:
        path, digest, was_new = await blob.put(client, payload)
    print(f"{'uploaded' if was_new else 'already present'}: {url_for(path)}")
    print(f"\nadd to .env so the feed can reference it:\n  HOMZ_PLACEHOLDER_PATH={path}")


if __name__ == "__main__":
    asyncio.run(main())
