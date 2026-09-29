"""Derive portal watermark parameters from the live corpus.

Writes `src/homz/images/calibration/<profile>.npz` + `.json`, which
`homz.images.watermark` loads at runtime. Re-run if a portal changes its
mark; nothing else needs to change.

Three stages per output size:

1. **Median-residual stack.** Each sampled image is high-pass filtered
   (subtract a wide Gaussian) and the per-pixel median taken across the
   stack. Scene content is uncorrelated between listings and cancels; the
   watermark is identical in every one and survives. This is what makes the
   mark visible enough to locate at all — in any single image it sits at a
   few percent contrast and is swamped by texture.

2. **Anchor + scale search.** Cross-correlate a normalized shape template
   against that residual over a range of scales. A genuine hit scores >0.70;
   sizes that score low (and whose best scale pins to the edge of the search
   range, the signature of a spurious match) carry no watermark and are
   excluded rather than guessed at.

3. **Per-pixel regression** of observed crops against a median-filtered
   estimate of their own content, recovering `slope = 1-alpha` and
   `c = alpha*W`, accumulated in streaming form so memory stays flat. See
   `homz.images.watermark` for why regression rather than a direct ratio.

Only a bounded window of each frame is retained in pass A — the bottom-right
corner for MagicBricks, the centre for SquareYards — because the portals
serve continuously-varying crop sizes (435+ distinct dimensions for
MagicBricks alone) and holding whole RGB frames for all of them exhausts
memory.

Usage:
    python scripts/calibrate_watermark.py --profile magicbricks.corner
    python scripts/calibrate_watermark.py --profile magicbricks.centre
    python scripts/calibrate_watermark.py --profile squareyards.centre

A profile is one *placement* of one portal's mark. MagicBricks uses two:
the original bottom-right wordmark, and a second copy dead centre on frames
that carry no corner mark. They are calibrated separately because their
anchors, scales and alpha maps are all different.
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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MIN_SCORE = 0.70
CONCURRENCY = 12

# Per-source geometry. `window` bounds what pass A retains; `tpl` is the
# template box the shape seed is cut to, sized to the mark plus padding.
PROFILES: dict[str, dict] = {
    "magicbricks.corner": {
        "source": "magicbricks",
        # Only `cropped_images/` carries the mark. `mbimages/project/` renders
        # are clean (verified by median stack), and `user/`+`topagent/` are
        # profile headshots the junk filter drops before this ever runs.
        "url_regex": "cropped_images",
        "referer": "https://www.magicbricks.com/",
        "anchor": "corner",
        "tpl": (125, 46),
        "window": (420, 320),
        "scales": [round(s, 3) for s in np.arange(0.60, 1.45, 0.025)],
        "blob_bounds": (40, 220, 8, 60),  # min/max width, min/max height
        # The median-filter regression under-read alpha here and left a
        # visible light ghost of the wordmark. The variance estimator does
        # not have to guess the hidden content, so it recovers the true
        # alpha and the mark comes out properly.
        "slope_floor": 0.45,
        "estimator": "variance",
    },
    "magicbricks.centre": {
        "source": "magicbricks",
        # A SECOND placement: the same wordmark, dead centre (cx/W 0.50,
        # cy/H 0.50), on frames that carry no corner mark. Stacking every
        # frame of a size hides it, because the centre variant is a minority
        # and the median suppresses a minority signal — hence `prefilter`.
        "url_regex": "cropped_images",
        "referer": "https://www.magicbricks.com/",
        "anchor": "centre",
        "tpl": (150, 56),
        "window": (460, 320),
        "scales": [round(x, 3) for x in np.arange(0.60, 1.45, 0.025)],
        "blob_bounds": (55, 260, 10, 70),
        "slope_floor": 0.30,
        "estimator": "variance",
        "prefilter": "no_corner_mark",
    },
    "squareyards.centre": {
        "source": "squareyards",
        # Only `resources/` (project imagery) is marked. `secondaryPortal/`
        # listing photos are clean — confirmed by a flat median residual.
        "url_regex": "/resources/",
        "referer": "https://www.squareyards.com/",
        "anchor": "centre",
        # The mark is far larger here: ~268x53 at 1600x800, ~168x71 at 800x600.
        "tpl": (320, 140),
        "window": (700, 420),
        "scales": [round(s, 3) for s in np.arange(0.45, 1.85, 0.05)],
        "blob_bounds": (90, 480, 20, 170),
        # SquareYards' mark is markedly more opaque; clamping at the
        # MagicBricks floor clipped it at alpha=0.50 and under-corrected.
        "slope_floor": 0.10,
        "close_kernel": (35, 25),
        # The wordmark's strokes are far too thick for the median-filter
        # regression to see past; recover alpha from variance instead.
        "estimator": "variance",
    },
}


def _window_origin(anchor: str, w: int, h: int, ww: int, wh: int) -> tuple[int, int]:
    """Top-left of the retained window, in full-image coordinates."""
    if anchor == "centre":
        return max(0, (w - ww) // 2), max(0, (h - wh) // 2)
    return max(0, w - ww), max(0, h - wh)


async def _sample_urls(profile: str, sample: int) -> list[str]:
    cfg = PROFILES[profile]
    db = get_database()
    urls: list[str] = []
    cursor = db[D.PROPERTIES].aggregate([
        {"$match": {"source": cfg["source"], "images.url": {"$regex": cfg["url_regex"]}}},
        {"$sample": {"size": sample}},
        {"$project": {"u": "$images.url"}},
    ])
    async for row in cursor:
        urls += row.get("u") or []
    return [u for u in dict.fromkeys(urls) if cfg["url_regex"] in u]


async def _download_windows(profile: str, urls: list[str], cap_per_size: int, global_cap: int):
    """Pass A: retain one grayscale window per image, plus url->size."""
    cfg = PROFILES[profile]
    ww, wh = cfg["window"]
    anchor = cfg["anchor"]
    prefilter = cfg.get("prefilter")
    groups: dict[tuple[int, int], list[np.ndarray]] = collections.defaultdict(list)
    seen_sizes: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    kept = 0

    headers = {"User-Agent": UA, "Referer": cfg["referer"]}
    async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True) as client:
        async def one(url: str) -> None:
            nonlocal kept
            if kept >= global_cap:
                return
            async with sem:
                if kept >= global_cap:
                    return
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        return
                    with Image.open(io.BytesIO(resp.content)) as im:
                        im.load()
                        size = im.size
                        w, h = size
                        if prefilter == "no_corner_mark":
                            # Keep only frames the corner calibration cannot
                            # find a mark in. The centre variant is a minority
                            # of frames, so stacking all of them lets the
                            # median erase exactly the signal being measured.
                            from homz.images.watermark import remove_watermark
                            rgb = np.asarray(im.convert("RGB"))
                            if remove_watermark(rgb, cfg["source"], url).removed:
                                return
                        ox, oy = _window_origin(anchor, w, h, ww, wh)
                        window = np.asarray(im.convert("L"), dtype=np.uint8)[
                            oy : oy + wh, ox : ox + ww
                        ]
                except Exception:  # noqa: BLE001
                    return
                async with lock:
                    seen_sizes[size].append(url)
                    if len(groups[size]) < cap_per_size and kept < global_cap:
                        groups[size].append(window)
                        kept += 1

        await asyncio.gather(*[one(u) for u in urls])
    return groups, seen_sizes


async def _regress_streaming(profile, urls, size, x, y, tw, th, want):
    """Pass B: accumulate regression sums without stacking the corpus.

    slope/c come from sums of obs, est, obs*est and est^2, all additive — so
    memory stays at a few small arrays no matter how many images feed in.
    """
    cfg = PROFILES[profile]
    n = 0
    s_o = np.zeros((th, tw, 3), np.float64)
    s_e = np.zeros((th, tw, 3), np.float64)
    s_oe = np.zeros((th, tw, 3), np.float64)
    s_ee = np.zeros((th, tw, 3), np.float64)
    s_oo = np.zeros((th, tw, 3), np.float64)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    headers = {"User-Agent": UA, "Referer": cfg["referer"]}
    async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True) as client:
        async def one(url: str) -> None:
            nonlocal n
            if n >= want:
                return
            async with sem:
                if n >= want:
                    return
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        return
                    with Image.open(io.BytesIO(resp.content)) as im:
                        im.load()
                        if im.size != size:
                            return
                        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                except Exception:  # noqa: BLE001
                    return
                crop = arr[y : y + th, x : x + tw]
                if crop.shape[:2] != (th, tw):
                    return
                o = crop.astype(np.float32)
                e = cv2.medianBlur(crop, 7).astype(np.float32)
                async with lock:
                    if n >= want:
                        return
                    s_o[:] += o
                    s_e[:] += e
                    s_oe[:] += o * e
                    s_ee[:] += e * e
                    s_oo[:] += o * o
                    n += 1

        await asyncio.gather(*[one(u) for u in urls])

    if n < 30:
        return None

    mo = s_o / n
    if cfg.get("estimator") == "variance":
        slope, c = _estimate_variance(mo, s_oo / n, cfg["slope_floor"])
    else:
        mi = s_e / n
        cov = s_oe / n - mo * mi
        var = s_ee / n - mi * mi
        slope = np.where(var > 1e-6, cov / np.maximum(var, 1e-6), 1.0)
        # alpha is bounded well below 1 for a translucent mark; clamping stops
        # a degenerate pixel producing an explosive 1/slope at removal time.
        slope = np.clip(slope, cfg["slope_floor"], 1.0).astype(np.float32)
        c = (mo - slope * mi).astype(np.float32)
    return slope, c, n


def _estimate_variance(mean_obs, mean_sq, slope_floor):
    """Recover (slope, c) from the variance an alpha blend destroys.

    For a fixed overlay, obs = (1-a)*I + a*W, so across many images

        std(obs) = (1-a) * std(I)

    Pixels under an opaque part of the mark barely vary at all, while
    unmarked pixels retain the scene's full variance. That ratio gives `a`
    directly, with no need to estimate the hidden content per image.

    This replaces the median-filter regression for SquareYards. That
    estimator assumes the mark is thin enough for a 7px median to erase --
    true of the MagicBricks wordmark, false of the SquareYards one whose
    strokes are ~25px thick. The filter preserved the mark, so `I_est`
    equalled the observation, the regression concluded alpha was ~0, and the
    removal measured a 22% reduction that was really just noise.

    `c = a*W` then follows from the mean, with the clean mean recovered by
    inpainting across the marked area -- valid because the scene mean over
    hundreds of unrelated photos is spatially smooth.
    """
    import cv2
    import numpy as np

    var = np.maximum(mean_sq - mean_obs * mean_obs, 0.0)
    sigma = np.sqrt(var)

    # Unmarked pixels keep full scene variance; use a high percentile as the
    # reference rather than the max, which would chase a single noisy pixel.
    sigma_ref = np.percentile(sigma, 90, axis=(0, 1), keepdims=True)
    sigma_ref = np.maximum(sigma_ref, 1e-6)

    slope = np.clip(sigma / sigma_ref, slope_floor, 1.0)
    alpha = 1.0 - slope

    # Clean scene mean under the mark: inpaint it from the surrounding area.
    mask = (alpha.mean(axis=2) > 0.06).astype(np.uint8)
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    if mask.any():
        base = np.clip(mean_obs, 0, 255).astype(np.uint8)
        clean = cv2.inpaint(base, mask, 7, cv2.INPAINT_TELEA).astype(np.float64)
    else:
        clean = mean_obs

    c = mean_obs - slope * clean
    return slope.astype(np.float32), c.astype(np.float32)


def _median_residual(arrs: list[np.ndarray]) -> np.ndarray:
    """Per-pixel median of the high-pass. Accepts grayscale or RGB frames."""
    acc = []
    for a in arrs:
        g = (a if a.ndim == 2 else a.mean(axis=2)).astype(np.float32)
        acc.append(g - cv2.GaussianBlur(g, (0, 0), 9))
    return np.median(np.stack(acc), axis=0)


def _locate(residual: np.ndarray, shape_tpl: np.ndarray, scales) -> tuple[float, float, int, int]:
    best = (-1.0, 1.0, 0, 0)
    h, w = residual.shape
    for s in scales:
        t = cv2.resize(shape_tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if t.shape[0] >= h or t.shape[1] >= w:
            continue
        r = cv2.matchTemplate(residual, t.astype(np.float32), cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(r)
        if score > best[0]:
            best = (float(score), s, int(loc[0]), int(loc[1]))
    return best


def _seed_shape(profile: str, groups) -> np.ndarray | None:
    """Bootstrap a shape template from whichever size has the most samples."""
    cfg = PROFILES[profile]
    tw, th = cfg["tpl"]
    wmin, wmax, hmin, hmax = cfg["blob_bounds"]
    for size, arrs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(arrs) < 40:
            continue
        med = _median_residual(arrs)
        thr = max(np.percentile(med, 99.5), med.std() * 3.5)
        mask = (med >= thr).astype(np.uint8)
        # Close vertically too: the SquareYards mark is two stacked lines
        # ('square' / 'yards') and must be found as ONE blob, or the seed
        # template captures half the mark and matches nothing at other sizes.
        kernel = np.ones(cfg.get("close_kernel", (5, 15)), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n < 2:
            continue
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, w, h = stats[i, :4]
        if not (wmin <= w <= wmax and hmin <= h <= hmax):
            continue
        pad_x, pad_y = (tw - w) // 2, (th - h) // 2
        x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
        crop = med[y0 : y0 + th, x0 : x0 + tw]
        if crop.shape != (th, tw):
            continue
        crop = np.clip(crop, 0, None)
        print(f"[seed] shape template from {size[0]}x{size[1]} "
              f"(blob {w}x{h}, n={len(arrs)})")
        return (crop / max(crop.max(), 1e-6)).astype(np.float32)
    return None


async def main(profile: str, sample: int, min_per_size: int, cap: int) -> None:
    cfg = PROFILES[profile]
    tw0, th0 = cfg["tpl"]
    ww, wh = cfg["window"]

    print(f"profile={profile} anchor={cfg['anchor']} filter={cfg['url_regex']!r}")
    print(f"sampling up to {sample} listings' image urls ...")
    urls = await _sample_urls(profile, sample)
    print(f"  {len(urls)} unique matching urls")
    print("pass A: locating the mark ...")
    groups, urls_by_size = await _download_windows(profile, urls, cap, global_cap=3000)
    await close_client()

    print(f"\nsize distribution (top 14 of {len(groups)}):")
    for size, arrs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:14]:
        print(f"  {size[0]:>5d}x{size[1]:<5d} {len(arrs)}")

    shape = _seed_shape(profile, groups)
    if shape is None:
        print("\nFAILED: could not seed a shape template — no size had a clear mark.")
        sys.exit(1)

    arrays: dict[str, np.ndarray] = {}
    anchors: dict[str, dict] = {}
    skipped: list[str] = []

    print(f"\npass B: calibrating (min {min_per_size} samples, score >= {MIN_SCORE}):")
    for size, arrs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        key = f"{size[0]}x{size[1]}"
        if len(arrs) < min_per_size:
            skipped.append(f"{key} (only {len(arrs)})")
            continue
        med = _median_residual(arrs)
        score, scale, lx, ly = _locate(med, shape, cfg["scales"])
        tw, th = round(tw0 * scale), round(th0 * scale)
        if score < MIN_SCORE:
            print(f"  {key:>11s}  score={score:.3f}  -> NO WATERMARK, skipped")
            skipped.append(f"{key} (score {score:.2f})")
            continue
        w, h = size
        ox, oy = _window_origin(cfg["anchor"], w, h, ww, wh)
        x, y = lx + ox, ly + oy
        got = await _regress_streaming(
            profile, urls_by_size.get(size, []), size, x, y, tw, th, want=cap
        )
        if got is None:
            print(f"  {key:>11s}  score={score:.3f} -> too few usable crops, skipped")
            skipped.append(f"{key} (regression sample too small)")
            continue
        slope, c, n = got
        alpha = 1.0 - slope
        arrays[f"slope_{key}"] = slope
        arrays[f"c_{key}"] = c
        anchors[key] = {"x": int(x), "y": int(y), "scale": float(scale),
                        "score": float(score), "n": int(n),
                        "anchor": cfg["anchor"]}
        print(f"  {key:>11s}  score={score:.3f} scale={scale:.3f} at=({x},{y}) "
              f"{tw}x{th} n={n} alpha_max={alpha.max():.3f}")

    if not anchors:
        print("\nFAILED: no size calibrated.")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT_DIR / f"{profile}.npz", **arrays)
    (OUT_DIR / f"{profile}.json").write_text(json.dumps(anchors, indent=2), encoding="utf-8")
    size_kb = (OUT_DIR / f"{profile}.npz").stat().st_size / 1024
    print(f"\nwrote {len(anchors)} size calibrations -> {OUT_DIR} ({size_kb:.0f} KB)")
    if skipped:
        print(f"skipped {len(skipped)}: " + ", ".join(skipped[:18]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="magicbricks.corner", choices=sorted(PROFILES))
    ap.add_argument("--sample", type=int, default=5200,
                    help="how many listings to draw image URLs from")
    ap.add_argument("--min-per-size", type=int, default=28,
                    help="minimum images before a size is calibrated")
    ap.add_argument("--cap", type=int, default=300, help="max images kept per size")
    args = ap.parse_args()
    asyncio.run(main(args.profile, args.sample, args.min_per_size, args.cap))
