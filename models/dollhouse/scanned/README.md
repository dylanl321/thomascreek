# Scanned rooms

Rooms that start from a Room Scanner scan instead of being drawn by hand. `scanner/tools/scan2room.py`
writes one folder per room here (`<room>/plan.json`, `plan.svg`, `photos/`), and
`scanner/tools/floorplan.py` writes `house.json` for the whole house. See `scanner/tools/README.md`.

`src/scanned_room.js` builds a room from its `plan.json` with the shared kit (`../kitchen/src/common.js`):
the 0.30 m slab, walls with cream cut tops and real holes for doors and windows, open doors swung into
the room, and soft low-poly furniture by RoomPlan category (sofa, bed, table, storage, TV, appliances,
sink, toilet, tub, stairs). Sampled colours only nudge the style palette. A room view leaves the walls
nearest the camera out, like every other room. The house view cuts every wall at 1.2 m, and a wall two
rooms share is drawn half from each side.

This is the first pass of a room, not the finished room. It gets the size, the doors, the windows and
where things stand right. The hand pass (like `kitchen/src/kitchen.js`) adds the detail and the devices,
using the reference photos in `<room>/photos/`.

## Look at it

From `models/dollhouse` (after `npm install` there):

```
npx --yes serve -l 8766 .
```

- `http://localhost:8766/scanned/floorplan` – the whole house, orbit with the mouse, one button per story.
- `cd scanned && node render.mjs guest-bath` – a still in `renders/` (`guest-bath:top`, `house`, `house:-1`).

`render.mjs` finds Chrome on a Mac or at `/usr/bin/google-chrome`; set `CHROME=` to use another one.

## Export for the app

```
cd scanned
node render.mjs export guest-bath   # guest-bath/guest-bath.glb, devices.json (no devices yet), catalog-entry.json
node render.mjs export house        # house.glb, house-devices.json, house-catalog-entry.json
python3 ../../../scanner/tools/install_room.py guest-bath
```

GLBs are build output (gitignored, like the other rooms). Commit `plan.json`, `plan.svg`, `house.json`
and `house-config.json`.
