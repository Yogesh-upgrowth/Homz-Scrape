"""Portal watermark removal by algebraic inversion.

Both portals composite a translucent wordmark onto the photos they serve. In
each case it is an **alpha blend of a fixed bitmap at a predictable anchor**,
not a per-image nondeterministic mark, which means it can be *solved for*
rather than painted over:

    observed = (1 - a) * I + a * W

with `a` the per-pixel alpha map and `W` the mark colour. Given `a` and
`a*W`, the original is recovered exactly:

    I = (observed - a*W) / (1 - a)

Measured on the live corpus `a` peaks at 0.19-0.26, so `(1 - a)` never drops
below ~0.74 and the inversion is numerically well-conditioned everywhere —
no division by a near-zero term, and therefore no noise amplification. This
is why inversion beats inpainting here: inpainting *discards* the pixels
under the mark and hallucinates replacements, while inversion recovers the
real ones.

## Estimating the parameters

`a` and `a*W` are recovered per pixel by regressing many observed crops
against an estimate of their own clean content:

    observed = slope * I_est + c,   slope = (1 - a),  c = a * W

`I_est` comes from a median filter, which erases the mark's strokes but
preserves scene structure. Regressing across hundreds of images cancels the
residual scene content that survives the filter, and recovers `W` as a free
by-product rather than assuming pure white (it measures 237-248, not 255).

An earlier ratio estimator — `a = (observed - I_est) / (255 - I_est)` — was
tried and rejected: its denominator collapses on bright scenes, which
overestimated `a` and turned the light wordmark into *dark* text on pale
floors and walls. The regression form has no such failure mode.

## Two portals, two geometries

The sources differ in how the mark is placed, which is why calibrations are
per-source and carry an explicit anchor mode:

* **MagicBricks** (`cropped_images/`) — a small wordmark pinned at a fixed
  pixel offset from the **bottom-right corner**, at one of a couple of fixed
  scales. 800x600 and 900x506 both sit at right=168, bottom=132.
* **SquareYards** (`resources/`) — a large wordmark **centred** in the frame
  (measured cx/W 0.50-0.52, cy/H 0.45-0.50), scaling sub-linearly with the
  image: 16.8% of width at 1600px but 21.0% at 800px. Its `secondaryPortal/`
  images carry no mark at all.

Neither offset is a clean function of the dimensions, so calibration measures
each output size from a median-residual stack (high SNR — scene content is
already cancelled) rather than assuming a formula. The anchor mode only
decides how a *near* size extrapolates from a calibrated one: corner-anchored
marks keep their corner offset, centred ones keep their centre.

Sizes with no calibration entry are **left untouched**. Corrupting a photo is
strictly worse than leaving a faint mark on it, so every uncertain path here
declines to act and says so via `WatermarkResult.removed`.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from homz.logging_setup import get_logger

log = get_logger(__name__)

CALIBRATION_DIR = Path(__file__).resolve().parent / "calibration"

#: Sources with a known mark. Anything else short-circuits to "no watermark".
ANCHOR_CORNER = "corner"
ANCHOR_CENTRE = "centre"

#: Per source: the anchor mode, and which URL family actually carries a mark.
#:
#: The URL gate is not optional. Within one portal, marked and unmarked
#: images share dimensions: a clean SquareYards `secondaryPortal/` listing
#: photo can be 800x600, exactly like a marked `resources/` render. Matching
#: on size alone would subtract a watermark that was never there and visibly
#: damage the clean one. Where no URL is supplied the gate cannot be applied,
#: so the size match alone decides — callers should pass the URL.
#: A source may place its mark in more than one spot. MagicBricks uses two:
#: the original bottom-right wordmark, and a second copy dead centre on
#: frames that carry no corner mark. They need separate calibrations (own
#: anchor, own scale, own alpha map), and a frame is tested against each in
#: turn — every attempt independently verified, so trying a placement that
#: is not present costs nothing.
SOURCE_CONFIG: dict[str, dict] = {
    "magicbricks": {
        "placements": ["corner", "centre"],
        "anchor": ANCHOR_CORNER,
        # `mbimages/project/` renders are clean (verified by median stack).
        "marked": re.compile(r"/cropped_images/", re.I),
    },
    "squareyards": {
        # Two marks, chosen by what the picture *is* rather than its size:
        # photographs carry a modest centred wordmark, while layout plans and
        # location maps carry a much larger one. Routing by image class keeps
        # them apart — averaging the two together erases whichever is rarer,
        # and applying the plan mark's map to a photograph would wreck it.
        "placements": ["centre", "plan"],
        "anchor": ANCHOR_CENTRE,
        # `secondaryPortal/` listing photos are clean; `resources/` are marked.
        "marked": re.compile(r"/resources/", re.I),
        # SquareYards renders the mark to a *formula*, so one reference
        # calibration covers every frame size (see `_scalable_apply`).
        "scalable": True,
    },
}

#: SquareYards places the mark dead centre and scales it linearly with width,
#: measured across four independently calibrated sizes:
#:
#:     1600x800  scale 1.00  centre (0.500, 0.501)
#:     1135x638  scale 0.70  centre (0.500, 0.500)
#:     1110x550  scale 0.70  centre (0.500, 0.500)
#:      800x600  scale 0.50  centre (0.500, 0.502)
#:
#: i.e. the mark is always 20% of the image width, centred. That is a formula
#: rather than a lookup, which matters enormously here: the `resources/`
#: family spans 1,677 distinct frame sizes, so per-size calibration would
#: leave most of the corpus uncovered no matter how long it ran.
_SY_SCALE_REFERENCE_WIDTH = 1600.0

#: Size delta within which an anchor is *derived* from a neighbouring
#: calibration rather than searched for.
_DERIVE_TOLERANCE = 24
#: Anchor-snap radius for the derived tier. Small: the offset is already known
#: to within a pixel or two, and a wide search lets scene texture outscore the
#: faint mark.
_SNAP_PAD = 5
#: Wider for the formula-placed path: the predicted anchor carries the
#: rounding error of a scale factor as well as the reference's own, so it can
#: land a dozen pixels out on a large frame.
_SCALABLE_SNAP_PAD = 16
#: Minimum correlation before the wider fallback search will act. Low on
#: purpose: this is a cheap prefilter, not the correctness gate. Every hit it
#: admits is applied and then checked by `_verify_improved`, which discards
#: the result unless the mark's energy actually fell — so a loose threshold
#: costs a little work on misses and buys real recall on faint marks, while
#: a tight one silently left plainly-visible watermarks in place at 0.31.
_MATCH_THRESHOLD = 0.18
#: How far from the nominal anchor that wider search looks.
_SEARCH_PAD = 14
#: Maximum inversion passes per placement. See the loop in `remove_watermark`.
_MAX_PASSES = 3


@dataclass(frozen=True)
class WatermarkResult:
    image: object  # np.ndarray
    #: True = a mark was present and inverted; False = expected but not
    #: confidently located; None = this source/size carries no known mark.
    removed: bool | None
    detail: str


class _Calibration:
    """Lazily-loaded per-size watermark parameters for one source.

    Thread-safe by construction. `ingest` pushes the CPU-bound image work onto
    a thread pool, so several workers can reach `load()` at once. An earlier
    version set `_loaded = True` up front and populated `_sizes` afterwards,
    which meant a concurrent caller could observe "loaded" against a table
    that was still empty, silently skip de-watermarking, and store a
    watermarked file — non-deterministically, and with nothing in the logs.
    The publish of `_loaded` must therefore happen *after* `_sizes` is filled,
    under a lock.
    """

    def __init__(self, source: str, placement: str = "corner") -> None:
        self.source = source
        self.placement = placement
        self.anchor = ANCHOR_CENTRE if placement == "centre" else ANCHOR_CORNER
        self._loaded = False
        self._sizes: dict[tuple[int, int], dict] = {}
        self._lock = threading.Lock()

    @property
    def key(self) -> str:
        return f"{self.source}.{self.placement}"

    @property
    def npz_path(self) -> Path:
        return CALIBRATION_DIR / f"{self.key}.npz"

    @property
    def json_path(self) -> Path:
        return CALIBRATION_DIR / f"{self.key}.json"

    def load(self) -> bool:
        if self._loaded:  # fast path, no lock once published
            return bool(self._sizes)
        with self._lock:
            if self._loaded:  # another thread won the race
                return bool(self._sizes)
            sizes = self._read()
            self._sizes = sizes
            self._loaded = True  # publish only after _sizes is complete
            return bool(sizes)

    def _read(self) -> dict[tuple[int, int], dict]:
        if not self.npz_path.exists() or not self.json_path.exists():
            log.info("watermark.no_calibration", profile=self.key)
            return {}
        try:
            import numpy as np

            blob = np.load(self.npz_path)
            anchors = json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - calibration is optional
            log.warning("watermark.calibration_unreadable",
                        profile=self.key, error=str(exc)[:200])
            return {}

        sizes: dict[tuple[int, int], dict] = {}
        for key, meta in anchors.items():
            try:
                w, h = (int(v) for v in key.split("x"))
                sizes[(w, h)] = {
                    # Stored as float16 to keep the shipped calibration small;
                    # widen once here so the arithmetic downstream is float32.
                    "slope": blob[f"slope_{key}"].astype("float32"),
                    "c": blob[f"c_{key}"].astype("float32"),
                    "x": int(meta["x"]),
                    "y": int(meta["y"]),
                }
            except (KeyError, ValueError):
                continue
        log.info("watermark.calibration_loaded", profile=self.key, sizes=len(sizes))
        return sizes

    def get(self, size: tuple[int, int]) -> dict | None:
        if not self.load():
            return None
        return self._sizes.get(size)

    def nearest(self, w: int, h: int):
        """Closest calibrated size of the same render class, or None.

        A crop a few pixels off a calibrated size is the *same render*: the
        portal re-crops the frame but composites an unchanged mark. Widths
        within ~15% pick the same render, so that is the class filter; beyond
        it the mark is a different size and its alpha map would not apply.
        """
        if not self.load():
            return None
        best = None
        for (cw, ch), params in self._sizes.items():
            if not (0.85 <= w / cw <= 1.18):
                continue
            cost = abs(w - cw) + abs(h - ch)
            if best is None or cost < best[0]:
                best = (cost, (cw, ch), params)
        return None if best is None else (best[1], best[2])

    def reference(self):
        """The widest calibrated size — the one to scale from.

        Widest, because downscaling a mark loses less than upscaling invents.
        """
        if not self.load():
            return None
        size = max(self._sizes, key=lambda s: s[0])
        return size, self._sizes[size]

    @property
    def sizes(self) -> list[tuple[int, int]]:
        self.load()
        return sorted(self._sizes)


_CALIBRATIONS: dict[str, _Calibration] = {}
_REGISTRY_LOCK = threading.Lock()


def _calibration_for(source: str, placement: str = "corner") -> _Calibration | None:
    if source not in SOURCE_CONFIG:
        return None
    key = f"{source}.{placement}"
    cal = _CALIBRATIONS.get(key)
    if cal is None:
        with _REGISTRY_LOCK:
            cal = _CALIBRATIONS.get(key)
            if cal is None:
                cal = _Calibration(source, placement)
                _CALIBRATIONS[key] = cal
    return cal


def _placements(source: str) -> list[str]:
    return SOURCE_CONFIG.get(source, {}).get("placements", ["corner"])


def is_plan_image(image) -> bool:
    """Line-art plan or map, rather than a photograph.

    The two classes separate cleanly on how much of the frame is near-white:
    plans measure above 0.82, photographs below 0.05, because a plan is ink on
    paper. Used to pick which watermark calibration applies.
    """
    import numpy as np

    g = image.mean(axis=2)
    bright = float((g > 205).mean())
    sat = float((image.max(axis=2).astype(np.int16)
                 - image.min(axis=2).astype(np.int16)).mean())
    return bright > 0.45 and sat < 45


def _placement_applies(placement: str, image, source: str | None = None) -> bool:
    """Whether this placement's mark can be on this particular frame."""
    if placement == "plan":
        return is_plan_image(image)
    if placement == "centre":
        # A plan carries the plan mark instead; trying the photo mark on it
        # only risks damage where the real mark is elsewhere and larger.
        #
        # Only where there *is* a plan mark to carry, though. SquareYards has
        # one; MagicBricks does not, so this gate was handing those frames to
        # nobody and leaving the centre mark fully intact -- on exactly the
        # frames most likely to trip `is_plan_image`, since a bright,
        # low-saturation photo of an empty white-walled room reads as line art
        # by that measure, and this catalogue is full of them.
        if source is not None and "plan" not in _placements(source):
            return True
        return not is_plan_image(image)
    return True


def has_calibration(source: str | None = None) -> bool:
    sources = [source] if source else list(SOURCE_CONFIG)
    return any(
        (c := _calibration_for(s, p)) is not None and c.load()
        for s in sources for p in _placements(s)
    )


def calibrated_sizes(source: str, placement: str | None = None) -> list[tuple[int, int]]:
    places = [placement] if placement else _placements(source)
    out: set[tuple[int, int]] = set()
    for p in places:
        cal = _calibration_for(source, p)
        if cal:
            out.update(cal.sizes)
    return sorted(out)


#: Alpha above which a pixel is treated as mark ink rather than a faint edge.
_INK_ALPHA = 0.30
#: Inpainting is skipped when the ink covers more than this share of the mark
#: box. A wordmark's strokes are sparse; a mask denser than this means the
#: alpha map is noisy rather than inky, and painting over that much area
#: smears real content — which is exactly what it did on the larger
#: SquareYards marks, erasing the text but leaving the building behind it
#: visibly mangled.
_MAX_INK_FRACTION = 0.22
#: Inpaint radius, in pixels, for the residue pass.
_INPAINT_RADIUS = 4
#: Match score above which a searched hit is trusted enough to paint over.
_CONFIDENT_SCORE = 0.45
#: Mark energy above which a formula-placed hit is trusted enough to paint over.
_CONFIDENT_ENERGY = 0.25


#: Per-frame gains tried against the calibrated alpha map. The calibration is
#: an *average* over hundreds of frames, and only a mark rendered to a formula
#: is the same on every one of them. SquareYards' is (always 20% of width,
#: dead centre), so its average equals the truth and inversion is exact.
#: MagicBricks composites a bitmap whose opacity varies frame to frame, so
#: subtracting the average left a remnant wherever the real mark was stronger
#: -- measured on 22.3% of stored MagicBricks images against 9.0% for
#: SquareYards. Solving one scalar per frame closes that gap without
#: recalibrating anything.
_GAIN_CANDIDATES = (0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75)


def _gain_params(params: dict, g: float) -> dict:
    """`params` with the mark's opacity scaled by `g`.

    The blend is `observed = (1-a)I + aW`. Scaling this frame's alpha to
    `g*a` gives `slope = 1 - g*(1-slope)` and `c = g*c`, since the mark's
    colour W is unchanged -- only how strongly it was laid down. Clamped so a
    high gain over an already-opaque pixel cannot drive the divisor to zero.
    """
    import numpy as np

    if g == 1.0:
        return params
    slope = np.maximum(1.0 - g * (1.0 - params["slope"]), 0.05)
    return {**params, "slope": slope, "c": params["c"] * g}


def _fit_gain(image, params, x: int, y: int, shape) -> tuple[dict, float]:
    """The gain whose inversion leaves the least mark energy behind.

    A one-dimensional search over a handful of candidates, scored by the same
    `_mark_energy` the verification uses -- so "best" here means exactly what
    "removed" means everywhere else in this module, rather than a second
    notion of quality that could disagree with the first.
    """
    import numpy as np

    th, tw = params["slope"].shape[:2]
    patch = image[y : y + th, x : x + tw].astype(np.float32)
    best, best_e = params, None
    for g in _GAIN_CANDIDATES:
        cand = _gain_params(params, g)
        out = np.clip((patch - cand["c"]) / np.maximum(cand["slope"], 1e-3), 0, 255)
        # Scored on the patch alone. Cloning the whole frame per candidate
        # cost 3 MB a go and blew up under the ingest's thread pool, for a
        # number that only ever depended on these few thousand pixels.
        e = _patch_energy(out, shape)
        if e is None:
            continue
        if best_e is None or abs(e) < abs(best_e):
            best, best_e = cand, e
    return best, (best_e if best_e is not None else 0.0)


def _apply(image, params, x: int, y: int, detail: str, *, verify=None,
           finish=True) -> WatermarkResult:
    """Invert the blend over the mark box, then erase whatever survives.

    Inversion alone is only as good as the alpha estimate, and on the more
    opaque marks a slightly low alpha leaves a legible ghost — the whole
    wordmark still readable, just fainter. So inversion is followed by an
    inpaint pass over the ink itself: the strokes are thin relative to the
    frame and the surrounding pixels are genuine, so filling them from their
    own neighbourhood removes the last of the mark without inventing anything
    the eye will notice.

    Inversion still runs first and does the bulk of the work; inpainting only
    cleans up the residue, over the small area where alpha is high enough that
    the recovered value cannot be trusted.

    `finish` is off for low-confidence hits, and that matters: on a frame
    carrying a watermark style this module does not model (SquareYards puts a
    large full-width mark on layout plans, unlike the small centred wordmark
    on photos) the search settles on the wrong box, and inpainting there
    smears real content while the actual mark survives. Painting over pixels
    is only safe once the location is certain; where it is not, inversion
    alone is the conservative choice because a wrong alpha shifts values
    slightly instead of destroying them.

    `verify` is the mark's normalized shape; when supplied the result is
    discarded unless it measurably reduces the mark's energy, which is what
    stops an extrapolated anchor from burning a phantom mark into a clean photo.
    """
    import cv2
    import numpy as np

    th, tw = params["slope"].shape[:2]
    if y < 0 or x < 0 or y + th > image.shape[0] or x + tw > image.shape[1]:
        return WatermarkResult(image, False, "anchor outside image bounds")

    # Fit this frame's own opacity before inverting. Needs the mark's shape to
    # score against, so it only runs on the verified paths -- which is every
    # path that reaches here with a known location.
    if verify is not None:
        params, _ = _fit_gain(image, params, x, y, verify)

    slope = params["slope"]
    c = params["c"]
    patch = image[y : y + th, x : x + tw].astype(np.float32)
    recovered = (patch - c) / np.maximum(slope, 1e-3)
    out = image.copy()
    out[y : y + th, x : x + tw] = np.clip(recovered, 0, 255).astype(np.uint8)

    if verify is not None and not _verify_improved(image, out, verify, x, y, tw, th):
        return WatermarkResult(image, None, f"{detail}: no mark here, left untouched")

    if finish:
        alpha = (1.0 - slope).mean(axis=2)
        ink = (alpha > _INK_ALPHA).astype(np.uint8)
        if ink.any() and ink.mean() <= _MAX_INK_FRACTION:
            ink = cv2.dilate(ink, np.ones((3, 3), np.uint8), iterations=1)
            pad = _INPAINT_RADIUS * 3
            ry0, rx0 = max(0, y - pad), max(0, x - pad)
            ry1 = min(image.shape[0], y + th + pad)
            rx1 = min(image.shape[1], x + tw + pad)
            region = np.ascontiguousarray(out[ry0:ry1, rx0:rx1])
            mask = np.zeros(region.shape[:2], np.uint8)
            mask[y - ry0 : y - ry0 + th, x - rx0 : x - rx0 + tw] = ink
            try:
                filled = cv2.inpaint(region, mask, _INPAINT_RADIUS, cv2.INPAINT_TELEA)
                out[ry0:ry1, rx0:rx1] = filled
            except cv2.error:
                pass

    return WatermarkResult(out, True, detail)


def _shape_of(params):
    """The mark's alpha map, normalized to a correlation template."""
    import numpy as np

    alpha = (1.0 - params["slope"]).mean(axis=2)
    peak = float(alpha.max())
    if peak <= 1e-6:
        return None
    return (alpha / peak).astype(np.float32)


def _mark_energy(image, shape, x: int, y: int, tw: int, th: int) -> float | None:
    """Signed correlation of the local high-pass with the mark's own shape.

    Positive means a light mark is present at (x, y); near zero means it is
    gone; negative means the area now holds an *inverse* of the mark, i.e. it
    has been over-subtracted into dark ghost text.
    """
    import cv2
    import numpy as np

    if y < 0 or x < 0 or y + th > image.shape[0] or x + tw > image.shape[1]:
        return None
    return _patch_energy(image[y : y + th, x : x + tw], shape)


def _patch_energy(patch, shape) -> float | None:
    """`_mark_energy` for a patch already in hand, with no frame to index."""
    import cv2
    import numpy as np

    gray = patch.mean(axis=2).astype(np.float32)
    highpass = gray - cv2.GaussianBlur(gray, (0, 0), 7)
    s = shape - shape.mean()
    hp = highpass - highpass.mean()
    denom = float(np.sqrt((s * s).sum() * (hp * hp).sum()))
    if denom < 1e-9:
        return None
    return float((s * hp).sum() / denom)


def _verify_improved(before, after, shape, x: int, y: int, tw: int, th: int) -> bool:
    """Did the subtraction actually make the mark *less* visible?

    This is the safety net that catches the case no threshold can: applying a
    neighbouring size's parameters to an image that never carried the mark.
    Calibration determined that MagicBricks 900x600 frames are clean, but the
    nearest-size fallback happily borrowed 900x599's alpha map and subtracted
    a watermark that was not there — producing dark "magicbricks" ghost text
    on a clean photo, which is far worse than the faint mark we were trying
    to remove.

    Comparing mark energy before and after is direct and needs no tuning: a
    real removal drives |energy| toward zero, while subtracting a phantom
    drives it negative and larger.
    """
    e_before = _mark_energy(before, shape, x, y, tw, th)
    e_after = _mark_energy(after, shape, x, y, tw, th)
    if e_before is None or e_after is None:
        return True  # cannot judge; the caller's own gating stands
    return abs(e_after) < abs(e_before)


def _snap(image, shape, x: int, y: int, tw: int, th: int, *, pad: int) -> tuple[int, int]:
    """Best-correlating anchor within +-`pad` of the nominal one.

    Unconditional: the caller has already established the mark is at roughly
    (x, y), so this only resolves which exact pixel. Borrowing a neighbouring
    size's `c` at an anchor even one pixel out subtracts the mark's energy in
    the wrong place, which measured as systematic *over*-correction — faint
    dark ghost text rather than a clean removal.
    """
    import cv2
    import numpy as np

    h_img, w_img = image.shape[0], image.shape[1]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w_img, x + tw + pad), min(h_img, y + th + pad)
    roi = image[y0:y1, x0:x1]
    if roi.shape[0] <= th or roi.shape[1] <= tw:
        return x, y
    gray = roi.mean(axis=2).astype(np.float32)
    highpass = gray - cv2.GaussianBlur(gray, (0, 0), 7)
    scores = cv2.matchTemplate(highpass, shape, cv2.TM_CCOEFF_NORMED)
    _, _, _, loc = cv2.minMaxLoc(scores)
    return x0 + loc[0], y0 + loc[1]


def _derive_anchor(anchor: str, w: int, h: int, cw: int, ch: int, params: dict,
                   tw: int, th: int) -> tuple[int, int]:
    """Map a calibrated anchor onto a near-identical frame of a different size.

    Corner-anchored marks keep their distance from the bottom-right corner;
    centred marks keep their centre. Using the wrong rule here misplaces the
    mark by the full size difference.
    """
    if anchor == ANCHOR_CENTRE:
        cx = (params["x"] + tw / 2) / cw
        cy = (params["y"] + th / 2) / ch
        return round(cx * w - tw / 2), round(cy * h - th / 2)
    return w - (cw - params["x"]), h - (ch - params["y"])


def _scalable_apply(image, cal: _Calibration, w: int, h: int) -> WatermarkResult:
    """Place the reference mark by formula: centred, scaled to the frame width.

    Resizing `slope` and `c` together is valid because both are per-pixel
    blend coefficients of the same overlay — `slope = 1-a` and `c = a*W`
    resample exactly as the mark bitmap does.

    The anchor is still snapped by correlation afterwards. The formula gets
    within a pixel or two, and the mark here is opaque enough (alpha 0.34 to
    0.62) that a small misplacement would be plainly visible.
    """
    import cv2

    ref = cal.reference()
    if ref is None:
        return WatermarkResult(image, None, "no reference calibration")
    (rw, rh), params = ref

    slope, c = params["slope"], params["c"]
    rth, rtw = slope.shape[:2]
    scale = w / rw
    tw, th = max(8, round(rtw * scale)), max(8, round(rth * scale))
    if tw >= w or th >= h:
        return WatermarkResult(image, False, f"mark larger than frame at {w}x{h}")

    slope_s = cv2.resize(slope, (tw, th), interpolation=cv2.INTER_AREA)
    c_s = cv2.resize(c, (tw, th), interpolation=cv2.INTER_AREA)
    scaled = {"slope": slope_s, "c": c_s}

    # Use the reference's *measured* centre, not a presumed 0.5. The mark sits
    # at cx/W 0.507, and assuming dead centre put the template ~11px off at
    # 1600px wide — outside the snap window, so the subtraction landed beside
    # the mark and left it visibly intact on a third of the sample.
    cx = (params["x"] + rtw / 2) / rw
    cy = (params["y"] + rth / 2) / rh
    x, y = round(cx * w - tw / 2), round(cy * h - th / 2)

    shape = _shape_of(scaled)
    if shape is None:
        return WatermarkResult(image, False, "degenerate calibration")
    sx, sy = _snap(image, shape, x, y, tw, th, pad=_SCALABLE_SNAP_PAD)
    # The formula places the mark confidently only when this frame's mark is
    # the one the reference models. A frame with a different mark style still
    # passes verification (inverting reduces *some* energy) but lands on the
    # wrong box, so the inpaint stays off unless the match is strong.
    energy = _mark_energy(image, shape, sx, sy, tw, th)
    confident = energy is not None and energy >= _CONFIDENT_ENERGY
    return _apply(image, scaled, sx, sy, f"scaled x{scale:.2f} from {rw}px ref",
                  verify=shape, finish=confident)


def remove_watermark(image, source: str, url: str | None = None) -> WatermarkResult:
    """Strip every known portal watermark from `image` (HxWx3 uint8 RGB).

    A frame is tested against each placement the source is known to use, in
    turn, and each removal that verifies is kept. MagicBricks needs this:
    it marks some frames bottom-right and others dead centre, so handling
    only one placement left the other fully visible.

    `removed` is True when at least one mark was inverted, False when one was
    expected but could not be confidently located, and None when this
    source/URL family carries no known mark at all.
    """
    if source not in SOURCE_CONFIG:
        return WatermarkResult(image, None, "source has no known watermark")

    # Only the marked URL family is touched. Subtracting a mark from an image
    # that never had one is visible damage, and within a portal the marked and
    # unmarked families share dimensions.
    pattern = SOURCE_CONFIG[source]["marked"]
    if url is not None and not pattern.search(url):
        return WatermarkResult(image, None, "url family carries no watermark")

    current = image
    hits: list[str] = []
    misses: list[str] = []
    for placement in _placements(source):
        if not _placement_applies(placement, image, source):
            continue
        cal = _calibration_for(source, placement)
        if cal is None or not cal.load():
            continue
        # Repeat while the mark keeps measurably weakening. `_verify_improved`
        # only asks that energy *fell*, not that it reached zero, so a partial
        # subtraction is a legitimate result -- and used to be the final one.
        # Bounded, because each pass that still finds something is doing real
        # work and a mark that survives three is not going to yield to a
        # fourth.
        for attempt in range(_MAX_PASSES):
            result = _remove_one(current, source, cal)
            if result.removed:
                current = result.image
                hits.append(f"{placement}: {result.detail}"
                            + (f" (pass {attempt + 2})" if attempt else ""))
                continue
            if result.removed is False and attempt == 0:
                misses.append(f"{placement}: {result.detail}")
            break

    if hits:
        return WatermarkResult(current, True, "; ".join(hits))
    if misses:
        return WatermarkResult(image, False, "; ".join(misses))
    return WatermarkResult(image, None, "no mark found in any placement")


def _remove_one(image, source: str, cal: _Calibration) -> WatermarkResult:
    """Try a single placement's calibration against the frame."""
    h, w = image.shape[0], image.shape[1]
    params = cal.get((w, h))
    if params is not None:
        # Verified like every other tier. A calibrated size says "frames of
        # this shape usually carry the mark", not "this frame does" — the
        # portals serve unmarked photos at marked sizes too. Applying
        # unconditionally turned clean 900x506 photos into ones with dark
        # "magicbricks" ghost text, which is worse than the mark itself.
        return _apply(image, params, params["x"], params["y"], f"exact {w}x{h}",
                      verify=_shape_of(params), finish=True)

    if SOURCE_CONFIG[source].get("scalable") or cal.placement == "plan":
        return _scalable_apply(image, cal, w, h)

    near = cal.nearest(w, h)
    if near is None:
        return WatermarkResult(image, None, f"no calibration near {w}x{h}")


    (cw, ch), params = near
    slope = params["slope"]
    th, tw = slope.shape[:2]
    x, y = _derive_anchor(cal.anchor, w, h, cw, ch, params, tw, th)

    shape = _shape_of(params)
    if shape is None:
        return WatermarkResult(image, False, "degenerate calibration")

    if abs(w - cw) <= _DERIVE_TOLERANCE and abs(h - ch) <= _DERIVE_TOLERANCE:
        sx, sy = _snap(image, shape, x, y, tw, th, pad=_SNAP_PAD)
        return _apply(image, params, sx, sy, f"derived from {cw}x{ch}",
                      verify=shape, finish=True)

    return _search_apply(image, cal, w, h, (cw, ch), params)


#: Scale multipliers tried around the size the nearest calibration predicts.
#: The mark is not always a fixed fraction of the frame — a 900x600 frame
#: carries a ~178px wordmark while the 900x506 calibration's is ~97px — so a
#: single predicted size finds nothing (measured correlation 0.04 against a
#: mark that is plainly visible). Searching scale as well as position is what
#: makes the fallback work.
_SCALE_CANDIDATES = (0.55, 0.7, 0.85, 1.0, 1.2, 1.45, 1.75, 2.1)
#: Position search radius for the multi-scale fallback, as a fraction of the
#: frame. Generous: this tier is already unsure where the mark is.
_WIDE_PAD_RATIO = 0.10
_WIDE_PAD_MIN = 26


def _search_apply(image, cal: _Calibration, w: int, h: int,
                  ref_size, params) -> WatermarkResult:
    """Find the mark by searching position *and* scale, then invert it."""
    import cv2
    import numpy as np

    cw, ch = ref_size
    slope, c = params["slope"], params["c"]
    rth, rtw = slope.shape[:2]
    base = w / cw

    gray_full = image.mean(axis=2).astype(np.float32)
    highpass_full = gray_full - cv2.GaussianBlur(gray_full, (0, 0), 7)

    best = None
    for mult in _SCALE_CANDIDATES:
        sc = base * mult
        tw, th = round(rtw * sc), round(rth * sc)
        if tw < 24 or th < 10 or tw >= w or th >= h:
            continue
        slope_s = cv2.resize(slope, (tw, th), interpolation=cv2.INTER_AREA)
        c_s = cv2.resize(c, (tw, th), interpolation=cv2.INTER_AREA)
        scaled = {"slope": slope_s, "c": c_s}
        shape = _shape_of(scaled)
        if shape is None:
            continue

        x, y = _derive_anchor(cal.anchor, w, h, cw, ch, params, tw, th)
        pad = max(_WIDE_PAD_MIN, int(min(w, h) * _WIDE_PAD_RATIO))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + tw + pad), min(h, y + th + pad)
        roi = highpass_full[y0:y1, x0:x1]
        if roi.shape[0] <= th or roi.shape[1] <= tw:
            continue
        scores = cv2.matchTemplate(roi, shape, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(scores)
        if best is None or score > best[0]:
            best = (float(score), scaled, shape, x0 + loc[0], y0 + loc[1], sc)

    if best is None:
        return WatermarkResult(image, False, f"no usable scale at {w}x{h}")
    score, scaled, shape, bx, by, sc = best
    if score < _MATCH_THRESHOLD:
        return WatermarkResult(image, False,
                               f"mark not located at {w}x{h} (score {score:.2f})")
    return _apply(image, scaled, bx, by,
                  f"searched x{sc:.2f} from {cw}x{ch} score={score:.2f}",
                  verify=shape, finish=score >= _CONFIDENT_SCORE)
