"""Run paper detection + rectification + auto detection over a folder of photos and save overlays.

usage: python scripts/batch_eval.py <photo_dir> <out_dir> [method=auto|classical]
"""

from __future__ import annotations

import glob
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("U2NET_HOME", str(Path(__file__).resolve().parent.parent.parent / "models" / "u2net"))
from app import outline, paper, segment  # noqa: E402
from app.config import MAX_UPLOAD_SIDE, PAPER_SIZES  # noqa: E402
from app.imageio import decode_image  # noqa: E402


def main():
    src, out = sys.argv[1], sys.argv[2]
    method = sys.argv[3] if len(sys.argv) > 3 else "auto"
    os.makedirs(out, exist_ok=True)
    files = sorted(
        f
        for f in glob.glob(f"{src}/*")
        if f.lower().endswith((".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"))
    )
    for f in files:
        name = Path(f).stem
        t0 = time.time()
        img = decode_image(open(f, "rb").read(), MAX_UPLOAD_SIDE)
        quad = paper.detect_paper(img, expected_aspect=279.4 / 215.9)
        t1 = time.time()
        ov = img.copy()
        if quad is not None:
            cv2.polylines(ov, [np.array(quad, np.int32)], True, (0, 0, 255), 6)
            for i, (x, y) in enumerate(quad):
                cv2.circle(ov, (int(x), int(y)), 18, (0, 255, 0), -1)
                cv2.putText(
                    ov,
                    "TL TR BR BL".split()[i],
                    (int(x) + 20, int(y)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.5,
                    (0, 255, 0),
                    3,
                )
        cv2.imwrite(f"{out}/{name}_1_paper.jpg", cv2.resize(ov, None, fx=0.4, fy=0.4))
        if quad is None:
            print(f"{name}: paper NOT found ({t1 - t0:.1f}s)")
            continue
        orient = paper.paper_orientation(quad)
        pw, ph = PAPER_SIZES["letter"]
        w_mm, h_mm = (max(pw, ph), min(pw, ph)) if orient == "landscape" else (min(pw, ph), max(pw, ph))
        rect, meta = paper.rectify(img, quad, w_mm, h_mm)
        ppm = meta["px_per_mm"]
        t2 = time.time()
        mask = segment.auto_mask(rect) if method == "auto" else segment.classical_mask(rect, meta["paper_px"])
        t3 = time.time()
        border = np.zeros_like(mask)
        border[:2, :] = border[-2:, :] = border[:, :2] = border[:, -2:] = True
        comps = [c for c in segment.split_components(mask, ppm, 150) if not (c & border).any()]
        ov = rect.copy()
        x0, y0, x1, y1 = [int(v) for v in meta["paper_px"]]
        cv2.rectangle(ov, (x0, y0), (x1, y1), (255, 128, 0), 2)
        n = 0
        for c in comps:
            for poly in outline.mask_to_polygons_mm(c, ppm, 0.3, 150):
                pts = (np.array(poly) * ppm).astype(np.int32)
                cv2.polylines(ov, [pts], True, (0, 0, 255), 3)
                st = outline.polygon_stats(poly)
                bx = st["bbox"]
                cv2.putText(
                    ov,
                    f"{bx[2] - bx[0]:.0f}x{bx[3] - bx[1]:.0f}mm",
                    (pts[0][0], pts[0][1] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 0, 255),
                    2,
                )
                n += 1
        cv2.imwrite(f"{out}/{name}_2_{method}.jpg", cv2.resize(ov, None, fx=0.6, fy=0.6))
        print(
            f"{name}: paper ok ({orient}), {n} tools, detect {t1 - t0:.1f}s rectify {t2 - t1:.1f}s {method} {t3 - t2:.1f}s"
        )


if __name__ == "__main__":
    main()
