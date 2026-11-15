# Scan to dollhouse tools

Plain Python 3 (the `python3` that ships with macOS is enough, no packages) that turns Room Scanner
zips into dollhouse rooms and a whole-house floor plan. The scan is a guide, not a blueprint: the
tools square things up and round them off so the rooms look like the other dollhouses, not like a
survey.

```
scan zip ──scan2room.py──▶ models/dollhouse/scanned/<room>/plan.json ──render.mjs export──▶ <room>.glb
                                         │                                                     │
                                  floorplan.py ──▶ house.json ──render.mjs export house──▶ house.glb
                                                                                               │
                                                     install_room.py ──▶ ios/Rooms/<Room>/ + catalog.json
```

## 1. One room: `scan2room.py`

```sh
python3 scanner/tools/scan2room.py ~/Downloads/20261115-091200-guest-bath.zip
```

Writes `models/dollhouse/scanned/<room>/plan.json`, a quick `plan.svg`, and `photos/` with the best
reference photos of each wall. What it does to the scan:

- **Squares it up.** Walls within 15° of square become square, every length lands on a 5 cm grid,
  and wall jogs under 25 cm go away. Furniture within 12° of square is squared to the walls, and a
  piece within 20 cm of a wall is pushed flush against it.
- **Puts it in the dollhouse frame** (`models/dollhouse/living-room/style.md`): metres, +Y up, the
  back corner at the origin, kept walls on x=0 and z=0, interior at x<0 and z>0. The back corner is
  the one with the most wall and furniture along it; `--back-corner 0-3` picks another.
- **Trims doorway bleed.** RoomPlan often follows the floor a little way through a doorway. A narrow
  lip of floor (under 1.3 m wide, 2 m deep) with a door or opening at its base and no furniture in it
  is cut back to the wall line (`--keep-tongues` turns this off). Furniture and doors whose centre is
  outside the room are left out and listed under `outside` in the plan.
- **Splits shared spaces into areas.** Areas come from the markers set in the app ("Mark area"), or
  from RoomPlan's own sections (kitchen, dining, living room) when no markers were set. Split lines
  follow real partition walls first, then avoid cutting through furniture, then land on the grid.
  Each piece of furniture gets its area. `--split-areas` writes each area as its own room, with the
  cut sides left open (no wall) so the rooms still read as one space when put back together.
- **Cuts off "Not this room".** A marker set with "Not this room" (the hallway past a doorway, the
  next room of an open plan) cuts that part of the floor off, and everything in it.

Options: `--room N` (one room of a multi-room scan), `--story -1` (basement), `--grid 0.1`,
`--no-photos`.

## 2. The whole house: `floorplan.py`

```sh
python3 scanner/tools/floorplan.py
```

Reads every `scanned/*/plan.json` and writes `scanned/house.json` plus `house_<story>.svg`.

- Rooms scanned in one session ("Next room") are placed from the scan itself.
- Rooms from different days are joined through a **shared doorway**: a door or opening of the same
  width (±25 cm) in a room already placed, the new room turned so the two doors face each other one
  wall (15 cm) apart. The fit with the least overlap and the most other doors that also line up wins.
- Rooms are nudged so facing walls closer than 45 cm end up exactly one wall apart.
- Stories: a room named or described as basement goes to story -1 (also `--story` in scan2room).
- A room nothing fits is parked beside the plan and listed under `notes`.

Fix a wrong guess in `models/dollhouse/scanned/house-config.json`:

```json
{
  "root": "Living Room",
  "rooms": {
    "Movie Room": { "story": -1 },
    "Garage Apartment": { "place": { "quarter_turns": 1, "x": 6.2, "z": -3.0 } }
  },
  "links": [["Kitchen", "opening_1", "Hall", "door_2"]]
}
```

Door and opening ids are in each `plan.json` (`openings[].id`) and on `plan.svg`.

## 3. Look at it, export it, put it in the app

See `models/dollhouse/scanned/README.md` for the three.js viewer and the GLB export, then:

```sh
python3 scanner/tools/install_room.py guest-bath     # one room as its own House tile
python3 scanner/tools/install_room.py house          # the "Floor Plan" tile
```

`install_room.py` converts the GLB with `ios/scripts/gltf_to_usdz.py --no-devices` (needs
`pip install usd-core`), writes `ios/Rooms/<Room>/` and adds the row to `ios/Rooms/catalog.json`.
The House tab grows a tile per catalog row with no app change. It never replaces a hand-built room
of the same id unless `--replace` is given, and keeps an existing `hub-mapping.json`.

## Tests

```sh
cd scanner/tools && python3 -m unittest discover -s tests -v
```

The tests build synthetic scans in RoomPlan's own JSON shape (rotated, offset and noisy) and check
squaring, snapping, the frame, doorway trimming, flush furniture, areas and splits, exclusions,
same-session placement, doorway matching across sessions, stories and config links.
