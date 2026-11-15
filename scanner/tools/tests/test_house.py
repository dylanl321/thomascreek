import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fixtures import World, room_json, write_package  # noqa: E402
from roomkit import geometry as g  # noqa: E402
from roomkit.house import assemble  # noqa: E402
from roomkit.scan import load_package  # noqa: E402
from roomkit.tidy import tidy_room  # noqa: E402

A = [(0, 0), (4, 0), (4, 4), (0, 4)]          # living room
B = [(4.15, 0.5), (7.15, 0.5), (7.15, 3.5), (4.15, 3.5)]   # office east of it, one wall between
# door through the shared wall at z = 2.0, 0.9 m wide: edge 1 of A (x=4), edge 3 of B (x=4.15)
DOOR_A = (1, 0.5, 0.9, "door")
DOOR_B = (3, 0.5, 0.9, "door")


def plans_from(pkgs):
    out = []
    for pkg in pkgs:
        for r in pkg.rooms:
            out += tidy_room(pkg, r)
    return out


class HouseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def pkg(self, name, rooms, world_kw=None):
        return load_package(write_package(os.path.join(self.tmp, name), rooms, package_id=name))

    def relative_outline(self, house, a, b):
        ra = next(r for r in house["rooms"] if r["name"] == a)
        rb = next(r for r in house["rooms"] if r["name"] == b)
        x0, z0, _, _ = g.bbox([tuple(p) for p in ra["outline"]])
        return ra, rb, [(p[0] - x0, p[1] - z0) for p in rb["outline"]]

    def test_same_scan_rooms_line_up_one_wall_apart(self):
        w = World(angle_deg=31, noise=0.01)
        pkg = self.pkg("s1", [("Living Room", "", room_json(w, A, doors=[DOOR_A])),
                              ("Office", "", room_json(w, B, doors=[DOOR_B]))])
        house = assemble(plans_from([pkg]))
        office = next(r for r in house["rooms"] if r["name"] == "Office")
        self.assertEqual(office["method"], "same scan")
        living = next(r for r in house["rooms"] if r["name"] == "Living Room")
        self.assertLess(g.polygon_overlap([tuple(p) for p in living["outline"]], [tuple(p) for p in office["outline"]]), 0.05)
        lb = g.bbox([tuple(p) for p in living["outline"]])
        ob = g.bbox([tuple(p) for p in office["outline"]])
        gaps = [abs(ob[0] - lb[2]), abs(lb[0] - ob[2]), abs(ob[1] - lb[3]), abs(lb[1] - ob[3])]
        self.assertAlmostEqual(min(gaps), 0.15, delta=0.03)

    def test_separate_scans_join_through_the_shared_door(self):
        w1 = World(angle_deg=31, offset=(0, 0), noise=0.01, seed=1)
        w2 = World(angle_deg=-64, offset=(9, -4), noise=0.01, seed=2)    # a different day, different frame
        p1 = self.pkg("day1", [("Living Room", "", room_json(w1, A, doors=[DOOR_A], windows=[(3, 0.5, 1.2)]))])
        p2 = self.pkg("day2", [("Office", "", room_json(w2, B, doors=[DOOR_B]))])
        together = self.pkg("both", [("Living Room", "", room_json(w1, A, doors=[DOOR_A], windows=[(3, 0.5, 1.2)])),
                                     ("Office", "", room_json(w1, B, doors=[DOOR_B]))])
        h_sep = assemble(plans_from([p1, p2]))
        h_same = assemble(plans_from([together]))
        office = next(r for r in h_sep["rooms"] if r["name"] == "Office")
        self.assertEqual(office["method"], "shared doorway")
        _, _, rel_sep = self.relative_outline(h_sep, "Living Room", "Office")
        _, _, rel_same = self.relative_outline(h_same, "Living Room", "Office")
        bs, bm = g.bbox(rel_sep), g.bbox(rel_same)
        for a, b in zip(bs, bm):
            self.assertAlmostEqual(a, b, delta=0.12)

    def test_basement_goes_on_its_own_story_and_unmatched_rooms_are_parked(self):
        w = World(angle_deg=0)
        pkg = self.pkg("s", [("Living Room", "", room_json(w, A))])
        pkg2 = self.pkg("b", [("Basement Bedroom", "", room_json(World(angle_deg=12, seed=3), A))])
        pkg3 = self.pkg("c", [("Garage Apartment", "", room_json(World(angle_deg=40, seed=4), B))])
        house = assemble(plans_from([pkg, pkg2, pkg3]))
        stories = {r["name"]: r["story"] for r in house["rooms"]}
        self.assertEqual(stories["Basement Bedroom"], -1)
        bed = next(r for r in house["rooms"] if r["name"] == "Basement Bedroom")
        self.assertLess(bed["placement"]["y"], -2.0)
        apt = next(r for r in house["rooms"] if r["name"] == "Garage Apartment")
        self.assertEqual(apt["method"], "unplaced")
        self.assertTrue(house["notes"])

    def test_config_link_forces_a_join(self):
        w1 = World(angle_deg=0, seed=5)
        w2 = World(angle_deg=90, offset=(20, 20), seed=6)
        p1 = self.pkg("x1", [("Kitchen", "", room_json(w1, A, doors=[DOOR_A, (2, 0.5, 0.9, "door")]))])
        p2 = self.pkg("x2", [("Hall", "", room_json(w2, B, doors=[DOOR_B]))])
        plans = plans_from([p1, p2])
        kitchen = next(p for p in plans if p["name"] == "Kitchen")
        hall = next(p for p in plans if p["name"] == "Hall")
        link = ["Kitchen", kitchen["openings"][1]["id"], "Hall", hall["openings"][0]["id"]]
        house = assemble(plans, {"links": [link]})
        self.assertEqual(next(r for r in house["rooms"] if r["name"] == "Hall")["method"], "config link")


if __name__ == "__main__":
    unittest.main()
