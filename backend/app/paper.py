"""Find the sheet of paper in a photo and rectify the photo so that 1 px = 1/PX_PER_MM mm.

The paper is the scale reference: once we know its four corners in the photo and its
real size in mm, a homography maps the photo onto a metric plane. Everything on the
paper plane is then measurable. (Objects with height are slightly magnified by
parallax; see outline.height_compensation.)

Detection strategy (glossy benches and glare defeat plain thresholding):
  1. Bright, low-saturation blobs give candidate regions and prompt points.
  2. SAM 2 is prompted with interior points of the best blob (+) and the frame
     edges (-); it understands "the sheet" semantically and ignores glare.
  3. The winning mask's boundary is split into four sides, a line is fitted to
     each, and the corners are the line intersections (sub-pixel, straight edges).
  4. Candidates are scored by rectangularity and brightness; classical contours
     remain as a fallback when SAM is unavailable.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .config import PX_PER_MM, RECTIFY_MARGIN_MM

log = logging.getLogger(__name__)
Quad = list[list[float]]  # [[x,y],...] 4 points, image pixels
_WORK = 1200  # long side used for detection


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    pts = pts[np.argsort(ang)]  # counter-clockwise in image coords (y down) == clockwise on screen
    # rotate so the first point is the one with the smallest x+y
    i = int(np.argmin(pts.sum(axis=1)))
    pts = np.roll(pts, -i, axis=0)
    return pts.astype(np.float32)


# ----------------------------------------------------------------- helpers
def _bright_mask(img_bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    m = ((v > 120) & (s < 80)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return m


def _largest_component(mask: np.ndarray, min_frac: float = 0.03) -> np.ndarray | None:
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n < 2:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[i, cv2.CC_STAT_AREA] < min_frac * mask.size:
        return None
    return (labels == i).astype(np.uint8) * 255


def _rectangularity(mask: np.ndarray) -> tuple[float, np.ndarray | None]:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.0, None
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area <= 0:
        return 0.0, None
    peri = cv2.arcLength(c, True)
    quad = None
    for eps in (0.01, 0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = approx.reshape(4, 2).astype(np.float32)
            break
    if quad is None:
        quad = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
    qarea = abs(cv2.contourArea(quad.reshape(-1, 1, 2)))
    return (area / qarea if qarea > 0 else 0.0), quad


def _refine_quad_from_mask(mask: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Fit a straight line to the boundary points nearest each quad side; intersect them."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return quad
    pts = max(contours, key=cv2.contourArea).reshape(-1, 2).astype(np.float32)
    q = order_corners(quad)
    lines = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        d = b - a
        L = np.linalg.norm(d) + 1e-9
        n = np.array([-d[1], d[0]]) / L
        # distance from the side's line, and position along it
        rel = pts - a
        dist = np.abs(rel @ n)
        t = (rel @ d) / (L * L)
        sel = pts[(dist < 0.03 * L) & (t > 0.1) & (t < 0.9)]
        if len(sel) < 20:
            lines.append((a, d / L))
            continue
        vx, vy, x0, y0 = cv2.fitLine(sel, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    out = []
    for i in range(4):
        p1, d1 = lines[(i - 1) % 4]
        p2, d2 = lines[i]
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 1e-6:
            out.append(q[i])
            continue
        s, _ = np.linalg.solve(A, p2 - p1)
        out.append(p1 + s * d1)
    return order_corners(np.array(out, np.float32))


def _edge_support(gray: np.ndarray, quad: np.ndarray, samples: int = 60) -> float:
    """Fraction of the quad's perimeter that sits on a strong intensity edge."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    H, W = gray.shape
    q = order_corners(quad)
    hits, total = 0, 0
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        d = b - a
        L = float(np.linalg.norm(d)) + 1e-9
        n = np.array([-d[1], d[0]]) / L
        for t in np.linspace(0.08, 0.92, samples):
            p = a + d * t
            best = 0.0
            for off in (-3, -2, -1, 0, 1, 2, 3):
                x, y = p + n * off
                xi, yi = int(round(x)), int(round(y))
                if 0 <= xi < W and 0 <= yi < H:
                    best = max(best, float(mag[yi, xi]))
            total += 1
            hits += best > 40.0
    return hits / max(total, 1)


def _score(mask: np.ndarray, gray: np.ndarray) -> tuple[float, np.ndarray | None]:
    """Score a candidate paper mask. Returns (score, refined quad)."""
    rect, quad = _rectangularity(mask)
    if quad is None or rect < 0.6:
        return 0.0, None
    area_frac = float((mask > 0).sum()) / mask.size
    if area_frac < 0.04 or area_frac > 0.95:
        return 0.0, None
    h, w = mask.shape
    touch = float(
        (
            (mask[0, :] > 0).mean()
            + (mask[-1, :] > 0).mean()
            + (mask[:, 0] > 0).mean()
            + (mask[:, -1] > 0).mean()
        )
        / 4
    )
    quad = _refine_quad_from_mask(mask, quad)
    quad = _refine_quad_hough(gray, quad)
    # convexity / sanity of the refined quad
    if not cv2.isContourConvex(quad.reshape(-1, 1, 2).astype(np.float32)):
        return 0.0, None
    qmask = np.zeros_like(mask)
    cv2.fillPoly(qmask, [quad.astype(np.int32)], 255)
    brightness = float(cv2.mean(gray, mask=qmask)[0]) / 255.0
    if brightness < 0.45:  # paper is white; dark rectangles (pouches, phones) are not the sheet
        return 0.0, None
    inside = gray[qmask > 0]
    uniformity = 1.0 / (1.0 + float(inside.std()) / 40.0) if inside.size else 0.0
    # Most of the interior should look like paper (near the region's own white level).
    white = float(np.percentile(inside, 90)) if inside.size else 255.0
    paper_frac = float((inside > 0.72 * white).mean()) if inside.size else 0.0
    support = _edge_support(gray, quad)
    qfrac = float((qmask > 0).mean())
    area_prior = min(1.0, qfrac / 0.2)  # sheets should fill a good part of the frame
    score = (
        (rect**2)
        * (support**2)
        * (0.3 + brightness)
        * (0.5 + uniformity)
        * (paper_frac**2)
        * (1 - 0.8 * touch)
        * area_prior
    )
    return score, quad


# ----------------------------------------------------------------- SAM path
def _sam_paper_masks(small: np.ndarray, blob: np.ndarray | None) -> list[np.ndarray]:
    """Several prompt variants x 3 SAM hypotheses -> candidate paper masks (deduplicated)."""
    try:
        from .segment import get_sam

        sam = get_sam()
    except Exception as e:  # noqa: BLE001
        log.info("SAM unavailable for paper detection: %s", e)
        return []
    h, w = small.shape[:2]
    centre = (w / 2, h / 2)
    frame = [
        (2.0, 2.0),
        (w - 3.0, 2.0),
        (w - 3.0, h - 3.0),
        (2.0, h - 3.0),
        (w / 2, 2.0),
        (w / 2, h - 3.0),
        (2.0, h / 2),
        (w - 3.0, h / 2),
    ]
    variants: list[tuple[list, list, tuple | None]] = []
    if blob is not None:
        dt = cv2.distanceTransform((blob > 0).astype(np.uint8), cv2.DIST_L2, 5)
        cy, cx = np.unravel_index(int(np.argmax(dt)), dt.shape)
        peak = (float(cx), float(cy))
        neg = [p for p in frame if blob[int(p[1]), int(p[0])] == 0] or frame
        variants.append(([peak], [1], None))
        variants.append(([peak] + neg, [1] + [0] * len(neg), None))
        variants.append(([peak, centre] + neg, [1, 1] + [0] * len(neg), None))
        x, y, bw, bh = cv2.boundingRect((blob > 0).astype(np.uint8))
        variants.append(([peak], [1], (x + 0.05 * bw, y + 0.05 * bh, x + 0.95 * bw, y + 0.95 * bh)))
    variants.append(([centre], [1], None))
    variants.append(([centre] + frame, [1] + [0] * len(frame), None))
    key = "paper-" + str(id(small))
    sam.embed(key, small)
    out: list[np.ndarray] = []
    try:
        for pts, labs, box in variants:
            try:
                masks, _ = sam.predict_all(key, pts, labs, box, small)
            except Exception as e:  # noqa: BLE001
                log.warning("SAM paper prompt failed: %s", e)
                continue
            for m in masks:
                mm = cv2.morphologyEx(m.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
                comp = _largest_component(mm)
                if comp is None:
                    continue
                if any(((comp > 0) ^ (o > 0)).mean() < 0.01 for o in out):
                    continue
                out.append(comp)
    finally:
        sam.forget(key)  # the embedding cache is tiny; never leave a one-shot key in it
    return out


def _refine_quad_hough(gray: np.ndarray, quad: np.ndarray, band_frac: float = 0.12) -> np.ndarray:
    """Snap each side of a coarse quad to the longest straight edge nearby (the paper edge).

    Segments from a probabilistic Hough transform inside a band around the side are
    clustered by their perpendicular offset; the cluster with the most total length wins,
    which favours the long, straight paper edge over ragged glare boundaries.
    """
    q = order_corners(quad)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 30, 90)
    H, W = gray.shape
    lines = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        d = b - a
        L = float(np.linalg.norm(d)) + 1e-9
        u = d / L
        n = np.array([-u[1], u[0]])
        band = band_frac * L
        a2, b2 = a + u * 0.05 * L, b - u * 0.05 * L
        poly = np.array([a2 + n * band, b2 + n * band, b2 - n * band, a2 - n * band], np.int32)
        m = np.zeros_like(edges)
        cv2.fillPoly(m, [poly], 255)
        e = cv2.bitwise_and(edges, m)
        segs = cv2.HoughLinesP(
            e, 1, np.pi / 360, threshold=25, minLineLength=max(10, 0.06 * L), maxLineGap=0.02 * L
        )
        cands = []  # (offset, length, p1, p2)
        if segs is not None:
            for x1, y1, x2, y2 in segs.reshape(-1, 4):
                v = np.array([x2 - x1, y2 - y1], np.float32)
                ln = float(np.linalg.norm(v))
                if ln < 1 or abs(float(v @ u)) / ln < np.cos(np.radians(10)):
                    continue
                mid = np.array([(x1 + x2) / 2, (y1 + y2) / 2])
                cands.append((float((mid - a) @ n), ln, (x1, y1), (x2, y2)))
        if not cands:
            lines.append((a, u))
            continue
        cands.sort(key=lambda c: c[0])
        # cluster by offset (within 1% of L)
        clusters, cur = [], [cands[0]]
        for c in cands[1:]:
            if c[0] - cur[-1][0] < 0.012 * L:
                cur.append(c)
            else:
                clusters.append(cur)
                cur = [c]
        clusters.append(cur)
        # Strong clusters are those with at least half the length of the longest; among them
        # take the innermost: glare merges and cast shadows both lie *outside* the real edge.
        centroid = q.mean(axis=0)
        inward = 1.0 if float((centroid - a) @ n) > 0 else -1.0

        lengths = [sum(c[1] for c in cl) for cl in clusters]
        strong = [
            cl for cl, ln in zip(clusters, lengths, strict=True) if ln >= 0.5 * max(lengths) and ln >= 0.3 * L
        ]
        if not strong:
            lines.append((a, u))
            continue
        best = max(strong, key=lambda cl: inward * float(np.mean([c[0] for c in cl])))
        total = sum(c[1] for c in best)
        if total < 0.3 * L:
            lines.append((a, u))
            continue
        pts = np.array([p for c in best for p in (c[2], c[3])], np.float32)
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    out = []
    for i in range(4):
        p1, d1 = lines[(i - 1) % 4]
        p2, d2 = lines[i]
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 1e-6:
            out.append(q[i])
            continue
        s_, _ = np.linalg.solve(A, p2 - p1)
        out.append(p1 + s_ * d1)
    r = order_corners(np.array(out, np.float32))
    if np.abs(r - q).max() > 0.2 * max(H, W):
        return q
    return r


def detect_paper(
    img_bgr: np.ndarray, use_sam: bool = True, expected_aspect: float | None = None
) -> Quad | None:
    """Detect the paper quadrilateral. Returns 4 ordered corners (TL, TR, BR, BL) or None."""
    h, w = img_bgr.shape[:2]
    scale = min(1.0, _WORK / max(h, w))
    small = (
        cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img_bgr
    )
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    candidates: list[tuple[float, np.ndarray, np.ndarray, str]] = []  # score, quad, mask, source
    bright = _bright_mask(small)
    blob = _largest_component(bright)
    if blob is not None:
        sc, q = _score(blob, gray)
        if q is not None:
            candidates.append((sc, q, blob, "bright"))
    # classical threshold contours at several levels, with a large opening to cut glare bridges
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    otsu_t, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for name, t in (("otsu", otsu_t), ("t+20", min(otsu_t + 20, 250)), ("t+40", min(otsu_t + 40, 250))):
        _, th = cv2.threshold(blur, t, 255, cv2.THRESH_BINARY)
        for k in (0, 31):
            m = (
                th
                if k == 0
                else cv2.morphologyEx(
                    th, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                )
            )
            comp = _largest_component(m)
            if comp is None:
                continue
            sc, q = _score(comp, gray)
            if q is not None:
                candidates.append((sc, q, comp, f"{name}/open{k}"))
    if use_sam:
        for k, sm in enumerate(_sam_paper_masks(small, blob)):
            sc, q = _score(sm, gray)
            if q is not None:
                candidates.append((sc * 1.2, q, sm, f"sam{k}"))  # semantic masks get the benefit of the doubt

    if not candidates:
        return None
    if expected_aspect:
        # Mild prior: for a near top-down shot the quad's long/short side ratio should match the sheet.
        def aspect_factor(c):
            qq = order_corners(c[1])
            top = (np.linalg.norm(qq[1] - qq[0]) + np.linalg.norm(qq[2] - qq[3])) / 2
            side = (np.linalg.norm(qq[3] - qq[0]) + np.linalg.norm(qq[2] - qq[1])) / 2
            r = max(top, side) / max(min(top, side), 1e-6)
            return float(np.exp(-abs(np.log(r / expected_aspect)) / 0.12))

        candidates = [(c[0] * (0.5 + 0.5 * aspect_factor(c)), c[1], c[2], c[3]) for c in candidates]
    sc, q, m, src = max(candidates, key=lambda c: c[0])
    log.info("paper: %s (score %.2f) of %s", src, sc, [(c[3], round(c[0], 2)) for c in candidates])
    q = np.clip(q / scale, [0, 0], [w - 1, h - 1])
    return order_corners(q).tolist()


def default_quad(width: int, height: int) -> Quad:
    """A centred quad used when detection fails, so the user can drag the corners."""
    mx, my = width * 0.1, height * 0.1
    return [[mx, my], [width - mx, my], [width - mx, height - my], [mx, height - my]]


def paper_orientation(quad: Quad) -> str:
    """'landscape' if the paper's long edge runs left-right in the photo."""
    q = order_corners(np.array(quad))
    top = np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])
    side = np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])
    return "landscape" if top > side else "portrait"


def sheet_size_mm(size: tuple[float, float], orientation: str) -> tuple[float, float]:
    """(width_mm, height_mm) of a sheet as it appears in the photo: the long side runs left-right
    for 'landscape', top-bottom otherwise. `size` may be given in either order."""
    short, long_ = min(size), max(size)
    return (long_, short) if orientation == "landscape" else (short, long_)


def rectify(
    img_bgr: np.ndarray,
    quad: Quad,
    paper_w_mm: float,
    paper_h_mm: float,
    px_per_mm: float = PX_PER_MM,
    margin_mm: float = RECTIFY_MARGIN_MM,
) -> tuple[np.ndarray, dict]:
    """Warp the photo so the paper becomes an axis-aligned rectangle at px_per_mm.

    paper_w_mm/paper_h_mm are the sheet's dimensions *as they appear in the photo*
    (already swapped for landscape). A margin of margin_mm is included on every side
    so tools overhanging the sheet are preserved. Returned meta gives the paper's
    pixel rectangle inside the output image.
    """
    src = order_corners(np.array(quad))
    pw, ph = paper_w_mm * px_per_mm, paper_h_mm * px_per_mm
    m = margin_mm * px_per_mm
    dst = np.array([[m, m], [m + pw, m], [m + pw, m + ph], [m, m + ph]], dtype=np.float32)
    H = cv2.getPerspectiveTransform(src, dst)
    out_w, out_h = int(round(pw + 2 * m)), int(round(ph + 2 * m))
    warped = cv2.warpPerspective(
        img_bgr, H, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    meta = {
        "width": out_w,
        "height": out_h,
        "px_per_mm": px_per_mm,
        "margin_mm": margin_mm,
        "paper_w_mm": paper_w_mm,
        "paper_h_mm": paper_h_mm,
        "paper_px": [m, m, m + pw, m + ph],
        "homography": H.tolist(),
    }
    return warped, meta
