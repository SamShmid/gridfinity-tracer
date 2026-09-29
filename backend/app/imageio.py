"""Decode any common photo format (JPEG, PNG, WebP, TIFF, HEIC/HEIF, ...) into a BGR array,
honouring EXIF orientation and downscaling to a maximum side length.

The pixel count is checked from the header (Image.open is lazy) before any pixel is decoded, so a
decompression bomb is refused instead of allocating gigabytes."""
from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image, ImageOps

with __import__("contextlib").suppress(Exception):
    from pillow_heif import register_heif_opener

    register_heif_opener()

MAX_PIXELS = 100_000_000  # 100 megapixels
# PIL raises DecompressionBombError above 2x this and warns above 1x; our own check below is strict.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageTooLarge(ValueError):
    pass


def decode_image(raw: bytes, max_side: int) -> np.ndarray:
    """Raises ImageTooLarge (> MAX_PIXELS) or ValueError (not an image / corrupt)."""
    try:
        pil = Image.open(io.BytesIO(raw))
        w, h = pil.size
    except Image.DecompressionBombError as e:
        raise ImageTooLarge(str(e)) from e
    except Exception as e:  # noqa: BLE001  (UnidentifiedImageError, truncated headers, ...)
        raise ValueError("Unsupported or corrupt image") from e
    if w * h > MAX_PIXELS:
        raise ImageTooLarge(f"{w}x{h} px is over the {MAX_PIXELS // 1_000_000} megapixel limit")
    try:
        pil = ImageOps.exif_transpose(pil).convert("RGB")
        img = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    except Image.DecompressionBombError as e:
        raise ImageTooLarge(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ValueError("Unsupported or corrupt image") from e
    h, w = img.shape[:2]
    if max(h, w) > max_side:
        s = max_side / max(h, w)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return img
