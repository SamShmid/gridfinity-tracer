"""Request / response models.

Every numeric field carries the same bounds as the UI so a hand-crafted request can never
push the geometry or the image pipeline into pathological territory (a 1e6 mm Gaussian sigma
would allocate a matrix with tens of millions of columns; a 0 mm STL tolerance would produce
a gigabyte mesh). Out-of-range input is answered with a 422, never a 500.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from .config import CUSTOM_PAPER_MAX_MM

# Every id the server hands out is 12 lowercase hex chars (uuid4().hex[:12]). Anything else is
# rejected before it can reach the filesystem.
ID_PATTERN = r"^[0-9a-f]{12}$"
IdStr = Annotated[str, StringConstraints(pattern=ID_PATTERN)]

Point = tuple[float, float]  # responses
# Request coordinates (photo px or mm) must be finite and sane: NaN/inf would 500 in the JSON
# encoder, and a 1e6 mm polygon makes the Gaussian resampler allocate gigabytes.
COORD_LIMIT = 1e5
Coord = Annotated[float, Field(ge=-COORD_LIMIT, le=COORD_LIMIT, allow_inf_nan=False)]
PointIn = tuple[Coord, Coord]
Poly = Annotated[list[PointIn], Field(min_length=3, max_length=20000)]
Quad = Annotated[list[PointIn], Field(min_length=4, max_length=4)]
CustomMM = Annotated[float | None, Field(ge=20, le=CUSTOM_PAPER_MAX_MM)]
Name = Annotated[
    str, StringConstraints(max_length=2000)
]  # handlers truncate to 120; a long name is not an error


class UploadResponse(BaseModel):
    image_id: str
    width: int
    height: int
    corners: list[Point]
    detected: bool
    orientation: Literal["landscape", "portrait"]
    project_id: str


class RectifyRequest(BaseModel):
    image_id: IdStr
    corners: Quad
    paper: Annotated[str, StringConstraints(max_length=20)] = "letter"  # key in PAPER_SIZES or "custom"
    custom_w_mm: CustomMM = None
    custom_h_mm: CustomMM = None
    orientation: Literal["auto", "landscape", "portrait"] = "auto"


class RectifyResponse(BaseModel):
    rect_id: str
    width: int
    height: int
    px_per_mm: float
    margin_mm: float
    paper_w_mm: float
    paper_h_mm: float
    paper_px: list[float]


class ToolOutline(BaseModel):
    polygon: list[Point]  # rectified mm, origin = image top-left, y down
    area_mm2: float
    bbox: list[float]
    centroid: Point
    source: str


class AutoDetectRequest(BaseModel):
    rect_id: IdStr
    method: Literal["auto", "classical"] = "auto"
    min_area_mm2: float = Field(150.0, ge=1, le=1e5)
    tolerance_mm: float = Field(0.3, ge=0.05, le=3)
    refine: bool = True  # zoom-in SAM pass per tool


class DetectResponse(BaseModel):
    tools: list[ToolOutline]


class SamPoint(BaseModel):
    x: float = Field(ge=-1e5, le=1e5)  # rectified px
    y: float = Field(ge=-1e5, le=1e5)
    label: Literal[0, 1] = 1


class SamRequest(BaseModel):
    rect_id: IdStr
    points: list[SamPoint] = Field(min_length=1, max_length=64)
    tolerance_mm: float = Field(0.3, ge=0.05, le=3)
    refine: bool = True


class RefineRequest(BaseModel):
    rect_id: IdStr
    polygon: Poly  # rectified mm
    tolerance_mm: float = Field(0.3, ge=0.05, le=3)


class WarmRequest(BaseModel):
    rect_id: IdStr


class OffsetRequest(BaseModel):
    polygon: Poly
    clearance_mm: float = Field(0.5, ge=-2, le=10)  # fit clearance (how loose the tool sits)
    printer_offset_mm: float = Field(0.2, ge=0, le=1)  # compensation for FDM over-extrusion / elephant foot
    gaussian_mm: float = Field(1.0, ge=0, le=5)  # contour smoothing sigma
    tolerance_mm: float = Field(0.3, ge=0.05, le=3)  # final simplification
    smooth_mm: float = Field(0.0, ge=0, le=10)  # morphological open/close radius
    bridge_mm: float = Field(1.5, ge=0, le=10)  # fill gaps narrower than 2x this (fragile slivers)
    snap: bool = False  # straighten near-axis edges
    symmetric: bool = False  # mirror about the long axis
    convex: bool = False
    tool_height_mm: float = Field(0.0, ge=0, le=500)
    camera_distance_mm: float = Field(0.0, ge=0, le=10000)


class OffsetResponse(BaseModel):
    polygon: list[Point]
    area_mm2: float
    bbox: list[float]
    straighten_deg: float = 0.0  # rotate by this (screen coords, degrees) to make the long axis horizontal


class FingerHoleIn(BaseModel):
    """A finger cut. Round recess when length <= diameter; otherwise a slot (stadium) of that overall
    length rotated by angle_deg (layout coords). It goes extra_depth below the pocket floor but never
    through the base."""

    x: float = Field(ge=-1000, le=1000)
    y: float = Field(ge=-1000, le=1000)
    diameter: float = Field(20.0, ge=4, le=60)  # slot width / recess diameter
    length: float = Field(20.0, ge=4, le=200)  # slot overall length
    angle_deg: float = Field(0.0, ge=-360, le=360)
    extra_depth: float = Field(3.0, ge=0, le=40)


class PocketIn(BaseModel):
    polygon: Poly  # layout mm (bin top-left origin, y down), already offset
    depth: float = Field(15.0, ge=0.5, le=140)
    finger_holes: list[FingerHoleIn] = Field(default_factory=list, max_length=20)


class BinIn(BaseModel):
    grid_x: int = Field(2, ge=1, le=10)
    grid_y: int = Field(2, ge=1, le=10)
    height_units: int = Field(3, ge=1, le=20)
    lip: Literal["regular", "none"] = "regular"
    holes: Literal["none", "magnet", "screw", "magnet_screw"] = "none"
    solid: bool = True
    dividers_x: int = Field(0, ge=0, le=10)
    dividers_y: int = Field(0, ge=0, le=10)
    scoop: bool = False
    scoop_radius: float = Field(10.0, ge=1, le=40)
    label_tab: bool = False
    label_width: float = Field(13.0, ge=1, le=40)


class GenerateRequest(BaseModel):
    bin: BinIn
    pockets: list[PocketIn] = Field(default_factory=list, max_length=100)
    format: Literal["stl", "3mf", "step"] = "stl"
    tolerance: float = Field(0.05, ge=0.01, le=1)
    filename: Annotated[str, StringConstraints(max_length=2000)] = (
        "gridfinity-holder"  # sanitised + cut to 80 chars
    )
    project_id: IdStr | None = None
    save: bool = False  # store the export (and state) in the library
    state: dict | None = None


class NewProjectRequest(BaseModel):
    name: Name | None = None


class RenameRequest(BaseModel):
    name: Name | None = None
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None
