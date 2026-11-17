# Third-party software

Thomas Creek itself is under the MIT licence (`LICENSE`). It uses the software below, each under its own licence.

## In the iPhone app

| Component | Where | Licence | What it asks of us |
|---|---|---|---|
| WireGuardKit (WireGuard LLC) | `ios/Vendor/WireGuardKit`, built into the VPN tunnel extension | MIT (`ios/Vendor/WireGuardKit/COPYING`) | Keep its copyright and permission notice with copies of the app and source. The app's About screen or this file is enough. |

The app uses only Apple's frameworks otherwise. `ios/TVPhone` and `ios/Bodies` are part of this project.

## In the hub (`server/`, installed by pip, not copied into this repository)

These are versions installed from `server/pyproject.toml` on 2026-11-17.

| Package | Licence |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| httpx, h2 | BSD-3-Clause, MIT |
| PyJWT | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| PyYAML | MIT |
| websockets | BSD-3-Clause |
| zeroconf | LGPL-2.1-or-later (used unmodified as a library; any change to it must be shared under the LGPL) |
| onepassword-sdk | MIT |
| dbus-fast | MIT |
| python-multipart | Apache-2.0 |
| segno | BSD-3-Clause |
| yt-dlp | Unlicense |
| anthropic | MIT |
| aiomqtt | BSD-3-Clause |
| curl_cffi | MIT |

Test-only tools (pytest, pytest-asyncio, respx, ruff, mypy) are not shipped.

## Related projects with their own licences

- The TV apps (`dylanl321/thomascreek-tv`) are a fork of IPVIO Stream under **GPL-3.0**. They talk to the hub over HTTP and share no code with this repository.
- The family board (`dylanl321/family-command-center`) is under **MIT**.
