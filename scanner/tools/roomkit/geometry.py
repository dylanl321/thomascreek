"""Small 2D/3D helpers for turning RoomPlan scans into tidy dollhouse plans.

Plan space is the floor plane: a point is (x, z) in metres, +Y is up. Pure Python, no numpy, so it
runs with the python3 that ships with macOS.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Pt = Tuple[float, float]


# ---------------------------------------------------------------- 4x4 matrices (column-major lists)

def mat_from_list(values: Sequence[float]) -> List[List[float]]:
    """RoomPlan/ARKit write 16 floats column-major. Returns rows: m[r][c]."""
    if len(values) != 16:
        raise ValueError("transform needs 16 values")
    return [[float(values[c * 4 + r]) for c in range(4)] for r in range(4)]


def mat_to_list(m: List[List[float]]) -> List[float]:
    return [m[r][c] for c in range(4) for r in range(4)]


def mat_mul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def mat_apply(m: List[List[float]], p: Sequence[float]) -> Tuple[float, float, float]:
    x, y, z = p[0], p[1], p[2]
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
            m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
            m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3])


def mat_dir(m: List[List[float]], v: Sequence[float]) -> Tuple[float, float, float]:
    x, y, z = v[0], v[1], v[2]
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z,
            m[1][0] * x + m[1][1] * y + m[1][2] * z,
            m[2][0] * x + m[2][1] * y + m[2][2] * z)


def mat_yaw_translate(yaw: float, tx: float, ty: float, tz: float) -> List[List[float]]:
    """Rotation about +Y by `yaw` (three.js rotation.y convention) then translation."""
    c, s = math.cos(yaw), math.sin(yaw)
    return [[c, 0.0, s, tx], [0.0, 1.0, 0.0, ty], [-s, 0.0, c, tz], [0.0, 0.0, 0.0, 1.0]]


def mat_inverse_rigid(m: List[List[float]]) -> List[List[float]]:
    r = [[m[j][i] for j in range(3)] for i in range(3)]
    t = [m[0][3], m[1][3], m[2][3]]
    nt = [-(r[i][0] * t[0] + r[i][1] * t[1] + r[i][2] * t[2]) for i in range(3)]
    return [[r[0][0], r[0][1], r[0][2], nt[0]], [r[1][0], r[1][1], r[1][2], nt[1]],
            [r[2][0], r[2][1], r[2][2], nt[2]], [0.0, 0.0, 0.0, 1.0]]


# ---------------------------------------------------------------- 2D basics

def rot2(p: Pt, angle: float) -> Pt:
    """Rotate a plan point counter-clockwise in the (x, z) plane, as plain 2D maths."""
    c, s = math.cos(angle), math.sin(angle)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def snap(v: float, step: float) -> float:
    return round(round(v / step) * step, 4) if step > 0 else v


def wrap_angle(a: float) -> float:
    while a <= -math.pi:
        a += 2 * math.pi
    while a > math.pi:
        a -= 2 * math.pi
    return a


def dominant_angle(segments: Iterable[Tuple[Pt, Pt]]) -> float:
    """Length-weighted wall direction modulo 90 degrees, in (-45, 45] degrees (radians)."""
    sx = sy = 0.0
    for a, b in segments:
        length = dist(a, b)
        if length < 1e-6:
            continue
        theta = math.atan2(b[1] - a[1], b[0] - a[0])
        sx += length * math.cos(4 * theta)
        sy += length * math.sin(4 * theta)
    if sx == 0 and sy == 0:
        return 0.0
    return math.atan2(sy, sx) / 4


def signed_area(poly: Sequence[Pt]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % len(poly)]
        s += x0 * z1 - x1 * z0
    return s / 2


def area(poly: Sequence[Pt]) -> float:
    return abs(signed_area(poly))


def centroid(poly: Sequence[Pt]) -> Pt:
    a = signed_area(poly)
    if abs(a) < 1e-9:
        n = max(len(poly), 1)
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)
    cx = cz = 0.0
    for i in range(len(poly)):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % len(poly)]
        f = x0 * z1 - x1 * z0
        cx += (x0 + x1) * f
        cz += (z0 + z1) * f
    return (cx / (6 * a), cz / (6 * a))


def bbox(points: Iterable[Pt]) -> Tuple[float, float, float, float]:
    pts = list(points)
    xs = [p[0] for p in pts]
    zs = [p[1] for p in pts]
    return (min(xs), min(zs), max(xs), max(zs))


def point_in_polygon(p: Pt, poly: Sequence[Pt]) -> bool:
    x, z = p
    inside = False
    n = len(poly)
    for i in range(n):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % n]
        if (z0 > z) != (z1 > z):
            xi = x0 + (z - z0) * (x1 - x0) / (z1 - z0)
            if x < xi:
                inside = not inside
    return inside


def point_segment_distance(p: Pt, a: Pt, b: Pt) -> Tuple[float, float]:
    """Distance from p to segment ab, and the position along it (0..1)."""
    dx, dz = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dz * dz
    if length2 < 1e-12:
        return dist(p, a), 0.0
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / length2
    t = max(0.0, min(1.0, t))
    q = (a[0] + t * dx, a[1] + t * dz)
    return dist(p, q), t


def distance_to_polygon(p: Pt, poly: Sequence[Pt]) -> float:
    return min(point_segment_distance(p, poly[i], poly[(i + 1) % len(poly)])[0] for i in range(len(poly)))


def dedupe(poly: Sequence[Pt], tol: float = 0.01) -> List[Pt]:
    out: List[Pt] = []
    for p in poly:
        if not out or dist(out[-1], p) > tol:
            out.append(p)
    while len(out) > 2 and dist(out[0], out[-1]) <= tol:
        out.pop()
    return out


def drop_collinear(poly: Sequence[Pt], tol: float = 1e-4) -> List[Pt]:
    pts = list(poly)
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cross) <= tol * max(dist(a, b), dist(b, c), 1e-6) or dist(a, b) < 1e-6:
                pts.pop(i)
                changed = True
                break
    return pts


def ensure_ccw(poly: Sequence[Pt]) -> List[Pt]:
    """Counter-clockwise in plain (x, z) maths, so the outward normal of edge a->b is (dz, -dx)."""
    return list(poly) if signed_area(poly) > 0 else list(reversed(poly))


def outward_normal(a: Pt, b: Pt) -> Pt:
    """Outward normal of edge a->b of a CCW polygon."""
    dx, dz = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dz) or 1.0
    return (dz / length, -dx / length)


def clip_halfplane(poly: Sequence[Pt], n: Pt, d: float) -> List[Pt]:
    """Keep the part of the polygon where n . p <= d (Sutherland-Hodgman, one edge)."""
    out: List[Pt] = []
    if not poly:
        return out
    for i in range(len(poly)):
        cur, nxt = poly[i], poly[(i + 1) % len(poly)]
        fc = n[0] * cur[0] + n[1] * cur[1] - d
        fn = n[0] * nxt[0] + n[1] * nxt[1] - d
        if fc <= 0:
            out.append(cur)
        if (fc < 0 < fn) or (fn < 0 < fc):
            t = fc / (fc - fn)
            out.append((cur[0] + t * (nxt[0] - cur[0]), cur[1] + t * (nxt[1] - cur[1])))
    return out


def clip_rect(poly: Sequence[Pt], rect: Tuple[float, float, float, float]) -> List[Pt]:
    x0, z0, x1, z1 = rect
    out = list(poly)
    for n, d in (((1.0, 0.0), x1), ((-1.0, 0.0), -x0), ((0.0, 1.0), z1), ((0.0, -1.0), -z0)):
        out = clip_halfplane(out, n, d)
    return drop_collinear(dedupe(out, 0.005), 1e-6) if len(out) >= 3 else []


def rect_overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def polygon_overlap(a: Sequence[Pt], b: Sequence[Pt], step: float = 0.1) -> float:
    """Approximate overlap area of two simple polygons by sampling a grid over their shared box."""
    ba, bb = bbox(a), bbox(b)
    x0, z0 = max(ba[0], bb[0]), max(ba[1], bb[1])
    x1, z1 = min(ba[2], bb[2]), min(ba[3], bb[3])
    if x1 <= x0 or z1 <= z0:
        return 0.0
    count = 0
    x = x0 + step / 2
    while x < x1:
        z = z0 + step / 2
        while z < z1:
            if point_in_polygon((x, z), a) and point_in_polygon((x, z), b):
                count += 1
            z += step
        x += step
    return count * step * step
