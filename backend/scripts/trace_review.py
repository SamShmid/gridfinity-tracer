"""Full tracing pipeline over a folder: paper -> rectify -> IS-Net -> refine -> pocket outline.
Writes one crop per tool with raw (red), refined (blue), and final pocket outline (green)."""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("U2NET_HOME", str(Path(__file__).resolve().parent.parent.parent / "models" / "u2net"))
from app import outline, paper, segment  # noqa: E402
from app.config import MAX_UPLOAD_SIDE  # noqa: E402
from app.imageio import decode_image  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
for f in sorted(glob.glob(f"{src}/*.HEIC")):
    name = Path(f).stem
    img = decode_image(open(f, "rb").read(), MAX_UPLOAD_SIDE)
    q = paper.detect_paper(img, expected_aspect=279.4 / 215.9)
    o = paper.paper_orientation(q)
    w, h = (279.4, 215.9) if o == "landscape" else (215.9, 279.4)
    rect, meta = paper.rectify(img, q, w, h)
    ppm = meta["px_per_mm"]
    mask = segment.auto_mask(rect)
    x0, y0, x1, y1 = [int(v) for v in meta["paper_px"]]
    sheet = np.zeros_like(mask); sheet[y0:y1, x0:x1] = True
    comps = [c for c in segment.split_components(mask, ppm, 150) if (c & sheet).sum() >= 0.6 * c.sum()]
    for k, c in enumerate(comps):
        r = segment.refine_mask(rect, c)
        raw = outline.mask_to_polygons_mm(c, ppm, 0.3, 150)[0]
        ref = outline.mask_to_polygons_mm(r, ppm, 0.3, 150)[0]
        pocket = outline.prepare_pocket(ref, 0.5, 0.2, gaussian_mm=1.0)
        pts = (np.array(pocket) * ppm).astype(np.int32)
        x, y, bw, bh = cv2.boundingRect(pts); pad = 30
        ox, oy = max(x - pad, 0), max(y - pad, 0)
        crop = rect[oy:y + bh + pad, ox:x + bw + pad].copy()
        for poly, col in [(raw, (0, 0, 255)), (ref, (255, 128, 0)), (pocket, (0, 170, 0))]:
            cv2.polylines(crop, [(np.array(poly) * ppm).astype(np.int32) - [ox, oy]], True, col, 2)
        if crop.shape[0] > crop.shape[1]:
            crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
        sc = 1200 / crop.shape[1]; crop = cv2.resize(crop, None, fx=sc, fy=sc)
        cv2.putText(crop, f"{name} tool{k}  red=IS-Net  blue=refined  green=pocket(+0.7mm)", (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
        cv2.imwrite(f"{out}/{name}_{k}.jpg", crop)
        print(name, k, "pts raw/ref/pocket", len(raw), len(ref), len(pocket))
