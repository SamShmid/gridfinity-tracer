"""Paths and constants. Everything is overridable by environment variables so the
same code runs in the dev venv and inside Docker."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # backend/
MODELS_DIR = Path(os.environ.get("GT_MODELS_DIR", ROOT.parent / "models"))
DATA_DIR = Path(os.environ.get("GT_DATA_DIR", ROOT.parent / "data"))
STATIC_DIR = Path(os.environ.get("GT_STATIC_DIR", ROOT / "app" / "static"))

SAM_REPO = "onnx-community/sam2.1-hiera-tiny-ONNX"
SAM_DIR = MODELS_DIR / "sam2.1-hiera-tiny"
SAM_FILES = [
    "onnx/vision_encoder.onnx",
    "onnx/vision_encoder.onnx_data",
    "onnx/prompt_encoder_mask_decoder.onnx",
    "onnx/prompt_encoder_mask_decoder.onnx_data",
]
REMBG_MODEL = os.environ.get("GT_REMBG_MODEL", "isnet-general-use")

# Rectified image resolution. 4 px/mm keeps a Letter sheet at ~1100 x 870 px,
# which is plenty for sub-mm outlines and fast for the models.
PX_PER_MM = float(os.environ.get("GT_PX_PER_MM", "4"))
# Extra margin rectified around the paper so tools that overhang the sheet are kept.
RECTIFY_MARGIN_MM = float(os.environ.get("GT_RECTIFY_MARGIN_MM", "25"))
# Longest side the uploaded photo is downscaled to before any processing.
MAX_UPLOAD_SIDE = int(os.environ.get("GT_MAX_UPLOAD_SIDE", "3000"))

# name -> (width_mm, height_mm) in portrait orientation
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
    "tabloid": (279.4, 431.8),
    "a3": (297.0, 420.0),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
}
