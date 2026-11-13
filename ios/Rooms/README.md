# Dollhouse rooms

Each room the app can draw is one entry in `catalog.json`. The living room is the first. Kitchen, dining, laundry and the garage apartment are the same shape of folder when their models exist: a USDZ, a `devices.json`, an `appearance.json` from the converter, and a `hub-mapping.json` you can edit. Nothing else in the app has to change. The page title comes from the catalog. The source model calls this corner "den"; the hub room id, and every label in the app, is Living Room.

```
Rooms/
  catalog.json
  LivingRoom/
    room_corner.usdz
    devices.json
    appearance.json
    hub-mapping.json
    textures/
```

`hub-mapping.json` is the only file that ties a model device (`chandelier`, `tv`, …) to a hub entity id. Leave `entities` empty when you don't know the id yet. The model still toggles. The house is not called. `climate_entity_id` is optional; when it is null the page uses a climate device the hub already places in that room.

## How room_corner.usdz was made

The all-off GLB (`room_corner.glb`) was exported with three.js `onlyVisible`, so the glow decals and the fireplace flames are not in it. They are in `room_corner_all_on.glb`, with the same node names as `devices.json`. The committed USDZ is that all-on file. The app hides the glows and flames and turns emission off until the hub (or a preset) says a device is on.

On a machine with Python 3.12:

```
pip install usd-core
python3 ios/scripts/gltf_to_usdz.py \
  room_corner_all_on.glb \
  ios/Rooms/LivingRoom/room_corner.usdz \
  ios/Rooms/LivingRoom/appearance.json
```

`usd-core` is the OpenUSD library. No Xcode, Reality Converter, or `usdzconvert` is required. The script writes a binary USD stage (Y up, metres), binds UsdPreviewSurface materials, packs the PNG textures into a USDZ, and writes `appearance.json` with the on-state colour and strength of every bulb, shade, glow, flame and the TV screen. Node names are kept. A repeated name (the sofa legs) gets a numeric suffix. A dot in a name would be a USD path separator, so it becomes an underscore. Device names have neither problem.

The script checks that the package still contains `anchor_tv`, `tv_screen`, `glow_chandelier_floor`, `fire_flame_1`, `soundbar_led` and `light_chandelier`, and the anchor sits at the position in `devices.json`.

`textures/` is the same PNGs, unpacked, so the app can paint the TV picture and the glow gradients even if SceneKit's importer drops a material texture. `tex_0.png` is the rug, `tex_1.png` is the TV picture, and `tex_2.png` / `tex_3.png` are the glow gradients.

## Adding another room

1. Export the room's GLB with the hidden glows included (or start from the all-on export).
2. Run the script into `ios/Rooms/<Room>/`.
3. Copy that room's `devices.json` next to the USDZ.
4. Add `hub-mapping.json` with the hub room id and whatever entity ids you know.
5. Add an entry to `catalog.json`.

The House tab grows a tile per catalog entry. The tile opens this page. Advanced on the page opens the existing room screen.
