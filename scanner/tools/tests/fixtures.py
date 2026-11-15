"""Synthetic Room Scanner packages in the same JSON shape RoomPlan's Codable CapturedRoom writes
(single-key category objects, 16-float column-major transforms, floor polygons in floor-local XY)."""
from __future__ import annotations

import json
import math
import os
import random
import uuid
from typing import Dict, List, Optional, Sequence, Tuple


def _uid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128))).upper()


def _cols(x, y, z, c) -> List[float]:
    return [x[0], x[1], x[2], 0, y[0], y[1], y[2], 0, z[0], z[1], z[2], 0, c[0], c[1], c[2], 1]


class World:
    """Builds rooms in plan coordinates, then rotates/translates them into a fake session frame."""

    def __init__(self, angle_deg: float = 23.0, offset=(1.7, -1.3), floor_y: float = -1.2, seed: int = 7,
                 noise: float = 0.015):
        self.angle = math.radians(angle_deg)
        self.offset = offset
        self.floor_y = floor_y
        self.rng = random.Random(seed)
        self.noise = noise

    def pt(self, p: Sequence[float]) -> Tuple[float, float]:
        c, s = math.cos(self.angle), math.sin(self.angle)
        jx = self.rng.uniform(-self.noise, self.noise)
        jz = self.rng.uniform(-self.noise, self.noise)
        x, z = p[0] + jx, p[1] + jz
        return (x * c - z * s + self.offset[0], x * s + z * c + self.offset[1])

    def dir(self, angle: float) -> Tuple[float, float]:
        a = angle + self.angle
        return (math.cos(a), math.sin(a))

    def wall(self, a, b, height=2.5) -> Dict:
        pa, pb = self.pt(a), self.pt(b)
        L = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        ux, uz = (pb[0] - pa[0]) / L, (pb[1] - pa[1]) / L
        c = ((pa[0] + pb[0]) / 2, self.floor_y + height / 2, (pa[1] + pb[1]) / 2)
        return {"identifier": _uid(self.rng), "category": {"wall": {}}, "confidence": {"high": {}},
                "dimensions": [L, height, 0], "transform": _cols((ux, 0, uz), (0, 1, 0), (-uz, 0, ux), c),
                "polygonCorners": [], "completedEdges": [], "curve": None, "parentIdentifier": None, "story": 0}

    def floor(self, poly) -> Dict:
        pts = [self.pt(p) for p in poly]
        cx = sum(p[0] for p in pts) / len(pts)
        cz = sum(p[1] for p in pts) / len(pts)
        ux, uz = math.cos(self.angle), math.sin(self.angle)
        yx, yz = uz, -ux
        corners = [[(p[0] - cx) * ux + (p[1] - cz) * uz, (p[0] - cx) * yx + (p[1] - cz) * yz, 0] for p in pts]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return {"identifier": _uid(self.rng), "category": {"floor": {}}, "confidence": {"high": {}},
                "dimensions": [max(xs) - min(xs), max(ys) - min(ys), 0],
                "transform": _cols((ux, 0, uz), (yx, 0, yz), (0, 1, 0), (cx, self.floor_y, cz)),
                "polygonCorners": corners, "completedEdges": [], "curve": None, "parentIdentifier": None, "story": 0}

    def opening(self, kind: str, wall: Dict, a, b, t: float, width: float, height: float, bottom: float = 0.0) -> Dict:
        pa, pb = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), None
        p = self.pt(pa)
        m = wall["transform"]
        cat = {"door": {"isOpen": True}} if kind == "door" else {kind: {}}
        return {"identifier": _uid(self.rng), "category": cat, "confidence": {"high": {}},
                "dimensions": [width, height, 0],
                "transform": m[:12] + [p[0], self.floor_y + bottom + height / 2, p[1], 1],
                "polygonCorners": [], "completedEdges": [], "curve": None, "parentIdentifier": wall["identifier"], "story": 0}

    def obj(self, category: str, center, size, yaw_deg: float = 0.0, bottom: float = 0.0) -> Dict:
        p = self.pt(center)
        ux, uz = self.dir(math.radians(yaw_deg))
        return {"identifier": _uid(self.rng), "category": {category: {}}, "confidence": {"high": {}},
                "dimensions": list(size),
                "transform": _cols((ux, 0, uz), (0, 1, 0), (-uz, 0, ux), (p[0], self.floor_y + bottom + size[1] / 2, p[1])),
                "parentIdentifier": None, "story": 0, "attributes": {}}

    def world3(self, p, y: float = 0.0) -> List[float]:
        q = self.pt(p)
        return [q[0], self.floor_y + y, q[1]]


def room_json(world: World, poly, doors=(), objects=(), sections=(), windows=(), extra_walls=()) -> Dict:
    walls = []
    edges = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        walls.append(world.wall(a, b))
        edges.append((a, b))
    for a, b in extra_walls:
        walls.append(world.wall(a, b))
    door_list, open_list, win_list = [], [], []
    for (edge, t, width, kind) in doors:
        a, b = edges[edge]
        (door_list if kind == "door" else open_list).append(world.opening(kind, walls[edge], a, b, t, width, 2.05))
    for (edge, t, width) in windows:
        a, b = edges[edge]
        win_list.append(world.opening("window", walls[edge], a, b, t, width, 1.2, bottom=0.9))
    secs = [{"label": lab, "center": world.world3(c), "story": 0} for lab, c in sections]
    return {"version": 2, "story": 0, "walls": walls, "floors": [world.floor(poly)], "doors": door_list,
            "openings": open_list, "windows": win_list, "objects": [world.obj(*o) for o in objects], "sections": secs}


def write_package(folder: str, rooms: List[Tuple[str, str, Dict]], areas=(), colors: Optional[Dict[str, str]] = None,
                  package_id: str = "20261115-090000-test") -> str:
    os.makedirs(folder, exist_ok=True)
    entries = []
    for i, (name, desc, raw) in enumerate(rooms):
        fname = "capturedroom.json" if i == 0 else f"capturedroom_{i + 1}.json"
        with open(os.path.join(folder, fname), "w") as fh:
            json.dump(raw, fh)
        entries.append({"index": i, "name": name, "description": desc, "capturedRoomFile": fname})
    manifest = {"schemaVersion": 2, "app": "Room Scanner", "appVersion": "0.2.0 (2)", "id": package_id,
                "roomName": rooms[0][0], "description": rooms[0][1], "createdAt": "2026-11-15T14:00:00Z",
                "units": "meters", "rooms": entries, "photosFile": "photos.json", "photoCount": 0,
                "colorsFile": "colors.json", "areas": list(areas), "files": []}
    with open(os.path.join(folder, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    with open(os.path.join(folder, "colors.json"), "w") as fh:
        json.dump({"method": "test", "colorSpace": "sRGB", "rooms": [
            {"roomIndex": 0, "entries": [{"id": k, "kind": "x", "hex": v, "sampleCount": 25} for k, v in (colors or {}).items()]}]}, fh)
    with open(os.path.join(folder, "photos.json"), "w") as fh:
        json.dump({"convention": "test", "photos": []}, fh)
    return folder
