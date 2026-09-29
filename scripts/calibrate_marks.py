"""Calibrate every portal watermark, one output size at a time.

## Why this replaces the seed-template approach

The previous calibrator cut one shape template from whichever size had most
samples, then matched it against every other size to locate the mark. That
works only while the mark is the same shape everywhere, and it is not:
surveyed across the live corpus the wordmark runs from 20% of frame width
(MagicBricks 450x600, SquareYards 800x600) to 57% (SquareYards 1600x900),
with different aspect ratios. Sizes whose mark did not match the seed scored
below threshold and were dropped as "no watermark" — which is how a plainly
visible mark on MagicBricks 900x600 came to be classified as absent.

Here each size is calibrated from its own evidence and nothing is compared
across sizes:

1. **Median-stack** the high-pass of many frames of that exact size. Scene
   content differs between listings and cancels; a watermark is identical in
   every frame and survives. This is the one robust primitive.
2. **Find the mark as a blob** in that residual, inside the region the
   placement predicts, and accept it on its own geometry (a wordmark is wide,
   short, and a plausible fraction of the frame) plus its prominence over the
   surrounding noise.
3. **Estimate alpha by variance.** An alpha blend compresses the variance of
   whatever is underneath: std(obs) = (1-a)*std(I). Pixels under opaque ink
   barely vary while unmarked pixels keep the scene's full variance, so the
   ratio gives alpha with no need to guess the hidden content. A median-filter
   regression was tried first and fails on thick strokes, which the filter
   preserves rather than erases.

Sums are accumulated in streaming form, so memory stays flat no matter how
many frames feed in.

Usage:
    python scripts/calibrate_marks.py --source magicbricks --placement centre
    python scripts/calibrate_marks.py --source squareyards --placement centre
    python scripts/calibrate_marks.py --source magicbricks --placement corner
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

import cv2  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "homz" / "images" / "calibration"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SOURCES = {
    "magicbricks": {"regex": "cropped_images", "referer": "https://www.magicbricks.com/"},
    "squareyards": {"regex": "/resources/", "referer": "https://www.squareyards.com/"},
}

#: Where each placement's mark may sit, as fractions of the frame. These must
#: not overlap, or the search for one placement locks onto the other: a first
#: pass with a wide centre window kept returning the bottom-right mark (found
#: at 0.71, 0.74 on 800x600) and calibrated it as if it were the centre one.
#: The centre window therefore stops short of the corner mark's band.
REGIONS = {
    # Narrow crops (269-340px wide) left almost no centre window under a
    # tighter fraction, so the search found nothing at all there.
    "centre": (0.14, 0.26, 0.86, 0.70),
    "corner": (0.42, 0.55, 1.00, 1.00),
    # Layout plans and location maps carry a much larger centred mark than
    # photographs do, so they are calibrated as their own placement over a
    # wider window rather than being averaged in with the photos.
    "plan": (0.08, 0.20, 0.92, 0.80),
}

#: A wordmark is wide and short. These bounds reject scene structure that
#: survives the median (a horizon, a repeated railing) without being so tight
#: they exclude a legitimately large mark.
MIN_WIDTH_FRAC, MAX_WIDTH_FRAC = 0.08, 0.80
MIN_ASPECT, MAX_ASPECT = 1.0, 22.0
MIN_AREA = 260
#: Blob mean residual divided by the region's own noise. A real mark stands
#: well clear of the scene leftovers.
MIN_PROMINENCE = 1.9
CONCURRENCY = 14
PAD = 14


def is_plan(rgb) -> bool:
    """Line-art layout plan or location map, rather than a photograph.

    Measured over the corpus the two classes separate cleanly: plans run
    above 0.82 for the fraction of near-white pixels while photographs sit
    below 0.05, because a plan is ink on paper and a photograph is not. This
    matters because the two carry different watermarks, and stacking them
    together averages one mark away.
    """
    g = rgb.mean(axis=2)
    bright = float((g > 205).mean())
    sat = float((rgb.max(2).astype(np.int16) - rgb.min(2).astype(np.int16)).mean())
    return bright > 0.45 and sat < 45


def _region_px(placement, w, h):
    fx0, fy0, fx1, fy1 = REGIONS[placement]
    return int(w * fx0), int(h * fy0), int(w * fx1), int(h * fy1)


def _stack(arrs, pct=50):
    """Per-pixel percentile of the high-pass across frames.

    The median is right when every frame carries the mark, and wrong when only
    some do: MagicBricks marks a minority of frames dead centre, and a median
    over the whole population erases exactly the signal being measured, which
    is how a visible mark came to be scored as absent. A high percentile keeps
    a minority signal while still rejecting per-frame scene detail, which is
    uncorrelated between listings and so cannot survive at any percentile.
    """
    acc = [a.astype(np.float32) - cv2.GaussianBlur(a.astype(np.float32), (0, 0), 9)
           for a in arrs]
    h, w = acc[0].shape
    out = np.empty((h, w), np.float32)
    # Band the reduction: stacking 130 frames of 1600x800 in one array wants
    # 635 MB, which the larger SquareYards renders exhaust. Bands keep peak
    # memory to a few MB and give an identical result.
    rows = max(1, int(6_000_000 / max(w * len(acc), 1)))
    for y in range(0, h, rows):
        y1 = min(h, y + rows)
        band = np.stack([a[y:y1] for a in acc])
        out[y:y1] = (np.median(band, axis=0) if pct == 50
                     else np.percentile(band, pct, axis=0))
        del band
    return out


#: How far the grown box may exceed the seed box in each dimension.
MAX_GROWTH = 1.9


def _clamp_growth(seed, grown):
    """Trim the grown box to a bounded expansion around the seed."""
    sx, sy, sw, sh = seed
    gx, gy, gw, gh = grown
    max_w, max_h = int(sw * MAX_GROWTH), int(sh * MAX_GROWTH)
    if gw > max_w:
        cx = sx + sw / 2
        gx = int(max(gx, cx - max_w / 2))
        gw = max_w
    if gh > max_h:
        cy = sy + sh / 2
        gy = int(max(gy, cy - max_h / 2))
        gh = max_h
    return gx, gy, gw, gh


def _find_mark(med, placement, w, h):
    """Locate the mark as a blob in the residual. Returns (box, prominence).

    Two thresholds, not one. A single high threshold finds only the brightest
    core of the mark and the box clips the rest: on SquareYards 1600x800 it
    returned 231x83 for a mark that is really 320x140, so removal erased the
    middle and left the edges of the wordmark plainly visible while still
    reporting success. So the high threshold only *seeds* the search, and the
    box is then grown through everything connected to that seed above a much
    lower threshold, which follows the mark out to its faint extremities.
    """
    x0, y0, x1, y1 = _region_px(placement, w, h)
    reg = med[y0:y1, x0:x1]
    if reg.size < 400:
        return None, 0.0
    noise = float(reg.std()) or 1e-6
    hi = max(float(np.percentile(reg, 99.0)), noise * 3.0)
    lo = max(float(np.percentile(reg, 92.0)), noise * 1.25)

    seed = (reg >= hi).astype(np.uint8)
    seed = cv2.morphologyEx(seed, cv2.MORPH_CLOSE, np.ones((7, 31), np.uint8))
    grow = (reg >= lo).astype(np.uint8)
    grow = cv2.morphologyEx(grow, cv2.MORPH_CLOSE, np.ones((9, 41), np.uint8))

    n_g, lab_g, stats_g, _ = cv2.connectedComponentsWithStats(grow, 8)
    n_s, lab_s, stats_s, _ = cv2.connectedComponentsWithStats(seed, 8)
    if n_g < 2 or n_s < 2:
        return None, 0.0

    best = None
    for i in range(1, n_s):
        if stats_s[i, cv2.CC_STAT_AREA] < MIN_AREA:
            continue
        # Which grown component does this seed sit inside?
        ys, xs = np.where(lab_s == i)
        comp = np.bincount(lab_g[ys, xs]).argmax()
        if comp == 0:
            continue
        gx, gy, gw, gh, garea = stats_g[comp]
        sx_, sy_, sw_, sh_, _ = stats_s[i]
        # Bound the growth to a multiple of the seed. The low threshold that
        # follows a strong mark out to its faint edges also follows a weak one
        # straight into the scenery: on MagicBricks, whose mark sits near
        # alpha 0.25, an unbounded grow returned 431x218 for a mark that is
        # really 99x19, and inverting that box cut removal from 72% to 27%.
        # Clamping keeps the benefit for strong marks without the runaway.
        gx, gy, gw, gh = _clamp_growth((sx_, sy_, sw_, sh_), (gx, gy, gw, gh))
        if not (MIN_WIDTH_FRAC <= gw / w <= MAX_WIDTH_FRAC):
            continue
        aspect = gw / max(gh, 1)
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            continue
        prom = float(reg[lab_s == i].mean()) / noise
        score = prom * stats_s[i, cv2.CC_STAT_AREA]
        if best is None or score > best[0]:
            best = (score, prom, (gx, gy, gw, gh))

    if best is None:
        return None, 0.0
    _, prom, (bx, by, bw, bh) = best
    fx = max(0, x0 + bx - PAD)
    fy = max(0, y0 + by - PAD)
    fw = min(w - fx, bw + PAD * 2)
    fh = min(h - fy, bh + PAD * 2)
    return (int(fx), int(fy), int(fw), int(fh)), prom


#: Alpha below this is treated as no ink at all and forced to exactly zero.
#: Without it the map carries a low, noisy alpha everywhere the mark is not,
#: and inverting that across a box covering half the frame smears the whole
#: picture while the wordmark itself disappears — visibly worse than leaving
#: the watermark alone. Zeroing makes the inversion a strict no-op off the
#: ink, so the box can be generous without the box itself doing damage.
#:
#: Set high deliberately. The variance estimate is noisy over a large box, and
#: a low cut admits that noise as phantom ink; the mark's real strokes measure
#: 0.45-0.90, so nothing genuine is lost here.
NO_INK_ALPHA = 0.15


def _estimate(mean_obs, mean_sq, slope_floor):
    """(slope, c) from the variance an alpha blend destroys."""
    var = np.maximum(mean_sq - mean_obs * mean_obs, 0.0)
    sigma = np.sqrt(var)
    sigma_ref = np.maximum(np.percentile(sigma, 90, axis=(0, 1), keepdims=True), 1e-6)
    slope = np.clip(sigma / sigma_ref, slope_floor, 1.0)
    alpha = 1.0 - slope

    ink = alpha.mean(axis=2) > NO_INK_ALPHA
    # Keep only ink that forms part of the mark, not isolated noisy pixels.
    ink = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_OPEN,
                           np.ones((2, 2), np.uint8)).astype(bool)

    mask = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8))
    if mask.any():
        base = np.clip(mean_obs, 0, 255).astype(np.uint8)
        clean = cv2.inpaint(base, mask, 7, cv2.INPAINT_TELEA).astype(np.float64)
    else:
        clean = mean_obs
    c = mean_obs - slope * clean

    # Off the ink: identity transform, so those pixels pass through untouched.
    keep = ink[:, :, None]
    slope = np.where(keep, slope, 1.0)
    c = np.where(keep, c, 0.0)
    return slope.astype(np.float32), c.astype(np.float32)


#: Cheap URL prefilter for the plan pass. Plans are a minority of the corpus,
#: so filtering only after download means fetching roughly five images per
#: usable one; these path fragments name the plan families directly and cut
#: that to almost nothing. The pixel test still has the final say.
PLAN_URL_HINTS = ("location-image", "floor-plan", "floor_plan", "master-plan",
                  "masterplan", "site-plan", "siteplan", "layout", "-floor-",
                  "sqft", "cluster-plan")


async def _sample_urls(source, sample, url_hints=()):
    cfg = SOURCES[source]
    db = get_database()
    urls = []
    agg = db[D.PROPERTIES].aggregate([
        {"$match": {"source": source, "images.url": {"$regex": cfg["regex"]}}},
        {"$sample": {"size": sample}}, {"$project": {"u": "$images.url"}}])
    async for r in agg:
        urls += [u for u in (r.get("u") or []) if cfg["regex"].strip("/\\") in u]
    await close_client()
    urls = list(dict.fromkeys(urls))
    if url_hints:
        hinted = [u for u in urls if any(h in u.lower() for h in url_hints)]
        if len(hinted) >= 200:
            return hinted
    return urls


async def _collect(source, urls, cap, global_cap, kind='any'):
    """Grayscale frames grouped by exact size, plus url->size."""
    cfg = SOURCES[source]
    groups = collections.defaultdict(list)
    by_size = collections.defaultdict(list)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    kept = 0
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Referer": cfg["referer"]},
                                 timeout=25, follow_redirects=True) as c:
        async def one(u):
            nonlocal kept
            if kept >= global_cap:
                return
            async with sem:
                if kept >= global_cap:
                    return
                try:
                    r = await c.get(u)
                    if r.status_code != 200:
                        return
                    with Image.open(io.BytesIO(r.content)) as im:
                        im.load()
                        size = im.size
                        if kind != "any":
                            want_plan = kind == "plan"
                            if is_plan(np.asarray(im.convert("RGB"))) != want_plan:
                                return
                        g = np.asarray(im.convert("L"), dtype=np.uint8)
                except Exception:
                    return
                async with lock:
                    by_size[size].append(u)
                    if len(groups[size]) < cap and kept < global_cap:
                        groups[size].append(g)
                        kept += 1
        await asyncio.gather(*[one(u) for u in urls])
    return groups, by_size


async def _accumulate(source, urls, size, box, want, kind='any'):
    """Streaming mean and mean-of-squares over the mark box."""
    cfg = SOURCES[source]
    x, y, bw, bh = box
    n = 0
    s_o = np.zeros((bh, bw, 3), np.float64)
    s_oo = np.zeros((bh, bw, 3), np.float64)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Referer": cfg["referer"]},
                                 timeout=25, follow_redirects=True) as c:
        async def one(u):
            nonlocal n
            if n >= want:
                return
            async with sem:
                if n >= want:
                    return
                try:
                    r = await c.get(u)
                    if r.status_code != 200:
                        return
                    with Image.open(io.BytesIO(r.content)) as im:
                        im.load()
                        if im.size != size:
                            return
                        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                        if kind != "any" and is_plan(arr) != (kind == "plan"):
                            return
                except Exception:
                    return
                crop = arr[y:y + bh, x:x + bw]
                if crop.shape[:2] != (bh, bw):
                    return
                o = crop.astype(np.float64)
                async with lock:
                    if n >= want:
                        return
                    s_o[:] += o
                    s_oo[:] += o * o
                    n += 1
        await asyncio.gather(*[one(u) for u in urls])
    if n < 22:
        return None
    return s_o / n, s_oo / n, n


async def main(source, placement, sample, min_n, cap, slope_floor, pct, kind):
    print(f"source={source} placement={placement} percentile={pct} kind={kind}")
    hints = PLAN_URL_HINTS if kind == 'plan' else ()
    urls = await _sample_urls(source, sample, hints)
    print(f"  {len(urls)} candidate urls")
    print("pass 1: stacking by size ...")
    groups, by_size = await _collect(source, urls, cap,
                                 global_cap=900 if kind != 'any' else 3400,
                                 kind=kind)
    print(f"  {len(groups)} distinct sizes")

    arrays, anchors, rejected = {}, {}, []
    print(f"\npass 2: locating and measuring (min {min_n} frames):")
    for size, arrs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(arrs) < min_n:
            continue
        w, h = size
        key = f"{w}x{h}"
        med = _stack(arrs, pct)
        box, prom = _find_mark(med, placement, w, h)
        if box is None or prom < MIN_PROMINENCE:
            rejected.append(f"{key}(prom {prom:.1f})")
            continue
        got = await _accumulate(source, by_size.get(size, []), size, box, cap, kind)
        if got is None:
            rejected.append(f"{key}(too few crops)")
            continue
        mean_obs, mean_sq, n = got
        slope, c = _estimate(mean_obs, mean_sq, slope_floor)
        alpha = 1.0 - slope
        if float(alpha.max()) < 0.06:
            rejected.append(f"{key}(alpha {alpha.max():.2f})")
            continue
        arrays[f"slope_{key}"] = slope
        arrays[f"c_{key}"] = c
        anchors[key] = {"x": box[0], "y": box[1], "w": box[2], "h": box[3],
                        "prominence": round(prom, 2), "n": n, "placement": placement}
        print(f"  {key:>11s} box {box[2]:4d}x{box[3]:<3d} at ({box[0]},{box[1]}) "
              f"widthfrac={box[2]/w:.2f} prom={prom:4.1f} n={n:3d} "
              f"alpha_max={alpha.max():.3f}")

    if not anchors:
        print("\nFAILED: nothing calibrated.")
        sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{source}.{placement}"
    # float16 halves the shipped calibration; these are blend
    # coefficients in [0,1] and [0,255], well inside its precision.
    np.savez_compressed(OUT_DIR / f"{stem}.npz",
                        **{k: v.astype(np.float16) for k, v in arrays.items()})
    (OUT_DIR / f"{stem}.json").write_text(json.dumps(anchors, indent=2), encoding="utf-8")
    kb = (OUT_DIR / f"{stem}.npz").stat().st_size / 1024
    print(f"\nwrote {len(anchors)} sizes -> {stem}.npz ({kb:.0f} KB)")
    if rejected:
        print(f"no mark at {len(rejected)} sizes: " + ", ".join(rejected[:14]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--placement", required=True, choices=sorted(REGIONS))
    ap.add_argument("--sample", type=int, default=3000)
    ap.add_argument("--min-n", type=int, default=22)
    ap.add_argument("--cap", type=int, default=150)
    ap.add_argument("--slope-floor", type=float, default=0.10)
    ap.add_argument("--kind", default="any", choices=["any", "plan", "photo"],
                    help="restrict to line-art plans or to photographs")
    ap.add_argument("--percentile", type=int, default=50,
                    help="stack percentile; >50 finds marks only some frames carry")
    a = ap.parse_args()
    asyncio.run(main(a.source, a.placement, a.sample, a.min_n, a.cap, a.slope_floor,
                     a.percentile, a.kind))
