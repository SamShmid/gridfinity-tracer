"""Synthetic test photo: Letter sheet under perspective on a dark bench with a known tool."""
from __future__ import annotations

import cv2
import numpy as np

LETTER = (279.4, 215.9)  # landscape w,h mm


def tool_polygon_mm(x0=60.0, y0=80.0):
    """A wrench-ish shape ~120 x 34 mm in paper mm (origin top-left of paper)."""
    pts = [(0, 10), (20, 0), (34, 6), (34, 28), (20, 34), (0, 24), (8, 17)]
    handle = [(30, 12), (120, 14), (120, 22), (30, 24)]
    body = [(x0 + x, y0 + y) for x, y in pts]
    return body, [(x0 + x, y0 + y) for x, y in handle]


def make_photo(w=1600, h=1200, px_per_mm=4.0, silver=False, seed=0):
    rng = np.random.default_rng(seed)
    # Flat "top-down" canvas in mm space, then warp with a mild perspective.
    pw, ph = LETTER
    canvas_w, canvas_h = int(pw * px_per_mm * 1.35), int(ph * px_per_mm * 1.45)
    img = np.full((canvas_h, canvas_w, 3), (70, 75, 80), np.uint8)  # dark bench
    ox, oy = int(canvas_w * 0.12), int(canvas_h * 0.13)
    paper = np.array([[ox, oy], [ox + pw * px_per_mm, oy], [ox + pw * px_per_mm, oy + ph * px_per_mm], [ox, oy + ph * px_per_mm]], np.float32)
    cv2.fillPoly(img, [paper.astype(np.int32)], (236, 238, 240))
    body, handle = tool_polygon_mm()
    colour = (185, 188, 190) if silver else (35, 38, 40)
    for poly in (body, handle):
        p = (np.array(poly, np.float32) * px_per_mm + [ox, oy]).astype(np.int32)
        cv2.fillPoly(img, [p], colour)
    # soft shadow offset from the tool
    shadow = np.zeros(img.shape[:2], np.float32)
    for poly in (body, handle):
        p = (np.array(poly, np.float32) * px_per_mm + [ox + 6, oy + 6]).astype(np.int32)
        cv2.fillPoly(shadow, [p], 1.0)
    shadow = cv2.GaussianBlur(shadow, (31, 31), 0) * 0.25
    img = (img.astype(np.float32) * (1 - shadow[..., None])).astype(np.uint8)
    # perspective: tilt the camera a bit
    src = np.array([[0, 0], [canvas_w, 0], [canvas_w, canvas_h], [0, canvas_h]], np.float32)
    dst = np.array([[80, 60], [w - 120, 30], [w - 40, h - 40], [30, h - 90]], np.float32)
    H = cv2.getPerspectiveTransform(src, dst)
    out = cv2.warpPerspective(img, H, (w, h), borderValue=(60, 62, 66))
    out = np.clip(out.astype(np.float32) + rng.normal(0, 3, out.shape), 0, 255).astype(np.uint8)
    corners = cv2.perspectiveTransform(paper.reshape(1, 4, 2), H).reshape(4, 2)
    return out, corners, (body, handle)
