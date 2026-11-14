# Thomas Creek for iPhone and iPad

The native SwiftUI app for the house. It talks to one thing: the Thomas Creek Hub (`../server`), which holds every
service password and checks each person's permissions. The phone holds two Secure Enclave keys and a 15-minute
access token, nothing else.

## Build (on the Mac)

```
brew install xcodegen
cd ~/Documents/GitHub/thomascreek/ios
xcodegen                      # app + widgets
open ThomasCreek.xcodeproj
```

The built-in VPN (a WireGuard tunnel extension) is a separate spec for now, because WireGuardKit's Go bridge needs Go:

```
brew install go xcodegen
xcodegen --spec project-vpn.yml
```

Edit `project.yml` / `project-vpn.yml` only; the `.xcodeproj` and Info.plists are generated. Bundle id `me.dlewis.thomascreek`, App Group
`group.me.dlewis.thomascreek` and team are set in `Config.xcconfig`. It is its own App Store Connect app, separate from Christmas Lights.
Bump `CURRENT_PROJECT_VERSION` for each upload.

## How it fits together

- **First open, on home Wi-Fi:** the app finds the hub with Bonjour (`_thomascreek._tcp`), creates its keys and enrolls.
  Dylan gets a notification to say whose device it is. No codes, no QR.
- **Every request:** short-lived bearer token from a signed challenge. Secure things (garage, locks, ovens, a child's
  internet) are signed again with a Face ID-bound key, so the hub knows a person confirmed it.
- **Live updates:** server-sent events from `/v1/events`; every card says "Live" or how old it is.
- **Away from home:** the app installs its own WireGuard tunnel (on demand, home traffic only), or uses the hub's
  published address if Dylan enables one.
- **Roles:** the hub's `/v1/me` decides the tabs. Admin and adults get Home, House, Media, Family (and Systems for
  Dylan); children get Home, My Room, Watch and Chores (a picture-first version for the 5-year-old); guests get
  Welcome and My Space. Anything a role can't do is absent, never greyed out.
- **Holiday lights:** the Christmas Lights app, moved in whole as a section (show, looks, schedule, 3D, controllers,
  Live Activity, widgets, Control Center, Siri). It now reaches the show player through the hub.
- **3D house:** drawn natively with SceneKit from `/v1/assets/house-model`. The model is not in this public repo.
- **Living Room and Kitchen:** dollhouse pages beside the existing room screens (House → Living Room, House → Kitchen). Advanced on either page opens the room screen that was already there. The three.js sources are in `models/dollhouse/`. The USDZ, `devices.json`, and the hub mapping live in `Rooms/`.

## Testing in the Simulator

Simulator builds join the hub as Dylan by themselves when the Mac has a test key, so a new named Simulator
needs no approval. Dylan makes the key once on the hub web page (`/admin`, **Test simulators**, Make a test key)
and pastes the Terminal line it shows, which saves it to `~/.thomascreek/test-key`. Each Simulator build reads that
file (or `THOMASCREEK_TEST_KEY`, e.g. `SIMCTL_CHILD_THOMASCREEK_TEST_KEY=... xcrun simctl launch ...`) on first open
on home Wi-Fi. The code is inside `#if targetEnvironment(simulator)`, so App Store and TestFlight builds never send it.
These devices show a **Test** tag in People and on the web page and get no VPN client. Turning the key off on the
web page removes all of them.

`docs/hub-contract.md` lists every route and JSON shape the app uses.

## Layout

```
App/Core        hub session, enrollment, live events, push, VPN, routing, Siri phrases
App/Design      colours from the mockups and shared components
App/Features    Home, House, Media, Family, Kid, Guest, Systems, Inbox, Profile, Holiday
Shared          used by the app and the widgets: hub client, keys, models, lights API, intents
Widgets         house status, modes, chores, and the lights widgets, Live Activity and controls
Tunnel          the WireGuard packet tunnel (project-vpn.yml only)
```
