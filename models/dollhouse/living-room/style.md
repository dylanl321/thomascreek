# Smart-Home Dollhouse — Style Guide (v1, from Room 01 "Den")

Every room is a cut-open miniature seen from a 3/4 angle. It should read like a well-made architectural model: calm, warm, legible at a glance. Devices are the only things that change between renders.

## 1. Presentation
- **Shell:** a floor slab 0.30 m thick plus the room's two back walls (0.22 m thick), cut flat at the **real wall-plate height** (den: 4.3 m). Leave the two walls nearest the camera out.
- **Ceilings:** flat ceilings are left out. A **vaulted or sloped ceiling** keeps only the half nearest the back wall, cut cleanly at the ridge. That half is a 0.16 m cream shell whose cut edge shows the vault profile. The end wall that holds up the vault (the tympanum) stays in, cut along the vault curve. Ceiling fixtures hang from the shell's cut edge so the mount still reads.
- **Section faces:** wall tops, slab edges and beam ends use one cream colour, `cut #EFE3CF` (slab sides `#E2D3BB`). This is the cue that says "cutaway". Never use wood on a cut face.
- **Background:** a soft vertical gradient from `#F1ECE4` to `#D9D0C3`. No horizon and no ground plane outside the slab.
- **Camera:** "dollhouse" view is a perspective camera at FOV 30° looking at the room centre from about 45° in azimuth and about 33° down. The distance is about 2.6 × the room's diagonal (the 6.6 × 6.6 m den uses about 19 m). Use the same angle in every room so rooms can sit next to each other. Optional extras: a closer view, a photo-match view, a top view and detail views.
- **Orientation:** +Y is up and units are metres. The room's back corner sits at the origin; the two kept walls are the planes x=0 and z=0.

## 2. Form language (low-poly, soft-shaded)
- Use primitive massing: boxes, cylinders, lathes and simple extrusions. Detail goes up to roughly the level of a raised panel. No fabric wrinkles, wood grain or text, with one exception: content shown on a screen.
- Rounded box edges by object type: architecture is crisp (0 mm). Joinery and trim get 4–10 mm. Upholstery gets 50–80 mm, which gives a soft "pillowy" look.
- Proportions stay true to the real room (±10 cm). Show signature features (arched shelf bays, the gold TV frame) but simplify them.
- Decor is sparse and is never seasonal. Book runs use a fixed 8-colour muted palette. A few ceramic objects per shelf at most.

## 3. Palette and materials (PBR, mostly matte)
| Role | Hex | Rough / Metal |
|---|---|---|
| Wall walnut | `#4B2C18` | 0.65 / 0 |
| Panel field | `#5B3720` | 0.60 / 0 |
| Trim / raised panels / shelves | `#6C4325` | 0.55 / 0 |
| Shelf back (lighter so glow reads) | `#7B4D2C` | 0.70 / 0 |
| Counter / plinths | `#2E1A0E` | 0.40 / 0 |
| Gold (frames) | `#C9A046` | 0.32 / 0.9 |
| Brass (fixtures, knobs) | `#B8893A` | 0.35 / 0.85 |
| Carpet | `#CDBB9F` | 1.0 / 0 |
| Upholstery grey | `#6F7377` / cushions `#7C8085` | 0.95 / 0 |
| Leather (tan-brown) | `#7A4A2B` | 0.45 / 0 |
| Vault plaster | `#EBE0CC` | 0.95 / 0 |
| Ceramic / lamp shades | `#EFE8DC` / `#EFE2C8` | 0.35 / 0.9 |
| Black shade, soundbar | `#1F1A17`, `#1C1C1E` | 0.8 / 0 |
| Glass | `#CFE2E8`, opacity 0.22 | 0.05 / 0 |
Rooms in other styles keep the system (cut cream, background, light colours) and replace only the "wood" and "fabric" rows.

## 4. Studio lighting (identical in every room and every state)
- A hemisphere light, sky `#FFF3E3` and ground `#5B4636`, at 0.85.
- A key directional light `#FFF1DD` at 1.25 from camera-left-above, casting soft shadows (PCF soft, 2k map).
- A RoomEnvironment reflection map at 0.28.
- ACES Filmic tone mapping at exposure 1.0. Bloom (Unreal) at strength 0.38, radius 0.45 and threshold 1.0. Because the threshold is 1.0, only emissive sources above 1 bloom.
- With every device OFF the room still reads clearly, like a dusk show-home. Device light always adds on top of this. It never replaces it.

## 5. Device states
| Device type | OFF | ON |
|---|---|---|
| Lights (bulbs, pucks, strips) | Matte warm-grey `#D8D2C8`, emissive 0 | Emissive `#FFE2B0` at 5–8 (blooms) |
| Lamp shades | Matte `#EFE2C8` | Emissive `#FFC77D` at 0.8–0.9 (glows through) |
| Candle sconces / chandelier candles | Cream candle sleeves, matte warm-grey flame bulbs | Flame bulbs emissive at 7–8, plus a warm halo and a scallop on the wall above each sconce. The chandelier adds a big floor pool and a 9 m point light |
| Shelf LED strips | Thin strip under each shelf, matte | Emissive strip, plus a soft wash band on the shelf back (bright under the strip, fading down) and a pool on the shelf below |
| Light spill ("light pool") | Hidden | (a) a warm point or spot light `#FFC77D`, no shadows, short range (1.5–6 m); **and** (b) additive radial-gradient decals on the surfaces it hits: floor pools under lamps and chandeliers, scallops on the wall or shelf back behind pucks and sconces |
| TV | Glossy black (rough 0.12, metal 0.3) with a visible env highlight | Screen shows content (emissive map at 1.25), soft warm glow decal on the wall around the frame and a faint floor wash |
| Soundbar / speakers | Dark, no LED | Small cyan LED strip `#7FD3FF` at 6 plus a tiny glow |
| Fireplace | Logs on grate, dark firebox | Low-poly cone flames (orange `#FF7A1A` at 5, yellow core `#FFCC55` at 7), ember strip, orange `#FF8A2A` point light, hearth floor pool. Flames flicker in the live viewer |
Rules:
- ON colour is always warm 2700K amber. The only exceptions are status LEDs (cyan) and fire (orange).
- One device = one named group (`devices/<name>`) and one anchor empty (`anchor_<name>`) where the UI hotspot goes.
- Glow decals are named `glow_*` and are hidden when the device is OFF. Lights stay in the file with intensity 0 when OFF.

## 6. Windows, doors and the outside
- Exterior glass (doors, windows) shows a fixed **night view**: deep blue gradient sky with small stars and a moon, a dark tree line, and a few unlit blue-grey deck boards and a railing.
- The outside is drawn only *through the glass*. An invisible portal plane behind each pane writes the stencil, and the exterior meshes draw only where the stencil passes. Outside geometry never shows up above the cut walls. The exterior uses unlit colours, so device states don't change it.
- Glass uses a light blue-grey tint at 10% opacity so the view reads. Wood muntins (French doors 2×5 lites per leaf; arched windows get a grid plus radial muntins).

## 7. Pets and people
- Pets are part of the miniature but are **not devices**. Build them from 6–12 scaled spheres, cones and one tube tail. Keep them chunky and round with pointed ears. Their markings come from 2–3 flat colours (den: `pet_cat`, black `#17161A` with white `#F2EFE8` chest, paw, belly and tail tip, plus amber eyes). Pose them so the face points toward the dollhouse camera.

## 8. Naming
`<object>_<part>_<index>` in snake_case, for example `light_shelf_puck_2_1`, `french_doors_leaf_1_pane` or `tv_frame_top`. The top-level groups are `architecture`, `vaulted_ceiling`, `builtin_*`, `doors_and_windows`, `fireplace_wall`, `exterior_deck`, `furniture`, `devices`, `device_anchors`. The machine-readable device list is in `devices.json`.

## 9. Additions for open-plan rooms (Room 02 kitchen; den values above unchanged)
- Shared kit: `kitchen/src/common.js` (textures, materials, `makeKit` builders, device registry, `createApp` with the den's lighting, bloom and tone mapping).
- Flat ceilings are omitted. Recessed cans float as thin trims at ceiling height; when ON they show soft additive light cones and floor pools.
- Tray ceilings are shown as a cut cream band along the kept wall, plus one beam that carries the hanging fixture.
- Interior partitions facing the camera are cut at 1.1 m (cream cap), like the exterior wall cuts.
- Appliances (washer, dryer) are devices of type `appliance`. "Running" means a glowing display, a lit status arc, and a coloured drum glow (washer #58c8ff, dryer #ffae5c).
- Kitchen palette: espresso shaker #2b1f1a, white quartz #f1efea, kitchen walls #6a4a4f, dining walls #7b5563, laundry walls #dcd8d0, stainless steel.
- Interior camera views (`interior: true` in VIEWS) may show `userData.interiorOnly` pieces, such as the full-height nook back wall. Dollhouse views and the GLB keep the 1.1 m cut.
- Openings into rooms that aren't modelled (e.g. the kitchen's cased opening to the living room) use the stencil portal with a dim floor that fades to dark.
- Tall pieces standing on a cut-away camera-side wall (e.g. the kitchen oven tower) are cut at about 1.24 m with a cream cap in dollhouse views (`userData.dollhouseOnly` cap). The upper part is `interiorOnly`.
- Each glass opening can have its own stencil ref so it shows a different scene beyond: ref 1 is the deck and yard, ref 2 is the sunroom seen through the dining door.
