#!/usr/bin/env python3
"""Tidy room plans -> one whole-house floor plan.

    python3 scanner/tools/floorplan.py                 # every models/dollhouse/scanned/*/plan.json
    python3 scanner/tools/floorplan.py --config models/dollhouse/scanned/house-config.json

Writes house.json (where each room sits, which story, how it was placed) and house_<story>.svg.
Rooms from one scan session line up from the scan itself; rooms from different sessions are joined
through a shared doorway. house-config.json can fix anything the guess gets wrong:

    {"root": "Living Room",
     "rooms": {"Movie Room": {"story": -1},
               "Garage Apartment": {"place": {"quarter_turns": 1, "x": 6.2, "z": -3.0}}},
     "links": [["Kitchen", "opening_1", "Hall", "door_2"]]}
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from roomkit.house import assemble  # noqa: E402
from roomkit.svg import house_svg  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DIR = os.path.join(REPO, "models", "dollhouse", "scanned")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plans", nargs="*", help="plan.json files (default: every plan under --dir)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="scanned rooms folder (default: models/dollhouse/scanned)")
    ap.add_argument("--config", help="house-config.json (default: <dir>/house-config.json if present)")
    args = ap.parse_args(argv)
    files = args.plans or sorted(glob.glob(os.path.join(args.dir, "*", "plan.json")))
    if not files:
        print("no plan.json files found; run scan2room.py first", file=sys.stderr)
        return 1
    plans = []
    for f in files:
        with open(f) as fh:
            plans.append(json.load(fh))
    names = [p["name"] for p in plans]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        print(f"two plans share a name: {', '.join(sorted(dupes))}; rename one (plan.json \"name\")", file=sys.stderr)
        return 1
    config_path = args.config or os.path.join(args.dir, "house-config.json")
    config = {}
    if os.path.isfile(config_path):
        with open(config_path) as fh:
            config = json.load(fh)
    house = assemble(plans, config)
    with open(os.path.join(args.dir, "house.json"), "w") as fh:
        json.dump(house, fh, indent=2)
        fh.write("\n")
    by_name = {p["name"]: p for p in plans}
    for s in house["stories"]:
        with open(os.path.join(args.dir, f"house_{s['level']}.svg"), "w") as fh:
            fh.write(house_svg(house, by_name, s["level"]))
    for r in house["rooms"]:
        link = f" via {r['linked_to']}" if r.get("linked_to") else ""
        print(f"{r['name']} (story {r['story']}): {r['method']}{link}")
    for n in house["notes"]:
        print(f"  - {n}")
    print(f"wrote {os.path.relpath(os.path.join(args.dir, 'house.json'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
