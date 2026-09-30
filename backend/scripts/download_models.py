"""Fetch the SAM 2.1 tiny ONNX graphs and warm the rembg IS-Net weights.

Runs on every container start (docker/entrypoint.sh) and once after a dev install, so the app works
offline afterwards. The Docker image ships no weights; they live in the /models volume.

Every SAM file is pinned to one Hugging Face revision and checked against its byte size and sha256.
It is downloaded under a temporary directory and moved into place only after the check passes, so
an interrupted download never leaves a file that looks complete. rembg verifies the IS-Net weights
itself (pooch, md5).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MODELS_DIR, REMBG_MODEL, SAM_DIR, SAM_FILES, SAM_REPO  # noqa: E402

# Commit of onnx-community/sam2.1-hiera-tiny-ONNX the checksums below were taken from.
SAM_REVISION = "814a066640debee5a91e70aa401fb8e17e030503"
# repo path -> (bytes, sha256)
SAM_CHECKSUMS: dict[str, tuple[int, str]] = {
    "onnx/vision_encoder.onnx": (
        354238,
        "4f30aacd3aaefbca81a0b7fe4c1fc96345570ea0a6f80ced599493d1b3be2e8c",
    ),
    "onnx/vision_encoder.onnx_data": (
        134084864,
        "e83df9866a5afe68ea7f0f721f18f65137fc3acbf0da1c74e946d363e09c69cc",
    ),
    "onnx/prompt_encoder_mask_decoder.onnx": (
        213114,
        "874414704c5d686db7d206a35f6e15d26563d50c8c4468fccc6739bd7e491dcf",
    ),
    "onnx/prompt_encoder_mask_decoder.onnx_data": (
        20958208,
        "e9874d900dd4134ed60eab1e97910327c2419e0b2954485d8fd6e7f1a1470f47",
    ),
}
INCOMING = SAM_DIR / ".incoming"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _valid(path: Path, size: int, digest: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _sha256(path) == digest


def _fetch_sam(rel: str) -> None:
    size, digest = SAM_CHECKSUMS[rel]
    dest = SAM_DIR / rel
    if _valid(dest, size, digest):
        print("have", dest)
        return
    if dest.exists():
        print("replacing corrupt or partial", dest)
        dest.unlink()
    # The entrypoint exports HF_HUB_OFFLINE=1 when every weight looked present; we now know one is
    # not, so allow the network. Must happen before huggingface_hub is imported (it reads env once).
    os.environ.pop("HF_HUB_OFFLINE", None)
    from huggingface_hub import hf_hub_download

    print("downloading", rel, "from", SAM_REPO, "@", SAM_REVISION[:12])
    got = Path(hf_hub_download(SAM_REPO, rel, revision=SAM_REVISION, local_dir=str(INCOMING)))
    if not _valid(got, size, digest):
        got.unlink(missing_ok=True)
        raise RuntimeError(f"{rel}: size/sha256 mismatch after download; refusing to install it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(got, dest)  # same filesystem: atomic
    print("installed", dest)


def main() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for rel in SAM_FILES:
            _fetch_sam(rel)
    finally:
        shutil.rmtree(INCOMING, ignore_errors=True)

    os.environ.setdefault("U2NET_HOME", str(MODELS_DIR / "u2net"))
    from rembg import new_session

    print("warming rembg model", REMBG_MODEL, "->", os.environ["U2NET_HOME"])
    new_session(REMBG_MODEL)
    print("done")


if __name__ == "__main__":
    main()
