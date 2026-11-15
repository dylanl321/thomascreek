"""Turn one scanned room into a tidy dollhouse room plan.

The scan is treated as a guide, not a blueprint. Walls are squared up, lengths land on a 5 cm grid,
small jogs go away, floor that leaked through a doorway is trimmed, furniture is squared to the
walls and pushed flush, and big shared spaces can be split into named areas. The result follows
models/dollhouse/living-room/style.md: metres, +Y up, the room's back corner at the origin, the two
kept walls on the planes x=0 and z=0, interior at x<0 and z>0.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import geometry as g
from .geometry import Pt
from .scan import Element, ScanPackage, ScannedRoom, section_name

FORMAT = "tc-room-plan/1"
QUARTER = math.pi / 2
# Plan direction from the room toward the dollhouse camera (camera sits at -x, +z).
CAMERA_DIR: Pt = (-math.sqrt(0.5), math.sqrt(0.5))

OBJECT_LABELS = {
    "sofa": "Sofa", "chair": "Chair", "bed": "Bed", "table": "Table", "storage": "Storage",
    "television": "TV", "fireplace": "Fireplace", "refrigerator": "Fridge", "stove": "Stove",
    "oven": "Oven", "dishwasher": "Dishwasher", "washerDryer": "Washer/Dryer", "sink": "Sink",
    "toilet": "Toilet", "bathtub": "Bathtub", "stairs": "Stairs",
}


@dataclass
class TidyOptions:
    grid: float = 0.05            # every length lands on this grid
    square_tolerance_deg: float = 15.0   # walls this close to square become square
    min_jog: float = 0.25         # wall jogs shorter than this are removed
    tongue_width: float = 1.3     # floor poking through a doorway narrower than this is trimmed
    tongue_depth: float = 2.0
    keep_tongues: bool = False
    flush_gap: float = 0.2        # furniture closer than this to a wall is pushed flush
    yaw_snap_deg: float = 12.0
    back_corner: Optional[int] = None   # 0..3 quarter turns; None picks the best corner
    split_areas: bool = False


# ---------------------------------------------------------------- outline clean-up

def _edge_class(a: Pt, b: Pt, tol: float) -> str:
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    if ang < tol or ang > 180.0 - tol:
        return "H"      # runs along x, constant z
    if abs(ang - 90.0) < tol:
        return "V"      # runs along z, constant x
    return "D"


def orthogonalize(poly: Sequence[Pt], opts: TidyOptions, snap: bool = True) -> List[Pt]:
    """Square up a floor outline: near-square edges become exactly square, tiny jogs go away."""
    pts = g.drop_collinear(g.dedupe(list(poly), 0.01), 1e-3)
    if len(pts) < 3:
        return pts
    tol = opts.square_tolerance_deg
    for _ in range(50):
        changed = False
        n = len(pts)
        cls = [_edge_class(pts[i], pts[(i + 1) % n], tol) for i in range(n)]
        # merge runs of the same direction
        for i in range(n):
            if n > 3 and cls[i - 1] == cls[i] and cls[i] in ("H", "V"):
                pts.pop(i)
                changed = True
                break
        if changed:
            continue
        # collapse short jogs
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            if n > 4 and g.dist(a, b) < opts.min_jog:
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                j = (i + 1) % n
                pts[i] = mid
                pts.pop(j)
                changed = True
                break
        if not changed:
            break
    n = len(pts)
    cls = [_edge_class(pts[i], pts[(i + 1) % n], tol) for i in range(n)]
    coord: List[Optional[float]] = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if cls[i] == "H":
            c = (a[1] + b[1]) / 2
        elif cls[i] == "V":
            c = (a[0] + b[0]) / 2
        else:
            c = None
        coord.append(g.snap(c, opts.grid) if (c is not None and snap) else c)
    out: List[Pt] = []
    for j in range(n):
        prev_c, prev_k = coord[j - 1], cls[j - 1]
        next_c, next_k = coord[j], cls[j]
        x, z = pts[j]
        if snap:
            x, z = g.snap(x, opts.grid), g.snap(z, opts.grid)
        if prev_k == "V":
            x = prev_c
        if next_k == "V":
            x = next_c
        if prev_k == "H":
            z = prev_c
        if next_k == "H":
            z = next_c
        out.append((x, z))
    out = g.drop_collinear(g.dedupe(out, 0.005), 1e-6)
    return g.ensure_ccw(out)


def _cross(o: Pt, a: Pt, b: Pt) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def trim_tongues(poly: List[Pt], blockers: Sequence[Pt], doors: Sequence[Tuple[Pt, float]],
                 opts: TidyOptions) -> Tuple[List[Pt], List[str]]:
    """Trim floor that leaked through a doorway: a narrow rectangular lip with a door or opening at
    its base and no furniture in it. Returns the new outline and a note per trim."""
    notes: List[str] = []
    if opts.keep_tongues:
        return poly, notes
    pts = g.ensure_ccw(poly)
    for _ in range(20):
        n = len(pts)
        if n < 6:
            break
        done = False
        for i in range(n):
            p0, p1, p2, p3 = pts[i - 1], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
            # p1->p2 is the tip; p0->p1 and p2->p3 are its sides. Both corners must be convex (left turns).
            if _cross(p0, p1, p2) <= 0 or _cross(p1, p2, p3) <= 0:
                continue
            width = g.dist(p1, p2)
            d1, d2 = g.dist(p0, p1), g.dist(p2, p3)
            depth = min(d1, d2)
            if width >= opts.tongue_width or depth >= opts.tongue_depth or depth < 0.05:
                continue
            # the sides must be parallel and perpendicular to the tip
            tip = ((p2[0] - p1[0]) / width, (p2[1] - p1[1]) / width)
            s1 = ((p1[0] - p0[0]) / d1, (p1[1] - p0[1]) / d1)
            if abs(tip[0] * s1[0] + tip[1] * s1[1]) > 0.2:
                continue
            inward = (-s1[0], -s1[1])
            base1 = (p1[0] + inward[0] * depth, p1[1] + inward[1] * depth)
            base2 = (p2[0] + inward[0] * depth, p2[1] + inward[1] * depth)
            region = [p1, p2, base2, base1]
            if any(g.point_in_polygon(b, region) for b in blockers):
                continue
            at_door = width < 0.5 or any(
                g.point_segment_distance(c, base1, base2)[0] < 0.45 and abs(w - width) < 0.6 for c, w in doors)
            if not at_door:
                continue
            pts[i] = base1
            pts[(i + 1) % n] = base2
            pts = g.drop_collinear(g.dedupe(pts, 0.005), 1e-6)
            notes.append(f"trimmed floor through a doorway ({width:.2f} m wide, {depth:.2f} m deep)")
            done = True
            break
        if not done:
            break
    return pts, notes


# ---------------------------------------------------------------- areas (shared spaces)

@dataclass
class Seed:
    name: str
    at: Pt
    exclude: bool
    source: str


def _kd_leaves(rect, seeds: List[Seed], blockers: List[Tuple[float, float, float, float]],
               partitions: List[Tuple[Pt, Pt]], grid: float):
    """Recursively split a box between seeds. Split lines prefer real partition walls, then avoid
    cutting through furniture, and land on the grid. Yields (rect, seed, split_lines)."""
    if len(seeds) <= 1:
        return [(rect, seeds[0] if seeds else None)]
    x0, z0, x1, z1 = rect
    xs = [s.at[0] for s in seeds]
    zs = [s.at[1] for s in seeds]
    axis = 0 if (max(xs) - min(xs)) >= (max(zs) - min(zs)) else 1
    ordered = sorted(seeds, key=lambda s: s.at[axis])
    m = len(ordered) // 2
    lo, hi = ordered[m - 1].at[axis], ordered[m].at[axis]
    mid = (lo + hi) / 2
    lo_lim, hi_lim = lo + 0.3, hi - 0.3
    candidates = []
    # real partition walls running across this axis
    for a, b in partitions:
        if axis == 0 and abs(a[0] - b[0]) < 0.05 and lo_lim < a[0] < hi_lim:
            candidates.append((0, abs(a[0] - mid), a[0]))
        if axis == 1 and abs(a[1] - b[1]) < 0.05 and lo_lim < a[1] < hi_lim:
            candidates.append((0, abs(a[1] - mid), a[1]))
    v = mid - 1.0
    while v <= mid + 1.0 + 1e-9:
        if lo_lim < v < hi_lim:
            cuts = 0
            for bx0, bz0, bx1, bz1 in blockers:
                if (axis == 0 and bx0 < v < bx1) or (axis == 1 and bz0 < v < bz1):
                    cuts += 1
            candidates.append((1 + cuts, abs(v - mid), v))
        v += grid
    split = g.snap(min(candidates)[2] if candidates else mid, grid)
    if axis == 0:
        left, right = (x0, z0, split, z1), (split, z0, x1, z1)
    else:
        left, right = (x0, z0, x1, split), (x0, split, x1, z1)
    a_seeds = [s for s in ordered if s.at[axis] < split]
    b_seeds = [s for s in ordered if s.at[axis] >= split]
    if not a_seeds or not b_seeds:
        a_seeds, b_seeds = ordered[:m], ordered[m:]
    return _kd_leaves(left, a_seeds, blockers, partitions, grid) + _kd_leaves(right, b_seeds, blockers, partitions, grid)


# ---------------------------------------------------------------- main entry

@dataclass
class Frame:
    """Session world -> room frame: p_room = rot2(p, angle) - origin, y_room = y - floor_y."""
    angle: float
    origin: Pt
    floor_y: float

    def pt(self, p: Pt) -> Pt:
        r = g.rot2(p, self.angle)
        return (r[0] - self.origin[0], r[1] - self.origin[1])

    def p3(self, p) -> Tuple[float, float, float]:
        x, z = self.pt((p[0], p[2]))
        return (x, p[1] - self.floor_y, z)

    def dir2(self, angle2d: float) -> float:
        return g.wrap_angle(angle2d + self.angle)

    def to_session(self) -> List[float]:
        # room = Ry(-angle) * world + t  (rot2(a) == Ry(-a)); invert it.
        to_room = g.mat_yaw_translate(-self.angle, -self.origin[0], -self.floor_y, -self.origin[1])
        return g.mat_to_list(g.mat_inverse_rigid(to_room))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "room"


def _choose_corner(poly: List[Pt], objects: List[Pt]) -> Tuple[int, List[float]]:
    """Pick which bbox corner becomes the back corner: the one whose two sides hold the most wall
    and furniture, so the kept walls are the interesting ones."""
    scores = []
    for k in range(4):
        rp = [g.rot2(p, k * QUARTER) for p in poly]
        ro = [g.rot2(p, k * QUARTER) for p in objects]
        x0, z0, x1, z1 = g.bbox(rp)
        score = 0.0
        for i in range(len(rp)):
            a, b = rp[i], rp[(i + 1) % len(rp)]
            if abs(a[0] - x1) < 0.15 and abs(b[0] - x1) < 0.15:
                score += g.dist(a, b)
            if abs(a[1] - z0) < 0.15 and abs(b[1] - z0) < 0.15:
                score += g.dist(a, b)
        for o in ro:
            if x1 - o[0] < 0.8 or o[1] - z0 < 0.8:
                score += 0.5
        scores.append(score)
    best = max(range(4), key=lambda k: (round(scores[k], 3), -k))
    return best, scores


def _wall_match(edge: Tuple[Pt, Pt], walls: List[Tuple[Element, Pt, Pt]]) -> Optional[Element]:
    a, b = edge
    length = g.dist(a, b)
    if length < 1e-6:
        return None
    ux, uz = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    best, best_score = None, 1e9
    for w, wa, wb in walls:
        wl = g.dist(wa, wb)
        if wl < 1e-6:
            continue
        vx, vz = (wb[0] - wa[0]) / wl, (wb[1] - wa[1]) / wl
        if abs(ux * vx + uz * vz) < math.cos(math.radians(25)):
            continue
        mid = ((wa[0] + wb[0]) / 2, (wa[1] + wb[1]) / 2)
        off = abs((mid[0] - a[0]) * uz - (mid[1] - a[1]) * ux)
        if off > 0.5:
            continue
        t0 = (wa[0] - a[0]) * ux + (wa[1] - a[1]) * uz
        t1 = (wb[0] - a[0]) * ux + (wb[1] - a[1]) * uz
        overlap = min(max(t0, t1), length) - max(min(t0, t1), 0)
        if overlap <= 0.05:
            continue
        score = off - 0.1 * overlap
        if score < best_score:
            best, best_score = w, score
    return best


def tidy_room(pkg: ScanPackage, room: ScannedRoom, opts: Optional[TidyOptions] = None) -> List[Dict]:
    """Returns one plan, or one plan per area when opts.split_areas is set."""
    opts = opts or TidyOptions()
    notes: List[str] = []
    floor_y = room.floor_y
    raw_outline = room.floor_polygon()
    wall_segs = [w.segment() for w in room.walls]
    if len(raw_outline) < 3:
        pts = [p for s in wall_segs for p in s]
        if not pts:
            raise ValueError(f"room {room.index} ({room.name}) has no floor and no walls")
        x0, z0, x1, z1 = g.bbox(pts)
        raw_outline = [(x0, z0), (x1, z0), (x1, z1), (x0, z1)]
        notes.append("RoomPlan gave no floor outline; used the walls' box")
    phi = g.dominant_angle(wall_segs or [(raw_outline[i], raw_outline[(i + 1) % len(raw_outline)])
                                         for i in range(len(raw_outline))])
    rough = orthogonalize([g.rot2(p, -phi) for p in raw_outline], opts, snap=False)
    obj_xz = [g.rot2(o.xz, -phi) for o in room.objects]
    if opts.back_corner is not None:
        k = opts.back_corner % 4
    else:
        k, _ = _choose_corner(rough, obj_xz)
    angle = -phi + k * QUARTER
    turned = [g.rot2(p, angle) for p in raw_outline]
    bx0, bz0, bx1, bz1 = g.bbox(turned)
    frame = Frame(angle=angle, origin=(bx1, bz0), floor_y=floor_y)

    outline = orthogonalize([frame.pt(p) for p in raw_outline], opts)
    # re-seat the back corner exactly on the origin after snapping
    ox0, oz0, ox1, oz1 = g.bbox(outline)
    outline = [(g.snap(x - ox1, opts.grid), g.snap(z - oz0, opts.grid)) for x, z in outline]
    frame = Frame(angle=angle, origin=(frame.origin[0] + ox1, frame.origin[1] + oz0), floor_y=floor_y)
    squared = sum(1 for i in range(len(raw_outline)))
    notes.append(f"squared the outline ({squared} scanned corners -> {len(outline)})")

    def tw(e: Element) -> Tuple[Pt, Pt]:
        a, b = e.segment()
        return frame.pt(a), frame.pt(b)

    walls_t = [(w, *tw(w)) for w in room.walls]
    door_marks = [(frame.pt(e.xz), e.dims[0]) for e in room.doors + room.openings]
    blockers = [frame.pt(o.xz) for o in room.objects]
    outline, trim_notes = trim_tongues(outline, blockers, door_marks, opts)
    notes += trim_notes

    # ---- areas
    seeds: List[Seed] = []
    for m in pkg.areas:
        if m.room_index == room.index:
            seeds.append(Seed(m.name, frame.pt((m.position[0], m.position[2])), m.exclude, "marker"))
    if not seeds:
        named = [(section_name(s.label), s) for s in room.sections]
        named = [(n, s) for n, s in named if n]
        if len(named) >= 2:
            for n, s in named:
                seeds.append(Seed(n, frame.pt((s.center[0], s.center[2])), False, "roomplan-section"))
    if seeds and all(s.exclude for s in seeds):
        inside = [b for b in blockers if g.point_in_polygon(b, outline)]
        c = g.centroid(outline) if not inside else (sum(p[0] for p in inside) / len(inside), sum(p[1] for p in inside) / len(inside))
        seeds.append(Seed(room.name, c, False, "room"))
    obj_boxes = []
    for o in room.objects:
        c = frame.pt(o.xz)
        r = max(o.dims[0], o.dims[2]) / 2
        obj_boxes.append((c[0] - r, c[1] - r, c[0] + r, c[1] + r))

    # partitions: RoomPlan walls well inside the outline
    partitions: List[Dict] = []
    part_segs: List[Tuple[Pt, Pt]] = []
    for w, a, b in walls_t:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if g.point_in_polygon(mid, outline) and g.distance_to_polygon(mid, outline) > 0.3:
            cls = _edge_class(a, b, opts.square_tolerance_deg)
            if cls == "H":
                zc = g.snap((a[1] + b[1]) / 2, opts.grid)
                a, b = (g.snap(a[0], opts.grid), zc), (g.snap(b[0], opts.grid), zc)
            elif cls == "V":
                xc = g.snap((a[0] + b[0]) / 2, opts.grid)
                a, b = (xc, g.snap(a[1], opts.grid)), (xc, g.snap(b[1], opts.grid))
            if g.dist(a, b) < 0.2:
                continue
            part_segs.append((a, b))
            partitions.append({"id": f"partition_{len(partitions) + 1}", "a": list(a), "b": list(b),
                               "height": g.snap(w.dims[1], opts.grid), "source_id": w.id,
                               "color": pkg.colors.get(w.id)})

    areas: List[Dict] = []
    outside: List[Dict] = []
    ox0, oz0, ox1, oz1 = g.bbox(outline)
    if len(seeds) >= 2:
        leaves = _kd_leaves((ox0, oz0, ox1, oz1), seeds, obj_boxes, part_segs, opts.grid)
        for rect, seed in leaves:
            poly = g.clip_rect(outline, rect)
            if len(poly) < 3 or seed is None:
                continue
            areas.append({"name": seed.name, "source": seed.source, "exclude": seed.exclude,
                          "rect": [round(v, 3) for v in rect], "outline": [list(p) for p in g.ensure_ccw(poly)],
                          "area_m2": round(g.area(poly), 2)})
        # exclusions: cut each excluded area off along one of its inner sides
        for ar in [a for a in areas if a["exclude"]]:
            x0, z0, x1, z1 = ar["rect"]
            options = []
            if x0 > ox0 + 0.01:
                options.append(g.clip_halfplane(outline, (1.0, 0.0), x0))
            if x1 < ox1 - 0.01:
                options.append(g.clip_halfplane(outline, (-1.0, 0.0), -x1))
            if z0 > oz0 + 0.01:
                options.append(g.clip_halfplane(outline, (0.0, 1.0), z0))
            if z1 < oz1 - 0.01:
                options.append(g.clip_halfplane(outline, (0.0, -1.0), -z1))
            options = [o for o in options if len(o) >= 3]
            if options:
                outline = g.ensure_ccw(g.drop_collinear(g.dedupe(max(options, key=g.area), 0.005), 1e-6))
                notes.append(f"cut off \"{ar['name']}\" (marked as not part of this room)")
        areas = [a for a in areas if not a["exclude"]]
        # re-clip kept areas to the final outline
        for ar in areas:
            ar["outline"] = [list(p) for p in g.ensure_ccw(g.clip_rect(outline, tuple(ar["rect"])))] or ar["outline"]
    elif seeds:
        areas.append({"name": seeds[0].name, "source": seeds[0].source, "exclude": False,
                      "rect": [ox0, oz0, ox1, oz1], "outline": [list(p) for p in outline], "area_m2": round(g.area(outline), 2)})

    # ---- walls from the outline
    height = g.snap(sorted(w.dims[1] for w in room.walls)[len(room.walls) // 2], opts.grid) if room.walls else 2.45
    walls: List[Dict] = []
    poly = g.ensure_ccw(outline)
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        match = _wall_match((a, b), walls_t)
        n = g.outward_normal(a, b)
        facing = n[0] * CAMERA_DIR[0] + n[1] * CAMERA_DIR[1]
        walls.append({
            "id": f"wall_{i + 1}", "a": [a[0], a[1]], "b": [b[0], b[1]], "length": round(g.dist(a, b), 3),
            "height": g.snap(match.dims[1], opts.grid) if match else height,
            "side": "front" if facing > 0.35 else "back", "normal": [round(n[0], 4), round(n[1], 4)],
            "source_id": match.id if match else None, "color": pkg.colors.get(match.id) if match else None,
            "open": False,
        })

    # ---- doors, windows, openings
    openings: List[Dict] = []
    for e in room.doors + room.windows + room.openings:
        c = frame.pt(e.xz)
        along = frame.dir2(e.yaw2d)
        best = None
        for wall in walls + [dict(p, side="partition") for p in partitions]:
            a, b = tuple(wall["a"]), tuple(wall["b"])
            wl = g.dist(a, b) or 1.0
            parallel = abs(math.cos(along) * (b[0] - a[0]) / wl + math.sin(along) * (b[1] - a[1]) / wl)
            if parallel < math.cos(math.radians(25)):
                continue
            d, t = g.point_segment_distance(c, a, b)
            if d < 0.45 and (best is None or d < best[0]):
                best = (d, t, wall)
        if best is None:
            outside.append({"kind": e.kind, "id": e.id, "reason": "not on any wall of this room", "at": list(c)})
            continue
        _, t, wall = best
        length = g.dist(tuple(wall["a"]), tuple(wall["b"]))
        width = max(opts.grid * 4, g.snap(e.dims[0], opts.grid))
        width = min(width, max(length - 0.1, opts.grid))
        offset = g.snap(t * length - width / 2, opts.grid)
        offset = max(0.05 if length > width + 0.1 else 0.0, min(offset, length - width - (0.05 if length > width + 0.1 else 0.0)))
        bottom = e.bottom - floor_y
        bottom = 0.0 if (e.kind != "window" and bottom < 0.12) else max(0.0, g.snap(bottom, opts.grid))
        openings.append({
            "id": f"{e.kind}_{len([o for o in openings if o['kind'] == e.kind]) + 1}", "kind": e.kind,
            "wall": wall["id"], "offset": round(offset, 3), "width": round(width, 3),
            "bottom": round(bottom, 3), "height": g.snap(e.dims[1], opts.grid),
            "is_open": e.is_open, "source_id": e.id, "color": pkg.colors.get(e.id),
        })

    # ---- furniture and fixtures
    objects: List[Dict] = []
    for o in room.objects:
        x, y, z = frame.p3(o.center)
        yaw = frame.dir2(o.yaw2d)
        q = round(yaw / QUARTER)
        squared_yaw = abs(yaw - q * QUARTER) < math.radians(opts.yaw_snap_deg)
        if squared_yaw:
            yaw = (q % 4) * QUARTER
        w, h, d = (max(opts.grid * 2, g.snap(v, opts.grid)) for v in o.dims)
        bottom = y - o.dims[1] / 2
        bottom = 0.0 if bottom < 0.12 else g.snap(bottom, opts.grid)
        x, z = g.snap(x, opts.grid), g.snap(z, opts.grid)
        inside = g.point_in_polygon((x, z), outline) or g.distance_to_polygon((x, z), outline) < 0.15
        rec = {"id": f"{o.category}_{len([q for q in objects if q['category'] == o.category]) + 1}",
               "category": o.category, "label": OBJECT_LABELS.get(o.category, o.category.title()),
               "position": [x, bottom, z], "size": [w, h, d], "yaw2d": round(yaw, 4),
               "rotation_y": round(-yaw, 4), "squared": squared_yaw, "source_id": o.id,
               "color": pkg.colors.get(o.id), "attributes": o.attributes or None}
        if not inside:
            outside.append(dict(rec, reason="outside this room (seen through a doorway or opening)"))
            continue
        if squared_yaw:
            _push_flush(rec, poly, opts)
        if areas:
            for ar in areas:
                if g.point_in_polygon((rec["position"][0], rec["position"][2]), [tuple(p) for p in ar["outline"]]):
                    rec["area"] = ar["name"]
                    break
        objects.append(rec)

    floor_color = None
    if room.floors:
        floor_color = pkg.colors.get(max(room.floors, key=lambda f: f.dims[0] * f.dims[1]).id)

    photos = _photos_for(pkg, room, frame, walls)
    base = {
        "format": FORMAT, "name": room.name, "slug": slugify(room.name), "description": room.description,
        "source": {"package": pkg.id, "room_index": room.index, "scanned_at": pkg.manifest.get("createdAt"),
                   "app_version": pkg.manifest.get("appVersion")},
        "units": "meters", "up": "+Y",
        "frame": "style.md: back corner at the origin, kept walls on x=0 and z=0, interior x<0 and z>0",
        "to_session": [round(v, 6) for v in frame.to_session()],
        "height": height, "outline": [list(p) for p in poly], "floor_color": floor_color,
        "walls": walls, "partitions": partitions, "openings": openings, "objects": objects,
        "areas": areas, "outside": outside, "photos": photos, "notes": notes,
        "settings": {"grid": opts.grid, "square_tolerance_deg": opts.square_tolerance_deg, "min_jog": opts.min_jog},
    }
    if not opts.split_areas or len(areas) < 2:
        return [base]
    return [_split_plan(base, ar, opts) for ar in areas]


def _push_flush(rec: Dict, poly: List[Pt], opts: TidyOptions) -> None:
    """Push furniture that almost touches a wall flush against it (and back inside if it pokes out)."""
    x, _, z = rec["position"]
    w, _, d = rec["size"]
    q = round(rec["yaw2d"] / QUARTER) % 2
    hx, hz = (w / 2, d / 2) if q == 0 else (d / 2, w / 2)
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        n = g.outward_normal(a, b)
        if abs(a[0] - b[0]) < 1e-6:          # wall at constant x
            lo, hi = sorted((a[1], b[1]))
            if z + hz <= lo or z - hz >= hi:
                continue
            face = x + hx if n[0] > 0 else x - hx
            gap = (a[0] - face) * (1 if n[0] > 0 else -1)
            if -0.3 < gap < opts.flush_gap:
                x += (gap - 0.01) * (1 if n[0] > 0 else -1)
        elif abs(a[1] - b[1]) < 1e-6:        # wall at constant z
            lo, hi = sorted((a[0], b[0]))
            if x + hx <= lo or x - hx >= hi:
                continue
            face = z + hz if n[1] > 0 else z - hz
            gap = (a[1] - face) * (1 if n[1] > 0 else -1)
            if -0.3 < gap < opts.flush_gap:
                z += (gap - 0.01) * (1 if n[1] > 0 else -1)
    rec["position"][0] = round(x, 3)
    rec["position"][2] = round(z, 3)


def _photos_for(pkg: ScanPackage, room: ScannedRoom, frame: Frame, walls: List[Dict]) -> List[Dict]:
    out = []
    for i, p in enumerate(pkg.photos):
        if p.room_index != room.index:
            continue
        pos = frame.p3(p.position)
        lx, ly, lz = p.look
        look2 = g.rot2((lx, lz), frame.angle)
        out.append({"file": p.file, "position": [round(v, 3) for v in pos],
                    "look": [round(look2[0], 4), round(ly, 4), round(look2[1], 4)]})
    # best photos per wall: inside the room, looking at the wall's face, wall centre in view
    for wall in walls:
        a, b = wall["a"], wall["b"]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        n = wall["normal"]
        scored = []
        for idx, ph in enumerate(out):
            px, pz = ph["position"][0], ph["position"][2]
            lx, lz = ph["look"][0], ph["look"][2]
            ll = math.hypot(lx, lz) or 1.0
            facing = -(lx * n[0] + lz * n[1]) / ll
            to = (mid[0] - px, mid[1] - pz)
            tl = math.hypot(*to) or 1.0
            aim = (to[0] * lx + to[1] * lz) / (tl * ll)
            if facing > 0.4 and aim > 0.75:
                scored.append((facing + aim, idx))
        wall["photos"] = [out[i]["file"] for _, i in sorted(scored, reverse=True)[:3]]
    return out


def _split_plan(base: Dict, area: Dict, opts: TidyOptions) -> Dict:
    """One area of a shared space as its own dollhouse room. Edges that were cut through open floor
    become open sides (no wall), so the rooms still read as one space when put back together."""
    import copy
    plan = copy.deepcopy(base)
    full = [tuple(p) for p in base["outline"]]
    poly = g.ensure_ccw([tuple(p) for p in area["outline"]])
    x0, z0, x1, z1 = g.bbox(poly)
    # move this area's own back corner onto the origin
    dx, dz = x1, z0
    moved = [(g.snap(x - dx, opts.grid), g.snap(z - dz, opts.grid)) for x, z in poly]
    to_session = g.mat_from_list(base["to_session"])
    shift = g.mat_yaw_translate(0.0, dx, 0.0, dz)
    plan["to_session"] = [round(v, 6) for v in g.mat_to_list(g.mat_mul(to_session, shift))]
    plan["name"] = area["name"]
    plan["slug"] = slugify(area["name"])
    plan["split_from"] = base["name"]
    plan["outline"] = [list(p) for p in moved]
    old_walls = base["walls"]
    walls = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        on_outline = g.distance_to_polygon(mid, full) < 0.05
        src = None
        if on_outline:
            for w in old_walls:
                if g.point_segment_distance(mid, tuple(w["a"]), tuple(w["b"]))[0] < 0.05:
                    src = w
                    break
        n = g.outward_normal(a, b)
        facing = n[0] * CAMERA_DIR[0] + n[1] * CAMERA_DIR[1]
        walls.append({"id": f"wall_{i + 1}", "a": [moved[i][0], moved[i][1]],
                      "b": [moved[(i + 1) % len(moved)][0], moved[(i + 1) % len(moved)][1]],
                      "length": round(g.dist(a, b), 3), "height": src["height"] if src else base["height"],
                      "side": "front" if facing > 0.35 else "back", "normal": [round(n[0], 4), round(n[1], 4)],
                      "source_id": src["source_id"] if src else None, "color": src["color"] if src else None,
                      "open": not on_outline, "_old": src["id"] if src else None, "_a": a})
    openings = []
    for o in base["openings"]:
        w_old = next((w for w in old_walls if w["id"] == o["wall"]), None)
        if not w_old:
            continue
        wa, wb = tuple(w_old["a"]), tuple(w_old["b"])
        L = g.dist(wa, wb) or 1.0
        t = (o["offset"] + o["width"] / 2) / L
        c = (wa[0] + t * (wb[0] - wa[0]), wa[1] + t * (wb[1] - wa[1]))
        for w in walls:
            if w["open"]:
                continue
            a = w["_a"]
            b = (a[0] + (w["b"][0] - w["a"][0]), a[1] + (w["b"][1] - w["a"][1]))
            d, tt = g.point_segment_distance(c, a, b)
            if d < 0.05:
                o2 = dict(o, wall=w["id"], offset=g.snap(tt * w["length"] - o["width"] / 2, opts.grid))
                openings.append(o2)
                break
    for w in walls:
        w.pop("_old", None)
        w.pop("_a", None)
    plan["walls"] = walls
    plan["openings"] = openings
    plan["objects"] = []
    for ob in base["objects"]:
        if ob.get("area") == area["name"]:
            o2 = copy.deepcopy(ob)
            o2["position"][0] = round(o2["position"][0] - dx, 3)
            o2["position"][2] = round(o2["position"][2] - dz, 3)
            plan["objects"].append(o2)
    plan["partitions"] = []
    for p in base["partitions"]:
        mid = ((p["a"][0] + p["b"][0]) / 2, (p["a"][1] + p["b"][1]) / 2)
        if g.point_in_polygon(mid, poly):
            plan["partitions"].append(dict(p, a=[p["a"][0] - dx, p["a"][1] - dz], b=[p["b"][0] - dx, p["b"][1] - dz]))
    plan["areas"] = []
    for ph in plan["photos"]:
        ph["position"][0] = round(ph["position"][0] - dx, 3)
        ph["position"][2] = round(ph["position"][2] - dz, 3)
    plan["notes"] = base["notes"] + [f"split from {base['name']}; open sides have no wall"]
    return plan
