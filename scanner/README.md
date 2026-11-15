# Room Scanner

A small iPhone app for scanning rooms of the house with LiDAR so they can be turned into the
stylized dollhouse models used by the Thomas Creek app. It's a separate app (bundle id
`me.dlewis.roomscanner`) and doesn't share code with `ios/`.

## How it works

1. **Say which room it is.** One line, typed or dictated with the keyboard mic, e.g.
   "Guest bath, through the door on the left wall of the bedroom." The room name comes from the
   first phrase ("Guest Bath").
2. **Scan.** Apple's RoomPlan coaching UI. The app takes photos on its own about every 1.5 s
   when the phone has moved or turned enough (up to 40 per room).
3. **Done.** RoomBuilder processes the room, colors are sampled from the photos, and a cartoony
   dollhouse preview appears that you can spin around.
4. **Send.** The share button sends the zip (AirDrop to the Mac, Save to Files, iCloud Drive).
   Every scan is also in Files › On My iPhone › Room Scanner (`Rooms/` unzipped, `Exports/` zips).

**Next room** (optional) finishes the current room and keeps scanning in the same session, so
rooms line up automatically and are merged with StructureBuilder.

## Scan package (schema version 1)

| File | What it is |
| --- | --- |
| `manifest.json` | schema version, room name, the one-line description, timestamp, device, units (meters), coordinate system, room list, file list |
| `capturedroom.json` | RoomPlan `CapturedRoom` (Codable JSON). Extra rooms: `capturedroom_2.json`, … |
| `room.usdz` | RoomPlan parametric USDZ. Extra rooms: `room_2.usdz`, … |
| `structure.json`, `structure.usdz` | merged `CapturedStructure`, only when "Next room" was used |
| `photos/photo_NNN.jpg` | auto-captured camera frames (landscape sensor orientation) |
| `photos.json` | per photo: camera-to-world transform (4x4 column-major), intrinsics (3x3 column-major, full-res pixels), timestamp, room index |
| `colors.json` | sampled sRGB hex per wall, floor, door, window, opening and object, keyed by RoomPlan identifier |
| `worldmap.arworldmap` | `ARWorldMap` (NSKeyedArchiver) for relocalizing later scans |
| `thumbnail.png` | dollhouse preview render |

World space is RoomPlan/ARKit: meters, right-handed, +Y up, origin where the session started.

## Not in v1

- **LLM review.** `RoomScanner/Review/RoomReviewer.swift` defines the `RoomReviewer` protocol and
  `RoomReview` format (labels, materials, smart devices, questions only when unsure). The plan is
  to write `review.json` into the package and set `manifest.reviewFile`. Nothing calls it yet.
- Cloud upload, relocalizing with a saved world map, and Thomas Creek app integration.

## Build

```sh
brew install xcodegen
cd scanner && xcodegen && open RoomScanner.xcodeproj
```

TestFlight: `BUILD=2 scanner/scripts/build_testflight.sh` (archives with automatic signing for team
Z9Q392PD67 and uploads; bump `BUILD` each time). The App Store Connect app record for
`me.dlewis.roomscanner` must exist first.

The icon is drawn by `swift scripts/make_icon.swift RoomScanner/Assets.xcassets/AppIcon.appiconset/AppIcon.png`.
