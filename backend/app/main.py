"""FastAPI routes.

One container serves every browser on the local network, so the endpoints are kept synchronous
but the heavy work is fenced off:
  * model inference (paper detection, IS-Net, SAM) runs in a 2-thread pool with a 60 s timeout. A
    thread cannot be killed, so a timed-out job keeps running to completion; the pool therefore
    refuses new work with a 503 once INFER_MAX_PENDING jobs are running or queued,
  * CAD (build123d / OpenCascade) runs in a 2-process pool with a 120 s timeout, so a pathological
    boolean can neither block the event loop nor hold the GIL; on timeout the pool is recycled,
  * generated model bytes are cached by request hash (32 entries, 128 MB) so repeated previews are instant,
  * every id is validated, every body is size-capped as it streams in, every numeric field is bounded
    (schemas.py), and anything unexpected becomes a clean JSON 500 without a stack trace.

Run with a single uvicorn worker: the SAM embedding cache, the preview cache and the pools live in
this process. Scale by CPU inside the pools, not by workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import multiprocessing
import os
import threading
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Annotated

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import StringConstraints
from starlette.datastructures import MutableHeaders

from . import gridfinity, library, outline, paper, segment
from .config import CUSTOM_PAPER_MAX_MM, DATA_DIR, MAX_UPLOAD_SIDE, MODELS_DIR, PAPER_SIZES, STATIC_DIR
from .imageio import MAX_PIXELS, ImageTooLarge, decode_image
from .schemas import (
    ID_PATTERN,
    AutoDetectRequest,
    DetectResponse,
    GenerateRequest,
    NewProjectRequest,
    OffsetRequest,
    OffsetResponse,
    RectifyRequest,
    RectifyResponse,
    RefineRequest,
    RenameRequest,
    SamRequest,
    ToolOutline,
    UploadResponse,
    WarmRequest,
)

VERSION = "0.3.0"
log = logging.getLogger("gridfinity-tracer")
logging.basicConfig(level=logging.INFO)
os.environ.setdefault("U2NET_HOME", str(MODELS_DIR / "u2net"))

# ------------------------------------------------------------------ limits
MB = 1024 * 1024
MAX_UPLOAD_BYTES = int(os.environ.get("GT_MAX_UPLOAD_MB", "50")) * MB
MAX_SNAPSHOT_BYTES = 3 * MB
MAX_JSON_BYTES = 2 * MB
INFER_TIMEOUT_S = float(os.environ.get("GT_INFER_TIMEOUT_S", "60"))
INFER_MAX_PENDING = int(os.environ.get("GT_INFER_MAX_PENDING", "6"))  # running + queued inference jobs
CAD_TIMEOUT_S = float(os.environ.get("GT_CAD_TIMEOUT_S", "120"))
SWEEP_INTERVAL_S = 24 * 3600
PREVIEW_CACHE_ENTRIES = 32
PREVIEW_CACHE_MAX_ITEM = 24 * MB
PREVIEW_CACHE_MAX_TOTAL = 128 * MB

MEDIA = {"stl": "model/stl", "3mf": "model/3mf", "step": "application/step"}
# Path ids: a pydantic constraint in an Annotated alias, which FastAPI copies per parameter.
IdP = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
BUSY = "The server is busy with other requests right now. Wait a moment and try again."


# ------------------------------------------------------------------ executors
_infer_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="infer")
_infer_pending = 0  # jobs submitted and not yet finished (a timed-out job still counts until it ends)
_infer_lock = threading.Lock()
_cad_pool: ProcessPoolExecutor | None = None
_cad_lock = threading.Lock()


def _new_cad_pool() -> ProcessPoolExecutor:
    # spawn (not fork): the parent holds onnxruntime thread pools that must not be duplicated.
    return ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"))


def _get_cad_pool() -> ProcessPoolExecutor:
    global _cad_pool
    with _cad_lock:
        if _cad_pool is None:
            _cad_pool = _new_cad_pool()
        return _cad_pool


def _reset_cad_pool() -> None:
    """A job overran CAD_TIMEOUT_S: OpenCascade may be stuck in a boolean. Kill the workers so the
    pool can serve the next request instead of staying wedged."""
    global _cad_pool
    with _cad_lock:
        old, _cad_pool = _cad_pool, None
    if old is None:
        return
    for p in list(getattr(old, "_processes", {}).values()):
        with contextlib.suppress(Exception):
            p.kill()
    old.shutdown(wait=False, cancel_futures=True)
    log.warning("CAD pool recycled after a timeout")


def _shutdown_pools() -> None:
    global _cad_pool
    _infer_pool.shutdown(wait=False, cancel_futures=True)
    with _cad_lock:
        old, _cad_pool = _cad_pool, None
    if old is not None:
        old.shutdown(wait=False, cancel_futures=True)


def _infer_done(_fut: Future) -> None:
    global _infer_pending
    with _infer_lock:
        _infer_pending -= 1


async def _run_infer(fn, *args):
    """Run fn in the inference pool. 503 when the pool is saturated (a Python thread cannot be
    interrupted, so a job that times out below keeps the slot until it finishes on its own)."""
    global _infer_pending
    with _infer_lock:
        if _infer_pending >= INFER_MAX_PENDING:
            raise HTTPException(503, BUSY)
        _infer_pending += 1
    fut = _infer_pool.submit(fn, *args)
    fut.add_done_callback(_infer_done)
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(fut, loop=asyncio.get_running_loop()), INFER_TIMEOUT_S
        )
    except TimeoutError:
        raise HTTPException(
            504,
            f"That took over {INFER_TIMEOUT_S:.0f} s and was abandoned. The server may still be finishing it "
            "in the background; wait a little before trying again.",
        ) from None


async def _run_cad(fn, *args):
    loop = asyncio.get_running_loop()
    fut: Future = _get_cad_pool().submit(fn, *args)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut, loop=loop), CAD_TIMEOUT_S)
    except TimeoutError:
        _reset_cad_pool()
        raise HTTPException(
            504,
            f"Building that model took over {CAD_TIMEOUT_S:.0f} s and was stopped. Try a simpler shape or fewer pockets.",
        ) from None


# ------------------------------------------------------------------ preview cache
class _LruBytes:
    """LRU of byte blobs bounded by entry count AND total bytes (32 x 24 MB STLs would be 768 MB)."""

    def __init__(self, entries: int, max_item: int, max_total: int):
        self._d: OrderedDict[str, bytes] = OrderedDict()
        self._n, self._max_item, self._max_total = entries, max_item, max_total
        self._total = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key: str, value: bytes) -> None:
        if len(value) > self._max_item:
            return
        with self._lock:
            old = self._d.pop(key, None)
            self._total -= len(old) if old is not None else 0
            self._d[key] = value
            self._total += len(value)
            while len(self._d) > self._n or self._total > self._max_total:
                _, evicted = self._d.popitem(last=False)
                self._total -= len(evicted)


_preview_cache = _LruBytes(PREVIEW_CACHE_ENTRIES, PREVIEW_CACHE_MAX_ITEM, PREVIEW_CACHE_MAX_TOTAL)


# ------------------------------------------------------------------ app
async def _retention_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(library.sweep)
        except Exception:  # noqa: BLE001
            log.exception("retention sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_retention_loop())
    # Import build123d in the CAD workers now so the first preview is not slow.
    with contextlib.suppress(Exception):
        for _ in range(2):
            _get_cad_pool().submit(gridfinity.warm)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        _shutdown_pools()


app = FastAPI(title="Gridfinity Tracer", version=VERSION, lifespan=_lifespan)
DATA_DIR.mkdir(parents=True, exist_ok=True)
library.init()


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        {
            "detail": "Something went wrong on the server. Try again, and check the container logs if it keeps happening."
        },
        status_code=500,
    )


@app.exception_handler(library.BadId)
async def _bad_id(request: Request, exc: library.BadId):
    return JSONResponse({"detail": "Not found."}, status_code=404)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    """Same shape as FastAPI's default 422 minus the echoed input: echoing NaN/inf back would make the
    JSON encoder raise and turn a bad request into a 500."""
    errors = [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


def _body_limit(path: str) -> int:
    if path == "/api/upload":
        return MAX_UPLOAD_BYTES
    if path.endswith("/snapshot"):
        return MAX_SNAPSHOT_BYTES
    return MAX_JSON_BYTES


def _too_big(limit: int) -> str:
    return f"That request is too big (max {limit // MB} MB)."


class _LimitsAndCacheHeaders:
    """Pure ASGI middleware (BaseHTTPMiddleware cannot wrap `receive`): caps every /api write body as it
    streams in, so a chunked request without Content-Length is capped too, and sets cache headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith("/api/") and scope["method"] in ("POST", "PUT", "PATCH"):
            limit = _body_limit(path)
            cl = dict(scope["headers"]).get(b"content-length", b"")
            if cl.isdigit() and int(cl) > limit:
                return await JSONResponse({"detail": _too_big(limit)}, status_code=413)(scope, receive, send)
            receive = _counting_receive(receive, limit)

        async def send_with_cache(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # index.html must never be cached (it references hashed asset names that change on
                # every build); the hashed assets themselves can be cached forever.
                if path.startswith("/assets/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                elif not path.startswith("/api/"):
                    headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            await send(message)

        await self.app(scope, receive, send_with_cache)


def _counting_receive(receive, limit: int):
    received = 0

    async def counting():
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                # Raised inside FastAPI's body read, which re-raises HTTPException untouched -> JSON 413.
                raise HTTPException(413, _too_big(limit))
        return message

    return counting


app.add_middleware(_LimitsAndCacheHeaders)


async def _read_limited(file: UploadFile, limit: int, what: str) -> bytes:
    buf = bytearray()
    while chunk := await file.read(MB):
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, f"That {what} is too big (max {limit // MB} MB).")
    return bytes(buf)


async def _read_json_body(request: Request, limit: int = MAX_JSON_BYTES) -> dict:
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413, f"That request is too big (max {limit // MB} MB).")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "That body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object.")
    return data


# ------------------------------------------------------------------ image helpers
_rect_meta_cache: OrderedDict[str, dict] = OrderedDict()  # rect_id -> meta, LRU of 256
_meta_lock = threading.Lock()


def _img_path(kind: str, id_: str) -> Path:
    p = library.image_path(kind, id_)
    if p is None:
        what = "photo" if kind == "upload" else "rectified image"
        raise HTTPException(404, f"That {what} is no longer available. Upload the photo again.")
    return p


def _load(kind: str, id_: str) -> np.ndarray:
    img = cv2.imread(str(_img_path(kind, id_)), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(404, "That image could not be read. Upload the photo again.")
    return img


def _rect_meta(rect_id: str) -> dict:
    with _meta_lock:
        m = _rect_meta_cache.get(rect_id)
        if m is not None:
            _rect_meta_cache.move_to_end(rect_id)
            return m
    p = library.image_path("rect", rect_id, ".json")
    m = library.read_json(p) if p else None
    if m is None:
        raise HTTPException(404, "That rectified image is no longer available. Rectify the photo again.")
    _remember_meta(rect_id, m)
    return m


def _remember_meta(rect_id: str, meta: dict) -> None:
    with _meta_lock:
        _rect_meta_cache[rect_id] = meta
        _rect_meta_cache.move_to_end(rect_id)
        while len(_rect_meta_cache) > 256:
            _rect_meta_cache.popitem(last=False)


def _outline_list(
    masks: list[np.ndarray], px_per_mm: float, tol: float, min_area: float, source: str
) -> list[ToolOutline]:
    tools = []
    for m in masks:
        for poly in outline.mask_to_polygons_mm(m, px_per_mm, tolerance_mm=tol, min_area_mm2=min_area):
            st = outline.polygon_stats(poly)
            tools.append(ToolOutline(polygon=poly, source=source, **st))
    return tools


def _safe_filename(name: str, fallback: str = "gridfinity-holder") -> str:
    return "".join(c for c in name if c.isascii() and (c.isalnum() or c in "-_"))[:80] or fallback


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    sam = all(
        (MODELS_DIR / "sam2.1-hiera-tiny" / f).exists()
        for f in ("onnx/vision_encoder.onnx", "onnx/prompt_encoder_mask_decoder.onnx")
    )
    u2 = Path(os.environ["U2NET_HOME"])
    isnet = u2.exists() and any(
        u2.rglob("*.onnx")
    )  # rembg layout changed between versions; any weight file counts
    return {"ok": True, "sam": sam, "models_ready": sam and isnet, "version": VERSION}


@app.get("/api/paper-sizes")
def paper_sizes():
    return {k: {"w_mm": v[0], "h_mm": v[1]} for k, v in PAPER_SIZES.items()}


@app.post("/api/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    paper_size: str = Form("letter", max_length=20),
    custom_w_mm: float | None = Form(None, ge=20, le=CUSTOM_PAPER_MAX_MM),
    custom_h_mm: float | None = Form(None, ge=20, le=CUSTOM_PAPER_MAX_MM),
    name: str | None = Form(None, max_length=120),
):
    if paper_size not in PAPER_SIZES and paper_size != "custom":
        raise HTTPException(400, f"Unknown paper size '{paper_size[:20]}'.")
    raw = await _read_limited(file, MAX_UPLOAD_BYTES, "file")
    if not raw:
        raise HTTPException(400, "That file is empty.")
    try:
        img = await _run_infer(decode_image, raw, MAX_UPLOAD_SIDE)  # JPEG/PNG/WebP/TIFF/HEIC..., EXIF-rotated
    except ImageTooLarge:
        raise HTTPException(
            413, f"That photo has too many pixels (max {MAX_PIXELS // 1_000_000} megapixels)."
        ) from None
    except ValueError:
        raise HTTPException(400, "Couldn't read that as a photo. Try a JPEG, PNG or HEIC.") from None
    h, w = img.shape[:2]
    image_id = library.new_id()
    ts = 360 / max(h, w)
    thumb = cv2.imencode(
        ".jpg",
        cv2.resize(img, None, fx=ts, fy=ts, interpolation=cv2.INTER_AREA),
        [cv2.IMWRITE_JPEG_QUALITY, 82],
    )[1].tobytes()
    project_id = library.create_project(
        raw, Path(file.filename or "photo").name[:200], image_id, w, h, paper_size, thumb
    )
    cv2.imwrite(str(library.new_image_path("upload", project_id, image_id)), img)
    if name and name.strip():
        library.rename(project_id, name.strip()[:120])
    aspect = None
    if paper_size in PAPER_SIZES:
        pw, ph = PAPER_SIZES[paper_size]
        aspect = max(pw, ph) / min(pw, ph)
    elif paper_size == "custom" and custom_w_mm and custom_h_mm:
        aspect = max(custom_w_mm, custom_h_mm) / min(custom_w_mm, custom_h_mm)
    try:
        quad = await _run_infer(paper.detect_paper, img, True, aspect)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001  (detection is best effort; the user can drag the corners)
        log.exception("paper detection failed for %s", image_id)
        quad = None
    detected = quad is not None
    if quad is None:
        quad = paper.default_quad(w, h)
    return UploadResponse(
        image_id=image_id,
        width=w,
        height=h,
        corners=quad,
        detected=detected,
        orientation=paper.paper_orientation(quad),
        project_id=project_id,
    )


@app.get("/api/image/upload/{image_id}")
def get_upload(image_id: IdP):
    return FileResponse(_img_path("upload", image_id), media_type="image/png")


@app.get("/api/image/rect/{rect_id}")
def get_rect(rect_id: IdP):
    return FileResponse(_img_path("rect", rect_id), media_type="image/png")


def _check_quad(corners, w: int, h: int) -> None:
    # Order first: rectify() reorders anyway, so a rectangle given as TL,BR,TR,BL is fine, while the
    # area of the *unordered* bow-tie would be ~0 and wrongly rejected.
    q = paper.order_corners(np.array(corners, np.float32))
    area = abs(cv2.contourArea(q.reshape(-1, 1, 2)))
    if area < 100 or not cv2.isContourConvex(q.reshape(-1, 1, 2)):
        raise HTTPException(
            400, "Those corners don't make a usable rectangle. Drag each one onto a corner of the paper."
        )
    if (q < -2 * max(w, h)).any() or (q > 3 * max(w, h)).any():
        raise HTTPException(400, "Those corners are way outside the photo.")


@app.post("/api/rectify", response_model=RectifyResponse)
async def rectify(req: RectifyRequest):
    img = _load("upload", req.image_id)
    pid = library.image_project(req.image_id)
    if pid is None or library.get_project(pid) is None:
        raise HTTPException(404, "That photo's project is gone. Upload the photo again.")
    if req.paper == "custom":
        if not req.custom_w_mm or not req.custom_h_mm:
            raise HTTPException(400, "Custom paper needs a width and a height in mm.")
        pw, ph = float(req.custom_w_mm), float(req.custom_h_mm)
    elif req.paper in PAPER_SIZES:
        pw, ph = PAPER_SIZES[req.paper]
    else:
        raise HTTPException(400, f"Unknown paper size '{req.paper}'.")
    h, w = img.shape[:2]
    _check_quad(req.corners, w, h)
    corners = [list(c) for c in req.corners]
    orient = req.orientation if req.orientation != "auto" else paper.paper_orientation(corners)
    w_mm, h_mm = paper.sheet_size_mm((pw, ph), orient)
    try:
        warped, meta = await _run_infer(paper.rectify, img, corners, w_mm, h_mm)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001  (singular homography)
        raise HTTPException(
            400, "Those corners don't make a usable rectangle. Drag each one onto a corner of the paper."
        ) from None
    rect_id = library.new_id()
    png = library.new_image_path("rect", pid, rect_id)
    cv2.imwrite(str(png), warped)
    library.write_json(png.with_suffix(".json"), meta)
    _remember_meta(rect_id, meta)
    return RectifyResponse(
        rect_id=rect_id,
        **{
            k: meta[k]
            for k in ("width", "height", "px_per_mm", "margin_mm", "paper_w_mm", "paper_h_mm", "paper_px")
        },
    )


@app.post("/api/detect/auto", response_model=DetectResponse)
async def detect_auto(req: AutoDetectRequest):
    img = _load("rect", req.rect_id)
    meta = _rect_meta(req.rect_id)
    ppm = meta["px_per_mm"]

    def work():
        if req.method == "classical":
            mask = segment.classical_mask(img, meta["paper_px"])
        else:
            mask = segment.auto_mask(img)
        x0, y0, x1, y1 = [int(v) for v in meta["paper_px"]]
        comps = segment.split_components(mask, ppm, req.min_area_mm2)
        # Keep only things that are mostly ON the sheet: clutter on the bench around it is not a tool.
        # (A tool may overhang the edge, so we ask for >= 60% of its area inside the paper.)
        sheet = np.zeros_like(mask)
        sheet[y0:y1, x0:x1] = True
        comps = [c for c in comps if (c & sheet).sum() >= 0.6 * c.sum()]
        if req.refine:
            comps = [segment.refine_mask(img, c) for c in comps]
        return _outline_list(
            comps, ppm, req.tolerance_mm, req.min_area_mm2, req.method + ("+refined" if req.refine else "")
        )

    tools = await _run_infer(work)
    return DetectResponse(tools=tools)


@app.post("/api/detect/sam", response_model=DetectResponse)
async def detect_sam(req: SamRequest):
    img = _load("rect", req.rect_id)
    meta = _rect_meta(req.rect_id)
    ppm = meta["px_per_mm"]

    def work():
        sam = segment.get_sam()
        sam.embed(req.rect_id, img)
        mask = sam.predict(
            req.rect_id, [(p.x, p.y) for p in req.points], [p.label for p in req.points], img_bgr=img
        )
        comps = segment.split_components(mask, ppm, 20.0)
        keep = segment.clicked_components(comps, [(p.x, p.y) for p in req.points if p.label == 1])
        if not keep:
            return []  # no positive click landed on anything (e.g. only "remove" points): no tool
        merged = np.zeros_like(mask)
        for c in keep:
            merged |= c
        if req.refine:
            merged = segment.refine_mask(img, merged)
        return _outline_list(
            [merged], ppm, req.tolerance_mm, 20.0, "sam" + ("+refined" if req.refine else "")
        )

    tools = await _run_infer(work)
    return DetectResponse(tools=tools[:1])


@app.post("/api/detect/refine", response_model=DetectResponse)
async def detect_refine(req: RefineRequest):
    """Re-segment an existing outline with the zoom-in SAM pass (e.g. after hand edits)."""
    img = _load("rect", req.rect_id)
    meta = _rect_meta(req.rect_id)
    ppm = meta["px_per_mm"]

    def work():
        mask = np.zeros(img.shape[:2], np.uint8)
        pts = (np.array(req.polygon, np.float64) * ppm).astype(np.int32)
        cv2.fillPoly(mask, [pts], 1)
        if mask.sum() < 50:
            return []
        refined = segment.refine_mask(img, mask > 0)
        return _outline_list([refined], ppm, req.tolerance_mm, 20.0, "refined")

    tools = await _run_infer(work)
    return DetectResponse(tools=tools[:1])


@app.post("/api/sam/warm")
async def sam_warm(req: WarmRequest):
    """Precompute the SAM embedding for a rectified image so the first click is instant."""
    img = _load("rect", req.rect_id)

    def work():
        segment.get_sam().embed(req.rect_id, img)

    await _run_infer(work)
    return {"ok": True}


@app.post("/api/polygon/offset", response_model=OffsetResponse)
def polygon_offset(req: OffsetRequest):
    polygon = [list(p) for p in req.polygon]
    poly = outline.prepare_pocket(
        polygon,
        req.clearance_mm,
        req.printer_offset_mm,
        req.gaussian_mm,
        req.tolerance_mm,
        req.smooth_mm,
        req.snap,
        req.symmetric,
        req.convex,
        req.tool_height_mm,
        req.camera_distance_mm,
        req.bridge_mm,
    )
    st = outline.polygon_stats(poly)
    return OffsetResponse(
        polygon=poly, area_mm2=st["area_mm2"], bbox=st["bbox"], straighten_deg=outline.straighten_angle(poly)
    )


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    bin_cfg = req.bin.model_dump()
    pockets = [p.model_dump() for p in req.pockets]
    key = hashlib.sha256(
        json.dumps(
            {"b": bin_cfg, "p": pockets, "f": req.format, "t": req.tolerance}, sort_keys=True, default=list
        ).encode()
    ).hexdigest()
    data = _preview_cache.get(key)
    if data is None:
        try:
            data = await _run_cad(gridfinity.generate_model, bin_cfg, pockets, req.format, req.tolerance)
        except HTTPException:
            raise
        except BrokenProcessPool:
            # The pool was recycled under us (another request's job timed out): not this user's fault.
            raise HTTPException(503, BUSY) from None
        except Exception as e:  # noqa: BLE001  (OpenCascade rejected the geometry)
            log.warning("CAD generation failed: %s", e)
            raise HTTPException(
                400,
                "Couldn't build that model. Check that pockets stay inside the bin and don't overlap each other, then try again.",
            ) from None
        _preview_cache.put(key, data)
    name = _safe_filename(req.filename)
    saved = False
    if req.save and req.project_id:
        try:
            if req.state is not None:
                library.save_state(req.project_id, req.state)
            summary = f"{req.bin.grid_x}x{req.bin.grid_y}x{req.bin.height_units}u, {len(pockets)} pocket(s), {'solid' if req.bin.solid else 'hollow'}"
            library.add_export(req.project_id, req.format, f"{name}.{req.format}", data, summary)
            saved = True
        except KeyError:
            log.warning("project %s not found; export not stored", req.project_id)
        except ValueError as e:
            raise HTTPException(400, f"That project state can't be saved: {e}.") from None
    headers = {
        "Content-Disposition": f'attachment; filename="{name}.{req.format}"',
        "X-Saved": "1" if saved else "0",
    }
    return Response(content=data, media_type=MEDIA[req.format], headers=headers)


# ------------------------------------------------------------------ library
@app.get("/api/library")
def library_list():
    return {"projects": library.list_projects(), "retention_days": library.RETENTION_DAYS}


@app.post("/api/library/new")
def library_new(body: NewProjectRequest):
    """Project without a photo (plain bin generator)."""
    pid = library.create_blank((body.name or "").strip()[:120] or "New bin")
    return {"project_id": pid}


@app.post("/api/library/{pid}/snapshot")
async def library_snapshot(pid: IdP, file: UploadFile = File(...)):
    """PNG snapshot of the 3D preview, shown on the library card."""
    data = await _read_limited(file, MAX_SNAPSHOT_BYTES, "snapshot")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "The snapshot has to be a PNG.")
    try:
        library.save_snapshot(pid, data)
    except KeyError:
        raise HTTPException(404, "Not found.") from None
    return {"ok": True}


@app.get("/api/library/{pid}/snapshot")
def library_snapshot_get(pid: IdP):
    p = library.project_dir(pid) / "snapshot.png"
    if not p.exists():
        raise HTTPException(404, "Not found.")
    return FileResponse(p, media_type="image/png")


@app.put("/api/library/{pid}/touch")
def library_touch(pid: IdP):
    """Bump updated_at so the project's 90-day retention clock restarts (called when a project is opened)."""
    exp = library.touch(pid)
    if exp is None:
        raise HTTPException(404, "Not found.")
    return {"ok": True, "expires_at": exp}


@app.get("/api/library/{pid}")
def library_get(pid: IdP):
    p = library.get_project(pid)
    if p is None:
        raise HTTPException(404, "Not found.")
    p["state"] = library.load_state(pid)
    return p


@app.put("/api/library/{pid}")
def library_rename(body: RenameRequest, pid: IdP):
    name = (body.name or "").strip()[:120] or "untitled"
    if not library.rename(pid, name, body.notes):
        raise HTTPException(404, "Not found.")
    return {"ok": True}


@app.post("/api/library/{pid}/state")
async def library_save_state(request: Request, pid: IdP):
    body = await _read_json_body(request)
    try:
        library.save_state(pid, body)
    except KeyError:
        raise HTTPException(404, "Not found.") from None
    except ValueError as e:
        raise HTTPException(400, f"That project state can't be saved: {e}.") from None
    return {"ok": True}


@app.delete("/api/library/{pid}")
def library_delete(pid: IdP):
    if not library.delete_project(pid):
        raise HTTPException(404, "Not found.")
    return {"ok": True}


@app.get("/api/library/{pid}/thumb")
def library_thumb(pid: IdP):
    p = library.project_dir(pid) / "thumb.jpg"
    if not p.exists():
        raise HTTPException(404, "Not found.")
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/library/{pid}/original")
def library_original(pid: IdP):
    p = library.get_project(pid)
    if p is None or not p["original_ext"]:
        raise HTTPException(404, "Not found.")
    path = library.project_dir(pid) / f"original{p['original_ext']}"
    if not path.exists():
        raise HTTPException(404, "Not found.")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=_safe_filename(Path(p["original_filename"]).stem, "photo") + p["original_ext"],
    )


@app.get("/api/library/exports/{eid}")
def library_export(eid: IdP, download: bool = True):
    found = library.export_path(eid)
    if found is None or not found[0].exists():
        raise HTTPException(404, "Not found.")
    path, row = found
    media = MEDIA.get(row["format"], "application/octet-stream")
    if download:
        return FileResponse(
            path, media_type=media, filename=_safe_filename(Path(row["filename"]).stem) + f".{row['format']}"
        )
    return FileResponse(path, media_type=media)


@app.delete("/api/library/exports/{eid}")
def library_export_delete(eid: IdP):
    if not library.delete_export(eid):
        raise HTTPException(404, "Not found.")
    return {"ok": True}


# Serve the built frontend if present (Docker image); dev uses the Vite proxy instead.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
