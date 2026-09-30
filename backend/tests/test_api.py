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
from app import library, main, outline, segment  # noqa: E402
from app.config import DATA_DIR  # noqa: E402
from app.gridfinity import BinConfig, build_bin  # noqa: E402
from app.imageio import decode_image  # noqa: E402
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
    orphan = library.LIB_DIR / "0123456789ab"  # folder without a row, e.g. a crash mid-delete
    orphan.mkdir()
    os.utime(orphan, (now - 8 * day, now - 8 * day))

    removed = library.sweep(now)
    assert removed["expired_projects"] >= 1 and removed["blank_projects"] >= 1 and removed["orphan_dirs"] == 1
    ids = {p["id"] for p in client.get("/api/library").json()["projects"]}
    assert old not in ids and blank_old not in ids
    assert fresh in ids and blank_new in ids
    assert not library.project_dir(old).exists()
    assert library.project_dir(fresh).exists()
    assert not orphan.exists()


def test_sweep_spares_a_project_touched_meanwhile(client):
    """The sweep snapshots updated_at, then deletes; a touch in between must win."""
    pid = client.post("/api/library/new", json={"name": "opened just now"}).json()["project_id"]
    client.post(f"/api/library/{pid}/state", json={"bin": BIN})
    stale = time.time() - 91 * 86400
    _set_times(pid, stale)
    assert library.touch(pid) is not None  # user opens it while the sweep runs
    assert library.delete_project(pid, if_updated_at=stale) is False
    assert client.get(f"/api/library/{pid}").status_code == 200
    assert library.delete_project(pid) is True


def test_writes_into_deleted_project_are_clean_errors(client):
    pid = client.post("/api/library/new", json={"name": "gone"}).json()["project_id"]
    assert client.delete(f"/api/library/{pid}").status_code == 200
    with pytest.raises(KeyError):
        library.save_state(pid, {"a": 1})
    with pytest.raises(KeyError):
        library.save_snapshot(pid, b"\x89PNG")
    with pytest.raises(KeyError):
        library.add_export(pid, "stl", "x.stl", b"solid", "")
    assert not library.project_dir(pid).exists()  # nothing resurrected the folder


# ------------------------------------------------------------------ regressions from the review
def test_polygon_coordinates_must_be_finite_and_bounded(client):
    base = {"polygon": [[0, 0], [50, 0], [50, 20], [0, 20]]}
    for bad in (b"NaN", b"Infinity", b"-Infinity", b"1e6"):
        body = b'{"polygon": [[' + bad + b",0],[50,0],[50,20],[0,20]]}"
        r = client.post("/api/polygon/offset", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 422, (bad, r.status_code, r.text[:100])
        pocket = b'{"bin": {}, "pockets": [{"polygon": [[' + bad + b",0],[50,0],[50,20],[0,20]]}]}"
        assert (
            client.post(
                "/api/generate", content=pocket, headers={"content-type": "application/json"}
            ).status_code
            == 422
        )
    r = client.post(
        "/api/rectify",
        content=b'{"image_id": "0123456789ab", "corners": [[NaN,0],[1,0],[1,1],[0,1]]}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert client.post("/api/polygon/offset", json=base).status_code == 200


def test_gaussian_smooth_is_bounded_in_memory():
    """A polygon with a 400 km perimeter used to allocate n = perimeter / 0.25 mm sample points."""
    t = time.time()
    out = outline.gaussian_smooth([[0, 0], [1e5, 0], [1e5, 1e5], [0, 1e5]], 1.0)
    assert time.time() - t < 2.0
    assert len(out) <= outline.MAX_RESAMPLE_POINTS


def test_chunked_body_without_content_length_is_capped(client):
    big = (
        b'{"polygon": [[0,0],[50,0],[50,20],[0,20]], "junk": "' + b"a" * (main.MAX_JSON_BYTES + 1024) + b'"}'
    )

    def chunks():
        for i in range(0, len(big), 65536):
            yield big[i : i + 65536]

    r = client.post("/api/polygon/offset", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413, (r.status_code, r.text[:100])
    assert "too big" in r.json()["detail"]
    r = client.post("/api/generate", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    pid = client.post("/api/library/new", json={"name": "chunked"}).json()["project_id"]
    r = client.post(
        f"/api/library/{pid}/state", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_state_nesting_depth_is_guarded(client):
    pid = client.post("/api/library/new", json={"name": "deep"}).json()["project_id"]
    deep = b'{"a":' + b"[" * 5000 + b"]" * 5000 + b"}"
    r = client.post(f"/api/library/{pid}/state", content=deep, headers={"content-type": "application/json"})
    assert r.status_code == 400 and "deep" in r.json()["detail"]
    r = client.post(
        "/api/generate",
        content=b'{"bin": {}, "project_id": "' + pid.encode() + b'", "save": true, "state": ' + deep + b"}",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    # a state.json that got deep by other means must not make the project unopenable
    (library.project_dir(pid) / "state.json").write_bytes(deep)
    r = client.get(f"/api/library/{pid}")
    assert r.status_code == 200 and r.json()["state"] is None
    assert client.post(f"/api/library/{pid}/state", json={"a": [[[1]]]}).status_code == 200
    assert client.get(f"/api/library/{pid}").json()["state"] == {"a": [[[1]]]}


def test_reordered_corners_are_accepted(client):
    up = client.post(
        "/api/upload",
        files={"file": ("p.png", _png_bytes(800, 600), "image/png")},
        data={"paper_size": "letter"},
    )
    assert up.status_code == 200, up.text
    iid = up.json()["image_id"]
    for order in (
        [[100, 100], [700, 100], [700, 500], [100, 500]],
        [[100, 100], [700, 500], [700, 100], [100, 500]],
    ):
        r = client.post("/api/rectify", json={"image_id": iid, "paper": "letter", "corners": order})
        assert r.status_code == 200, (order, r.text)
    # still rejected: collinear, duplicate-point, tiny
    for bad in (
        [[100, 100], [400, 100], [700, 100], [100, 500]],
        [[100, 100], [100, 100], [700, 100], [700, 500]],
        [[5, 5], [6, 5], [6, 6], [5, 6]],
    ):
        assert (
            client.post("/api/rectify", json={"image_id": iid, "paper": "letter", "corners": bad}).status_code
            == 400
        )


def test_16bit_grayscale_png_keeps_its_gradient():
    from PIL import Image

    ramp = np.linspace(0, 65535, 256).astype(np.uint16)[None, :].repeat(8, 0)
    buf = io.BytesIO()
    Image.fromarray(ramp).save(buf, format="PNG")  # uint16 -> mode "I;16"
    img = decode_image(buf.getvalue(), 3000)
    assert img.shape == (8, 256, 3)
    assert len(np.unique(img)) > 200 and img[0, 0, 0] == 0 and img[0, -1, 0] == 255


def test_upload_validates_paper_size_and_custom_cap(client):
    r = client.post(
        "/api/upload", files={"file": ("p.png", _png_bytes(), "image/png")}, data={"paper_size": "../../../x"}
    )
    assert r.status_code == 400 and "paper size" in r.json()["detail"]
    r = client.post(
        "/api/upload",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        data={"paper_size": "custom", "custom_w_mm": "1500", "custom_h_mm": "300"},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/rectify",
        json={
            "image_id": "0123456789ab",
            "paper": "custom",
            "custom_w_mm": 1500,
            "custom_h_mm": 300,
            "corners": [[0, 0], [1, 0], [1, 1], [0, 1]],
        },
    )
    assert r.status_code == 422


def test_clicked_components_needs_a_positive_hit():
    a = np.zeros((10, 10), bool)
    a[1:4, 1:4] = True
    b = np.zeros((10, 10), bool)
    b[6:9, 6:9] = True
    assert segment.clicked_components([a, b], [(2, 2)]) == [a]
    assert segment.clicked_components([a, b], []) == []  # only "remove" points: no tool, no guess
    assert segment.clicked_components([a, b], [(1e5, 1e5), (-1, 2)]) == []  # off-image clicks never hit
    assert len(segment.clicked_components([a, b], [(2, 2), (7, 7)])) == 2


def test_infer_pool_backlog_answers_503(client, monkeypatch):
    monkeypatch.setattr(main, "INFER_MAX_PENDING", 0)
    r = client.post(
        "/api/upload", files={"file": ("p.png", _png_bytes(), "image/png")}, data={"paper_size": "letter"}
    )
    assert r.status_code == 503 and "busy" in r.json()["detail"]


def test_recycled_cad_pool_answers_503(client, monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    async def broken(*_a):
        raise BrokenProcessPool("pool was reset by another request's timeout")

    monkeypatch.setattr(main, "_run_cad", broken)
    r = client.post("/api/generate", json={"bin": {**BIN, "height_units": 19}, "pockets": []})  # not cached
    assert r.status_code == 503 and "busy" in r.json()["detail"]


def test_preview_cache_is_capped_by_total_bytes():
    cache = main._LruBytes(entries=10, max_item=100, max_total=250)
    for i in range(4):
        cache.put(str(i), b"x" * 100)
    assert cache.get("0") is None and cache.get("1") is None  # evicted to stay under 250 bytes
    assert cache.get("2") is not None and cache.get("3") is not None
    cache.put("2", b"y" * 50)  # replacing an entry accounts for the old size
    assert cache._total == 150
    cache.put("big", b"z" * 101)  # over max_item: ignored
    assert cache.get("big") is None


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
