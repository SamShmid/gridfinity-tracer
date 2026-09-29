"""Fetch the SAM 2.1 tiny ONNX graphs and warm the rembg IS-Net weights.

Run once after install (and in the Docker image build) so the app works offline.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MODELS_DIR, REMBG_MODEL, SAM_DIR, SAM_FILES, SAM_REPO  # noqa: E402


def main() -> None:
    from huggingface_hub import hf_hub_download

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for f in SAM_FILES:
        dest = SAM_DIR / f
        if dest.exists() and dest.stat().st_size > 0:
            print("have", dest)
            continue
        print("downloading", f)
        hf_hub_download(SAM_REPO, f, local_dir=str(SAM_DIR))
    os.environ.setdefault("U2NET_HOME", str(MODELS_DIR / "u2net"))
    from rembg import new_session

    print("warming rembg model", REMBG_MODEL, "->", os.environ["U2NET_HOME"])
    new_session(REMBG_MODEL)
    print("done")


if __name__ == "__main__":
    main()
