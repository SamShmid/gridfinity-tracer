"""Masks -> polygons in millimetres, plus simplification and clearance offsets."""

from __future__ import annotations

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.validation import make_valid

PolyMM = list[list[float]]  # [[x_mm, y_mm], ...] ring, not closed
# Upper bound on resampled contour points: 100k at 0.25 mm is a 25 m perimeter. Beyond that the
# step grows, so a hostile polygon cannot make the Gaussian index matrix (n x 2k+1) eat memory.
MAX_RESAMPLE_POINTS = 100_000


def _largest_polygon(geom) -> Polygon | None:
    if geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return geom
    if hasattr(geom, "geoms"):  # MultiPolygon / GeometryCollection
        polys = [g for g in geom.geoms if isinstance(g, Polygon)]
        return max(polys, key=lambda g: g.area) if polys else None
    return None


def mask_to_polygons_mm(
    mask: np.ndarray,
    px_per_mm: float,
    tolerance_mm: float = 0.3,
    min_area_mm2: float = 100.0,
) -> list[PolyMM]:
    """Extract external contours from a boolean mask and convert to mm polygons.

    Coordinates are in rectified-image mm (origin = image top-left, y down).
    Holes (e.g. inside plier handles) are filled: a pocket with an island would
    need a bridge anyway and is almost never what a tool holder wants.
    """
    m = (mask.astype(np.uint8) > 0).astype(np.uint8) * 255
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[PolyMM] = []
    min_area_px = min_area_mm2 * px_per_mm * px_per_mm
    eps = max(tolerance_mm * px_per_mm, 0.5)
    for c in contours:
        if cv2.contourArea(c) < min_area_px:
            continue
        approx = cv2.approxPolyDP(c, eps, True).reshape(-1, 2).astype(np.float64)
        if len(approx) < 3:
            continue
        poly = Polygon(approx / px_per_mm)
        poly = _largest_polygon(make_valid(poly))
        if poly is None or poly.area < min_area_mm2:
            continue
        out.append([[float(x), float(y)] for x, y in poly.exterior.coords[:-1]])
    out.sort(key=lambda p: -Polygon(p).area)
    return out


def clean_polygon(poly: PolyMM) -> Polygon | None:
    if len(poly) < 3:
        return None
    p = make_valid(Polygon(poly))
    return _largest_polygon(p)


def offset_polygon(
    poly: PolyMM, clearance_mm: float, tolerance_mm: float = 0.2, smooth_mm: float = 0.0
) -> PolyMM:
    """Inflate (or deflate) a polygon by clearance_mm with round joins.

    smooth_mm > 0 applies a morphological open/close (erode then dilate and back),
    which removes spikes and pinches smaller than that radius.
    """
    p = clean_polygon(poly)
    if p is None:
        return poly
    if smooth_mm > 0:
        p = _largest_polygon(p.buffer(-smooth_mm).buffer(2 * smooth_mm).buffer(-smooth_mm)) or p
    if abs(clearance_mm) > 1e-6:
        q = p.buffer(clearance_mm, join_style="round", quad_segs=8)
        p = _largest_polygon(q) or p
    if tolerance_mm > 0:
        p = p.simplify(tolerance_mm, preserve_topology=True)
    return [[float(x), float(y)] for x, y in p.exterior.coords[:-1]]


def convex_hull(poly: PolyMM) -> PolyMM:
    p = clean_polygon(poly)
    if p is None:
        return poly
    h = p.convex_hull
    return [[float(x), float(y)] for x, y in h.exterior.coords[:-1]]


def polygon_stats(poly: PolyMM) -> dict:
    p = clean_polygon(poly)
    if p is None:
        return {"area_mm2": 0, "bbox": [0, 0, 0, 0], "centroid": [0, 0]}
    minx, miny, maxx, maxy = p.bounds
    c = p.centroid
    return {"area_mm2": float(p.area), "bbox": [minx, miny, maxx, maxy], "centroid": [float(c.x), float(c.y)]}


def height_compensation(poly: PolyMM, tool_height_mm: float, camera_distance_mm: float) -> PolyMM:
    """Shrink a silhouette traced at tool_height_mm above the paper back to true size.

    A pinhole camera at distance D sees an object of height h magnified by D/(D-h).
    We scale about the centroid by (D-h)/D. Rough, but recovers most of the parallax
    error for flat tools shot from arm's length.
    """
    if tool_height_mm <= 0 or camera_distance_mm <= tool_height_mm:
        return poly
    p = clean_polygon(poly)
    if p is None:
        return poly
    c = p.centroid
    k = (camera_distance_mm - tool_height_mm) / camera_distance_mm
    return [[float(c.x + (x - c.x) * k), float(c.y + (y - c.y) * k)] for x, y in poly]


def straighten_angle(poly: PolyMM) -> float:
    """Rotation (degrees, screen/y-down coords) that makes the polygon's long axis horizontal.

    Uses the minimum-area bounding rectangle, which is what makes a tool "look straight"
    in a bin (a wrench handle along X, pliers lengthwise).
    """
    pts = np.asarray(poly, np.float32)
    if len(pts) < 3:
        return 0.0
    (cx, cy), (w, h), ang = cv2.minAreaRect(pts)
    # OpenCV: ang in (0, 90]; w is the side that ang refers to. Make the long side horizontal.
    if w < h:
        ang = ang - 90.0
    rot = -ang
    while rot <= -90:
        rot += 180
    while rot > 90:
        rot -= 180
    return float(rot)


# ----------------------------------------------------------------- realism pipeline
def _resample(pts: np.ndarray, step_mm: float) -> np.ndarray:
    """Resample a closed ring at roughly uniform spacing along its perimeter."""
    p = np.vstack([pts, pts[:1]])
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    total = float(seg.sum())
    n = max(int(total / step_mm), 16)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.linspace(0, total, n, endpoint=False)
    out = np.empty((n, 2))
    j = 0
    for i, t in enumerate(targets):
        while j < len(seg) - 1 and cum[j + 1] < t:
            j += 1
        f = (t - cum[j]) / seg[j] if seg[j] > 0 else 0.0
        out[i] = p[j] + (p[j + 1] - p[j]) * f
    return out


def gaussian_smooth(poly: PolyMM, sigma_mm: float, step_mm: float = 0.25) -> PolyMM:
    """Smooth the contour with a periodic Gaussian on x(t), y(t).

    This is what removes pixel-level wobble from a mask outline while keeping the real shape:
    round tips stay round, long edges become straight. sigma 0.8-1.5 mm suits hand tools.
    """
    if sigma_mm <= 0 or len(poly) < 4:
        return poly
    arr = np.asarray(poly, float)
    perimeter = float(np.linalg.norm(np.diff(np.vstack([arr, arr[:1]]), axis=0), axis=1).sum())
    step_mm = max(step_mm, perimeter / MAX_RESAMPLE_POINTS)
    pts = _resample(arr, step_mm)
    n = len(pts)
    k = int(np.ceil(3 * sigma_mm / step_mm))
    if k < 1:
        return poly
    x = np.arange(-k, k + 1) * step_mm
    w = np.exp(-0.5 * (x / sigma_mm) ** 2)
    w /= w.sum()
    idx = (np.arange(n)[:, None] + np.arange(-k, k + 1)[None, :]) % n
    sm = (pts[idx] * w[None, :, None]).sum(axis=1)
    return [[float(a), float(b)] for a, b in sm]


def principal_axis(poly: PolyMM) -> tuple[np.ndarray, np.ndarray]:
    """(centroid, unit direction of the long axis) from the minimum-area rectangle."""
    pts = np.asarray(poly, np.float32)
    (cx, cy), (w, h), ang = cv2.minAreaRect(pts)
    a = np.radians(ang if w >= h else ang - 90.0)
    return np.array([cx, cy], float), np.array([np.cos(a), np.sin(a)], float)


def snap_edges(poly: PolyMM, tol_deg: float = 6.0, min_len_mm: float = 4.0) -> PolyMM:
    """Straighten edges that are nearly parallel/perpendicular to the tool's long axis.

    Edges longer than min_len_mm whose direction is within tol_deg of the axis (or its normal)
    are rotated about their midpoint to lie exactly on it; adjacent vertices are then re-joined by
    intersecting consecutive edge lines. Short edges (round tips) are left alone.
    """
    pts = np.asarray(poly, float)
    n = len(pts)
    if n < 4:
        return poly
    _, ax = principal_axis(poly)
    nrm = np.array([-ax[1], ax[0]])
    lines = []  # (point, direction) for each edge i: pts[i] -> pts[i+1]
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        d = b - a
        L = np.linalg.norm(d)
        if L < 1e-9:
            lines.append(None)
            continue
        u = d / L
        mid = (a + b) / 2
        snapped = None
        if L >= min_len_mm:
            for ref in (ax, nrm):
                c = abs(float(u @ ref))
                if c >= np.cos(np.radians(tol_deg)):
                    snapped = ref if float(u @ ref) > 0 else -ref
                    break
        lines.append((mid, snapped if snapped is not None else u, snapped is not None))
    out = []
    for i in range(n):
        prev, cur = lines[(i - 1) % n], lines[i]
        if prev is None or cur is None:
            out.append(pts[i])
            continue
        if not prev[2] and not cur[2]:
            out.append(pts[i])
            continue
        p1, d1, _ = prev
        p2, d2, _ = cur
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 1e-3:
            out.append(pts[i])
            continue
        s, _ = np.linalg.solve(A, p2 - p1)
        q = p1 + s * d1
        if np.linalg.norm(q - pts[i]) > 3.0:  # never move a vertex far; keep the original instead
            out.append(pts[i])
        else:
            out.append(q)
    p = clean_polygon([[float(a), float(b)] for a, b in out])
    return [[float(x), float(y)] for x, y in p.exterior.coords[:-1]] if p is not None else poly


def symmetrize(poly: PolyMM) -> PolyMM:
    """Make the outline mirror-symmetric about its long axis (union of shape and its mirror).

    Good for screwdrivers, wrenches, closed pliers; leave off for asymmetric tools.
    """
    p = clean_polygon(poly)
    if p is None:
        return poly
    c, ax = principal_axis(poly)
    pts = np.asarray(poly, float) - c
    along = pts @ ax
    across = pts @ np.array([-ax[1], ax[0]])
    mirrored = np.outer(along, ax) - np.outer(across, np.array([-ax[1], ax[0]])) + c
    m = clean_polygon([[float(a), float(b)] for a, b in mirrored])
    if m is None:
        return poly
    u = _largest_polygon(make_valid(p.union(m)))
    if u is None:
        return poly
    u = u.buffer(0.3).buffer(-0.3)  # heal the seam
    u = _largest_polygon(u) or u
    return [[float(x), float(y)] for x, y in u.exterior.coords[:-1]]


def prepare_pocket(
    poly: PolyMM,
    clearance_mm: float = 0.5,
    printer_offset_mm: float = 0.2,
    gaussian_mm: float = 1.0,
    tolerance_mm: float = 0.3,
    smooth_mm: float = 0.0,
    snap: bool = False,
    symmetric: bool = False,
    convex: bool = False,
    tool_height_mm: float = 0.0,
    camera_distance_mm: float = 0.0,
    bridge_mm: float = 1.5,
) -> PolyMM:
    """Traced outline -> printable pocket outline.

    Order matters: de-noise the trace first (gaussian, open/close), regularise (symmetry,
    straight edges), then apply clearance + printer compensation, then simplify for CAD.
    """
    if tool_height_mm > 0 and camera_distance_mm > 0:
        poly = height_compensation(poly, tool_height_mm, camera_distance_mm)
    poly = gaussian_smooth(poly, gaussian_mm)
    if bridge_mm > 0:
        # Morphological closing: fills gaps narrower than 2*bridge_mm (between open plier jaws,
        # between hex keys) that would otherwise become fragile slivers of bin wall.
        p = clean_polygon(poly)
        if p is not None:
            closed = _largest_polygon(
                p.buffer(bridge_mm, join_style="round").buffer(-bridge_mm, join_style="round")
            )
            if closed is not None:
                poly = [[float(x), float(y)] for x, y in closed.exterior.coords[:-1]]
    if smooth_mm > 0:
        poly = offset_polygon(poly, 0.0, 0.0, smooth_mm)
    if convex:
        poly = convex_hull(poly)
    if symmetric:
        poly = symmetrize(poly)
    if snap:
        poly = snap_edges(poly)
    return offset_polygon(poly, clearance_mm + printer_offset_mm, tolerance_mm, 0.0)
