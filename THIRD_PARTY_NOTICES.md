# Third-party notices

Gridfinity Tracer's own code is MIT (see [LICENSE](LICENSE)). This file lists everything else the project uses
and the license each comes under. Last audited 2026-09-29 against `backend/requirements.lock` and
`frontend/package-lock.json`.

The short version: **this repository contains no copyleft code.** Everything in it is MIT or compatible. Some
third-party libraries that get *downloaded when you build the Docker image* are LGPL or GPL shared libraries
(listed below). That's fine for building and running it yourself. It only matters if you **publish a built
image**; see [Distributing a built image](#distributing-a-built-image).

## In this repository

| What | License | Notes |
|---|---|---|
| All source code, docs, config | MIT | Written for this project. No code copied from other projects. |
| `frontend/public/fonts/Inter-latin.woff2` | SIL OFL 1.1 | Inter by Rasmus Andersson. License text next to it in `LICENSE-Inter-OFL.txt`. No Reserved Font Name. |
| `gridfinity-tracer-photos/`, `frontend/public/example.jpg`, `docs/` images | MIT (with the repo) | Photos and screenshots taken by the author. |
| Gridfinity dimensions (`backend/app/gridfinity.py`) | MIT | Gridfinity is by Zack Freedman / Voidstar Lab, released under MIT. Only the published dimensions are used; no model files are copied. |

Ideas (not code) were borrowed from [tracefinity](https://github.com/tracefinity/tracefinity) (MIT).

## AI models (downloaded on first start, not in this repo)

| Model | License | Notes |
|---|---|---|
| SAM 2.1 Hiera-Tiny, Meta ([facebookresearch/sam2](https://github.com/facebookresearch/sam2)) | Apache-2.0 | Code and checkpoints are Apache-2.0. The ONNX files come from [onnx-community/sam2.1-hiera-tiny-ONNX](https://huggingface.co/onnx-community/sam2.1-hiera-tiny-ONNX), a conversion of Meta's weights; that repo doesn't state a license of its own, so the upstream Apache-2.0 terms apply. |
| IS-Net `isnet-general-use` ([xuebinqin/DIS](https://github.com/xuebinqin/DIS)) | Apache-2.0 (code) | The weights have no separate license and ship with the Apache-2.0 repo. **Caveat:** DIS was built around the DIS5K dataset, whose [terms](https://github.com/xuebinqin/DIS/blob/main/DIS5K-Dataset-Terms-of-Use.pdf) allow non-commercial research and education only. Whether that reaches models trained on it is legally unsettled. Personal and non-commercial use is fine; for commercial use, ask the authors or switch the auto-detect model (`GT_REMBG_MODEL`). |

## Python packages (installed into the Docker image)

All 97 pinned packages are under permissive licenses:

* **MIT / BSD / ISC / Apache-2.0 / PSF / MIT-CMU:** everything else, including FastAPI, uvicorn, pydantic, numpy,
  scipy, scikit-image, scikit-learn, OpenCV (Apache-2.0), ONNX Runtime (MIT), Shapely (BSD), Pillow (MIT-CMU),
  rembg (MIT), build123d (Apache-2.0), cadquery-ocp (Apache-2.0), ocpsvg (Apache-2.0), lib3mf (BSD),
  ezdxf (MIT), numba / llvmlite (BSD, LLVM under Apache-2.0 with LLVM exception).
* **MPL-2.0 (weak, file-level copyleft):** `certifi` (CA bundle), `tqdm` (MPL-2.0 and MIT). Used unmodified.
  MPL only requires sharing changes to those files.

* **pi-heif:** its Python code is BSD-3, but its binary wheel is LGPL-3.0 as a whole because it bundles libheif and
  libde265 (below). It declares both, which is why tools report it either way.

No Python package is GPL or AGPL.

### Native libraries bundled inside those wheels

Several wheels carry prebuilt C/C++ libraries. These are the ones under copyleft licenses (same set on x86_64 and
arm64). They're used unmodified and loaded dynamically.

| Library | Comes with | License |
|---|---|---|
| Open CASCADE Technology (`libTK*`) | cadquery-ocp | LGPL-2.1 with OCCT exception |
| FreeImage | cadquery-ocp | GPL-2.0 / GPL-3.0 / FreeImage Public License (your choice) |
| jbigkit (`libjbig`) | cadquery-ocp | GPL-2.0-or-later |
| LibRaw | cadquery-ocp | LGPL-2.1 or CDDL-1.0 |
| libheif, libde265 | pi-heif | LGPL-3.0 |
| FFmpeg (`libavcodec`, `libavformat`, `libavutil`, `libswscale`, `libswresample`) | opencv-python-headless | LGPL-2.1-or-later |
| GEOS | shapely | LGPL-2.1 |
| libgfortran, libgomp | numpy, scipy, scikit-learn, cadquery-ocp | GPL-3.0 with GCC Runtime Library Exception |
| libquadmath | numpy, scipy | LGPL-2.1 |

HEIC decoding uses **pi-heif**, the decode-only build of pillow-heif, specifically so the GPL x265 encoder isn't
pulled in. HEVC is patent-encumbered in some countries; this app only decodes.

## Frontend (npm)

* **Shipped in the built site:** react, react-dom, scheduler, loose-envify, js-tokens (MIT) and three.js (MIT).
  That's the whole production bundle.
* **Build tools only (not shipped):** Vite, TypeScript (Apache-2.0), lightningcss (MPL-2.0), plus MIT / BSD / ISC
  tooling.

## Docker base images

`node:22-slim` (build stage only, thrown away) and `python:3.12-slim` (Debian). Debian includes the usual mix of
GPL and LGPL system packages (bash, coreutils, glibc, ...). Running them is no problem.

## Distributing a built image

Building and running the image yourself, or on your own network, is not distribution and carries no obligations.

If you **push a built image to a public registry** or hand it to others, you're distributing the GPL and LGPL
binaries above (and Debian's). Everything combines fine: MIT, Apache-2.0 and BSD code can ship alongside GPL-3.0
code, and every GPL library above allows GPL-3.0. But you'd need to:

1. include the license texts (they're already inside each wheel's `*.dist-info/licenses` folder and Debian's
   `/usr/share/doc/*/copyright`), and
2. make the corresponding source available for the GPL and LGPL libraries. All of them are unmodified upstream
   releases, so a written offer or links to the exact upstream source versions is enough.

This project doesn't publish an image. CI builds one to test it and throws it away.

## How this was checked

* Python: `pip-licenses` over the installed lock, plus the lock installed for `x86_64-manylinux` and
  `aarch64-manylinux` and every `*.libs/` folder inspected.
* npm: `license-checker-rseidelsohn` on production and dev dependencies.
* Models and upstream projects: license files and model cards on GitHub / Hugging Face, and the DIS5K terms of use.
