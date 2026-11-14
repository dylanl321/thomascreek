# Dollhouse source

These are the procedural three.js rooms the iOS models are built from. Change a wall, a light, or a material here, then regenerate the GLB and the USDZ. The committed GLBs are not the source of truth.

`living-room/` was `room/` in the original tree. The scripts inside it are relative (`src/scene.js`, a server rooted at that folder), so the rename does not change how they run. `index.html` now points its first iframe at `living-room/viewer.html`. The three.js viewer still labels that room "Den"; the iOS app calls it the Living Room. `kitchen/src/kitchen.js` follows `living-room/style.md`.

```
models/dollhouse/
  index.html              combined viewer (living room, kitchen, movie room, basement bedroom)
  package.json            npm install here
  living-room/            den corner: scene.js, style.md, photo.jpg
  kitchen/                kitchen, dining booth, and laundry, plus two reference photos
  movie-room/             basement theater
  basement-bedroom/       basement bedroom, plus photo.png and sconce_ref.jpg
```

`living-room/style.md` is the guide for every later room: camera, cut cream, palette, and how a device is named. Reference photos are the real rooms the models were matched to. `kitchen/src/common.js` is the shared kit. `setDevice` takes `true` for full on, or a number from 0 to 1 to dim the emissive, the light, and the glow. Movie night leaves the table lamps at 0.3. Bedtime leaves the sconces at 0.3. The iOS page uses the same curve.

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

Movie room and basement bedroom use that same server, because their render scripts ask for `/movie-room/src/render.html` and `/basement-bedroom/src/render.html`:

```
cd models/dollhouse
npx --yes serve -l 8766 .
cd movie-room && node render.mjs export
cd ../basement-bedroom && node render.mjs export
```

Those write `movie_room.glb` / `movie_room_all_on.glb` and `bedroom.glb` / `bedroom_all_on.glb`. Presets include `movie_night` and `party`, and `bedtime` and `reading`.

## USDZ for the iOS app

From the repo root, with `usd-core` installed (`pip install usd-core`):

```
python3 ios/scripts/gltf_to_usdz.py \
  models/dollhouse/kitchen/kitchen_all_on.glb \
  ios/Rooms/Kitchen/kitchen.usdz \
  ios/Rooms/Kitchen/appearance.json
```

The living room command is the same shape, with `room_corner_all_on.glb` and `ios/Rooms/LivingRoom/`. Movie room uses `movie_room_all_on.glb` and `ios/Rooms/MovieRoom/`. Basement bedroom uses `bedroom_all_on.glb` and `ios/Rooms/BasementBedroom/`. The script keeps node names (a dot becomes an underscore, because a dot is a USD path separator), writes the on-state colours into `appearance.json`, and copies the PNGs into `textures/`. Then add or edit that room's row in `ios/Rooms/catalog.json` and its `hub-mapping.json`.

A preset value between 0 and 1 is a dim level. The app multiplies emission, lamp intensity, and glow opacity by that level. `light.set` from a preset sends `brightness` as the percent. A live light uses the brightness the hub reports.

## Fill in a hub id

`ios/Rooms/<Room>/hub-mapping.json` is the config. Each modeled device already has a `control` and an `action`. The page shows Not set up while `id` is null, and starts calling the hub as soon as that id is a real entity:

- `light` sends `light.set` (on, and brightness when the hub reports it). A preset level of 0.3 sends brightness 30.
- `switch` sends `switch.set`. The fireplace is a switch; change the action to `light.set` if it turns out to be a light.
- `media` sends `media.command` with `command` `power`. That is the soundbar, the TV, and the movie-room projector.
- `select` sends `select.set` with `option`. The hub refuses any option the device does not already list. The range hood is Off, Dim, and High. `on_option` and `off_option` are what a scene uses.
- `fan` sends `fan.set`. The basement bedroom ceiling fan also has a `light.set` entry for the light kit. While the fan is on, the blades spin.
- `status` is read only. Washer, dryer, and the popcorn machine use it. The model animates while the entity is running. Nothing is sent.
- `climate_entity_id` is the thermostat. Null keeps a Not set up climate card.

`also` is for a hub device that shares the room but is not drawn, such as the hallway light. It gets a normal control under Also in this room.

`ios/Rooms/README.md` is the same note from the app side.

## Room life

Each room folder also has `life.json` (draft scenes), `check_life_names.mjs` (`node check_life_names.mjs <glb>` fails on a missing or retired life name), `smoke_life.mjs`, and `src/life_viewer.js`, the browser reference for what the app animates. `LIFE_NOTE.md` has the node names, pivots, and the rename table from the life pass. The app's own `ios/Rooms/<Room>/life.json` is merged from these drafts and is what ships.
