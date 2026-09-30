"""Tool segmentation on the rectified image.

Three tiers, all CPU:
  * classical  - Otsu threshold: free, works for dark tools on white paper.
  * auto       - IS-Net salient object model (via rembg): finds every tool, no clicks.
  * sam        - SAM 2.1 hiera-tiny (ONNX): click-to-segment with add/remove points,
                 for reflective or multi-material tools the others miss.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import cv2
import numpy as np

from .config import PX_PER_MM, REMBG_MODEL, SAM_DIR

_SAM_SIZE = 1024
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)


# ---------------------------------------------------------------- classical
def classical_mask(img_bgr: np.ndarray, paper_px: list[float] | None = None) -> np.ndarray:
    """Dark-object mask via Otsu on a lightly blurred grayscale image."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    k = np.ones((5, 5), np.uint8)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, k)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)
    mask = th > 0
    if paper_px is not None:
        # Otsu assumes "dark = tool"; the bench outside the sheet is dark too, so clip to the paper.
        x0, y0, x1, y1 = [int(round(v)) for v in paper_px]
        keep = np.zeros_like(mask)
        keep[max(y0, 0) : y1, max(x0, 0) : x1] = True
        mask &= keep
    return mask


def split_components(
    mask: np.ndarray, px_per_mm: float = PX_PER_MM, min_area_mm2: float = 100.0
) -> list[np.ndarray]:
    """Split a mask into one mask per connected component, largest first."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    min_px = min_area_mm2 * px_per_mm * px_per_mm
    comps = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_px:
            comps.append((stats[i, cv2.CC_STAT_AREA], labels == i))
    comps.sort(key=lambda t: -t[0])
    return [m for _, m in comps]


def clicked_components(comps: list[np.ndarray], clicks_xy: list[tuple[float, float]]) -> list[np.ndarray]:
    """The components that contain at least one positive click (SAM sometimes adds stray blobs).
    Off-image clicks never match, and no positive click means no tool: an empty list, not a guess."""
    out = []
    for c in comps:
        h, w = c.shape
        if any(0 <= int(y) < h and 0 <= int(x) < w and c[int(y), int(x)] for x, y in clicks_xy):
            out.append(c)
    return out


# ---------------------------------------------------------------- IS-Net (rembg)
_rembg_session = None
_rembg_lock = threading.Lock()


def _get_rembg():
    global _rembg_session
    with _rembg_lock:
        if _rembg_session is None:
            from rembg import new_session  # lazy: import is slow

            _rembg_session = new_session(REMBG_MODEL)
    return _rembg_session


def auto_mask(img_bgr: np.ndarray) -> np.ndarray:
    """Salient-object mask from IS-Net. Returns bool mask at image resolution."""
    from rembg import remove

    session = _get_rembg()
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    out = remove(rgb, session=session, only_mask=True, post_process_mask=True)
    alpha = np.asarray(out)
    if alpha.ndim == 3:
        alpha = alpha[..., 0]
    mask = alpha > 127
    k = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, k) > 0
    return mask


# ---------------------------------------------------------------- SAM 2.1
class Sam2:
    """Thin wrapper over the two ONNX graphs with an embedding cache keyed by image id."""

    def __init__(self, cache_size: int = 6):
        import onnxruntime as ort

        enc_path = SAM_DIR / "onnx" / "vision_encoder.onnx"
        dec_path = SAM_DIR / "onnx" / "prompt_encoder_mask_decoder.onnx"
        if not enc_path.exists() or not dec_path.exists():
            raise FileNotFoundError(f"SAM 2 model missing under {SAM_DIR}. Run scripts/download_models.py")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 0
        self.enc = ort.InferenceSession(str(enc_path), opts, providers=["CPUExecutionProvider"])
        self.dec = ort.InferenceSession(str(dec_path), opts, providers=["CPUExecutionProvider"])
        self._cache: OrderedDict[str, tuple[list[np.ndarray], tuple[int, int]]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        # key -> Event while an encoder pass for that key is in flight, so concurrent requests for
        # the same image wait for one embedding instead of each running the encoder.
        self._pending: dict[str, threading.Event] = {}

    def _cached(self, key: str) -> tuple[list[np.ndarray], tuple[int, int]] | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def embed(self, key: str, img_bgr: np.ndarray) -> None:
        for _ in range(3):
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return
                ev = self._pending.get(key)
                owner = ev is None
                if owner:
                    ev = self._pending[key] = threading.Event()
            if not owner:
                ev.wait()  # the owner finished (or failed); loop to re-check the cache
                continue
            try:
                rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                x = (
                    cv2.resize(rgb, (_SAM_SIZE, _SAM_SIZE), interpolation=cv2.INTER_AREA).astype(np.float32)
                    / 255.0
                )
                x = ((x - _MEAN) / _STD).transpose(2, 0, 1)[None]
                emb = self.enc.run(None, {"pixel_values": x})
                with self._lock:
                    self._cache[key] = (emb, img_bgr.shape[:2])
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                return
            finally:
                with self._lock:
                    self._pending.pop(key, None)
                ev.set()
        raise RuntimeError(f"could not embed {key}")

    def forget(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def predict(
        self,
        key: str,
        points: list[tuple[float, float]],
        labels: list[int],
        box: tuple[float, float, float, float] | None = None,
        img_bgr: np.ndarray | None = None,
    ) -> np.ndarray:
        """points in image pixels; labels 1=foreground, 0=background. Returns the best mask."""
        masks, iou = self.predict_all(key, points, labels, box, img_bgr)
        return masks[int(np.argmax(iou))]

    def predict_all(
        self,
        key: str,
        points: list[tuple[float, float]],
        labels: list[int],
        box: tuple[float, float, float, float] | None = None,
        img_bgr: np.ndarray | None = None,
    ) -> tuple[list[np.ndarray], np.ndarray]:
        """All three SAM mask hypotheses (full resolution, bool) and their predicted IoUs.

        If the embedding for `key` was evicted (other clients' images pushed it out of the small cache)
        and img_bgr is given, it is recomputed instead of failing."""
        hit = self._cached(key)
        if hit is None:
            if img_bgr is None:
                raise KeyError(f"no embedding for {key}; call embed() first")
            self.embed(key, img_bgr)
            hit = self._cached(key)
            if hit is None:
                raise RuntimeError(f"embedding for {key} was evicted immediately; cache too small")
        emb, (h, w) = hit
        sx, sy = _SAM_SIZE / w, _SAM_SIZE / h
        if points:
            pts = np.array([[[[px * sx, py * sy] for px, py in points]]], np.float32)
            lab = np.array([[labels]], np.int64)
        else:
            pts = np.zeros((1, 1, 0, 2), np.float32)
            lab = np.zeros((1, 1, 0), np.int64)
        if box is not None:
            x0, y0, x1, y1 = box
            boxes = np.array([[[x0 * sx, y0 * sy, x1 * sx, y1 * sy]]], np.float32)
        else:
            boxes = np.zeros((1, 0, 4), np.float32)
        iou, masks, _ = self.dec.run(
            None,
            {
                "input_points": pts,
                "input_labels": lab,
                "input_boxes": boxes,
                "image_embeddings.0": emb[0],
                "image_embeddings.1": emb[1],
                "image_embeddings.2": emb[2],
            },
        )
        out = [
            cv2.resize(masks[0, 0, i], (w, h), interpolation=cv2.INTER_LINEAR) > 0
            for i in range(masks.shape[2])
        ]
        return out, iou[0, 0]


_sam: Sam2 | None = None
_sam_lock = threading.Lock()


def get_sam() -> Sam2:
    global _sam
    with _sam_lock:
        if _sam is None:
            _sam = Sam2()
    return _sam


# ---------------------------------------------------------------- zoom-in refinement
def _roughness(mask: np.ndarray) -> float:
    """Isoperimetric roughness: perimeter^2 / (4 pi area). 1 = circle; wobbly edges push it up."""
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return 1e9
    c = max(contours, key=cv2.contourArea)
    a = cv2.contourArea(c)
    return (cv2.arcLength(c, True) ** 2) / (4 * np.pi * a) if a > 0 else 1e9


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    u = (a | b).sum()
    return float((a & b).sum() / u) if u else 0.0


def _crop_box(mask: np.ndarray, margin_frac: float, min_side: int):
    ys, xs = np.where(mask)
    h, w = mask.shape
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    side = max(max(x1 - x0, y1 - y0) * (1 + 2 * margin_frac), min_side)
    side = int(min(side, max(h, w)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cx0 = int(max(0, min(round(cx - side / 2), w - side)))
    cy0 = int(max(0, min(round(cy - side / 2), h - side)))
    return cx0, cy0, min(w, cx0 + side), min(h, cy0 + side), (x0, y0, x1, y1)


def _sam_crop(crop: np.ndarray, cmask: np.ndarray, box) -> np.ndarray | None:
    sam = get_sam()
    key = f"refine-{id(crop)}"
    sam.embed(key, crop)
    dt = cv2.distanceTransform(cmask.astype(np.uint8), cv2.DIST_L2, 5)
    py, px = np.unravel_index(int(np.argmax(dt)), dt.shape)
    pts = [(float(px), float(py))]
    ys2, xs2 = np.where(dt > 0.6 * dt.max())
    if len(xs2) > 20:
        rng = np.random.default_rng(1)
        for i in rng.choice(len(xs2), size=min(4, len(xs2)), replace=False):
            pts.append((float(xs2[i]), float(ys2[i])))
    try:
        masks, _ = sam.predict_all(key, pts, [1] * len(pts), box, crop)
    finally:
        sam.forget(key)
    best, best_iou = None, 0.0
    for cand in masks:
        i = _iou(cand, cmask)
        if i > best_iou:
            best, best_iou = cand, i
    return best


def _isnet_crop(crop: np.ndarray) -> np.ndarray | None:
    try:
        return auto_mask(crop)
    except Exception:  # noqa: BLE001
        return None


def refine_mask(
    img_bgr: np.ndarray,
    mask: np.ndarray,
    margin_frac: float = 0.3,
    min_side: int = 512,
    min_iou: float = 0.85,
) -> np.ndarray:
    """Re-segment a tool on a crop around it (3-5x more pixels per mm for the models) and keep the
    best candidate.

    Candidates: the original mask, SAM 2 on the crop (box + interior points), IS-Net on the crop.
    A candidate must agree with the original (IoU >= min_iou); among those the *smoothest* wins,
    because real tool outlines are smooth and wobble is what a low-resolution mask adds. This keeps
    IS-Net's clean trace on transparent handles where SAM gets confused, and takes SAM's sharper
    edges on thin shafts and blades.
    """
    if mask.sum() < 50:
        return mask
    cx0, cy0, cx1, cy1, (x0, y0, x1, y1) = _crop_box(mask, margin_frac, min_side)
    crop = img_bgr[cy0:cy1, cx0:cx1]
    cmask = mask[cy0:cy1, cx0:cx1]
    box = (float(x0 - cx0 - 2), float(y0 - cy0 - 2), float(x1 - cx0 + 2), float(y1 - cy0 + 2))
    cands: list[tuple[str, np.ndarray]] = [("orig", cmask)]
    try:
        sm = _sam_crop(crop, cmask, box)
        if sm is not None:
            cands.append(("sam", sm))
    except Exception:  # noqa: BLE001
        pass
    im = _isnet_crop(crop)
    if im is not None:
        cands.append(("isnet", im))
    scored = []
    for name, m in cands:
        comps = split_components(m, 1.0, 0.0)
        keep = np.zeros_like(cmask)
        for c in comps:
            if (c & cmask).sum() > 0.2 * c.sum():
                keep |= c
        if keep.sum() == 0:
            continue
        iou = _iou(keep, cmask)
        if name != "orig" and iou < min_iou:
            continue
        scored.append((_roughness(keep), name, keep))
    scored.sort(key=lambda t: t[0])
    best = scored[0][2]
    out = mask.copy()
    out[cy0:cy1, cx0:cx1] = best
    return cv2.morphologyEx(out.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) > 0
