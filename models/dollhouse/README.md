# Dollhouse source

These are the procedural three.js rooms the iOS models are built from. Change a wall, a light, or a material here, then regenerate the GLB and the USDZ. The committed GLBs are not the source of truth.

`living-room/` was `room/` in the original tree. The scripts inside it are relative (`src/scene.js`, a server rooted at that folder), so the rename does not change how they run. `index.html` now points its first iframe at `living-room/viewer.html`. The three.js viewer still labels that room "Den"; the iOS app calls it the Living Room. `kitchen/src/kitchen.js` follows `living-room/style.md`.

```
models/dollhouse/
  index.html              combined viewer (den + kitchen)
  package.json            npm install here
  living-room/            den corner: scene.js, style.md, photo.jpg
  kitchen/                kitchen, dining booth, and laundry, plus two reference photos
```

`living-room/style.md` is the guide for every later room: camera, cut cream, palette, and how a device is named. Reference photos are the real rooms the models were matched to.

## Install

From this directory:

```
npm install
```

That puts `three` and `puppeteer-core` in `node_modules/`, which both rooms find by walking up. `node_modules/` and `renders/` are gitignored. The render scripts need Chrome at `/usr/bin/google-chrome` and a static server already running.

## Regenerate a GLB

Living room. The server root is the room folder, port 8765:

```
cd models/dollhouse/living-room
npx --yes serve -l 8765 .
node render.mjs export
```

That writes `room_corner.glb` (all off) and `room_corner_all_on.glb` (all on), and rewrites `devices.json`. The all-off export omits hidden glows and flames. Use the all-on file for the app.

A still is `node render.mjs evening:dollhouse` and lands in `renders/`.

`node build_viewer.mjs` writes `viewer.html`, the orbit viewer `index.html` embeds.

Kitchen. The server root is `models/dollhouse`, port 8766, because the script asks for `/kitchen/src/render.html`:

```
cd models/dollhouse
npx --yes serve -l 8766 .
cd kitchen && node render.mjs export
```

That writes `kitchen.glb` and `kitchen_all_on.glb`. Stills use the same `state:view` form (`evening:dollhouse`, `laundry_running:dollhouse`). `node smoke.mjs` reloads the GLBs in the kitchen viewer. `node smoke_index.mjs` opens the combined `index.html`.

## USDZ for the iOS app

From the repo root, with `usd-core` installed (`pip install usd-core`):

```
python3 ios/scripts/gltf_to_usdz.py \
  models/dollhouse/kitchen/kitchen_all_on.glb \
  ios/Rooms/Kitchen/kitchen.usdz \
  ios/Rooms/Kitchen/appearance.json
```

The living room command is the same shape, with `room_corner_all_on.glb` and `ios/Rooms/LivingRoom/`. The script keeps node names (a dot becomes an underscore, because a dot is a USD path separator), writes the on-state colours into `appearance.json`, and copies the PNGs into `textures/`. Then add or edit that room's row in `ios/Rooms/catalog.json` and its `hub-mapping.json`. `ios/Rooms/README.md` is the app side of that.
