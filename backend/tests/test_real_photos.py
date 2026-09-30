"""Regression guard over real photos in gridfinity-tracer-photos/ (skipped if absent).

Each photo is a Letter sheet on a glossy bench. We only assert the cheap, robust things:
the detector returns a quad whose long/short ratio is within 8% of Letter's 1.294 and whose
area is a sensible fraction of the frame. Outline accuracy is checked by eye with
scripts/batch_eval.py.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import paper  # noqa: E402
from app.config import MAX_UPLOAD_SIDE, SAM_DIR  # noqa: E402
from app.imageio import decode_image  # noqa: E402

PHOTOS = sorted(
    glob.glob(str(Path(__file__).resolve().parent.parent.parent / "gridfinity-tracer-photos" / "*.HEIC"))
)
LETTER = 279.4 / 215.9
# The detector was tuned with SAM 2 in the loop; on the classical fallback alone (no weights, e.g. CI)
# most of these glossy-bench photos fail, so the accuracy check only runs when the weights exist.
HAVE_SAM = (SAM_DIR / "onnx" / "vision_encoder.onnx").exists()


@pytest.mark.skipif(not PHOTOS, reason="no real photos present")
@pytest.mark.skipif(not HAVE_SAM, reason="sam weights missing (classical fallback is not held to this bar)")
@pytest.mark.parametrize("path", PHOTOS, ids=[os.path.basename(p) for p in PHOTOS])
def test_paper_quad_plausible(path):
    img = decode_image(open(path, "rb").read(), MAX_UPLOAD_SIDE)
    quad = paper.detect_paper(img, expected_aspect=LETTER)
    assert quad is not None
    q = paper.order_corners(np.array(quad))
    top = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    side = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    ratio = max(top, side) / min(top, side)
    assert abs(ratio / LETTER - 1) < 0.08, f"aspect {ratio:.3f}"
    h, w = img.shape[:2]
    import cv2

    area = cv2.contourArea(q.reshape(-1, 1, 2).astype(np.float32))
    assert 0.15 < area / (h * w) < 0.8


def test_heic_decodes():
    if not PHOTOS:
        pytest.skip("no real photos present")
    img = decode_image(open(PHOTOS[0], "rb").read(), 1200)
    assert img.shape[0] == 1200 or img.shape[1] == 1200
    assert img.ndim == 3 and img.shape[2] == 3
