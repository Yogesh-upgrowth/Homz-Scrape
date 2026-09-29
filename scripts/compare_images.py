"""See exactly what the image pipeline does to a photo, before vs after.

Runs real images through the real `_process_bytes` path — the same
de-watermark, EXIF, resize, brand and WebP encode the full run uses — and
writes a browsable report so the result can be judged by eye rather than by a
number.

For each image it emits four views, because each answers a different question:

* **side**  — before | after, whole frame. "Does the output look right?"
* **zoom**  — the watermark box at 3x, before above after. "Did the mark go?"
* **diff**  — |after - before|, amplified 6x. "What exactly was touched?"
  This is the one that catches a removal firing in the wrong place: a clean
  removal lights up only the mark, while a misfire lights up a rectangle of
  untouched scenery.
* **brand** — the bottom-right corner at 3x. "Is the logo legible here?"

...plus an `index.html` tying them together, which is the thing to actually
open.

Usage:
    python scripts/compare_images.py --source magicbricks --count 10
    python scripts/compare_images.py --source squareyards --count 10
    python scripts/compare_images.py --property magicbricks:4d423334383139353431
    python scripts/compare_images.py --url https://img.staticmb.com/.../x.jpg
    python scripts/compare_images.py --source magicbricks --count 6 --open
"""

from __future__ import annotations

import argparse
import asyncio
import html
import io
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.images.ingest import _process_bytes  # noqa: E402
from homz.images.watermark import remove_watermark  # noqa: E402

OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "compare"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERERS = {
    "magicbricks": "https://www.magicbricks.com/",
    "squareyards": "https://www.squareyards.com/",
}
ZOOM = 3
DIFF_GAIN = 6


def _source_of(url: str) -> str:
    if "staticmb.com" in url or "magicbricks" in url:
        return "magicbricks"
    if "squareyards" in url:
        return "squareyards"
    return "unknown"


def _stack(top: Image.Image, bottom: Image.Image, gap: int = 6) -> Image.Image:
    w = max(top.width, bottom.width)
    out = Image.new("RGB", (w, top.height + bottom.height + gap), (255, 255, 255))
    out.paste(top, (0, 0))
    out.paste(bottom, (0, top.height + gap))
    return out


def _beside(left: Image.Image, right: Image.Image, gap: int = 8) -> Image.Image:
    h = max(left.height, right.height)
    out = Image.new("RGB", (left.width + right.width + gap, h), (255, 255, 255))
    out.paste(left, (0, 0))
    out.paste(right, (left.width + gap, 0))
    return out


def _mark_box(before: np.ndarray, source: str, url: str) -> tuple[int, int, int, int] | None:
    """Where the de-watermarker actually touched, so the zoom lands on it."""
    result = remove_watermark(before, source, url)
    if not result.removed:
        return None
    diff = np.abs(np.asarray(result.image).astype(int) - before.astype(int)).sum(2)
    ys, xs = np.where(diff > 2)
    if not len(ys):
        return None
    pad = 24
    h, w = before.shape[:2]
    return (
        max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
        min(w, int(xs.max()) + pad), min(h, int(ys.max()) + pad),
    )


def _render(idx: int, url: str, raw: bytes, out_dir: Path) -> dict | None:
    source = _source_of(url)
    try:
        before = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        print(f"  [{idx}] decode failed: {type(exc).__name__}")
        return None

    before_arr = np.asarray(before)
    try:
        encoded, _w, _h, dewm = _process_bytes(raw, source, url)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{idx}] process failed: {type(exc).__name__}: {exc}")
        return None
    after = Image.open(io.BytesIO(encoded)).convert("RGB")

    names: dict[str, str] = {}

    # 1. whole frame, side by side
    side = _beside(before, after.resize(before.size, Image.LANCZOS))
    if side.width > 1800:
        side = side.resize((1800, round(side.height * 1800 / side.width)), Image.LANCZOS)
    names["side"] = f"{idx:02d}_side.png"
    side.save(out_dir / names["side"])

    # 2. the watermark region, zoomed. Compared at the ORIGINAL resolution so
    #    the resize does not blur away what we are trying to inspect.
    box = _mark_box(before_arr, source, url)
    if box:
        result = remove_watermark(before_arr, source, url)
        cleaned = Image.fromarray(np.asarray(result.image))
        cb, ca = before.crop(box), cleaned.crop(box)
        z = (cb.width * ZOOM, cb.height * ZOOM)
        names["zoom"] = f"{idx:02d}_zoom.png"
        _stack(cb.resize(z, Image.LANCZOS), ca.resize(z, Image.LANCZOS)).save(
            out_dir / names["zoom"]
        )

        # 3. amplified difference — shows precisely which pixels moved
        diff = np.abs(np.asarray(cleaned).astype(int) - before_arr.astype(int))
        diff = np.clip(diff * DIFF_GAIN, 0, 255).astype(np.uint8)
        names["diff"] = f"{idx:02d}_diff.png"
        Image.fromarray(diff).crop(box).resize(z, Image.NEAREST).save(
            out_dir / names["diff"]
        )

    # 4. the branded corner
    bw, bh = min(440, after.width), min(190, after.height)
    corner = after.crop((after.width - bw, after.height - bh, after.width, after.height))
    names["brand"] = f"{idx:02d}_brand.png"
    corner.resize((bw * 2, bh * 2), Image.LANCZOS).save(out_dir / names["brand"])

    status = ("watermark removed" if dewm else
              "no mark found — left as-is" if dewm is None else
              "mark expected but NOT removed")
    print(f"  [{idx}] {before.size} -> {after.size}  {status}")
    return {
        "idx": idx, "url": url, "source": source, "status": status,
        "before": f"{before.size[0]}x{before.size[1]}",
        "after": f"{after.size[0]}x{after.size[1]}",
        "kb": len(encoded) // 1024, "names": names,
    }


def _write_index(out_dir: Path, rows: list[dict]) -> Path:
    cards = []
    for r in rows:
        n = r["names"]
        imgs = [f'<h4>whole image &mdash; left: BEFORE, right: AFTER</h4>'
                f'<img src="{n["side"]}">']
        if "zoom" in n:
            imgs.append('<h4>watermark area &mdash; top: BEFORE, bottom: AFTER</h4>'
                        f'<img src="{n["zoom"]}">')
        if "diff" in n:
            imgs.append(f'<h4>what changed (amplified {DIFF_GAIN}x) &mdash; only the '
                        'mark should light up</h4>'
                        f'<img src="{n["diff"]}">')
        imgs.append('<h4>homzrealtor logo</h4>'
                    f'<img src="{n["brand"]}">')
        cards.append(
            f'<section><h2>#{r["idx"]:02d} &mdash; {html.escape(r["source"])}</h2>'
            f'<p class="meta">{r["before"]} &rarr; {r["after"]} &middot; {r["kb"]} KB '
            f'&middot; <b>{html.escape(r["status"])}</b></p>'
            f'<p class="url">{html.escape(r["url"])}</p>'
            + "".join(imgs) + "</section>"
        )
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Image pipeline: before vs after</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; padding: 24px;
        max-width: 1180px; background: #fafafa; color: #16181d; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #14161a; color: #e8e8ea; }}
   section {{ background: #1c1f25 !important; border-color: #2a2e36 !important; }}
 }}
 h1 {{ font-size: 22px; }}
 section {{ background: #fff; border: 1px solid #e3e6ec; border-radius: 10px;
            padding: 18px; margin: 0 0 22px; }}
 h2 {{ font-size: 17px; margin: 0 0 4px; }}
 h4 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
       color: #6b7280; margin: 16px 0 6px; }}
 .meta {{ margin: 0 0 4px; }}
 .url {{ font: 11px/1.4 ui-monospace, monospace; color: #6b7280;
         word-break: break-all; margin: 0 0 8px; }}
 img {{ max-width: 100%; border: 1px solid #e3e6ec; border-radius: 6px;
        display: block; background: #fff; }}
 b {{ color: #b4531f; }}
</style></head><body>
<h1>Image pipeline &mdash; before vs after</h1>
<p>{len(rows)} images &middot; generated {datetime.now():%Y-%m-%d %H:%M}</p>
{"".join(cards)}
</body></html>"""
    path = out_dir / "index.html"
    path.write_text(doc, encoding="utf-8")
    return path


async def _collect(args) -> list[str]:
    if args.url:
        return [args.url]

    db = get_database()
    try:
        if args.property:
            doc = await db[D.PROPERTIES].find_one({"_id": args.property}, {"images": 1})
            if not doc:
                print(f"no such property: {args.property}")
                return []
            return [i["url"] for i in (doc.get("images") or [])][: args.count]

        match: dict = {"source": args.source, "images.0": {"$exists": True}}
        if args.marked:
            match["images.url"] = {
                "$regex": "cropped_images" if args.source == "magicbricks" else "/resources/"
            }
        urls: list[str] = []
        cursor = db[D.PROPERTIES].aggregate([
            {"$match": match},
            {"$sample": {"size": max(60, args.count * 6)}},
            {"$project": {"u": "$images.url"}},
        ])
        async for row in cursor:
            for u in row.get("u") or []:
                if args.marked:
                    needle = "cropped_images" if args.source == "magicbricks" else "/resources/"
                    if needle not in u:
                        continue
                urls.append(u)
        return list(dict.fromkeys(urls))[: args.count]
    finally:
        await close_client()


async def main(args) -> None:
    urls = await _collect(args)
    if not urls:
        print("no images found")
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(urls)} images -> {out_dir}\n")

    rows = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for i, url in enumerate(urls, start=1):
            headers = {"User-Agent": UA, "Referer": REFERERS.get(_source_of(url), "")}
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                print(f"  [{i}] fetch failed: {type(exc).__name__}")
                continue
            if resp.status_code != 200:
                print(f"  [{i}] fetch failed: HTTP {resp.status_code}")
                continue
            row = _render(i, url, resp.content, out_dir)
            if row:
                rows.append(row)

    if not rows:
        print("\nnothing rendered")
        return
    index = _write_index(out_dir, rows)
    print(f"\nOpen this in a browser:\n  {index}")

    if args.open:
        import webbrowser

        webbrowser.open(index.as_uri())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="magicbricks",
                    choices=["magicbricks", "squareyards"])
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--property", help="a specific _id, e.g. magicbricks:4d4233...")
    ap.add_argument("--url", help="a single image URL")
    ap.add_argument("--marked", action="store_true", default=True,
                    help="only sample the URL family that carries a watermark")
    ap.add_argument("--any", dest="marked", action="store_false",
                    help="sample all images, including unmarked families")
    ap.add_argument("--open", action="store_true", help="open the report when done")
    asyncio.run(main(ap.parse_args()))
