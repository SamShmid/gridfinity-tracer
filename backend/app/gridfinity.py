"""Gridfinity bin generator (build123d / OpenCascade).

Dimensions follow the unofficial spec (gridfinity.xyz) as implemented by
gridfinity-rebuilt-openscad: 42 mm pitch, 7 mm height unit, 41.5 mm cell footprint,
foot profile 0.8 / 1.8 / 2.15 mm (bottom-up), stacking lip 0.7 / 1.8 / 1.9 mm,
magnet 6.5 x 2.4 mm, screw 3 x 6 mm, holes 8 mm in from the cell edge.

Coordinate convention for tool pockets: the frontend works in "layout mm" with the
origin at the bin's top-left corner (top view), x to the right, y DOWN. We convert to
CAD coordinates (origin at bin centre, y up) here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import build123d as bd

from .outline import clean_polygon

PITCH = 42.0
UNIT_H = 7.0
CELL = PITCH - 0.5  # 41.5
R_BASE = 3.75
FOOT_H = 4.75  # 0.8 + 1.8 + 2.15
FLOOR_TOP = 7.0  # top of the solid floor above the feet (2.25 mm floor)
LIP_H = 4.4
LIP_DEPTH = 2.6
WALL = 0.95
DIVIDER = 1.2
MAGNET_D, MAGNET_H = 6.5, 2.4
SCREW_D, SCREW_H = 3.0, 6.0
HOLE_INSET = 8.0
# The body is solid from FLOOR_TOP (7.0) upward. Below that are the feet with voids between cells
# (the 0.5 mm gaps and the chamfers), so ANY cut below 7.0 can open the underside. Pockets and
# finger cuts therefore both stop at FLOOR_TOP: a 2.25 mm floor over the inter-cell voids, more over feet.
MIN_POCKET_FLOOR = FLOOR_TOP
MIN_CUT_FLOOR = FLOOR_TOP


@dataclass
class FingerHole:
    x: float  # layout mm
    y: float
    diameter: float = 20.0  # width
    length: float = 20.0  # overall length; == diameter -> round recess
    angle_deg: float = 0.0  # layout coords (y down), degrees
    extra_depth: float = 3.0


@dataclass
class Pocket:
    polygon: list[list[float]]  # layout mm, already offset for clearance
    depth: float = 15.0
    finger_holes: list[FingerHole] = field(default_factory=list)


@dataclass
class BinConfig:
    grid_x: int = 2
    grid_y: int = 2
    height_units: int = 3
    lip: Literal["regular", "none"] = "regular"
    holes: Literal["none", "magnet", "screw", "magnet_screw"] = "none"
    solid: bool = True  # solid block with pockets (tool holder) vs hollow bin
    wall: float = WALL
    dividers_x: int = 0  # number of compartments along X minus one -> walls
    dividers_y: int = 0
    scoop: bool = False
    scoop_radius: float = 10.0
    label_tab: bool = False
    label_width: float = 13.0

    @property
    def width(self) -> float:
        return PITCH * self.grid_x - 0.5

    @property
    def length(self) -> float:
        return PITCH * self.grid_y - 0.5

    @property
    def wall_top(self) -> float:
        return UNIT_H * self.height_units

    @property
    def total_height(self) -> float:
        return self.wall_top + (LIP_H if self.lip == "regular" else 0.0)


def _rr(size_x: float, size_y: float, r: float, z: float):
    r = max(min(r, size_x / 2 - 0.01, size_y / 2 - 0.01), 0.05)
    return bd.Plane.XY.offset(z) * bd.RectangleRounded(size_x, size_y, r)


def _foot() -> bd.Part:
    """One 41.5 mm foot, bottom at z=0, built as a ruled loft (exact chamfers)."""
    sections = [
        _rr(CELL - 2 * 2.95, CELL - 2 * 2.95, R_BASE - 2.95, 0.0),  # 35.6, r 0.8
        _rr(CELL - 2 * 2.15, CELL - 2 * 2.15, R_BASE - 2.15, 0.8),  # 37.2, r 1.6
        _rr(CELL - 2 * 2.15, CELL - 2 * 2.15, R_BASE - 2.15, 2.6),
        _rr(CELL, CELL, R_BASE, FOOT_H),  # 41.5, r 3.75
    ]
    return bd.loft(sections, ruled=True)


def _lip_cavity(cfg: BinConfig) -> bd.Part:
    """Volume removed from the top of the walls to form the stacking lip (+ its support)."""
    W, L, H = cfg.width, cfg.length, cfg.wall_top
    w = cfg.wall

    def sec(inset: float, z: float):
        return _rr(W - 2 * inset, L - 2 * inset, R_BASE - inset, z)

    sections = [
        sec(w, H - (LIP_DEPTH - w)),  # support: 45 deg from the wall up to the lip
        sec(LIP_DEPTH, H),
        sec(LIP_DEPTH - 0.7, H + 0.7),
        sec(LIP_DEPTH - 0.7, H + 0.7 + 1.8),
        sec(0.0, H + LIP_H),
        sec(-0.6, H + LIP_H + 0.6),  # overshoot so the cut cleanly opens the top
    ]
    return bd.loft(sections, ruled=True)


def layout_to_cad(cfg: BinConfig, x: float, y: float) -> tuple[float, float]:
    return x - cfg.width / 2, cfg.length / 2 - y


def build_bin(cfg: BinConfig, pockets: list[Pocket] | None = None) -> bd.Part:
    pockets = pockets or []
    W, L, H = cfg.width, cfg.length, cfg.wall_top

    # Body block from the top of the feet to the top of the wall
    body = bd.extrude(_rr(W, L, R_BASE, FOOT_H), H - FOOT_H)
    # Feet: cells are 41.5 with a 0.5 gap; the bin outline is 42n-0.5, so cell i is centred at
    # -W/2 + 20.75 + 42 i.
    foot = _foot()
    feet = []
    for i in range(cfg.grid_x):
        for j in range(cfg.grid_y):
            cx = -W / 2 + CELL / 2 + i * PITCH
            cy = -L / 2 + CELL / 2 + j * PITCH
            feet.append(bd.Pos(cx, cy, 0) * foot)
    part = body.fuse(*feet)

    if cfg.lip == "regular":
        part = part + bd.extrude(_rr(W, L, R_BASE, H), LIP_H)
        part = part - _lip_cavity(cfg)

    if not cfg.solid:
        # cut_top: how far up the cavity cut reaches (past the rim so it opens the top cleanly).
        # feature_top: where dividers / scoop / label stop. With a lip that is the bottom of the lip
        # support; without one it is the rim itself. (Using cut_top for both put the dividers
        # 5.4 mm above the rim of a lip="none" bin.)
        if cfg.lip == "none":
            cut_top, feature_top = H + LIP_H + 1.0, H
        else:
            feature_top = H - (LIP_DEPTH - cfg.wall)
            cut_top = feature_top + 0.01
        inner = _rr(W - 2 * cfg.wall, L - 2 * cfg.wall, R_BASE - cfg.wall, FLOOR_TOP)
        part = part - bd.extrude(inner, cut_top - FLOOR_TOP)
        iw, il = W - 2 * cfg.wall, L - 2 * cfg.wall
        fh = feature_top - FLOOR_TOP
        # Dividers
        adds = []
        for k in range(1, cfg.dividers_x + 1):
            x = -iw / 2 + iw * k / (cfg.dividers_x + 1)
            adds.append(
                bd.Pos(x, 0, FLOOR_TOP)
                * bd.Box(DIVIDER, il, fh, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
            )
        for k in range(1, cfg.dividers_y + 1):
            y = -il / 2 + il * k / (cfg.dividers_y + 1)
            adds.append(
                bd.Pos(0, y, FLOOR_TOP)
                * bd.Box(iw, DIVIDER, fh, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
            )
        # Scoop: quarter-round fillet along the front (-Y) inner bottom edge
        if cfg.scoop and cfg.scoop_radius > 0:
            r = min(cfg.scoop_radius, il / 2, fh)
            block = bd.Pos(0, -il / 2, FLOOR_TOP) * bd.Box(
                iw, r, r, align=(bd.Align.CENTER, bd.Align.MIN, bd.Align.MIN)
            )
            cyl = bd.Pos(0, -il / 2 + r, FLOOR_TOP + r) * bd.Cylinder(r, iw + 2, rotation=(0, 90, 0))
            adds.append(block - cyl)
        # Label tab: shelf along the back (+Y) wall, top flush with feature_top
        if cfg.label_tab and cfg.label_width > 0:
            lw = min(cfg.label_width, il - 1, fh - 1.2)
            z_top = feature_top
            prof = bd.Plane.YZ * bd.Polygon(
                (il / 2, z_top),
                (il / 2 - lw, z_top),
                (il / 2 - lw, z_top - 1.2),
                (il / 2, z_top - 1.2 - lw),
                align=None,
            )
            tab = bd.extrude(prof, iw / 2, both=True)
            adds.append(tab)
        if adds:
            part = part.fuse(*adds)

    # Tool pockets (solid mode is the intended use, but they work on hollow bins too)
    for p in pockets:
        poly = clean_polygon(p.polygon)
        if poly is None or poly.area < 1.0:
            continue
        pts = [layout_to_cad(cfg, x, y) for x, y in poly.exterior.coords[:-1]]
        floor_z = max(H - p.depth, MIN_POCKET_FLOOR)
        top_z = cfg.total_height + 1.0
        face = bd.Plane.XY.offset(floor_z) * bd.Polygon(*pts, align=None)
        part = part - bd.extrude(face, top_z - floor_z)
        for fh in p.finger_holes:
            cx, cy = layout_to_cad(cfg, fh.x, fh.y)
            bottom = max(floor_z - fh.extra_depth, MIN_CUT_FLOOR)
            length = max(fh.length, fh.diameter)
            # layout angle is measured with y down; CAD y is up, so negate
            plane = bd.Plane.XY.offset(bottom).rotated((0, 0, -fh.angle_deg))
            slot = bd.Pos(cx, cy, 0) * bd.extrude(plane * bd.SlotOverall(length, fh.diameter), top_z - bottom)
            part = part - slot

    # Magnet / screw holes under every cell corner
    if cfg.holes != "none":
        cutters = []
        for i in range(cfg.grid_x):
            for j in range(cfg.grid_y):
                cx = -W / 2 + CELL / 2 + i * PITCH
                cy = -L / 2 + CELL / 2 + j * PITCH
                off = PITCH / 2 - HOLE_INSET  # 13 mm from cell centre
                for sx in (-1, 1):
                    for sy in (-1, 1):
                        x, y = cx + sx * off, cy + sy * off
                        if cfg.holes in ("magnet", "magnet_screw"):
                            cutters.append(
                                bd.Pos(x, y, -0.01)
                                * bd.Cylinder(
                                    MAGNET_D / 2,
                                    MAGNET_H + 0.01,
                                    align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN),
                                )
                            )
                        if cfg.holes in ("screw", "magnet_screw"):
                            cutters.append(
                                bd.Pos(x, y, -0.01)
                                * bd.Cylinder(
                                    SCREW_D / 2,
                                    SCREW_H + 0.01,
                                    align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN),
                                )
                            )
        if cutters:
            part = part.cut(*cutters)  # one boolean instead of up to 800
    return part


def export_bytes(part: bd.Part, fmt: Literal["stl", "3mf", "step"], tolerance: float = 0.05) -> bytes:
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, f"bin.{fmt}")
        if fmt == "stl":
            bd.export_stl(part, path, tolerance=tolerance, angular_tolerance=0.2)
        elif fmt == "step":
            bd.export_step(part, path)
        elif fmt == "3mf":
            m = bd.Mesher(unit=bd.Unit.MM)
            m.add_shape(part, linear_deflection=tolerance, angular_deflection=0.2)
            m.write(path)
        else:
            raise ValueError(fmt)
        with open(path, "rb") as f:
            return f.read()


def generate_model(bin_cfg: dict, pockets: list[dict], fmt: str, tolerance: float = 0.05) -> bytes:
    """Plain-data entry point for the CAD process pool: dicts in (BinIn / PocketIn .model_dump()),
    file bytes out. Nothing here touches the database or the event loop."""
    cfg = BinConfig(**bin_cfg)
    pk = [
        Pocket(
            polygon=[list(pt) for pt in p["polygon"]],
            depth=float(p.get("depth", 15.0)),
            finger_holes=[FingerHole(**f) for f in p.get("finger_holes", [])],
        )
        for p in pockets
    ]
    return export_bytes(build_bin(cfg, pk), fmt, tolerance)  # type: ignore[arg-type]


def warm() -> None:
    """Imported build123d / OpenCascade in this process; nothing else to do."""
