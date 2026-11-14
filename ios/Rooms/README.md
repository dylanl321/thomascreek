# Dollhouse rooms

Each room the app can draw is one entry in `catalog.json`. The living room, the kitchen (the kitchen, the dining booth, and the laundry in one cutaway), the movie room (the basement theater), and the basement bedroom are the catalog. A garage apartment is the same shape of folder when its model exists: a USDZ, a `devices.json`, an `appearance.json` from the converter, and a `hub-mapping.json` you can edit. Nothing else in the app has to change. The page title comes from the catalog. The living-room generator still calls that corner "den"; the hub room id, and every label in the app, is Living Room. The three.js sources live in `models/dollhouse/`.

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

`hub-mapping.json` is the only file that ties a model device (`chandelier`, `tv`, …) to a hub entity id. The control is already chosen (`light`, `switch`, `media`, `select`, `fan`, or `status`). A null `id` shows Not set up: the model stays off and the hub is not called. Put the entity id in that slot and the same control starts working. No app change. A light's glow follows its brightness: the hub's 0–100 percent, or a preset level such as bedtime at 0.3.

```json
"fireplace": {
  "control": "switch",
  "entities": [{ "id": null, "action": "switch.set" }]
}
```

becomes

```json
"fireplace": {
  "control": "switch",
  "entities": [{ "id": "switch.living_room_fireplace", "action": "switch.set" }]
}
```

`climate_entity_id` is the same idea for the thermostat. Null shows a Not set up climate card, unless the hub snapshot already puts a climate device in that room. Washer and dryer use `"control": "status"`: they never send a command, and the drum spins while that entity is running. A hood light uses `"control": "select"` and `select.set`, which the hub only accepts if the option is one the device already offers.

`also` lists hub devices that belong in the room but are not in the cutaway. They show under Also in this room.

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

`usd-core` is the OpenUSD library. No Xcode, Reality Converter, or `usdzconvert` is required. The script writes a binary USD stage (Y up, metres), binds UsdPreviewSurface materials, packs the PNG textures into a USDZ, and writes `appearance.json` with the on-state colour and strength of every bulb, shade, glow, flame and the TV screen. Node names are kept. A repeated name (the sofa legs) gets a numeric suffix. A dot in a name would be a USD path separator, so it becomes an underscore. Device names have neither problem. Every glTF primitive is written: a mesh with several materials (three.js writes one primitive per material) becomes the node's `mesh` child plus `mesh_p1`, `mesh_p2`, ..., each bound to its own material. The script fails if the USD mesh count differs from the GLB's primitive count. Model Generator's exports also split multi-material meshes into `<name>_part_N` child nodes, so in practice each node has one primitive.

The script checks that the package still contains `anchor_tv`, `tv_screen`, `glow_chandelier_floor`, `fire_flame_1`, `soundbar_led` and `light_chandelier`, and the anchor sits at the position in `devices.json`.

`textures/` is the same PNGs, unpacked, so the app can paint the TV picture and the glow gradients even if SceneKit's importer drops a material texture. `tex_0.png` is the rug, `tex_1.png` is the TV picture, and `tex_2.png` / `tex_3.png` are the glow gradients.

## Adding another room

1. Export the room's GLB with the hidden glows included (or start from the all-on export).
2. Run the script into `ios/Rooms/<Room>/`.
3. Copy that room's `devices.json` next to the USDZ.
4. Add `hub-mapping.json` with the hub room id and whatever entity ids you know.
5. Add an entry to `catalog.json`.

The House tab grows a tile per catalog entry. The tile opens this page. Advanced on the page opens the existing room screen.

## Room life

Each room can have a `life.json` next to its `devices.json`, named from `catalog.json` (`"life": "LivingRoom/life.json"`). The app picks one scene at random every 45 to 120 seconds, weighted by the time of day, and does not repeat the scene that is already showing. The crossfade is about 2 seconds. A scene only changes the model: props, the cat, steam, and devices that are not linked to the hub. It never calls the hub.

A linked device always wins. If `hub-mapping.json` has an entity id, that device's real level is what the model shows, and the scene's number for it is ignored. `while_on` lists device names that, when the hub says one of them is on, pull that scene forward. The living-room TV does this for Picture on. The washer and dryer do it for Laundry day. Until an id is filled in, `while_on` does not lock, and the scene can still appear on its own so the room has something to do.

```json
{
  "spots": {
    "sofa": { "position": [-4.5, 0.54, 1.92], "yaw": -2.0 }
  },
  "scenes": [
    {
      "id": "cat_nap",
      "title": "Cat napping",
      "weights": { "morning": 1.1, "midday": 1.6, "dusk": 0.8, "night": 1.3 },
      "devices": { "fireplace": 1 },
      "props": ["prop_throw_blanket", "prop_open_book"],
      "steam": ["prop_mug"],
      "while_on": ["tv"],
      "cat": { "activity": "sleep", "spot": "sofa" }
    }
  ]
}
```

`weights` is morning, midday, dusk, and night. A missing part counts as 1. `0` keeps the scene out of the random draw. `devices` is a 0...1 level for model-only devices (a null id in the hub mapping). `props` and `steam` are node names. A name that is not in the USDZ is skipped. `spots` are where `pet_cat` can stand: `position` is the group's world position in metres, `yaw` is radians. The cat's head is the group's local +X.

`cat.activity` is `sleep`, `sit`, `walk`, or `stretch`. `spot` is one place. `path` is a walk (`["sofa", "rug"]`) and the cat curls up at the end. `walk` uses the path. The other activities use `spot`.

Daylight uses the hub weather `sunrise` and `sunset` when they are for today. Otherwise it estimates a northern-hemisphere day. Morning is warm, midday is the neutral studio colour already in the app, dusk is golden, and night is a cool window with the same key and ambient brightness as now (ambient 260, key 750, environment 0.16, exposure −0.6). Only the hue changes, plus a small window fill.

Motion pauses when the page is off screen, the app is in the background, or Low Power Mode is on. Reduce Motion keeps the scene and the lights and skips the walks and the looping effects.

The camera never moves on its own: it stays on the fitted dollhouse shot. (Build 36 orbited it a few degrees and tilted it with the phone's gyro, which read as the whole room spinning, so both are gone.) Only small, named parts move, and only when the part pivots on itself: the app skips any node that is the room root, wider than about a metre, or whose pivot is off the object. Every turn is computed from the part's rest pose and restored when motion stops, so nothing creeps.

## Node names for Model Generator

The app looks these up by name and skips anything missing. New clutter only needs a `prop_*` node and a line in `life.json`.

| What | Node name |
| --- | --- |
| Cat root | `pet_cat` |
| Fallback mesh, direct children of the root | `pet_cat_body`, `pet_cat_head`, `pet_cat_tail`, `pet_cat_tail_tip_white`, `pet_cat_ear_left`, `pet_cat_ear_right`, `pet_cat_ear_inner_left`, `pet_cat_ear_inner_right` |
| Pose groups, direct children of `pet_cat` at its origin, matched by exact name | `pet_cat_pose_sleep`, `pet_cat_pose_sit`, `pet_cat_pose_stretch`, `pet_cat_pose_walk_1`, `pet_cat_pose_walk_2` (walk alternates 1 and 2). They export visible; the app hides them on load |
| Pose joints | `<pose>_head` (neck; yaw on local Y), `<pose>_tail` (tail root; swish on local Y), identity at rest |
| Cat spots | `life.json` → `spots`; living room: `sofa`, `sofa_main`, `rug`, `window` (the floor at the French doors), `fireplace`, `armchair` |
| Flames | `fire_flame_1` … and the light `light_fireplace` |
| Fan blades | `ceiling_fan_blades` (spins on Y) |
| Washer / dryer drum | `washer_drum`, `dryer_drum`: a solid drum pivoted on its axle, spinning about local +Z (out of the door). Glow decals never spin |
| Screens | `tv_screen`, `screen_surface` |
| Curtains | exact names `fireplace_curtain_1`, `fireplace_curtain_2`, `screen_curtain_1`, `screen_curtain_2`, `rear_black_curtain_cut`; origin at the heading, sway about local X |
| Window dust | a mesh whose name contains both `window` and `glass`, or `skylight_diffuser` |
| Steam | the names in that scene's `steam` list (`prop_mug`, `prop_kettle`); the emitter sits at the top of the prop's bounds |
| Night sparkles | one mesh at the lamp, not the group at the room origin: `table_lamp_shade`, `table_lamp_booth_shade`, `table_lamp_1_shade`, `table_lamp_2_shade`, `dresser_lamp_shade`, `sconce_left_shade`, `sconce_right_shade`, `sconce_left_bulb_1`, `sconce_right_bulb_1`, `chandelier_bulb_1`, `chandelier_bulb_4`, `chandelier_lower_bulb_1`, `dining_pendant_bulb`, `flower_light_head`, `flower_light_bloom_1`, `prop_lamp` |
| Lived-in clutter | `prop_throw_blanket`, `prop_open_book`, `prop_mug`, `prop_kettle`, `prop_toy`, `prop_mail`, `prop_dishes`, `prop_cutting_board`, `prop_shoes`, `prop_laundry_basket`, `prop_detergent`, `prop_popcorn_bowl`, `prop_drink`, `prop_lamp`, `prop_catch_all_bowl`, `prop_table_runner`. A second copy takes `_2` (`prop_drink_2`). Each sits directly under the room root, pivoted on the surface under it |

Pose groups and `prop_*` nodes can start hidden. The app fades them. A drum or a curtain whose pivot is the object's centre sways or spins around that centre, so keep the pivot on the fan hub, the drum axle, or the curtain heading.

## Sunlight

A room with windows can have a `sun` entry in `catalog.json`. The app then works out where the real sun is from the house's location (the hub's weather sends it rounded to 0.1 degree) and the clock, and shines a shadow-casting sun light through the glass. Frames, muntins, walls and furniture block it, so patches of light land on the floor and the counters and move through the day. Cloud cover fades it (overcast leaves a faint trace), rain, snow and fog take most of it away, and it is off at night or when the sun is on the other side of the house. It updates every minute while the page is showing.

```json
"sun": {
  "bearing_x": 180,
  "windows": ["french_doors", "arched_window", "glass_door_right"],
  "floors": ["floor_carpet"],
  "walls": ["wall_fireplace"]
}
```

- `bearing_x` is the compass bearing (degrees clockwise from north) of the model's +X axis. In the living room and the kitchen, +X is the way the window wall faces outward, so it is simply the direction the French doors (or the sink window) look out to: 90 east, 180 south, 270 west.
- `windows` are window and glass-door groups. Nodes inside them named `*glass*` or `*pane*` let the sun through; everything else in the group casts a shadow.
- `floors` size the invisible shell that stands in for the ceiling and the cut-away walls, so the sun only gets in through the windows. The shell is never drawn, lights nothing and can't be tapped.
- `walls` are the real walls holding the windows; the tallest one sets the shell's ceiling height.

The basement rooms have no `sun`. `ios/scripts/dollhouse_snapshot.swift` takes an optional date, latitude, longitude and cloud cover to render a room offscreen with the sun at that moment.

## Property (the whole lot)

`Property/` is the exterior view: the lot on Thomas Creek Ct, the slope down to the floodplain, the river, the field, the drive, the trees, the blueberry orchard, and placeholder massing for the house and the garage apartment. It is the first row in `catalog.json`, so it is the first dollhouse tile on House. The source is Model Generator's three.js build in `/workspace/handoff/property/` (frame: 1 unit = 1 m, +Y up, north = -Z, origin = lot centre; about 102 x 186 m). The committed USDZ is `property_all_on.glb` through the same converter:

```
python3 ios/scripts/gltf_to_usdz.py property_all_on.glb ios/Rooms/Property/property.usdz ios/Rooms/Property/appearance.json
```

The row has `"kind": "property"`. That one flag:
- scales the key light, its shadow box and the camera's far plane to the lot (a room's 100 m far plane would clip it), and lets pinch zoom go to 6x instead of 2.5x;
- drops the Climate card and Advanced, and lists the rooms instead;
- makes a tap on `house_massing*` open a list of the rooms, and a tap on `garage_massing*` say the garage apartment has no model yet.

The property has no hub devices. Model Generator's first export had a `light_pole`, which was really the lone tree in the field; the re-export has `tree_field_lone` instead, the blueberry orchard (`blueberry_row_1`, `blueberry_bed_west_1`-`3`), and anchors `anchor_river`, `anchor_field`, `anchor_orchard`, `anchor_tree`. The lot is now scaled to 5 acres with the terrain relief exaggerated x2 (see the handoff README). With no devices, the page hides Scenes, and `life.json` has no scenes: the lot only follows the time-of-day light. To add an outdoor light later, put it in `devices.json` and `hub-mapping.json` the same way as a room device. The river gets a slow shimmer from a surface shader while motion is allowed (`DollhouseAmbient.syncRiver`).

Check the lighting offscreen (the snapshot knows the `kind` flag too):

```
mkdir -p /tmp/dhsnap && cp ios/scripts/dollhouse_snapshot.swift /tmp/dhsnap/main.swift
swiftc -O /tmp/dhsnap/main.swift ios/App/Features/Rooms/DollhouseSunlight.swift -o /tmp/dollhouse_snapshot
SNAP_SIZE=780x880 /tmp/dollhouse_snapshot ios/Rooms property allon dusk ios/scripts/dollhouse_rig.json /tmp/property.png
```

