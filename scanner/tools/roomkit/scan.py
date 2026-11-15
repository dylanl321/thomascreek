"""Read a Room Scanner package (folder or .zip) into plain plan-space records.

The package layout is described in scanner/README.md. Everything here is in the scan session's
world space (metres, +Y up), projected to the floor plane as (x, z).
"""
from __future__ import annotations

import io
import json
import math
import os
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .geometry import Pt, mat_apply, mat_from_list

# RoomPlan section labels (iOS 17+) to readable names.
SECTION_NAMES = {
    "livingRoom": "Living Room", "bedroom": "Bedroom", "bathroom": "Bathroom", "kitchen": "Kitchen",
    "diningRoom": "Dining", "unidentified": None,
}


@dataclass
class Element:
    id: str
    kind: str                      # wall, floor, door, window, opening, object
    category: str                  # wall, floor, door, ... or an object category (sofa, table, ...)
    center: Tuple[float, float, float]
    xaxis: Tuple[float, float, float]
    yaxis: Tuple[float, float, float]
    zaxis: Tuple[float, float, float]
    dims: Tuple[float, float, float]
    parent: Optional[str] = None
    is_open: Optional[bool] = None
    polygon: List[Tuple[float, float, float]] = field(default_factory=list)
    confidence: Optional[str] = None
    attributes: Dict = field(default_factory=dict)

    @property
    def xz(self) -> Pt:
        return (self.center[0], self.center[2])

    @property
    def yaw2d(self) -> float:
        """Plan angle of the element's local +X axis (plain 2D maths in the x, z plane)."""
        return math.atan2(self.xaxis[2], self.xaxis[0])

    def segment(self) -> Tuple[Pt, Pt]:
        """Wall-like element as a floor-plane segment along its local X axis."""
        hx = self.dims[0] / 2
        a = (self.center[0] - self.xaxis[0] * hx, self.center[2] - self.xaxis[2] * hx)
        b = (self.center[0] + self.xaxis[0] * hx, self.center[2] + self.xaxis[2] * hx)
        return a, b

    @property
    def bottom(self) -> float:
        return self.center[1] - self.dims[1] / 2

    @property
    def top(self) -> float:
        return self.center[1] + self.dims[1] / 2


@dataclass
class Section:
    label: str
    center: Tuple[float, float, float]


@dataclass
class ScannedRoom:
    index: int
    name: str
    description: str
    walls: List[Element]
    floors: List[Element]
    doors: List[Element]
    windows: List[Element]
    openings: List[Element]
    objects: List[Element]
    sections: List[Section]

    def floor_polygon(self) -> List[Pt]:
        """Biggest floor outline in plan space, or the walls' box when RoomPlan gave no floor."""
        best: List[Pt] = []
        best_area = 0.0
        for f in self.floors:
            m = _matrix(f)
            corners = f.polygon or [(-f.dims[0] / 2, -f.dims[1] / 2, 0), (f.dims[0] / 2, -f.dims[1] / 2, 0),
                                    (f.dims[0] / 2, f.dims[1] / 2, 0), (-f.dims[0] / 2, f.dims[1] / 2, 0)]
            pts = [mat_apply(m, c) for c in corners]
            poly = [(p[0], p[2]) for p in pts]
            from .geometry import area
            a = area(poly)
            if a > best_area:
                best, best_area = poly, a
        return best

    @property
    def floor_y(self) -> float:
        if self.floors:
            return min(f.center[1] for f in self.floors)
        if self.walls:
            return min(w.bottom for w in self.walls)
        return 0.0


@dataclass
class AreaMarker:
    name: str
    position: Tuple[float, float, float]
    room_index: int
    exclude: bool = False


@dataclass
class Photo:
    file: str
    room_index: int
    transform: List[float]
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @property
    def position(self) -> Tuple[float, float, float]:
        return (self.transform[12], self.transform[13], self.transform[14])

    @property
    def look(self) -> Tuple[float, float, float]:
        # ARKit cameras look down their local -Z axis (column 2).
        return (-self.transform[8], -self.transform[9], -self.transform[10])


@dataclass
class ScanPackage:
    path: str
    manifest: Dict
    rooms: List[ScannedRoom]
    colors: Dict[str, str]
    photos: List[Photo]
    areas: List[AreaMarker]

    @property
    def id(self) -> str:
        return str(self.manifest.get("id") or os.path.splitext(os.path.basename(self.path.rstrip("/")))[0])

    def read_bytes(self, name: str) -> Optional[bytes]:
        return _Reader(self.path).read(name)


def _matrix(e: Element):
    m = [[e.xaxis[0], e.yaxis[0], e.zaxis[0], e.center[0]],
         [e.xaxis[1], e.yaxis[1], e.zaxis[1], e.center[1]],
         [e.xaxis[2], e.yaxis[2], e.zaxis[2], e.center[2]],
         [0.0, 0.0, 0.0, 1.0]]
    return m


class _Reader:
    """Reads files by package-relative name from a folder or a zip (with or without a top folder)."""

    def __init__(self, path: str):
        self.path = path
        self._zip: Optional[zipfile.ZipFile] = None
        self._prefix = ""
        if os.path.isfile(path) and zipfile.is_zipfile(path):
            self._zip = zipfile.ZipFile(path)
            names = self._zip.namelist()
            manifests = sorted((n for n in names if n.endswith("manifest.json")), key=len)
            if manifests:
                self._prefix = manifests[0][: -len("manifest.json")]

    def read(self, name: str) -> Optional[bytes]:
        if ".." in name.split("/") or name.startswith("/"):
            return None
        if self._zip is not None:
            try:
                return self._zip.read(self._prefix + name)
            except KeyError:
                return None
        full = os.path.join(self.path, name)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def json(self, name: str):
        data = self.read(name)
        return json.load(io.BytesIO(data)) if data is not None else None


def _category(raw) -> Tuple[str, Optional[bool]]:
    if isinstance(raw, dict) and raw:
        key = next(iter(raw))
        inner = raw[key]
        is_open = inner.get("isOpen") if isinstance(inner, dict) else None
        return str(key), is_open
    if isinstance(raw, str):
        return raw, None
    return "unknown", None


def _confidence(raw) -> Optional[str]:
    if isinstance(raw, dict) and raw:
        return str(next(iter(raw)))
    return raw if isinstance(raw, str) else None


def parse_element(raw: Dict, kind: str) -> Element:
    t = raw.get("transform") or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    m = mat_from_list(t)
    dims = raw.get("dimensions") or [0, 0, 0]
    category, is_open = _category(raw.get("category"))
    poly = [tuple(float(v) for v in c[:3]) for c in (raw.get("polygonCorners") or []) if len(c) >= 2]
    poly = [(c[0], c[1], c[2] if len(c) > 2 else 0.0) for c in poly]
    return Element(
        id=str(raw.get("identifier", "")),
        kind=kind,
        category=category,
        center=(m[0][3], m[1][3], m[2][3]),
        xaxis=(m[0][0], m[1][0], m[2][0]),
        yaxis=(m[0][1], m[1][1], m[2][1]),
        zaxis=(m[0][2], m[1][2], m[2][2]),
        dims=(float(dims[0]), float(dims[1]), float(dims[2])),
        parent=raw.get("parentIdentifier"),
        is_open=is_open,
        polygon=poly,
        confidence=_confidence(raw.get("confidence")),
        attributes=raw.get("attributes") or {},
    )


def parse_captured_room(raw: Dict, index: int, name: str, description: str) -> ScannedRoom:
    def many(key: str, kind: str) -> List[Element]:
        return [parse_element(e, kind) for e in raw.get(key) or [] if isinstance(e, dict)]

    sections = []
    for s in raw.get("sections") or []:
        label, _ = _category(s.get("label")) if isinstance(s.get("label"), dict) else (s.get("label"), None)
        c = s.get("center") or [0, 0, 0]
        sections.append(Section(label=str(label or "unidentified"), center=(float(c[0]), float(c[1]), float(c[2]))))
    return ScannedRoom(
        index=index, name=name, description=description,
        walls=many("walls", "wall"), floors=many("floors", "floor"), doors=many("doors", "door"),
        windows=many("windows", "window"), openings=many("openings", "opening"),
        objects=many("objects", "object"), sections=sections,
    )


def load_package(path: str) -> ScanPackage:
    reader = _Reader(path)
    manifest = reader.json("manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError(f"{path}: no manifest.json; is this a Room Scanner package?")
    rooms: List[ScannedRoom] = []
    for i, entry in enumerate(manifest.get("rooms") or []):
        raw = reader.json(entry.get("capturedRoomFile", ""))
        if not isinstance(raw, dict):
            continue
        rooms.append(parse_captured_room(raw, int(entry.get("index", i)), str(entry.get("name") or f"Room {i + 1}"),
                                         str(entry.get("description") or "")))
    colors: Dict[str, str] = {}
    colors_raw = reader.json(manifest.get("colorsFile") or "colors.json") or {}
    for room in colors_raw.get("rooms", []):
        for e in room.get("entries", []):
            if e.get("hex"):
                colors[str(e.get("id"))] = str(e["hex"])
    photos: List[Photo] = []
    photos_raw = reader.json(manifest.get("photosFile") or "photos.json") or {}
    for p in photos_raw.get("photos", []):
        t = p.get("transform") or []
        if len(t) != 16:
            continue
        photos.append(Photo(file=str(p.get("file")), room_index=int(p.get("roomIndex", 0)), transform=[float(v) for v in t],
                            width=int(p.get("imageWidth", 0)), height=int(p.get("imageHeight", 0)),
                            fx=float(p.get("fx", 0)), fy=float(p.get("fy", 0)), cx=float(p.get("cx", 0)), cy=float(p.get("cy", 0))))
    areas: List[AreaMarker] = []
    for a in manifest.get("areas") or []:
        pos = a.get("position") or [0, 0, 0]
        areas.append(AreaMarker(name=str(a.get("name") or "Area"), position=(float(pos[0]), float(pos[1]), float(pos[2])),
                                room_index=int(a.get("roomIndex", 0)), exclude=bool(a.get("exclude", False))))
    return ScanPackage(path=path, manifest=manifest, rooms=rooms, colors=colors, photos=photos, areas=areas)


def section_name(label: str) -> Optional[str]:
    if label in SECTION_NAMES:
        return SECTION_NAMES[label]
    return label[:1].upper() + label[1:] if label else None


