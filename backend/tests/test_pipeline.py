import sys
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import outline, paper, segment  # noqa: E402
from app.config import MODELS_DIR, SAM_DIR  # noqa: E402
from app.gridfinity import BinConfig, Pocket, build_bin, export_bytes  # noqa: E402
from tests.synth import LETTER, make_photo  # noqa: E402

HAVE_SAM = (SAM_DIR / "onnx" / "vision_encoder.onnx").exists()
HAVE_ISNET = any(MODELS_DIR.rglob("isnet-general-use.onnx"))


def _truth():
    body, handle = make_photo()[2]
    return Polygon(body).union(Polygon(handle))


def _rectified(silver=False):
    img, corners, _ = make_photo(silver=silver)
    det = paper.detect_paper(img)
    assert det is not None
    err = np.abs(np.array(det) - paper.order_corners(corners)).max()
    assert err < 6, f"corner error {err}px"
    warped, meta = paper.rectify(img, det, LETTER[0], LETTER[1])
    return warped, meta


def _check(polys, meta, iou_min=0.9):
    assert polys, "no tool found"
    m = meta["margin_mm"]
    got = Polygon([[x - m, y - m] for x, y in polys[0]])
    truth = _truth()
    iou = got.intersection(truth).area / got.union(truth).area
    assert iou > iou_min, f"IoU {iou:.3f}"
    gb, tb = got.bounds, truth.bounds
    assert abs((gb[2] - gb[0]) - (tb[2] - tb[0])) < 1.5, "width off"
    assert abs((gb[3] - gb[1]) - (tb[3] - tb[1])) < 1.5, "height off"
    return iou


def test_paper_and_classical():
    warped, meta = _rectified()
    mask = segment.classical_mask(warped, meta["paper_px"])
    comps = segment.split_components(mask, meta["px_per_mm"], 100)
    polys = outline.mask_to_polygons_mm(comps[0], meta["px_per_mm"])
    print("classical IoU", _check(polys, meta))


@pytest.mark.skipif(not HAVE_ISNET, reason="isnet weights missing")
def test_isnet_auto():
    warped, meta = _rectified(silver=True)
    mask = segment.auto_mask(warped)
    comps = segment.split_components(mask, meta["px_per_mm"], 100)
    polys = outline.mask_to_polygons_mm(comps[0], meta["px_per_mm"])
    print("isnet IoU", _check(polys, meta, iou_min=0.85))


@pytest.mark.skipif(not HAVE_SAM, reason="sam weights missing")
def test_sam_click():
    warped, meta = _rectified(silver=True)
    sam = segment.get_sam()
    sam.embed("t", warped)
    ppm, m = meta["px_per_mm"], meta["margin_mm"]
    click = ((60 + 80 + m) * ppm, (80 + 18 + m) * ppm)  # on the handle
    mask = sam.predict("t", [click], [1])
    polys = outline.mask_to_polygons_mm(mask, ppm)
    print("sam IoU", _check(polys, meta, iou_min=0.85))


def test_offset_and_bin():
    poly = [[0, 0], [50, 0], [50, 20], [0, 20]]
    off = outline.offset_polygon(poly, 1.0)
    st = outline.polygon_stats(off)
    assert abs(st["area_mm2"] - (1000 + 140 + np.pi)) < 8
    cfg = BinConfig(grid_x=2, grid_y=1, height_units=3, holes="magnet")
    part = build_bin(cfg, [Pocket(polygon=[[p[0] + 10, p[1] + 10] for p in off], depth=10)])
    assert part.is_valid
    solid = build_bin(cfg).volume
    assert solid - part.volume > 50 * 20 * 10 * 0.9
    for fmt in ("stl", "3mf", "step"):
        assert len(export_bytes(part, fmt)) > 1000
