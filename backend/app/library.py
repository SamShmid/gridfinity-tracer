"""Project library: every upload, its state, and every exported model, on disk + SQLite.

Layout under DATA_DIR/library/<project_id>/:
    original.<ext>          the file exactly as uploaded (HEIC, JPEG, ...)
    thumb.jpg               small preview of the photo
    snapshot.png            3D preview snapshot shown on the library card
    state.json              last saved session snapshot (corners, tools, bin, ...)
    upload_<image_id>.png   the decoded, downscaled photo the pipeline works on
    rect_<rect_id>.png/json rectified image + its metadata (px_per_mm, paper rectangle, ...)
    exports/<export_id>.<fmt>

Every id is 12 lowercase hex chars. project_dir() refuses anything else, so no path built here can
ever leave LIB_DIR (the old code let `DELETE /api/library/..` wipe the data volume).

Every write (file + row) happens under one process-wide lock, so a delete can never interleave with
a save into the same folder.

Retention (owner's choice): projects untouched for RETENTION_DAYS are deleted, blank projects
(no photo, no state, no exports, no snapshot) after BLANK_DAYS, orphan project folders after
ORPHAN_DAYS. sweep() does the work; main.py runs it at startup and every 24 h.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger("gridfinity-tracer.library")
# state.json is echoed back through FastAPI's recursive encoder; deeper than this and a GET 500s.
MAX_STATE_DEPTH = 64

LIB_DIR = DATA_DIR / "library"
DB_PATH = DATA_DIR / "library.db"
_lock = threading.Lock()
_ID_RE = re.compile(r"^[0-9a-f]{12}$")

DAY = 86400.0
RETENTION_DAYS = float(os.environ.get("GT_RETENTION_DAYS", "90"))
ORPHAN_DAYS = float(os.environ.get("GT_ORPHAN_DAYS", "7"))
BLANK_DAYS = float(os.environ.get("GT_BLANK_DAYS", "1"))
IMAGE_KINDS = ("upload", "rect")


class BadId(ValueError):
    """An id that is not 12 lowercase hex chars (so it can never be one we handed out)."""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise BadId(f"invalid id {value!r}")
    return value


@contextlib.contextmanager
def _db():
    """`with _db() as c:` -> locked, auto-committing (or rolling back), auto-closing connection."""
    with _lock:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)  # timeout == busy_timeout
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA foreign_keys=ON")
            with c:
                yield c
        finally:
            c.close()


def _exists(c: sqlite3.Connection, pid: str) -> bool:
    return c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone() is not None


def json_depth(obj, limit: int) -> int:
    """Nesting depth of decoded JSON, stopping early once it exceeds `limit` (iterative: no recursion)."""
    depth, stack = 0, [(obj, 1)]
    while stack:
        o, d = stack.pop()
        depth = max(depth, d)
        if depth > limit:
            break
        if isinstance(o, dict):
            stack.extend((v, d + 1) for v in o.values())
        elif isinstance(o, list):
            stack.extend((v, d + 1) for v in o)
    return depth


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file + os.replace so a crash never leaves a truncated file behind."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def init() -> None:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                name TEXT NOT NULL,
                paper TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                original_ext TEXT NOT NULL,
                original_size INTEGER NOT NULL,
                image_id TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                format TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS exports_project ON exports(project_id);
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS images_project ON images(project_id);
            """
        )


def project_dir(pid: str) -> Path:
    """LIB_DIR/<pid>. Raises BadId (a ValueError) unless pid is a valid id that stays inside LIB_DIR."""
    check_id(pid)
    d = (LIB_DIR / pid).resolve()
    if d.parent != LIB_DIR.resolve():
        raise BadId(pid)
    return d


def expires_at(updated_at: float) -> float:
    return updated_at + RETENTION_DAYS * DAY


def _with_expiry(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["expires_at"] = expires_at(d["updated_at"])
    return d


# ------------------------------------------------------------------ projects
def create_project(
    original: bytes, filename: str, image_id: str, width: int, height: int, paper: str, thumb_jpeg: bytes
) -> str:
    pid = new_id()
    check_id(image_id)
    ext = Path(filename).suffix.lower()
    ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ".bin"
    d = project_dir(pid)
    (d / "exports").mkdir(parents=True, exist_ok=True)
    (d / f"original{ext}").write_bytes(original)
    (d / "thumb.jpg").write_bytes(thumb_jpeg)
    name = Path(filename).stem[:120] or f"project-{pid}"
    now = time.time()
    with _db() as c:
        c.execute(
            "INSERT INTO projects (id, created_at, updated_at, name, paper, original_filename, original_ext, original_size, image_id, width, height) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, now, now, name, paper, filename[:255], ext, len(original), image_id, width, height),
        )
        c.execute(
            "INSERT INTO images (id, project_id, kind, created_at) VALUES (?,?,?,?)",
            (image_id, pid, "upload", now),
        )
    return pid


def create_blank(name: str) -> str:
    """A project with no photo, e.g. from the plain bin generator."""
    pid = new_id()
    d = project_dir(pid)
    (d / "exports").mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _db() as c:
        c.execute(
            "INSERT INTO projects (id, created_at, updated_at, name, paper, original_filename, original_ext, original_size, image_id, width, height) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, now, now, name, "none", "", "", 0, "", 0, 0),
        )
    return pid


def _touch_row(c: sqlite3.Connection, pid: str, now: float | None = None) -> None:
    c.execute("UPDATE projects SET updated_at=? WHERE id=?", (now or time.time(), pid))


def touch(pid: str) -> float | None:
    """Bump updated_at (the frontend calls this when a project is opened). Returns the new expires_at."""
    check_id(pid)
    now = time.time()
    with _db() as c:
        cur = c.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, pid))
        if cur.rowcount == 0:
            return None
    return expires_at(now)


def save_snapshot(pid: str, png: bytes) -> None:
    """Raises KeyError if the project is gone."""
    d = project_dir(pid)
    with _db() as c:  # the write sits inside the lock so delete_project cannot pull the folder away
        if not _exists(c, pid) or not d.exists():
            raise KeyError(pid)
        _atomic_write(d / "snapshot.png", png)
        _touch_row(c, pid)


def save_state(pid: str, state: dict) -> None:
    """Raises KeyError if the project is gone, ValueError if the state nests deeper than MAX_STATE_DEPTH
    (FastAPI's encoder recurses over it on GET, and a RecursionError there would brick the project)."""
    if json_depth(state, MAX_STATE_DEPTH) > MAX_STATE_DEPTH:
        raise ValueError(f"state nests deeper than {MAX_STATE_DEPTH} levels")
    d = project_dir(pid)
    with _db() as c:
        if not _exists(c, pid) or not d.exists():
            raise KeyError(pid)
        _atomic_write(d / "state.json", json.dumps(state).encode())
        _touch_row(c, pid)


def load_state(pid: str) -> dict | None:
    """The saved state, or None when there is none or the file is unreadable / too deeply nested
    (a corrupt state.json must not make the project unopenable)."""
    p = project_dir(pid) / "state.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        log.warning("state.json for %s is corrupt (%s); ignoring it", pid, e)
        return None
    if not isinstance(data, dict) or json_depth(data, MAX_STATE_DEPTH) > MAX_STATE_DEPTH:
        log.warning("state.json for %s is not a sane object; ignoring it", pid)
        return None
    return data


def add_export(pid: str, fmt: str, filename: str, data: bytes, summary: str = "") -> str:
    """Raises KeyError if the project is gone."""
    d = project_dir(pid)
    if fmt not in ("stl", "3mf", "step"):
        raise ValueError(fmt)
    eid = new_id()
    now = time.time()
    with _db() as c:
        if not _exists(c, pid) or not d.exists():
            raise KeyError(pid)
        (d / "exports").mkdir(exist_ok=True)
        _atomic_write(d / "exports" / f"{eid}.{fmt}", data)
        c.execute(
            "INSERT INTO exports (id, project_id, created_at, format, filename, size, summary) VALUES (?,?,?,?,?,?,?)",
            (eid, pid, now, fmt, filename, len(data), summary),
        )
        _touch_row(c, pid, now)
    return eid


def export_path(eid: str) -> tuple[Path, sqlite3.Row] | None:
    check_id(eid)
    with _db() as c:
        row = c.execute("SELECT * FROM exports WHERE id=?", (eid,)).fetchone()
    if row is None:
        return None
    return project_dir(row["project_id"]) / "exports" / f"{eid}.{row['format']}", row


def list_projects() -> list[dict]:
    with _db() as c:
        projects = c.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        exports = c.execute("SELECT * FROM exports ORDER BY created_at DESC").fetchall()
    by_pid: dict[str, list[dict]] = {}
    for e in exports:
        by_pid.setdefault(e["project_id"], []).append(dict(e))
    out = []
    for p in projects:
        d = _with_expiry(p)
        pd = project_dir(p["id"])
        d["exports"] = by_pid.get(p["id"], [])
        d["has_state"] = (pd / "state.json").exists()
        d["has_snapshot"] = (pd / "snapshot.png").exists()
        d["has_photo"] = bool(p["original_ext"])
        out.append(d)
    return out


def get_project(pid: str) -> dict | None:
    check_id(pid)
    with _db() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return _with_expiry(p) if p else None


def rename(pid: str, name: str, notes: str | None = None) -> bool:
    check_id(pid)
    with _db() as c:
        if notes is None:
            cur = c.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, time.time(), pid))
        else:
            cur = c.execute(
                "UPDATE projects SET name=?, notes=?, updated_at=? WHERE id=?",
                (name, notes, time.time(), pid),
            )
    return cur.rowcount > 0


def delete_project(pid: str, if_updated_at: float | None = None) -> bool:
    """Delete the row (exports and images cascade) and then the folder, both under the lock. Returns
    False if no such project (nothing on disk is touched) or, with if_updated_at, if the project was
    touched since that timestamp was read (the retention sweep must not delete a project someone
    just opened)."""
    d = project_dir(pid)
    with _db() as c:
        if if_updated_at is None:
            cur = c.execute("DELETE FROM projects WHERE id=?", (pid,))
        else:
            cur = c.execute("DELETE FROM projects WHERE id=? AND updated_at=?", (pid, if_updated_at))
        if cur.rowcount == 0:
            return False
        shutil.rmtree(d, ignore_errors=True)
    return True


def delete_export(eid: str) -> bool:
    found = export_path(eid)
    with _db() as c:
        cur = c.execute("DELETE FROM exports WHERE id=?", (eid,))
    if found and found[0].exists():
        found[0].unlink()
    return cur.rowcount > 0


# ------------------------------------------------------------------ images
def register_image(pid: str, image_id: str, kind: str) -> None:
    check_id(pid)
    check_id(image_id)
    if kind not in IMAGE_KINDS:
        raise ValueError(kind)
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO images (id, project_id, kind, created_at) VALUES (?,?,?,?)",
            (image_id, pid, kind, time.time()),
        )


def image_project(image_id: str) -> str | None:
    """The project an upload/rect image belongs to, or None."""
    check_id(image_id)
    with _db() as c:
        row = c.execute("SELECT project_id FROM images WHERE id=?", (image_id,)).fetchone()
    return row["project_id"] if row else None


def image_path(kind: str, image_id: str, suffix: str = ".png") -> Path | None:
    """Where `<kind>_<id><suffix>` lives, or None if unknown."""
    if kind not in IMAGE_KINDS:
        raise ValueError(kind)
    check_id(image_id)
    pid = image_project(image_id)
    if pid is None:
        return None
    p = project_dir(pid) / f"{kind}_{image_id}{suffix}"
    return p if p.exists() else None


def new_image_path(kind: str, pid: str, image_id: str, suffix: str = ".png") -> Path:
    """Reserve `<kind>_<id>` for project pid and return the path to write it to."""
    d = project_dir(pid)
    if not d.exists():
        raise KeyError(pid)
    register_image(pid, image_id, kind)
    return d / f"{kind}_{image_id}{suffix}"


def write_json(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data).encode())


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ retention
def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def sweep(now: float | None = None) -> dict:
    """Delete (a) projects not updated for RETENTION_DAYS, (b) blank projects older than BLANK_DAYS,
    (c) orphan project folders older than ORPHAN_DAYS. Returns counts; logs every removal.
    A project touched between the snapshot read and the delete is left alone (delete_project's guard)."""
    now = time.time() if now is None else now
    removed = {"expired_projects": 0, "blank_projects": 0, "orphan_dirs": 0}

    with _db() as c:
        projects = c.execute("SELECT * FROM projects").fetchall()
        export_counts = {
            r["project_id"]: r["n"]
            for r in c.execute("SELECT project_id, COUNT(*) AS n FROM exports GROUP BY project_id")
        }
    known = set()
    for p in projects:
        pid = p["id"]
        known.add(pid)
        pd = project_dir(pid)
        age_days = (now - p["updated_at"]) / DAY
        if age_days > RETENTION_DAYS:
            if delete_project(pid, if_updated_at=p["updated_at"]):
                removed["expired_projects"] += 1
                log.info(
                    "retention: deleted project %s (%r), last updated %.0f days ago", pid, p["name"], age_days
                )
            continue
        blank = (
            not p["original_ext"]
            and export_counts.get(pid, 0) == 0
            and not (pd / "state.json").exists()
            and not (pd / "snapshot.png").exists()
        )
        if blank and (now - p["created_at"]) / DAY > BLANK_DAYS:
            if delete_project(pid, if_updated_at=p["updated_at"]):
                removed["blank_projects"] += 1
                log.info("retention: deleted blank project %s (%r)", pid, p["name"])

    cutoff = now - ORPHAN_DAYS * DAY
    # project folders without a row (e.g. a crash between the DB delete and rmtree, or manual edits)
    for d in LIB_DIR.iterdir() if LIB_DIR.exists() else []:
        if d.is_dir() and d.name not in known and _ID_RE.match(d.name) and _mtime(d) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed["orphan_dirs"] += 1
            log.info("retention: deleted orphan folder %s", d.name)
    if any(removed.values()):
        log.info("retention sweep: %s", removed)
    return removed
