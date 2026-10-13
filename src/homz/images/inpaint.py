"""Remove the SquareYards wordmark by inpainting, for frames inversion cannot fix.

`watermark.py` removes a mark by algebraic inversion, which is exact when the
alpha map is exact. For SquareYards it is not: the stored map is contaminated
with scene residue and the mark sits a few pixels off its formula position on
lossy JPEGs, so inversion leaves ghost outlines or smears -- 60% of the corpus
by the measured failure rate.

This takes the other route. It *locates* the wordmark with a clean template
(alignment is accurate to a pixel or two, which is all it needs), masks it, and
lets LaMa fill the region from the surrounding scene. Only a small crop around
the mark goes through the model, so it costs 0.3-2 s on CPU rather than the
seconds a full frame would.

## What it must not touch

* **Plans and maps.** Inpainting invents content. On a photograph that is
  invisible; on a layout plan it silently deletes or alters a room label,
  which misrepresents the property. `is_plan_image` frames are refused.
* **Frames with no mark.** A weak template match means there is nothing to
  remove, and painting would only alter clean scene. The frame is returned
  untouched and reported as `absent`.

The TorchScript weights (`big-lama.pt`) are loaded directly with `torch`; the
`simple-lama-inpainting` wrapper is deliberately not used because it pins
numpy 1.x and Pillow 9, which would downgrade the rest of the project.
"""

from __future__ import annotations

import functools
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from homz.logging_setup import get_logger

log = get_logger(__name__)

CALIBRATION_NPZ = Path(__file__).resolve().parent / "calibration" / "squareyards.centre.npz"
#: Public mirror of the LaMa TorchScript export used by simple-lama-inpainting.
MODEL_URL = ("https://github.com/enesmsahin/simple-lama-inpainting/releases/"
             "download/v0.1.0/big-lama.pt")
MODEL_PATH = Path(os.environ.get("HOMZ_LAMA_MODEL")
                  or Path.home() / ".cache" / "torch" / "hub" / "checkpoints" / "big-lama.pt")

#: The reference frame the template was measured on.
_REF_WIDTH = 1920.0
#: Minimum normalized correlation for the template to count as found. On a
#: 60-photo sample every frame with a visible mark scored 0.25 or more, and
#: every frame scoring below 0.17 was inspected and carried no mark -- some
#: `/resources/` photos were simply never stamped. Nothing fell in between,
#: so the threshold sits in that gap.
MIN_SCORE = 0.20
_SCALES = (0.94, 0.97, 1.0, 1.03, 1.06)
_WHITE = 245.0

_model = None
_model_lock = threading.Lock()
_infer_lock = threading.Lock()


@dataclass(frozen=True)
class InpaintResult:
    image: object  # np.ndarray
    ok: bool
    detail: str
    #: True when the frame carries no SquareYards mark at all, so leaving it
    #: untouched is correct. Distinct from a failure to remove a mark that is
    #: there.
    absent: bool = False


@functools.lru_cache(maxsize=1)
def _template():
    """The wordmark's alpha, cleaned: strokes only, flat level, feathered edge.

    The calibration's 1920x1080 map holds the wordmark *and* a field of scene
    residue across its whole 1103x343 box. Keeping only the large connected
    strokes at the mark's own position gives a template that aligns exactly
    and carries none of that noise.
    """
    import cv2
    import numpy as np

    blob = np.load(CALIBRATION_NPZ)
    a = (1 - blob["slope_1920x1080"].astype("float32")).mean(axis=2)
    core = (a > 0.22).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    keep = np.zeros_like(core)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if (area >= 400 and w >= 12 and h >= 12
                and 355 <= x + w / 2 <= 760 and 25 <= y + h / 2 <= 215):
            keep[lab == i] = 1
    keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    region = cv2.dilate(keep, np.ones((5, 5), np.uint8))
    ys, xs = np.nonzero(region)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    crop = keep[y0:y1, x0:x1].astype(np.float32)
    level = float(np.median((a * region)[y0:y1, x0:x1][crop > 0]))
    return (cv2.GaussianBlur(crop, (0, 0), 1.2) * level).astype(np.float32)


def locate(image):
    """Best (x, y, w, h, score) for the wordmark in `image`, or None."""
    import cv2
    import numpy as np

    tpl = _template()
    h, w = image.shape[:2]
    gray = image.mean(axis=2).astype(np.float32)
    hp = gray - cv2.GaussianBlur(gray, (0, 0), 5)
    best = None
    for sc in _SCALES:
        s = (w / _REF_WIDTH) * sc
        tw = max(8, round(tpl.shape[1] * s))
        th = max(8, round(tpl.shape[0] * s))
        if tw >= w or th >= h:
            continue
        al = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
        shape = al / max(float(al.max()), 1e-6)
        shape = (shape - shape.mean()).astype(np.float32)
        cx, cy = w // 2 - tw // 2, h // 2 - th // 2
        pad = max(40, int(0.05 * max(w, h)))
        x0, y0 = max(0, cx - pad), max(0, cy - pad)
        x1, y1 = min(w, cx + tw + pad), min(h, cy + th + pad)
        roi = hp[y0:y1, x0:x1]
        if roi.shape[0] <= th or roi.shape[1] <= tw:
            continue
        scores = cv2.matchTemplate(roi, shape, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(scores)
        if best is None or mx > best[4]:
            best = (x0 + loc[0], y0 + loc[1], tw, th, float(mx), al)
    return best


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        import torch

        if not MODEL_PATH.exists():
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            log.info("inpaint.downloading_model", url=MODEL_URL)
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)  # noqa: S310
        threads = int(os.environ.get("HOMZ_LAMA_THREADS", "0"))
        if threads:
            torch.set_num_threads(threads)
        m = torch.jit.load(str(MODEL_PATH), map_location="cpu")
        m.eval()
        _model = m
        return _model


def _lama(rgb, mask):
    """LaMa fill of `mask` (255 = paint) over an RGB uint8 crop."""
    import numpy as np
    import torch

    model = _load_model()
    h, w = rgb.shape[:2]
    ph, pw = (-h) % 8, (-w) % 8
    img = np.pad(rgb.astype(np.float32).transpose(2, 0, 1) / 255.0,
                 ((0, 0), (0, ph), (0, pw)), mode="symmetric")
    msk = np.pad((mask > 0).astype(np.float32)[None], ((0, 0), (0, ph), (0, pw)),
                 mode="symmetric")
    with _infer_lock, torch.inference_mode():
        out = model(torch.from_numpy(img)[None], torch.from_numpy(msk)[None])
    res = out[0].permute(1, 2, 0).numpy()
    return np.clip(res * 255, 0, 255).astype(np.uint8)[:h, :w]


def inpaint_wordmark(image) -> InpaintResult:
    """Paint the SquareYards wordmark out of `image` (HxWx3 uint8 RGB)."""
    import cv2
    import numpy as np

    from homz.images.watermark import is_plan_image

    if is_plan_image(image):
        return InpaintResult(image, False, "plan or map: not inpainted")
    found = locate(image)
    if found is None:
        return InpaintResult(image, False, "frame too small for the mark", absent=True)
    x, y, tw, th, score, alpha = found
    if score < MIN_SCORE:
        return InpaintResult(image, False, f"no mark present (score {score:.2f})", absent=True)

    h, w = image.shape[:2]
    ink = cv2.dilate((alpha > 0.1).astype(np.uint8) * 255, np.ones((7, 7), np.uint8))
    mask = np.zeros((h, w), np.uint8)
    mask[y:y + th, x:x + tw] = ink
    pad = max(40, int(0.25 * th))
    y0, x0, y1, x1 = max(0, y - pad), max(0, x - pad), min(h, y + th + pad), min(w, x + tw + pad)
    filled = _lama(np.ascontiguousarray(image[y0:y1, x0:x1]), mask[y0:y1, x0:x1])
    out = image.copy()
    # Take the model's pixels only under the mask; everything else stays the
    # original, so the rest of the frame is byte-identical.
    m = mask[y0:y1, x0:x1] > 0
    region = out[y0:y1, x0:x1]
    region[m] = filled[m]
    return InpaintResult(out, True, f"lama inpaint (score {score:.2f})")
