# Thomas Creek

The whole-home app for the Thomas Creek house.

## What this is

A home app for one family, built around a small server (the hub) that runs on the home network:

- **The hub** connects Home Assistant, cameras, the network, Plex, IPTV, the family board and the other house services. It keeps every key, decides what each person may see and do (adults, children, guests) and sends the apps what to show.
- **The iPhone and iPad app** draws what the hub sends: the home, rooms, cameras, the TVs and what's on them, chores and meals, and media requests. It has a built-in VPN for when you're away.
- **The TV apps** (Android TV and Apple TV, in a separate repository) show the same home on every television.

Every outside service is an optional adapter, so the hub runs with whichever ones a house has. It is written for one house today, and making it installable by another household is tracked in the open-source issues.

Licence: MIT (`LICENSE`). Third-party licences are listed in `THIRD_PARTY_NOTICES.md`.

## Layout

- `ios/` is the native SwiftUI app (iPhone and iPad).
- `server/` is the Thomas Creek Hub, the home-side service that holds every secret and enforces each person's permissions.

This repository is public, so it never contains addresses, hostnames, keys, tokens or the 3D house model. The hub serves anything private at run time.
