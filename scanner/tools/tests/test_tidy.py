import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fixtures import World, room_json, write_package  # noqa: E402
from roomkit import geometry as g  # noqa: E402
from roomkit.scan import load_package  # noqa: E402
from roomkit.tidy import TidyOptions, tidy_room  # noqa: E402


def on_grid(v, step=0.05):
    return abs(v / step - round(v / step)) < 1e-6


class TidyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def plan(self, rooms, areas=(), opts=None, index=0):
        pkg = load_package(write_package(os.path.join(self.tmp, "pkg"), rooms, areas=areas))
        return tidy_room(pkg, pkg.rooms[index], opts or TidyOptions())

    def test_rectangle_is_squared_snapped_and_on_the_origin(self):
        w = World(angle_deg=23)
        rect = [(0, 0), (5.03, 0), (5.03, 4.02), (0, 4.02)]
        p = self.plan([("Den", "Den", room_json(w, rect))])[0]
        self.assertEqual(len(p["outline"]), 4)
        xs = sorted({round(x, 3) for x, _ in p["outline"]})
        zs = sorted({round(z, 3) for _, z in p["outline"]})
        self.assertEqual(xs[-1], 0.0)
        self.assertEqual(zs[0], 0.0)
        dims = sorted([-xs[0], zs[-1]])
        self.assertAlmostEqual(dims[0], 4.0, delta=0.051)
        self.assertAlmostEqual(dims[1], 5.05, delta=0.051)
        for x, z in p["outline"]:
            self.assertTrue(on_grid(x) and on_grid(z), (x, z))
        self.assertEqual({w["side"] for w in p["walls"]}, {"front", "back"})

    def test_to_session_maps_back_to_the_scan(self):
        w = World(angle_deg=-37, offset=(3.0, 2.0), noise=0.0)
        rect = [(0, 0), (4.0, 0), (4.0, 3.0), (0, 3.0)]
        p = self.plan([("Room", "", room_json(w, rect))])[0]
        m = g.mat_from_list(p["to_session"])
        corners = [w.pt(c) for c in rect]
        for x, z in p["outline"]:
            sx, sy, sz = g.mat_apply(m, (x, 0, z))
            self.assertAlmostEqual(sy, w.floor_y, places=4)
            self.assertLess(min(math.hypot(sx - c[0], sz - c[1]) for c in corners), 0.04)

    def test_small_jog_goes_away_but_an_l_shape_stays(self):
        w = World(angle_deg=10)
        poly = [(0, 0), (6, 0), (6, 2.0), (6.1, 2.0), (6.1, 4), (3, 4), (3, 6), (0, 6)]
        p = self.plan([("L", "", room_json(w, poly))])[0]
        self.assertEqual(len(p["outline"]), 6)

    def test_floor_through_a_doorway_is_trimmed(self):
        w = World(angle_deg=0, noise=0.005)
        poly = [(0, 0), (5, 0), (5, 1.5), (5.6, 1.5), (5.6, 2.4), (5, 2.4), (5, 4), (0, 4)]
        raw = room_json(w, poly)
        # a door on the x=5 wall line between z=1.5 and 2.4, where the floor leaks out
        raw["doors"].append(w.opening("door", w.wall((5, 1.5), (5, 2.4)), (5, 1.5), (5, 2.4), 0.5, 0.85, 2.05))
        p = self.plan([("Bedroom", "", raw)])[0]
        self.assertEqual(len(p["outline"]), 4, p["outline"])
        self.assertTrue(any("doorway" in n for n in p["notes"]))
        self.assertEqual(len([o for o in p["openings"] if o["kind"] == "door"]), 1)

    def test_alcove_with_furniture_is_kept(self):
        w = World(angle_deg=0, noise=0.005)
        poly = [(0, 0), (5, 0), (5, 1.5), (5.6, 1.5), (5.6, 2.4), (5, 2.4), (5, 4), (0, 4)]
        raw = room_json(w, poly, objects=[("storage", (5.3, 1.95), (0.5, 1.8, 0.8))])
        raw["doors"].append(w.opening("door", w.wall((5, 1.5), (5, 2.4)), (5, 1.5), (5, 2.4), 0.5, 0.85, 2.05))
        p = self.plan([("Bedroom", "", raw)])[0]
        self.assertEqual(len(p["outline"]), 8)

    def test_furniture_is_squared_and_pushed_flush(self):
        w = World(angle_deg=0, noise=0.0)
        rect = [(0, 0), (5, 0), (5, 4), (0, 4)]
        raw = room_json(w, rect, objects=[("sofa", (2.5, 0.55), (2.2, 0.85, 0.95), 4.0)])
        p = self.plan([("Den", "", raw)], opts=TidyOptions(back_corner=0))[0]
        sofa = p["objects"][0]
        self.assertTrue(sofa["squared"])
        self.assertAlmostEqual(sofa["rotation_y"] % (math.pi / 2), 0.0, places=4)
        # the wall it was near is now 1 cm away from its back
        x, _, z = sofa["position"]
        w2, _, d2 = sofa["size"]
        dz = min(abs((z - d2 / 2) - zz) for zz in {pz for _, pz in p["outline"]})
        dx = min(abs((x + w2 / 2) - xx) for xx in {px for px, _ in p["outline"]})
        self.assertLess(min(dz, dx, abs(z + d2 / 2 - max(pz for _, pz in p["outline"]))), 0.02)

    def test_things_seen_through_a_doorway_are_left_out(self):
        w = World(angle_deg=5)
        rect = [(0, 0), (4, 0), (4, 4), (0, 4)]
        raw = room_json(w, rect, objects=[("table", (2, 2), (1.2, 0.75, 0.8)), ("bed", (6.0, 2), (2, 0.6, 1.6))])
        p = self.plan([("Office", "", raw)])[0]
        self.assertEqual([o["category"] for o in p["objects"]], ["table"])
        self.assertEqual(p["outside"][0]["category"], "bed")

    def test_door_lands_on_its_wall(self):
        w = World(angle_deg=0, noise=0.0)
        rect = [(0, 0), (4, 0), (4, 3), (0, 3)]
        p = self.plan([("Bath", "", room_json(w, rect, doors=[(0, 0.25, 0.8, "door")], windows=[(2, 0.5, 1.0)]))])[0]
        kinds = sorted(o["kind"] for o in p["openings"])
        self.assertEqual(kinds, ["door", "window"])
        door = next(o for o in p["openings"] if o["kind"] == "door")
        wall = next(x for x in p["walls"] if x["id"] == door["wall"])
        self.assertAlmostEqual(wall["length"], 4.0, delta=0.06)
        self.assertTrue(0 <= door["offset"] <= wall["length"] - door["width"])
        win = next(o for o in p["openings"] if o["kind"] == "window")
        self.assertAlmostEqual(win["bottom"], 0.9, delta=0.06)

    def test_open_plan_splits_into_marked_areas(self):
        w = World(angle_deg=15)
        rect = [(0, 0), (8, 0), (8, 4), (0, 4)]
        raw = room_json(w, rect, objects=[("stove", (1, 0.4), (0.8, 0.9, 0.7)), ("table", (6, 2), (1.6, 0.75, 0.9))])
        areas = [{"name": "Kitchen", "position": w.world3((1.5, 2)), "roomIndex": 0},
                 {"name": "Dining", "position": w.world3((6.5, 2)), "roomIndex": 0}]
        plans = self.plan([("Kitchen And Dining", "", raw)], areas=areas)
        self.assertEqual(len(plans), 1)
        names = sorted(a["name"] for a in plans[0]["areas"])
        self.assertEqual(names, ["Dining", "Kitchen"])
        by_area = {o["category"]: o.get("area") for o in plans[0]["objects"]}
        self.assertEqual(by_area, {"stove": "Kitchen", "table": "Dining"})
        split = self.plan([("Kitchen And Dining", "", raw)], areas=areas, opts=TidyOptions(split_areas=True))
        self.assertEqual(sorted(p["name"] for p in split), ["Dining", "Kitchen"])
        for p in split:
            self.assertEqual(sum(1 for x in p["walls"] if x["open"]), 1)
            self.assertEqual(len(p["objects"]), 1)
            xs = [x for x, _ in p["outline"]]
            zs = [z for _, z in p["outline"]]
            self.assertAlmostEqual(max(xs), 0.0)
            self.assertAlmostEqual(min(zs), 0.0)

    def test_split_line_avoids_furniture(self):
        w = World(angle_deg=0, noise=0.0)
        rect = [(0, 0), (8, 0), (8, 4), (0, 4)]
        # a long island straddling the midpoint at x=4
        raw = room_json(w, rect, objects=[("table", (4.0, 2), (1.4, 0.9, 0.9))])
        areas = [{"name": "A", "position": w.world3((1, 2)), "roomIndex": 0},
                 {"name": "B", "position": w.world3((7, 2)), "roomIndex": 0}]
        p = self.plan([("Big", "", raw)], areas=areas, opts=TidyOptions(back_corner=0))[0]
        rects = [a["rect"] for a in p["areas"]]
        split_x = sorted({r[2] for r in rects} & {r[0] for r in rects})
        if not split_x:
            split_x = sorted({r[3] for r in rects} & {r[1] for r in rects})
        island = p["objects"][0]
        c = island["position"][0] if split_x and abs(rects[0][1] - rects[1][1]) < 1e-6 else island["position"][2]
        self.assertTrue(split_x)
        self.assertGreater(abs(split_x[0] - c), 0.69)

    def test_area_marked_not_this_room_is_cut_off(self):
        w = World(angle_deg=0, noise=0.0)
        rect = [(0, 0), (7, 0), (7, 4), (0, 4)]
        raw = room_json(w, rect, objects=[("sofa", (2, 2), (2, 0.8, 0.9)), ("chair", (6.3, 2), (0.6, 0.9, 0.6))])
        areas = [{"name": "Hallway", "position": w.world3((6.3, 2.5)), "roomIndex": 0, "exclude": True}]
        p = self.plan([("Living Room", "", raw)], areas=areas)[0]
        self.assertLess(g.area([tuple(q) for q in p["outline"]]), 7 * 4 - 1.0)
        self.assertEqual([o["category"] for o in p["objects"]], ["sofa"])
        self.assertTrue(any("not part of this room" in n for n in p["notes"]))

    def test_roomplan_sections_name_areas_without_markers(self):
        w = World(angle_deg=0)
        rect = [(0, 0), (8, 0), (8, 4), (0, 4)]
        raw = room_json(w, rect, sections=[("kitchen", (1.5, 2)), ("livingRoom", (6, 2))])
        p = self.plan([("Great Room", "", raw)])[0]
        self.assertEqual(sorted(a["name"] for a in p["areas"]), ["Kitchen", "Living Room"])

    def test_reads_a_zipped_package(self):
        import shutil
        w = World()
        folder = write_package(os.path.join(self.tmp, "z"), [("Den", "", room_json(w, [(0, 0), (4, 0), (4, 3), (0, 3)]))])
        zip_base = shutil.make_archive(os.path.join(self.tmp, "den"), "zip", self.tmp, "z")
        pkg = load_package(zip_base)
        self.assertEqual(pkg.rooms[0].name, "Den")


if __name__ == "__main__":
    unittest.main()
