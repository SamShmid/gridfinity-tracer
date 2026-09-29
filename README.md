# Gridfinity Tracer

Take a top-down photo of tools lying on a sheet of paper, and get a Gridfinity bin
with perfectly fitted pockets as STL, 3MF or STEP. Everything runs locally in one
Docker container; photos never leave the machine.

## Run it

```bash
docker compose up --build
# then open http://localhost:8080
```

First start downloads about 330 MB of model weights (SAM 2.1 tiny and IS-Net) into
the `models` volume. After that it works offline. Uploads, exports and the library database live in the
`data` volume, so they survive rebuilds; `docker compose down -v` wipes them.

If the download fails (no internet) the container still starts: photo tracing falls back to the classical
detector and `GET /api/health` reports `models_ready: false` until the weights exist (`docker compose restart`
retries). Docker's healthcheck polls the same endpoint. The process runs as the unprivileged `tracer` user;
the entrypoint fixes volume ownership on the way in.

### Limits

One container serves every browser on the LAN, so requests are fenced in:

* Uploads: 50 MB per file (`GT_MAX_UPLOAD_MB`), 100 megapixels (checked from the header before decoding),
  then downscaled to 3000 px on the long side. Snapshots 3 MB, JSON bodies (state, generate) 2 MB.
* Every numeric field is bounded to the UI's range (schemas.py); every id must be 12 hex chars. Bad input gets
  a 400 / 413 / 422 with a plain message, never a stack trace.
* Model inference runs in a 2-thread pool with a 60 s timeout; CAD runs in a 2-process pool with a 120 s
  timeout (the pool is recycled if OpenCascade gets stuck). Timeouts answer 504.
* Generated models are cached by request hash (32 entries), so re-previewing the same design is instant.

### Retention

The library is not an archive. A sweep runs at startup and every 24 h and deletes:

* projects not opened or changed for **90 days** (`GT_RETENTION_DAYS`); each project in `GET /api/library`
  carries `expires_at`, and opening a project calls `PUT /api/library/{id}/touch` to restart the clock,
* blank projects (no photo, no state, no exports, no snapshot) older than 1 day (`GT_BLANK_DAYS`),
* orphan working images left over from older versions after 7 days (`GT_ORPHAN_DAYS`).

Everything removed is logged. Download the STL / STEP of anything you want to keep.

## Flow

The app opens on a **home page**: name the project, then pick **Trace tools from a photo** or **Regular
Gridfinity bin**. Recent projects with 3D snapshots sit down the side.

Photo tracing (steps are named Photo, Paper, Trace, Design in the app):
1. **Photo**: upload or take a photo (JPEG, PNG, WebP, TIFF or iPhone HEIC) and pick the paper size (Letter, A4, A3, Tabloid, Legal, A5 or custom).
2. **Paper corners**: the sheet is auto-detected (SAM 2 prompted for "the sheet", several hypotheses scored by
   rectangularity, brightness and edge support, then each side snapped to the nearest strong straight edge);
   drag the four corners if needed. The photo is perspective-corrected so the paper is a known metric rectangle (4 px/mm).
3. **Trace tools**: "Find tools" runs IS-Net over the sheet, or click a tool to trace it with SAM 2. Every mask then gets a
   zoom-in refinement pass: SAM 2 and IS-Net are re-run on a crop around the tool (3-5x more pixels per mm) and the
   smoothest candidate that still agrees with the original wins ("refine" button re-runs it after hand edits). Per tool: fit clearance,
   printer compensation (0.2 mm default), Gaussian smoothing, straighten edges, mirror-symmetric, convex hull,
   tool thickness; edit the outline vertex by vertex.
4. **Design and export**: 2D layout and live 3D preview side by side. Auto-arrange straightens every tool along its long
   axis and packs it (lengthwise or upright); drag/rotate tools, add finger holes. Pocket depth follows the tool
   thickness and "how it sits" (fully sunk / 3/4 / half / custom), with checks for too deep (one-click raise the
   bin), too shallow, or deeper than the tool. Export STL, 3MF or STEP; a 3D snapshot and the layout are saved.

Plain bin: the same Design screen without pockets (hollow bin with dividers, scoop, label tab, lip, holes).

**Library**: every upload is kept as the original file (HEIC included) with a thumbnail, every download with its
snapshot and settings. Download originals or old models, view old STLs in 3D, rename, delete, or Open to continue.

## Styling

One file, `frontend/src/theme.ts`, defines every colour (light + dark), font, size, spacing, radius and shadow.
It writes them to `:root` as CSS variables at startup; `styles.css` and the components only reference those
variables, and TypeScript code (the three.js viewer) imports `theme` directly. Change a token there and it changes
everywhere.

## Stack

* Backend: Python 3.12, FastAPI, OpenCV (paper detection, homography), ONNX Runtime
  (SAM 2.1 hiera-tiny, IS-Net via rembg), Shapely (offsets), build123d / OpenCascade (bin CAD + export).
* Frontend: React + Vite + TypeScript, three.js preview. Built into the backend image.
* CPU only. On a 10-core laptop: paper detect < 1 s, IS-Net ~3 s, SAM embed ~1 s then ~50 ms per click, bin generation ~1 s.

## Development

```bash
cd backend && uv venv --python 3.12 && uv pip install -e ".[dev]"
uv pip compile pyproject.toml --universal --python-version 3.12 -o requirements.lock   # after changing dependencies
U2NET_HOME=../models/u2net .venv/bin/python scripts/download_models.py
U2NET_HOME=../models/u2net .venv/bin/uvicorn app.main:app --reload --port 8000
cd ../frontend && npm install && npm run dev      # http://localhost:5173, proxies /api to :8000
cd ../backend && .venv/bin/python -m pytest       # synthetic end-to-end tests + real-photo regression
.venv/bin/python scripts/batch_eval.py ../gridfinity-tracer-photos /tmp/eval   # overlays for every photo
```

`backend/tests/synth.py` renders a synthetic photo (Letter sheet under perspective on a dark
bench with a known tool) that the tests trace and compare against ground truth. `tests/test_api.py` drives the
HTTP layer (id validation, size limits, bounds, retention sweep) against a temp data dir set up in `conftest.py`.

### Dependencies and ops

* `backend/requirements.lock` pins every Python package; the Dockerfile installs from it, so rebuilds are
  reproducible. Regenerate it with the `uv pip compile` line above whenever `pyproject.toml` changes.
* uvicorn runs with `--workers 1` on purpose (see `docker/entrypoint.sh`): the caches and worker pools live in
  the process. Do not raise it; concurrency comes from the pools.
* `GET /api/health` returns `{"ok", "sam", "models_ready", "version"}`.
* Working images live with their project under `data/library/<project_id>/` (`upload_*.png`, `rect_*.png/json`)
  and are deleted with it. Files from older versions in the `data/` root are migrated on start where possible
  and swept after 7 days otherwise.

## Accuracy notes

* Glossy benches are the hard case: reflections of the sheet and glare are as bright as the paper.
  The detector was tuned on 10 iPhone photos on a brushed-steel bench (`gridfinity-tracer-photos/`) and gets
  all 10; a matte dark surface is still easier. Always glance at the corners in step 2.

* Paper size and corner placement set the scale. A wrong sheet size scales everything.
* Parallax: a tool 20 mm tall photographed from 50 cm is traced ~4 % oversize. Shoot from far and
  zoom in. The `/api/polygon/offset` endpoint accepts `tool_height_mm` + `camera_distance_mm` to
  compensate (not yet exposed in the UI).
* Silver or light tools on white paper: use the AI auto-detect or SAM, not the dark-on-white tier.
* Clearance 0.3–0.5 mm gives a snug fit on a well-tuned printer; 1 mm or more to drop in easily. Printer compensation
  (default 0.2 mm) is added on top to absorb over-extrusion / elephant foot.
* Gap bridging (default 1.5 mm radius) fills slots narrower than 3 mm inside a pocket (between open plier jaws,
  between hex keys) that would otherwise print as fragile slivers of wall.
* Outline realism: the mask outline is resampled and Gaussian-smoothed (sigma 1 mm) so pixel wobble disappears while
  round tips stay round; optional edge straightening snaps near-axis edges, and mirror-symmetry unions the shape
  with its reflection about the long axis (screwdrivers, wrenches).

## Gridfinity dimensions used

42 mm pitch, 7 mm height units, 41.5 mm cell footprint. Foot profile 0.8 / 1.8 / 2.15 mm,
stacking lip 0.7 / 1.8 / 1.9 mm, corner radius 3.75 mm, magnet 6.5 × 2.4 mm, screw 3 × 6 mm,
holes 8 mm in from the cell edge, wall 0.95 mm. See `backend/app/gridfinity.py`.

## Layout

```
backend/app/paper.py       paper quad detection + homography
backend/app/segment.py     classical / IS-Net / SAM 2 masks
backend/app/outline.py     mask -> polygon, simplify, clearance offset
backend/app/gridfinity.py  bin CAD (build123d) + STL/3MF/STEP export
backend/app/main.py        FastAPI routes, serves the built frontend
backend/app/library.py     SQLite + on-disk project library (originals, images, state, exports, retention sweep)
backend/app/schemas.py     request models with the UI's bounds
backend/tests/test_api.py  HTTP-level tests (ids, limits, bounds, retention)
frontend/src/LibraryView.tsx, StlViewer.tsx, project.ts (open/snapshot)
docs/eval-2026-09-22/      corner + outline overlays for the 10 test photos
docs/trace-review-2026-09-22/  per-tool crops: IS-Net (red), refined (blue), final pocket (green)
frontend/src/steps/        Upload, Paper, Tools, Design (layout + live 3D + export)
frontend/src/HomeView.tsx  landing page with project choice + recent library
docker/entrypoint.sh       downloads models on first start, runs uvicorn
```

## Credits and license

MIT, see [LICENSE](LICENSE).

Model weights are downloaded at first start, not shipped in this repo: [SAM 2.1](https://github.com/facebookresearch/sam2)
hiera-tiny (Apache-2.0, Meta) and [IS-Net](https://github.com/xuebinqin/DIS) via [rembg](https://github.com/danielgatis/rembg)
(Apache-2.0 / MIT). Bin geometry follows the [Gridfinity](https://gridfinity.xyz) spec by Zack Freedman. Some ideas
borrowed from [tracefinity](https://github.com/tracefinity/tracefinity) (MIT).
