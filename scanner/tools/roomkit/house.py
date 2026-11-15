"""Put tidy room plans together into a whole-house floor plan.

Rooms scanned in the same session ("Next room") already share one world frame, so they are placed
from their scan transforms. Rooms from different sessions are placed by their shared doorways: a
door or opening in a placed room is matched with one of the same width in the new room, the new
room is turned so the two doors face each other through one wall, and the fit with the least
overlap (and the most other doors that also line up) wins. Placements are kept square (quarter
turns) and on the grid, and rooms are nudged so neighbouring walls meet at one wall thickness.
A config file can fix a room's story, placement, or which doors join, when the guess is wrong.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import geometry as g
from .geometry import Pt

FORMAT = "tc-house-plan/1"
WALL = 0.15          # gap between two rooms' interior faces where they share a wall
QUARTER = math.pi / 2


class Placement:
    """Room plan -> house plan: rotate by quarter turns (plain 2D, CCW in x/z), then translate."""

    def __init__(self, q: int = 0, x: float = 0.0, z: float = 0.0, y: float = 0.0):
        self.q, self.x, self.z, self.y = q % 4, x, z, y

    def pt(self, p: Pt) -> Pt:
        r = g.rot2(p, self.q * QUARTER)
        return (round(r[0] + self.x, 4), round(r[1] + self.z, 4))

    def vec(self, v: Pt) -> Pt:
        r = g.rot2(v, self.q * QUARTER)
        return (round(r[0], 6), round(r[1], 6))

    def matrix(self):
        # rot2(a) == three.js rotation.y = -a
        return g.mat_yaw_translate(-self.q * QUARTER, self.x, self.y, self.z)

    def to_dict(self) -> Dict:
        return {"quarter_turns": self.q, "rotation_y": round(-self.q * QUARTER, 6), "x": round(self.x, 3),
                "y": round(self.y, 3), "z": round(self.z, 3), "matrix": [round(v, 6) for v in g.mat_to_list(self.matrix())]}

    @staticmethod
    def from_matrix(m, grid: float) -> "Placement":
        angle = math.atan2(m[2][0], m[0][0])          # where local +x lands, as a plain 2D angle
        q = int(round(angle / QUARTER)) % 4
        return Placement(q, g.snap(m[0][3], grid), g.snap(m[2][3], grid), m[1][3])


def _outline(plan: Dict) -> List[Pt]:
    return [tuple(p) for p in plan["outline"]]


def _doorways(plan: Dict) -> List[Dict]:
    """Doors and openings of a plan with their centre and outward wall normal (room frame)."""
    walls = {w["id"]: w for w in plan["walls"]}
    out = []
    for o in plan.get("openings", []):
        if o["kind"] not in ("door", "opening"):
            continue
        w = walls.get(o["wall"])
        if not w:
            continue
        a, b = tuple(w["a"]), tuple(w["b"])
        L = g.dist(a, b) or 1.0
        t = (o["offset"] + o["width"] / 2) / L
        c = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        out.append({"id": o["id"], "kind": o["kind"], "width": o["width"], "center": c,
                    "normal": tuple(w.get("normal") or g.outward_normal(a, b))})
    for w in plan["walls"]:
        if w.get("open"):
            a, b = tuple(w["a"]), tuple(w["b"])
            out.append({"id": w["id"], "kind": "open-side", "width": g.dist(a, b),
                        "center": ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), "normal": tuple(w["normal"])})
    return out


def _story_of(plan: Dict, config: Dict) -> int:
    rc = config.get("rooms", {}).get(plan["name"]) or config.get("rooms", {}).get(plan["slug"]) or {}
    if "story" in rc:
        return int(rc["story"])
    if "story" in plan:
        return int(plan["story"])
    text = f"{plan['name']} {plan.get('description', '')} {plan.get('split_from', '')}".lower()
    if "basement" in text or "movie room" in text:
        return -1
    if "upstairs" in text or "second floor" in text:
        return 1
    return 0


def _placed_outline(plan: Dict, pl: Placement) -> List[Pt]:
    return [pl.pt(p) for p in _outline(plan)]


def _overlap_total(poly: Sequence[Pt], others: List[List[Pt]]) -> float:
    return sum(g.polygon_overlap(poly, o, 0.1) for o in others)


def _door_fits(placed: Dict[str, Tuple[Dict, Placement]], plan: Dict, grid: float, forced: Optional[Tuple] = None):
    """Every way to join `plan` to a placed room through a doorway, best first."""
    fits = []
    mine = _doorways(plan)
    for name, (other, pl) in placed.items():
        theirs = [dict(d, center=pl.pt(d["center"]), normal=pl.vec(d["normal"])) for d in _doorways(other)]
        for da in theirs:
            for db in mine:
                if forced and (name != forced[0] or da["id"] != forced[1] or db["id"] != forced[3]):
                    continue
                if not forced and abs(da["width"] - db["width"]) > 0.25:
                    continue
                # turn so B's wall normal points back at A: rot(nb) == -na
                want = math.atan2(-da["normal"][1], -da["normal"][0])
                have = math.atan2(db["normal"][1], db["normal"][0])
                q = int(round((want - have) / QUARTER)) % 4
                trial = Placement(q)
                cb = trial.pt(db["center"])
                sep = 0.0 if (da["kind"] == "open-side" or db["kind"] == "open-side") else WALL
                tx = da["center"][0] + da["normal"][0] * sep - cb[0]
                tz = da["center"][1] + da["normal"][1] * sep - cb[1]
                cand = Placement(q, g.snap(tx, grid / 5), g.snap(tz, grid / 5))
                fits.append((name, da, db, cand))
    return fits


def _score(plan: Dict, cand: Placement, placed_polys: List[List[Pt]], placed: Dict, da: Dict, db: Dict) -> Tuple[float, float]:
    poly = _placed_outline(plan, cand)
    overlap = _overlap_total(poly, placed_polys)
    agree = 0
    mine = [dict(d, center=cand.pt(d["center"])) for d in _doorways(plan)]
    for other, pl in placed.values():
        for d in _doorways(other):
            c = pl.pt(d["center"])
            for m in mine:
                if m["id"] == db["id"]:
                    continue
                if g.dist(c, m["center"]) < 0.4 and abs(d["width"] - m["width"]) < 0.25:
                    agree += 1
    score = -10.0 * overlap + agree - abs(da["width"] - db["width"])
    return score, overlap


def _settle(plan: Dict, pl: Placement, others: List[List[Pt]], grid: float) -> Placement:
    """Nudge a room so it neither overlaps nor leaves a sliver gap next to a neighbour: facing walls
    closer than 45 cm end up exactly one wall thickness apart."""
    for _ in range(4):
        moved = False
        x0, z0, x1, z1 = g.bbox(_placed_outline(plan, pl))
        for o in others:
            a0, b0, a1, b1 = g.bbox(o)
            overlap_z = min(z1, b1) - max(z0, b0)
            overlap_x = min(x1, a1) - max(x0, a0)
            if overlap_z > 0.3:
                for gap, sign in (((x0 - a1), 1), ((a0 - x1), -1)):
                    if -0.45 < gap < 0.45 and abs(gap - WALL) > 0.01 and overlap_x < 0.45:
                        pl = Placement(pl.q, pl.x + sign * (WALL - gap), pl.z, pl.y)
                        moved = True
                        break
            if not moved and overlap_x > 0.3:
                for gap, sign in (((z0 - b1), 1), ((b0 - z1), -1)):
                    if -0.45 < gap < 0.45 and abs(gap - WALL) > 0.01 and overlap_z < 0.45:
                        pl = Placement(pl.q, pl.x, pl.z + sign * (WALL - gap), pl.y)
                        moved = True
                        break
            if moved:
                break
        if not moved:
            break
    return Placement(pl.q, g.snap(pl.x, 0.01), g.snap(pl.z, 0.01), pl.y)


def assemble(plans: List[Dict], config: Optional[Dict] = None, grid: float = 0.05) -> Dict:
    config = config or {}
    notes: List[str] = []
    by_story: Dict[int, List[Dict]] = {}
    for p in plans:
        by_story.setdefault(_story_of(p, config), []).append(p)
    rooms_out: List[Dict] = []
    story_heights: Dict[int, float] = {s: max(p.get("height", 2.5) for p in ps) for s, ps in by_story.items()}
    links = config.get("links", [])

    for story, ps in sorted(by_story.items()):
        placed: Dict[str, Tuple[Dict, Placement]] = {}
        method: Dict[str, str] = {}
        linked: Dict[str, Optional[str]] = {}
        root_name = config.get("root", {}).get(str(story)) if isinstance(config.get("root"), dict) else config.get("root")
        root = next((p for p in ps if p["name"] == root_name), None) or max(ps, key=lambda p: g.area(_outline(p)))
        placed[root["name"]] = (root, Placement())
        method[root["name"]] = "root"
        # fixed placements from the config
        for p in ps:
            rc = config.get("rooms", {}).get(p["name"], {})
            if p["name"] not in placed and "place" in rc:
                pr = rc["place"]
                placed[p["name"]] = (p, Placement(int(pr.get("quarter_turns", 0)), float(pr.get("x", 0)), float(pr.get("z", 0))))
                method[p["name"]] = "config"
        pending = [p for p in ps if p["name"] not in placed]
        while pending:
            progress = False
            # 1. same scan session as a placed room: use the scan transforms
            for p in list(pending):
                mate = next(((o, pl) for o, pl in placed.values() if o["source"]["package"] == p["source"]["package"]), None)
                if not mate:
                    continue
                o, pl = mate
                m = g.mat_mul(g.mat_mul(pl.matrix(), g.mat_inverse_rigid(g.mat_from_list(o["to_session"]))),
                              g.mat_from_list(p["to_session"]))
                cand = Placement.from_matrix(m, grid)
                others = [_placed_outline(x, xp) for x, xp in placed.values()]
                cand = _settle(p, Placement(cand.q, cand.x, cand.z), others, grid)
                placed[p["name"]] = (p, cand)
                method[p["name"]] = "same scan"
                linked[p["name"]] = o["name"]
                pending.remove(p)
                progress = True
            if not pending:
                break
            # 2. explicit links from the config, then 3. the best doorway fit
            best = None
            for p in pending:
                forced = None
                for lk in links:
                    if len(lk) == 4 and lk[2] == p["name"] and lk[0] in placed:
                        forced = (lk[0], lk[1], p["name"], lk[3])
                    elif len(lk) == 4 and lk[0] == p["name"] and lk[2] in placed:
                        forced = (lk[2], lk[3], p["name"], lk[1])
                polys = [_placed_outline(x, xp) for x, xp in placed.values()]
                for name, da, db, cand in _door_fits(placed, p, grid, forced):
                    score, overlap = _score(p, cand, polys, placed, da, db)
                    if forced:
                        score += 100
                    elif overlap > 0.5:
                        continue
                    if best is None or score > best[0]:
                        best = (score, p, cand, name, da, db, forced)
            if best:
                _, p, cand, name, da, db, forced = best
                placed[p["name"]] = (p, cand)
                method[p["name"]] = "config link" if forced else "shared doorway"
                linked[p["name"]] = f"{name} ({da['id']} = {db['id']})"
                pending.remove(p)
                progress = True
            if not progress:
                # nothing fits: park the rest beside the plan so they still show up
                x0, z0, x1, z1 = g.bbox([q for x, xp in placed.values() for q in _placed_outline(x, xp)])
                cursor = x1 + 1.5
                for p in pending:
                    px0, pz0, px1, pz1 = g.bbox(_outline(p))
                    placed[p["name"]] = (p, Placement(0, cursor - px0, z0 - pz0))
                    method[p["name"]] = "unplaced"
                    cursor += (px1 - px0) + 1.0
                    notes.append(f"{p['name']}: no shared doorway found; set a link or a placement in the config")
                pending = []
        y = 0.0
        if story < 0:
            y = -sum(story_heights.get(s, 2.5) + 0.3 for s in range(story, 0))
        elif story > 0:
            y = sum(story_heights.get(s, 2.5) + 0.3 for s in range(0, story))
        for name, (p, pl) in placed.items():
            pl.y = round(y, 3)
            rooms_out.append({"name": name, "slug": p["slug"], "plan": f"{p['slug']}/plan.json", "story": story,
                              "placement": pl.to_dict(), "method": method.get(name, "?"), "linked_to": linked.get(name),
                              "outline": [list(q) for q in _placed_outline(p, pl)], "height": p.get("height", 2.5)})
    names = {-1: "Basement", 0: "Main floor", 1: "Upstairs"}
    return {"format": FORMAT, "units": "meters", "up": "+Y",
            "stories": [{"level": s, "name": names.get(s, f"Level {s}"), "height": story_heights[s]} for s in sorted(by_story)],
            "wall_thickness": WALL, "rooms": rooms_out, "notes": notes}
