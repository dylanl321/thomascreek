# Life scenes: final node names (all 4 rooms)

Updated 2026-11-13 21:30 (America/New_York) to match the app's lookup rules in `ios/Rooms/README.md` (dylanl321/thomascreek).
The "Room life" and "Node names for Model Generator" sections are on `main`. The PR #87 branch (`cursor/living-room-dollhouse-86f1`) has an older README without them, so `main` was followed.
Units are metres, +Y is up, and each room keeps its existing origin (see `devices.json` → `origin`).

| room | folder | GLBs (the USDZ comes from `*_all_on.glb`) |
|---|---|---|
| Living room (den) | `handoff/living-room-life/` | `room_corner.glb`, `room_corner_all_on.glb` |
| Kitchen · dining · laundry | `handoff/kitchen-life/` | `kitchen.glb`, `kitchen_all_on.glb` |
| Movie room | `handoff/movie-room-life/` | `movie_room.glb`, `movie_room_all_on.glb` |
| Basement bedroom | `handoff/basement-bedroom-life/` | `bedroom.glb`, `bedroom_all_on.glb` |

**Each folder contains:**
- the GLBs and `devices.json`
- a `life` section in `devices.json` listing the cat, spots, props, drums, fan, curtains and sparkle nodes, each with its world position
- a **draft `life.json`** in the README's format; its spots, scenes, props, steam, devices and `while_on` names are all checked against the GLB
- `src/` builders
- `render.mjs` / `build_viewer.mjs` / `viewer.html`
- `check_life_names.mjs` and `smoke_life.mjs`
- `package.json` and a README

**Checks:**
- `node check_life_names.mjs <glb>` fails if any of these is true:
  - a README name is missing
  - an earlier-pass name is still present
  - a `prop_*` falls outside the app vocabulary
  - a `steam_spot_*` empty remains
  - a cat fallback mesh or pose group is not a direct child of `pet_cat`
- Flames and glow decals are only in the all-on GLB, as the README says.

**Names:** unique within each GLB (no converter suffixing needed). Existing device and anchor names are unchanged; the only existing nodes renamed are the six den chandelier upper-tier bulbs (living room section) to match the README.

## Global rules
- **Props:**
  - every `prop_*` node is a **direct child of the room root**
  - its own position is AT the object (origin on the surface it rests on, centred under it)
  - it is a group of `pm_<prop>_<part>` meshes
  - all props are visible in the GLB; the app fades them per scene
- **Duplicates:** the first copy keeps the bare vocabulary name and the second takes `_2` (for example `prop_drink`, `prop_drink_2`), matching the README's numbered style (`pet_cat_pose_walk_1`, `fire_flame_1`).
- **Steam:** emit from `prop_mug` / `prop_kettle` themselves (the names in a scene's `steam` list). There are **no `steam_spot_*` empties** anymore.
- **Pose groups:** included in the GLB, flagged `extras.defaultVisible = false`. The GLB has no visibility flag, and GLTFExporter drops hidden nodes, so the app should start them hidden (or faded out).
- **Exterior (app drops these):**
  - den: `exterior_deck`
  - kitchen: `exterior_yard`, plus its children `living_room_glimpse` and `sunroom_glimpse`
  - movie room: `exterior_patio`
  - bedroom: none

## Living room (den)
**Cat root `pet_cat`:**
- world position (-4.5, 0.54, 1.92), yaw −2.0, scale 1.15
- head along local **+X**
- origin on the surface under the cat

**Fallback meshes:** all are **direct children of `pet_cat`**.
- `pet_cat_body`
- `pet_cat_head` (head ball; origin at its centre)
- `pet_cat_tail` (tube whose origin is the tail root, so a rotation swings it from the base)
- `pet_cat_tail_tip_white`
- `pet_cat_ear_left` and `pet_cat_ear_right`
- `pet_cat_ear_inner_left` and `pet_cat_ear_inner_right`
- Also direct children (not looked up by the app): `pet_cat_haunch`, `pet_cat_belly_white`, `pet_cat_chest_white`, `pet_cat_muzzle_white`, `pet_cat_nose`, `pet_cat_eye_left/right`, `pet_cat_paw_front_white/black`.

**Pose groups:** direct children of `pet_cat` at its origin (identity transform), so they travel with it. Hide the fallback meshes while a pose shows.
- `pet_cat_pose_sleep`, `pet_cat_pose_sit`, `pet_cat_pose_stretch`
- `pet_cat_pose_walk_1` and `pet_cat_pose_walk_2` (two gait frames; alternate them for `walk`)

**Pose joints:** the README defines none, so they are named `<pose group>_head` / `<pose group>_tail` (for example `pet_cat_pose_walk_1_head`, `pet_cat_pose_walk_1_tail`).
- Identity rotation at rest; the origin is the neck joint or tail root.
- Head: `rotation.y` = yaw, `rotation.z` + = look up.
- Tail (extends toward −X): `rotation.y` = swish, `rotation.z` − = lift.
- Internal children: `<pose>_head_shape` (resting tilt), `<pose>_tail_tube`, `<pose>_tail_tip_white`.

**Cat spots:** `cat_spot_*` empties, mirrored in `life.json` → `spots` with keys `sofa`, `sofa_main`, `rug`, `window`, `fireplace`. Position = the world position for `pet_cat`; yaw = `rotation.y`.

| spot | position | yaw |
|---|---|---|
| `sofa` | (-4.50, 0.54, 1.92) | −2.0 (pet_cat's own place) |
| `sofa_main` | (-5.72, 0.54, 3.55) | 0 |
| `rug` | (-3.00, 0.012, 3.90) | 0.35 |
| `window` | (-0.30, 0.52, 1.35) | 0 (on the new low window seat, looking out the French doors) |
| `fireplace` | (-1.05, 0, 3.65) | π |

**`window_sill_low` (new):** a walnut window seat with a cushion (`window_sill_low_top`, `_face`, `_cushion`) inside the French-door reveal. Cushion top at y = 0.52, x −0.58…−0.06, z 0.84…1.86. The arched window sill stays at y = 2.98.

**Props:**

| node | where |
|---|---|
| `prop_throw_blanket` | plaid throw over the sectional end arm |
| `prop_open_book` | face-up on the sectional |
| `prop_mug` | side table by the lamp; steam |
| `prop_shoes` | pink slippers on the rug |
| `prop_toy` | **new**: red yarn ball with a loose strand, plus a felt mouse, on the rug |

**Sparkle names present:**
- `table_lamp_shade`
- `sconce_left_bulb_1`, `sconce_right_bulb_1`
- **`chandelier_bulb_1…6`** (upper tier, renamed from `chandelier_upper_bulb_N`)
- `chandelier_lower_bulb_1…8`

**Also present:** `fire_flame_1…` / `light_fireplace` and `tv_screen`.

## Kitchen
**Props:** all direct children of `kitchen`.

| node | where |
|---|---|
| `prop_mug` | island; steam |
| `prop_kettle` | back-right burner; steam |
| `prop_mail` | island, dining end |
| `prop_dishes` | in the sink |
| `prop_cutting_board` | **new**: counter left of the cooktop, with onion/tomato slices and a knife |
| `prop_detergent` | **new**: orange jug on the laundry counter above the washer |
| `prop_laundry_basket` | **new**: laundry floor by the utility cabinet |

**Drums:** `washer_drum` (world -0.581, 0.41, 8.84) and `dryer_drum` (world -0.581, 0.41, 9.56).
- Each is a **solid closed cylinder mesh** (r 0.165, depth 0.30).
- Origin at the drum centre on the axle.
- **Local +Z points out of the door** (world −X); spin = `rotation.z`.
- Children: 3 baffles and laundry lumps on the door-side face, seen through the now see-through door glass.
- The `glow_washer_drum` / `glow_dryer_drum` decals are unchanged.

**Sparkles:** `table_lamp_booth_shade`, `dining_pendant_bulb`.

## Movie room
**Props:**
- `prop_popcorn_bowl` (ottoman)
- `prop_popcorn_bowl_2` (back-row seat)
- `prop_drink` (front-row side table)
- `prop_drink_2` (back-row side table)
- `prop_throw_blanket` (front-row right seat; origin at seat height)

**Curtains:** these are the meshes the app sways (name contains `curtain`, not rod/track/light). Each mesh's **origin is at its heading** (top rod), and the geometry hangs down −Y. Sway = rotation about the mesh's local X (the rod direction).
- `fireplace_curtain_1` and `fireplace_curtain_2`
- `screen_curtain_1` and `screen_curtain_2`
- `rear_black_curtain_cut` (dollhouse height)
- `rear_black_curtain` (full height, interior views only, so it is not in the dollhouse GLB)
- The `curtain_*` pivot groups from the earlier pass were removed, so they cannot double-sway.

**Sparkles:**
- `table_lamp_1_shade` and `table_lamp_2_shade`
- `flower_light_bloom_1`
- **`flower_light_head`** (new): a glowing bud atop the tallest bloom; it lights with the `flower_light` device

## Basement bedroom
**Props:**

| node | where |
|---|---|
| `prop_throw_blanket` | crumpled sage throw on the duvet |
| `prop_open_book` | open, right nightstand |
| `prop_shoes` | by the bed |
| `prop_laundry_basket` | corner past the door |
| **`prop_lamp`** | **new**: small moon night-lamp on the left nightstand (night sparkle) |

**Fan:** **`ceiling_fan_blades`** spins on Y (origin at the hub, world 1.85, 2.14, 2.35). The earlier `fan_blades` child group was removed; the blade arms are direct children of `ceiling_fan_blades` again.

**Sparkles:** `sconce_left_shade`, `sconce_right_shade`, `dresser_lamp_shade`, `prop_lamp`.

## Renamed in this pass

| room | before | after |
|---|---|---|
| den | `pet_cat_pose_walk_a` / `_walk_b` (+ `_head` / `_tail`) | `pet_cat_pose_walk_1` / `_walk_2` (+ `_head` / `_tail`) |
| den | pose groups as siblings at spots | children of `pet_cat` at its origin |
| den | `pet_cat_head` / `pet_cat_tail` pivot groups (+ `_head_ball`, `_tail_tube`) | flat fallback meshes `pet_cat_head`, `pet_cat_tail`; ears / tip direct children again |
| den | `prop_slippers` | `prop_shoes` |
| den | `prop_remote` | `armchair_remote` (decor) |
| den | `steam_spot_mug` | removed |
| den | `chandelier_upper_bulb_1…6` | `chandelier_bulb_1…6` |
| den | `cat_spot_window` on the floor | on `window_sill_low`; `cat_spot_sofa` = pet_cat's place; old sofa spot → `cat_spot_sofa_main` |
| kitchen | `prop_coffee_mug` | `prop_mug` |
| kitchen | `prop_mail_stack` | `prop_mail` |
| kitchen | `prop_sink_dishes` | `prop_dishes` |
| kitchen | `prop_fruit_bowl` | `island_fruit_bowl` (decor) |
| kitchen | `prop_dish_towel` | `dishwasher_towel` (decor) |
| kitchen | `steam_spot_*` | removed |
| kitchen | `washer_drum` (thin disc at the door) | solid drum, centre-pivot; plus new `dryer_drum` |
| movie | `prop_popcorn_bowl_a` / `_b` | `prop_popcorn_bowl` / `prop_popcorn_bowl_2` |
| movie | `prop_soda_cup_a` / `_b` | `prop_drink` / `prop_drink_2` |
| movie | `prop_blanket` | `prop_throw_blanket` |
| movie | `curtain_*` groups | removed (curtain meshes pivot at the heading) |
| bedroom | `prop_unmade_throw` | `prop_throw_blanket` |
| bedroom | `prop_nightstand_book` (closed) | `prop_open_book` (open) |
| bedroom | `fan_blades` | removed (use `ceiling_fan_blades`) |

## Viewer reference
`src/life_viewer.js` does what the app should do:
- hides poses
- moves `pet_cat` between spots and swaps pose groups (walk alternates walk_1 / walk_2)
- spins `washer_drum` / `dryer_drum` / `ceiling_fan_blades` while their device is on
- sways the curtain meshes from their heading
- steams above `prop_mug` / `prop_kettle`
- adds a Props on/off toggle
