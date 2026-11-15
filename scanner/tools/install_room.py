#!/usr/bin/env python3
"""Put a scanned room (or the whole floor plan) into the iOS app's House tab.

    cd models/dollhouse/scanned && node render.mjs export guest-bath      # GLB + devices.json first
    python3 scanner/tools/install_room.py guest-bath
    cd models/dollhouse/scanned && node render.mjs export house
    python3 scanner/tools/install_room.py house                           # the "Floor Plan" tile

Converts the GLB to USDZ (ios/scripts/gltf_to_usdz.py, needs `pip install usd-core`), writes
ios/Rooms/<Folder>/ and adds or updates the room's row in ios/Rooms/catalog.json. No app code
changes: the House tab shows one tile per catalog row. An existing hub-mapping.json is kept, so
entity ids filled in by hand survive a re-scan. A hand-built room with the same id is never touched
unless --replace is given.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from roomkit.jsonfmt import dumps  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCANNED = os.path.join(REPO, "models", "dollhouse", "scanned")
ROOMS = os.path.join(REPO, "ios", "Rooms")


def folder_name(title: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", title) if w) or "Room"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("room", help="scanned room folder name (slug), or 'house' for the floor plan")
    ap.add_argument("--title", help="tile title (default: the plan's name)")
    ap.add_argument("--symbol", help="SF Symbol for the tile")
    ap.add_argument("--replace", action="store_true", help="overwrite a catalog row that wasn't made by this script")
    args = ap.parse_args(argv)

    if args.room == "house":
        glb = os.path.join(SCANNED, "house.glb")
        entry_path = os.path.join(SCANNED, "house-catalog-entry.json")
        devices_path = os.path.join(SCANNED, "house-devices.json")
        stem = "house"
    else:
        glb = os.path.join(SCANNED, args.room, f"{args.room}.glb")
        entry_path = os.path.join(SCANNED, args.room, "catalog-entry.json")
        devices_path = os.path.join(SCANNED, args.room, "devices.json")
        stem = args.room
    for p in (glb, entry_path, devices_path):
        if not os.path.isfile(p):
            print(f"missing {os.path.relpath(p, REPO)}: run `node render.mjs export {args.room}` in models/dollhouse/scanned first",
                  file=sys.stderr)
            return 1
    with open(entry_path) as fh:
        entry = json.load(fh)
    title = args.title or entry["title"]
    if args.title:
        entry["id"] = re.sub(r"[^a-z0-9]+", "_", args.title.lower()).strip("_") or entry["id"]
    folder = folder_name(title)
    dest = os.path.join(ROOMS, folder)
    os.makedirs(dest, exist_ok=True)

    catalog_path = os.path.join(ROOMS, "catalog.json")
    with open(catalog_path) as fh:
        catalog = json.load(fh)
    existing = next((r for r in catalog["rooms"] if r["id"] == entry["id"]), None)
    if existing and not existing.get("from_scan") and not args.replace:
        print(f"catalog already has a hand-built room with id {entry['id']!r}; pass --replace to swap it, "
              f"or --title to install the scan under a new name", file=sys.stderr)
        return 1

    usdz = os.path.join(dest, f"{stem}.usdz")
    cmd = [sys.executable, os.path.join(REPO, "ios", "scripts", "gltf_to_usdz.py"), glb, usdz,
           os.path.join(dest, "appearance.json"), "--no-devices"]
    if subprocess.call(cmd) != 0:
        return 1
    with open(devices_path) as src, open(os.path.join(dest, "devices.json"), "w") as out:
        out.write(src.read())
    mapping = os.path.join(dest, "hub-mapping.json")
    if not os.path.isfile(mapping):
        with open(mapping, "w") as fh:
            json.dump({"room_id": entry["id"], "title": title, "climate_entity_id": None,
                       "notes": "Made from a Room Scanner scan. Add devices to the model, then fill in their hub ids here.",
                       "devices": {}}, fh, indent=2)
            fh.write("\n")
    row = {"id": entry["id"], "title": title, "symbol": args.symbol or entry.get("symbol", "square.split.bottomrightquarter"),
           "model": f"{folder}/{stem}.usdz", "devices": f"{folder}/devices.json", "mapping": f"{folder}/hub-mapping.json",
           "appearance": f"{folder}/appearance.json", "views": entry["views"], "from_scan": True}
    if existing:
        keep = {k: v for k, v in existing.items() if k in ("life", "sun", "symbol") and not args.symbol}
        existing.clear()
        existing.update(row, **keep)
    else:
        catalog["rooms"].append(row)
    with open(catalog_path, "w") as fh:
        fh.write(dumps(catalog) + "\n")
    print(f"installed {title} -> ios/Rooms/{folder}/ and catalog.json (run xcodegen; the House tab gets a tile)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
