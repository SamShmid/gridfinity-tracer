"""HTTP-level tests: id validation, size limits, bounds, filename sanitising, CAD, retention.

conftest.py points GT_DATA_DIR at a temp dir before anything imports app.config, so nothing here can
touch a real data volume.
"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import library  # noqa: E402
from app.config import DATA_DIR  # noqa: E402
from app.gridfinity import BinConfig, build_bin  # noqa: E402
from app.main import MAX_UPLOAD_BYTES, app  # noqa: E402
from tests.synth import make_photo  # noqa: E402

assert "gt-test-data-" in str(DATA_DIR) or os.environ.get("GT_DATA_DIR"), (
    "tests must run against a temp data dir"
)

BIN = {
    "grid_x": 2,
    "grid_y": 1,
    "height_units": 3,
    "lip": "regular",
    "holes": "none",
    "solid": True,
    "dividers_x": 0,
    "dividers_y": 0,
    "scoop": False,
    "scoop_radius": 10,
    "label_tab": False,
    "label_width": 13,
}
# A rectangle with a notch cut out of one long side (concave), plus a finger slot.
NOTCHED = [[10, 10], [70, 10], [70, 30], [45, 30], [45, 22], [35, 22], [35, 30], [10, 30]]


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _png_bytes(w=64, h=48) -> bytes:
    img = np.full((h, w, 3), 200, np.uint8)
    return cv2.imencode(".png", img)[1].tobytes()


def _photo_jpeg() -> bytes:
    img, _, _ = make_photo(w=1000, h=750)
    return cv2.imencode(".jpg", img)[1].tobytes()


# ------------------------------------------------------------------ ids / traversal
def test_project_dir_rejects_bad_ids():
    for bad in ("..", "../..", "x" * 12, "ABCDEFABCDEF", "0123456789ab/..", "", "0123456789abc"):
        with pytest.raises(ValueError):
            library.project_dir(bad)
    assert library.project_dir("0123456789ab").parent == library.LIB_DIR.resolve()


def test_traversal_ids_rejected_and_nothing_deleted(client):
    pid = client.post("/api/library/new", json={"name": "keep me"}).json()["project_id"]
    db = library.DB_PATH
    assert db.exists()
    before = sorted(p.name for p in DATA_DIR.iterdir())
    for bad in ("%2e%2e", "..%2f..", "notanid12345", "ZZZZZZZZZZZZ"):
        for method, url in (
            ("delete", f"/api/library/{bad}"),
            ("get", f"/api/library/{bad}"),
            ("get", f"/api/library/{bad}/thumb"),
            ("get", f"/api/library/{bad}/snapshot"),
            ("put", f"/api/library/{bad}/touch"),
            ("get", f"/api/image/upload/{bad}"),
            ("get", f"/api/image/rect/{bad}"),
            ("get", f"/api/library/exports/{bad}"),
            ("delete", f"/api/library/exports/{bad}"),
        ):
            r = getattr(client, method)(url, follow_redirects=False)
            # 405/307: a decoded '/../..' has a slash, matches no API route and falls through to the static mount
            assert r.status_code in (404, 405, 307, 422), (method, url, r.status_code, r.text)
        r = client.post(f"/api/library/{bad}/state", json={"a": 1})
        assert r.status_code in (404, 405, 422)
    assert db.exists()
    assert sorted(p.name for p in DATA_DIR.iterdir()) == before
    assert client.get(f"/api/library/{pid}").status_code == 200
    # body ids go through the same pattern
    r = client.post(
        "/api/rectify", json={"image_id": "../../etc", "corners": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    )
    assert r.status_code == 422
    r = client.post("/api/sam/warm", json={"rect_id": ".."})
    assert r.status_code == 422
    # a well-formed but unknown id is a plain 404
    assert client.delete("/api/library/0123456789ab").status_code == 404
    assert client.get("/api/image/upload/0123456789ab").status_code == 404


# ------------------------------------------------------------------ limits
def test_oversized_upload_413(client):
    big = b"\0" * (MAX_UPLOAD_BYTES + 1024)
    r = client.post(
        "/api/upload", files={"file": ("big.jpg", big, "image/jpeg")}, data={"paper_size": "letter"}
    )
    assert r.status_code == 413
    assert "too big" in r.json()["detail"]
    assert "—" not in r.json()["detail"]


def test_oversized_snapshot_and_state_413(client):
    pid = client.post("/api/library/new", json={"name": "limits"}).json()["project_id"]
    r = client.post(
        f"/api/library/{pid}/snapshot",
        files={"file": ("s.png", b"\x89PNG\r\n\x1a\n" + b"\0" * (3 * 1024 * 1024 + 10), "image/png")},
    )
    assert r.status_code == 413
    r = client.post(
        f"/api/library/{pid}/state",
        content=b'{"x": "' + b"a" * (2 * 1024 * 1024 + 10) + b'"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_bad_image_400(client):
    r = client.post(
        "/api/upload",
        files={"file": ("nope.jpg", b"definitely not a photo", "image/jpeg")},
        data={"paper_size": "letter"},
    )
    assert r.status_code == 400
    assert "photo" in r.json()["detail"].lower()


def test_megapixel_limit_413(client):
    # A valid PNG header claiming 20000 x 20000 px; PIL reads the size without decoding the pixels.
    buf = io.BytesIO()
    from PIL import Image

    Image.new("L", (16, 16)).save(buf, format="PNG")
    png = bytearray(buf.getvalue())
    png[16:24] = (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big")  # IHDR width/height
    import zlib

    png[29:33] = zlib.crc32(bytes(png[12:29])).to_bytes(4, "big")  # keep the IHDR CRC valid
    r = client.post(
        "/api/upload", files={"file": ("huge.png", bytes(png), "image/png")}, data={"paper_size": "letter"}
    )
    assert r.status_code == 413, r.text
    assert "megapixel" in r.json()["detail"]


def test_bounds_422(client):
    base = {"polygon": [[0, 0], [50, 0], [50, 20], [0, 20]]}
    assert client.post("/api/polygon/offset", json=base).status_code == 200
    for bad in (
        {"gaussian_mm": 1e6},
        {"clearance_mm": 50},
        {"tolerance_mm": 0},
        {"bridge_mm": -1},
        {"printer_offset_mm": 2},
    ):
        r = client.post("/api/polygon/offset", json={**base, **bad})
        assert r.status_code == 422, bad
    # polygons need 3 points, points need exactly 2 coordinates
    assert client.post("/api/polygon/offset", json={"polygon": [[0, 0], [1, 1]]}).status_code == 422
    assert (
        client.post("/api/polygon/offset", json={"polygon": [[0, 0, 0], [1, 1, 1], [2, 0, 0]]}).status_code
        == 422
    )
    assert client.post("/api/polygon/offset", json={"polygon": []}).status_code == 422
    for bad in ({"height_units": 0}, {"grid_x": 11}, {"scoop_radius": 0}, {"label_width": 41}):
        r = client.post("/api/generate", json={"bin": {**BIN, **bad}, "pockets": []})
        assert r.status_code == 422, bad
    assert client.post("/api/generate", json={"bin": BIN, "pockets": [], "tolerance": 0}).status_code == 422
    assert (
        client.post(
            "/api/generate", json={"bin": BIN, "pockets": [{"polygon": NOTCHED, "depth": 500}]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/generate",
            json={
                "bin": BIN,
                "pockets": [
                    {"polygon": NOTCHED, "depth": 10, "finger_holes": [{"x": 0, "y": 0, "diameter": 1}]}
                ],
            },
        ).status_code
        == 422
    )
    # degenerate corners are a 400, not a 500
    up = client.post(
        "/api/upload", files={"file": ("p.png", _png_bytes(), "image/png")}, data={"paper_size": "letter"}
    )
    assert up.status_code == 200
    r = client.post(
        "/api/rectify",
        json={
            "image_id": up.json()["image_id"],
            "corners": [[5, 5], [5, 5], [5, 5], [5, 5]],
            "paper": "letter",
        },
    )
    assert r.status_code == 400
    r = client.post(
        "/api/rectify",
        json={"image_id": up.json()["image_id"], "corners": [[0, 0], [1, 0], [1, 1]], "paper": "letter"},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ generate
def test_generate_with_notch_and_unicode_filename(client):
    body = {
        "bin": BIN,
        "pockets": [
            {
                "polygon": NOTCHED,
                "depth": 10,
                "finger_holes": [
                    {"x": 20, "y": 20, "diameter": 8, "length": 8, "angle_deg": 0, "extra_depth": 3}
                ],
            }
        ],
        "format": "stl",
        "filename": "héllo wörld/../中文 bin",
    }
    t = time.time()
    r = client.post("/api/generate", json=body)
    assert r.status_code == 200, r.text
    first = time.time() - t
    assert r.headers["content-type"].startswith("model/stl")
    assert len(r.content) > 10_000
    cd = r.headers["content-disposition"]
    assert cd.isascii()
    assert 'filename="hllowrldbin.stl"' in cd
    # second identical request is served from the preview cache
    t = time.time()
    r2 = client.post("/api/generate", json=body)
    assert r2.status_code == 200 and r2.content == r.content
    assert time.time() - t < max(0.5, first / 4)
    # saving into a project stores the export and state and flags it in the header
    pid = client.post("/api/library/new", json={"name": "gen"}).json()["project_id"]
    r3 = client.post("/api/generate", json={**body, "project_id": pid, "save": True, "state": {"bin": BIN}})
    assert r3.status_code == 200 and r3.headers["x-saved"] == "1"
    proj = client.get(f"/api/library/{pid}").json()
    assert proj["state"] == {"bin": BIN}
    lib = client.get("/api/library").json()
    me = next(p for p in lib["projects"] if p["id"] == pid)
    assert len(me["exports"]) == 1 and me["exports"][0]["filename"] == "hllowrldbin.stl"
    assert me["expires_at"] == pytest.approx(me["updated_at"] + library.RETENTION_DAYS * 86400)
    eid = me["exports"][0]["id"]
    assert client.get(f"/api/library/exports/{eid}").status_code == 200
    assert client.delete(f"/api/library/exports/{eid}").status_code == 200
    assert client.get(f"/api/library/exports/{eid}").status_code == 404


def test_hollow_lip_none_features_stay_below_rim():
    for units in (2, 4):
        cfg = BinConfig(
            grid_x=2,
            grid_y=1,
            height_units=units,
            lip="none",
            solid=False,
            dividers_x=1,
            dividers_y=1,
            scoop=True,
            label_tab=True,
        )
        part = build_bin(cfg)
        bb = part.bounding_box()
        assert abs(bb.max.Z - 7 * units) < 0.05, (units, bb.max.Z)
        assert part.is_valid
    cfg = BinConfig(grid_x=2, grid_y=1, height_units=3, lip="regular", solid=False, dividers_x=2)
    assert abs(build_bin(cfg).bounding_box().max.Z - (21 + 4.4)) < 0.05


# ------------------------------------------------------------------ pipeline round trip
def test_upload_rectify_images_live_in_project_folder(client):
    r = client.post(
        "/api/upload",
        files={"file": ("bench.jpg", _photo_jpeg(), "image/jpeg")},
        data={"paper_size": "letter", "name": "round trip"},
    )
    assert r.status_code == 200, r.text
    up = r.json()
    pid, image_id = up["project_id"], up["image_id"]
    assert (library.project_dir(pid) / f"upload_{image_id}.png").exists()
    assert not (DATA_DIR / f"upload_{image_id}.png").exists()
    assert client.get(f"/api/image/upload/{image_id}").status_code == 200
    r = client.post(
        "/api/rectify",
        json={"image_id": image_id, "corners": up["corners"], "paper": "letter", "orientation": "auto"},
    )
    assert r.status_code == 200, r.text
    rect_id = r.json()["rect_id"]
    assert (library.project_dir(pid) / f"rect_{rect_id}.png").exists()
    assert (library.project_dir(pid) / f"rect_{rect_id}.json").exists()
    assert client.get(f"/api/image/rect/{rect_id}").status_code == 200
    r = client.post("/api/detect/auto", json={"rect_id": rect_id, "method": "classical", "refine": False})
    assert r.status_code == 200 and r.json()["tools"], r.text
    assert client.get(f"/api/library/{pid}/thumb").status_code == 200
    assert client.get(f"/api/library/{pid}/original").status_code == 200
    # touch bumps updated_at and returns the new expiry
    before = client.get(f"/api/library/{pid}").json()["updated_at"]
    time.sleep(0.01)
    t = client.put(f"/api/library/{pid}/touch")
    assert t.status_code == 200 and t.json()["expires_at"] > before
    # deleting the project removes its images with it
    assert client.delete(f"/api/library/{pid}").status_code == 200
    assert not library.project_dir(pid).exists()
    assert client.get(f"/api/image/upload/{image_id}").status_code == 404
    assert client.get(f"/api/image/rect/{rect_id}").status_code == 404


def test_corrupt_state_returns_null(client):
    pid = client.post("/api/library/new", json={"name": "corrupt"}).json()["project_id"]
    assert client.post(f"/api/library/{pid}/state", json={"ok": 1}).status_code == 200
    (library.project_dir(pid) / "state.json").write_text("{not json")
    r = client.get(f"/api/library/{pid}")
    assert r.status_code == 200 and r.json()["state"] is None


def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert set(h) >= {"ok", "sam", "models_ready", "version"}


# ------------------------------------------------------------------ retention
def _set_times(pid: str, updated: float, created: float | None = None):
    with library._db() as c:
        c.execute(
            "UPDATE projects SET updated_at=?, created_at=? WHERE id=?",
            (updated, created if created is not None else updated, pid),
        )


def test_retention_sweep(client):
    day = 86400
    now = time.time()
    old = client.post("/api/library/new", json={"name": "old"}).json()["project_id"]
    client.post(f"/api/library/{old}/state", json={"bin": BIN})
    _set_times(old, now - 91 * day)
    fresh = client.post("/api/library/new", json={"name": "fresh"}).json()["project_id"]
    client.post(f"/api/library/{fresh}/state", json={"bin": BIN})
    _set_times(fresh, now - 30 * day)
    blank_old = client.post("/api/library/new", json={"name": "New bin"}).json()["project_id"]
    _set_times(blank_old, now - 2 * day)
    blank_new = client.post("/api/library/new", json={"name": "New bin"}).json()["project_id"]
    legacy = DATA_DIR / "rect_0123456789ab.png"
    legacy.write_bytes(b"x")
    os.utime(legacy, (now - 8 * day, now - 8 * day))
    legacy_fresh = DATA_DIR / "upload_0123456789ab.png"
    legacy_fresh.write_bytes(b"x")

    removed = library.sweep(now)
    assert (
        removed["expired_projects"] >= 1 and removed["blank_projects"] >= 1 and removed["orphan_files"] == 1
    )
    ids = {p["id"] for p in client.get("/api/library").json()["projects"]}
    assert old not in ids and blank_old not in ids
    assert fresh in ids and blank_new in ids
    assert not library.project_dir(old).exists()
    assert library.project_dir(fresh).exists()
    assert not legacy.exists() and legacy_fresh.exists()
    legacy_fresh.unlink()


def test_every_id_route_binds_its_own_param(client):
    """Regression: a shared Path() instance made every id route after the first return 422
    'image_id: Field required'. Each route must accept its own id (404 for unknown, never 422)."""
    good = "0123456789ab"
    checks = [
        ("GET", f"/api/image/rect/{good}"),
        ("GET", f"/api/library/{good}"),
        ("GET", f"/api/library/{good}/thumb"),
        ("GET", f"/api/library/{good}/snapshot"),
        ("GET", f"/api/library/{good}/original"),
        ("PUT", f"/api/library/{good}/touch"),
        ("GET", f"/api/library/exports/{good}"),
    ]
    for method, url in checks:
        r = client.request(method, url)
        assert r.status_code == 404, f"{method} {url} -> {r.status_code} {r.text[:120]}"
    r = client.post(f"/api/library/{good}/state", json={"a": 1})
    assert r.status_code == 404, r.text
    r = client.get("/api/library/zz")  # bad id shape -> 422
    assert r.status_code == 422
