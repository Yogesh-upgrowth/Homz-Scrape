"""Export processed images for inspection — on disk and as a review page.

Two outputs, because they answer different questions:

* `data/verify/index.html` — every image on one page, grouped by property and
  filterable by watermark outcome, for scanning the whole batch quickly.
* `data/verify/<source>/<property>/NN_<status>.webp` — full-resolution files,
  with `--files`. Worth having when judging whether a faint watermark trace
  survives, which a thumbnail cannot settle.

Images hosted on Blob are referenced by URL rather than embedded, which keeps
the page a few hundred KB instead of tens of MB and lets the browser load
full-resolution files straight from the CDN. Images still held inline in
MongoDB are embedded as data URIs, so this works either way.

Usage:
    python scripts/export_image_review.py
    python scripts/export_image_review.py --status left,no-mark   # risk cases
    python scripts/export_image_review.py --source squareyards --limit 40
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from PIL import Image  # noqa: E402

from homz.db.mongo import close_client, get_database  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "verify"
COLLECTION = "property_images"
SAFE = re.compile(r"[^A-Za-z0-9._-]")
#: Total budget for embedded thumbnails. The page must stay openable.
THUMB_BUDGET = 11 * 1024 * 1024


def status_of(img: dict) -> str:
    wm = img.get("watermark_removed")
    return "removed" if wm else ("no-mark" if wm is None else "left")


def thumb(data: bytes, width: int, quality: int) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def _thumb_src(img: dict, width: int, quality: int) -> str:
    """Where the page should load this image from.

    A Blob-hosted image is referenced by its URL: the browser fetches it from
    the CDN, so the page stays small and shows full resolution. Only images
    still stored as bytes in MongoDB need embedding.
    """
    if img.get("data") is None and img.get("url"):
        return img["url"]
    return ("data:image/jpeg;base64,"
            + base64.b64encode(thumb(bytes(img["data"]), width, quality)).decode())


async def main(sources, statuses, limit, write_files) -> None:
    db = get_database()
    query: dict = {}
    if len(sources) == 1:
        query["source"] = sources[0]
    cursor = db[COLLECTION].find(query).sort("_id", 1)
    if limit:
        cursor = cursor.limit(limit)

    props, total_imgs, kept_imgs = [], 0, 0
    async for doc in cursor:
        entries = []
        for img in doc.get("images", []):
            total_imgs += 1
            st = status_of(img)
            if statuses and st not in statuses:
                continue
            entries.append((img, st))
            kept_imgs += 1
        if entries:
            props.append((doc, entries))
    await close_client()

    if not props:
        print("nothing matched")
        return
    print(f"{len(props)} properties, {kept_imgs} images "
          f"(of {total_imgs} stored)")

    if write_files:
        OUT.mkdir(parents=True, exist_ok=True)
        written = 0
        for doc, entries in props:
            slug = SAFE.sub("_", f"{doc.get('name') or doc['_id']}")[:60]
            folder = OUT / doc["source"] / f"{slug}_{SAFE.sub('_', str(doc['_id']))[:40]}"
            folder.mkdir(parents=True, exist_ok=True)
            for img, st in entries:
                if img.get("data") is None:
                    continue    # hosted on Blob; fetch from the URL instead
                (folder / f"{img['idx']:02d}_{st}.webp").write_bytes(bytes(img["data"]))
                written += 1
        print(f"wrote {written} full-resolution files under {OUT}")

    hosted = sum(1 for _, es in props for i, _ in es if i.get("data") is None)
    width, quality = 420, 76
    if hosted < kept_imgs:
        # Some images are still inline; size their thumbnails to a budget.
        for width, quality in ((300, 74), (260, 70), (220, 66), (180, 60), (150, 55)):
            sample = [(i, st) for _, es in props[:6] for i, st in es[:4]
                      if i.get("data") is not None]
            if not sample:
                break
            est = sum(len(thumb(bytes(i["data"]), width, quality)) for i, _ in sample)
            projected = (est / len(sample)) * (kept_imgs - hosted) * 1.37
            if projected <= THUMB_BUDGET:
                break
    print(f"{hosted} images referenced by URL, {kept_imgs - hosted} embedded "
          f"at {width}px q{quality}")

    cards = []
    for doc, entries in props:
        tiles = []
        for img, st in entries:
            src = _thumb_src(img, width, quality)
            tiles.append(
                f'<figure class="t" data-st="{st}" data-src="{doc["source"]}">'
                f'<img loading="lazy" src="{html.escape(src)}" alt="">'
                f'<figcaption><span class="dot {st}"></span>'
                f'<span class="mono">{img["width"]}&times;{img["height"]}</span>'
                f'<span class="mono dim">{img["bytes"] // 1024}K</span></figcaption>'
                f'</figure>')
        cards.append(
            f'<section class="p" data-src="{doc["source"]}">'
            f'<h3>{html.escape(str(doc.get("name") or doc["_id"]))}'
            f'<span class="src">{html.escape(doc["source"])}</span></h3>'
            f'<p class="loc">{html.escape(str(doc.get("location") or ""))} '
            f'<span class="mono dim">{html.escape(str(doc["_id"]))}</span></p>'
            f'<div class="g">{"".join(tiles)}</div></section>')

    counts = {"removed": 0, "no-mark": 0, "left": 0}
    for _, es in props:
        for _, st in es:
            counts[st] += 1

    page = _PAGE.replace("{{CARDS}}", "".join(cards)) \
                .replace("{{NPROP}}", str(len(props))) \
                .replace("{{NIMG}}", str(kept_imgs)) \
                .replace("{{NREM}}", str(counts["removed"])) \
                .replace("{{NNONE}}", str(counts["no-mark"])) \
                .replace("{{NLEFT}}", str(counts["left"]))
    OUT.mkdir(parents=True, exist_ok=True)
    index = OUT / "index.html"
    index.write_text(page, encoding="utf-8")
    print(f"page: {index}  ({index.stat().st_size / 1024 / 1024:.1f} MB)")


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Processed image review</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ec;--ink:#16181d;--ink2:#626a76;
 --ink3:#8a93a0;--acc:#b4531f;--ok:#17795e;--warn:#b3261e;color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--bg:#101215;--card:#191c21;--line:#2b3037;
 --ink:#e9eaec;--ink2:#a2aab6;--ink3:#79818d;--acc:#e3813f;--ok:#4fc79c;--warn:#f08b83}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}
.dim{color:var(--ink3)}
header{padding:24px 20px 16px;border-bottom:1px solid var(--line)}
h1{font-size:21px;margin:0 0 6px;letter-spacing:-.02em}
.sum{color:var(--ink2);margin:0}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
 padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--ink2);
 font:inherit;font-size:12.5px;padding:6px 12px;border-radius:7px;cursor:pointer}
button[aria-pressed="true"]{background:var(--acc);border-color:var(--acc);color:#fff}
.wrap{padding:18px 20px 60px}
.p{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:14px;margin:0 0 14px}
.p h3{font-size:15px;margin:0;display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.src{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--acc)}
.loc{margin:2px 0 10px;color:var(--ink2);font-size:12.5px}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.t{margin:0;background:var(--bg);border:1px solid var(--line);border-radius:7px;overflow:hidden;cursor:zoom-in}
.t img{display:block;width:100%;height:auto}
figcaption{display:flex;gap:6px;align-items:center;padding:5px 7px;border-top:1px solid var(--line)}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.dot.removed{background:var(--ok)}.dot.no-mark{background:var(--acc)}.dot.left{background:var(--warn)}
.lb{position:fixed;inset:0;background:rgba(8,10,14,.93);display:none;
 align-items:center;justify-content:center;z-index:50;padding:20px;cursor:zoom-out}
.lb.on{display:flex}.lb img{max-width:100%;max-height:100%;image-rendering:auto}
[hidden]{display:none!important}
</style></head><body>
<header>
  <h1>Processed image review</h1>
  <p class="sum">{{NPROP}} properties &middot; {{NIMG}} images &middot;
  <b>{{NREM}}</b> watermark removed &middot; {{NNONE}} no mark found &middot;
  <b>{{NLEFT}}</b> mark left</p>
</header>
<div class="bar">
  <button data-f="all" aria-pressed="true">All</button>
  <button data-f="removed" aria-pressed="false">Removed</button>
  <button data-f="no-mark" aria-pressed="false">No mark</button>
  <button data-f="left" aria-pressed="false">Mark left</button>
  <span style="width:12px"></span>
  <button data-s="all" aria-pressed="true">Both sources</button>
  <button data-s="magicbricks" aria-pressed="false">MagicBricks</button>
  <button data-s="squareyards" aria-pressed="false">SquareYards</button>
</div>
<div class="wrap">{{CARDS}}</div>
<div class="lb" id="lb"><img alt=""></div>
<script>
(function(){
 var f='all',s='all';
 function apply(){
  document.querySelectorAll('.t').forEach(function(t){
   var ok=(f==='all'||t.dataset.st===f)&&(s==='all'||t.dataset.src===s);
   t.hidden=!ok;
  });
  document.querySelectorAll('.p').forEach(function(p){
   p.hidden=!p.querySelector('.t:not([hidden])');
  });
 }
 document.querySelectorAll('button[data-f]').forEach(function(b){
  b.addEventListener('click',function(){
   f=b.dataset.f;
   document.querySelectorAll('button[data-f]').forEach(function(x){
    x.setAttribute('aria-pressed',String(x===b));});
   apply();});
 });
 document.querySelectorAll('button[data-s]').forEach(function(b){
  b.addEventListener('click',function(){
   s=b.dataset.s;
   document.querySelectorAll('button[data-s]').forEach(function(x){
    x.setAttribute('aria-pressed',String(x===b));});
   apply();});
 });
 var lb=document.getElementById('lb');
 document.addEventListener('click',function(e){
  var t=e.target.closest('.t');
  if(t){lb.querySelector('img').src=t.querySelector('img').src;lb.classList.add('on');}
  else if(e.target.closest('.lb')){lb.classList.remove('on');}
 });
 document.addEventListener('keydown',function(e){
  if(e.key==='Escape')lb.classList.remove('on');});
})();
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="both",
                    choices=["magicbricks", "squareyards", "both"])
    ap.add_argument("--status", default="",
                    help="comma list: removed,no-mark,left (default all)")
    ap.add_argument("--limit", type=int, default=None, help="max properties")
    ap.add_argument("--files", action="store_true",
                    help="also write full-resolution files (inline images only)")
    a = ap.parse_args()
    srcs = ["magicbricks", "squareyards"] if a.source == "both" else [a.source]
    sts = [s.strip() for s in a.status.split(",") if s.strip()]
    asyncio.run(main(srcs, sts, a.limit, a.files))
