"""Quick 2D drawings of a room plan or a house plan, for checking a conversion at a glance."""
from __future__ import annotations

import html
from typing import Dict, List, Tuple

from . import geometry as g

AREA_FILLS = ["#efe3cf", "#e3ecdf", "#e6e1ef", "#f1e2dc", "#dfe9ef", "#efe9d6"]


def _tf(scale: float, x0: float, z0: float, pad: float):
    return lambda p: (round((p[0] - x0) * scale + pad, 1), round((p[1] - z0) * scale + pad, 1))


def _poly(points, fill, stroke="none", width=0.0, opacity=1.0) -> str:
    pts = " ".join(f"{x},{y}" for x, y in points)
    return f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="{width}" fill-opacity="{opacity}"/>'


def _room_parts(plan: Dict, place, T) -> List[str]:
    parts: List[str] = []
    outline = [T(place(tuple(p))) for p in plan["outline"]]
    parts.append(_poly(outline, "#f6f1e8"))
    for i, ar in enumerate(plan.get("areas") or []):
        if len(plan.get("areas") or []) > 1:
            parts.append(_poly([T(place(tuple(p))) for p in ar["outline"]], AREA_FILLS[i % len(AREA_FILLS)], opacity=0.9))
            ac = T(place(g.centroid([tuple(p) for p in ar["outline"]])))
            parts.append(f'<text x="{ac[0]}" y="{ac[1] + 16}" font-family="-apple-system,Helvetica,sans-serif" '
                         f'font-size="11" text-anchor="middle" fill="#8a7763">{html.escape(ar["name"])}</text>')
    walls = {w["id"]: w for w in plan["walls"]}
    for w in plan["walls"]:
        a, b = T(place(tuple(w["a"]))), T(place(tuple(w["b"])))
        if w.get("open"):
            parts.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#b9ab95" stroke-width="1.5" stroke-dasharray="5 4"/>')
        else:
            color = "#4b3a2c" if w.get("side") == "back" else "#8d7b69"
            parts.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="{color}" stroke-width="5" stroke-linecap="square"/>')
    for p in plan.get("partitions") or []:
        a, b = T(place(tuple(p["a"]))), T(place(tuple(p["b"])))
        parts.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#6b5a49" stroke-width="3"/>')
    for o in plan.get("openings") or []:
        w = walls.get(o["wall"])
        if not w:
            continue
        a, b = tuple(w["a"]), tuple(w["b"])
        L = g.dist(a, b) or 1.0
        p0 = (a[0] + (b[0] - a[0]) * o["offset"] / L, a[1] + (b[1] - a[1]) * o["offset"] / L)
        p1 = (a[0] + (b[0] - a[0]) * (o["offset"] + o["width"]) / L, a[1] + (b[1] - a[1]) * (o["offset"] + o["width"]) / L)
        A, B = T(place(p0)), T(place(p1))
        color = {"window": "#7fb6d9", "door": "#f6f1e8", "opening": "#f6f1e8"}.get(o["kind"], "#fff")
        parts.append(f'<line x1="{A[0]}" y1="{A[1]}" x2="{B[0]}" y2="{B[1]}" stroke="{color}" stroke-width="6"/>')
        if o["kind"] == "door":
            parts.append(f'<line x1="{A[0]}" y1="{A[1]}" x2="{B[0]}" y2="{B[1]}" stroke="#a0784f" stroke-width="1.5"/>')
    for ob in plan.get("objects") or []:
        x, _, z = ob["position"]
        w, _, d = ob["size"]
        yaw = ob.get("yaw2d", 0.0)
        corners = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
        pts = []
        for c in corners:
            r = g.rot2(c, yaw)
            pts.append(T(place((x + r[0], z + r[1]))))
        parts.append(_poly(pts, ob.get("color") or "#c9b79c", "#6b5a49", 1, 0.85))
    c = T(place(g.centroid([tuple(p) for p in plan["outline"]])))
    parts.append(f'<text x="{c[0]}" y="{c[1]}" font-family="-apple-system,Helvetica,sans-serif" font-size="13" '
                 f'text-anchor="middle" fill="#3b2f25">{html.escape(plan["name"])}</text>')
    return parts


def _svg(parts: List[str], w: float, h: float) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}">'
            f'<rect width="100%" height="100%" fill="#efe9df"/>' + "".join(parts) + "</svg>")


def room_svg(plan: Dict, scale: float = 60.0, pad: float = 30.0) -> str:
    pts = [tuple(p) for p in plan["outline"]] + [(o["position"][0], o["position"][2]) for o in plan.get("objects", [])]
    x0, z0, x1, z1 = g.bbox(pts)
    T = _tf(scale, x0, z0, pad)
    return _svg(_room_parts(plan, lambda p: p, T), (x1 - x0) * scale + 2 * pad, (z1 - z0) * scale + 2 * pad)


def house_svg(house: Dict, plans: Dict[str, Dict], story: int, scale: float = 45.0, pad: float = 30.0) -> str:
    rooms = [r for r in house["rooms"] if r["story"] == story]
    if not rooms:
        return _svg([], 100, 100)
    x0, z0, x1, z1 = g.bbox([tuple(p) for r in rooms for p in r["outline"]])
    T = _tf(scale, x0, z0, pad)
    parts: List[str] = []
    for r in rooms:
        plan = plans[r["name"]]
        pl = r["placement"]
        q = pl["quarter_turns"]

        def place(p: Tuple[float, float], q=q, pl=pl):
            rr = g.rot2(p, q * 3.141592653589793 / 2)
            return (rr[0] + pl["x"], rr[1] + pl["z"])
        parts += _room_parts(plan, place, T)
    return _svg(parts, (x1 - x0) * scale + 2 * pad, (z1 - z0) * scale + 2 * pad)
