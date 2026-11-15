#!/usr/bin/env python3
"""Room Scanner package -> tidy dollhouse room plan(s).

    python3 scanner/tools/scan2room.py ~/Downloads/20261115-091200-guest-bath.zip
    python3 scanner/tools/scan2room.py scan.zip --split-areas      # one plan per marked area
    python3 scanner/tools/scan2room.py scan.zip --room 1 --back-corner 2 --story -1

Writes models/dollhouse/scanned/<room>/plan.json, plan.svg and photos/ (the best reference photos
of each wall). Nothing in the source package is changed. See scanner/tools/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from roomkit.scan import load_package  # noqa: E402
from roomkit.svg import room_svg  # noqa: E402
from roomkit.tidy import TidyOptions, tidy_room  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_OUT = os.path.join(REPO, "models", "dollhouse", "scanned")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="+", help="scan .zip files or unzipped package folders")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: models/dollhouse/scanned)")
    ap.add_argument("--room", type=int, action="append", help="only this room index (repeatable)")
    ap.add_argument("--split-areas", action="store_true", help="write each marked area of a shared space as its own room")
    ap.add_argument("--back-corner", type=int, choices=range(4), help="override which corner becomes the back corner (0-3)")
    ap.add_argument("--story", type=int, help="story for the floor plan: 0 main, -1 basement, 1 upstairs")
    ap.add_argument("--keep-tongues", action="store_true", help="don't trim floor that leaked through doorways")
    ap.add_argument("--grid", type=float, default=0.05, help="snap grid in metres (default 0.05)")
    ap.add_argument("--no-photos", action="store_true", help="don't copy reference photos")
    args = ap.parse_args(argv)

    opts = TidyOptions(grid=args.grid, back_corner=args.back_corner, split_areas=args.split_areas,
                       keep_tongues=args.keep_tongues)
    written = 0
    for path in args.packages:
        pkg = load_package(path)
        for room in pkg.rooms:
            if args.room and room.index not in args.room:
                continue
            for plan in tidy_room(pkg, room, opts):
                if args.story is not None:
                    plan["story"] = args.story
                folder = os.path.join(args.out, plan["slug"])
                os.makedirs(folder, exist_ok=True)
                if not args.no_photos:
                    wanted = sorted({f for w in plan["walls"] for f in w.get("photos", [])})
                    os.makedirs(os.path.join(folder, "photos"), exist_ok=True)
                    for f in wanted:
                        data = pkg.read_bytes(f)
                        if data:
                            with open(os.path.join(folder, "photos", os.path.basename(f)), "wb") as fh:
                                fh.write(data)
                    plan["reference_photos"] = [f"photos/{os.path.basename(f)}" for f in wanted]
                with open(os.path.join(folder, "plan.json"), "w") as fh:
                    json.dump(plan, fh, indent=2)
                    fh.write("\n")
                with open(os.path.join(folder, "plan.svg"), "w") as fh:
                    fh.write(room_svg(plan))
                xs = [p[0] for p in plan["outline"]]
                zs = [p[1] for p in plan["outline"]]
                print(f"{plan['name']}: {max(xs) - min(xs):.2f} x {max(zs) - min(zs):.2f} m, {len(plan['walls'])} walls, "
                      f"{len(plan['openings'])} doors/windows, {len(plan['objects'])} pieces, "
                      f"{len(plan['outside'])} left out -> {os.path.relpath(folder)}")
                for n in plan["notes"]:
                    print(f"  - {n}")
                written += 1
    if not written:
        print("nothing written (no rooms matched)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
