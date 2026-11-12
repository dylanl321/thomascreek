# Thomas Creek Hub

The home-side service behind the Thomas Creek app. It runs on the NAS, holds
**every** credential for every home system, and decides what each person may do.
Phones hold one Secure Enclave key and a 15-minute access token, nothing else.

```
iPhone / iPad ──HTTPS (pinned)──▶ Hub ──▶ Home Assistant, Frigate/go2rtc, Plex, Jellyseerr,
     │                            │       Sonarr/Radarr, TrueNAS, Mealie, Homebox,
     └── WireGuard (on demand) ───┘       Family Command Center, show_player.py, Firewalla, APNs
```

## What it does

- **Secrets vault.** AES-256-GCM, one master key from a separate root-only dataset. Values never appear in config, logs, API replies or this repo.
- **Zero-step enrollment.** The app finds the hub over Bonjour on home Wi-Fi, registers its Secure Enclave key, and Dylan assigns the person from a notification.
- **Roles and permissions.** admin, adult, child, guest, tablet, pending; per-person grants and denies with areas and time windows. Deny by default. Children's thermostat clamped, shared rooms quiet at bedtime, never secure-tier.
- **Face ID enforced by hardware.** Secure actions (garage, unlock, cameras away, a child's internet) need a signature from a biometric-bound key, checked by the hub.
- **Practice mode.** Reads are real; writes are logged and skipped until each action is listed in `live_actions`. Home Assistant stays read-only until Dylan turns actions on.
- **Activity log** of who, from which device, did what, and the outcome (ok, dry_run, denied, error).
- **Inbox and push** (APNs, token auth) with four levels and per-device categories.
- **Requests and approvals** for kids: movies, more internet time, rewards.
- **Chores and rewards** from Family Command Center, normalized for the app.
- **Kids' internet time** through the Firewalla MSP API: the hub creates a per-device block rule and pauses or resumes it, with timed restore.
- **Built-in VPN.** Hands out WireGuard peers for the app's tunnel; revoking a phone removes its peer.
- **Cameras** over WebRTC with short-lived passes; video never passes through the hub, credentials never reach the phone.

## Layout

```
hub/            the service (FastAPI)
  adapters/     one connector per home system
  routes/       HTTP API
  policy.py     roles, capabilities, tiers
  vault.py      encrypted secrets
tests/          pytest suite (no live systems needed)
docs/API.md     contract for the iOS app
docs/DEPLOY.md  TrueNAS setup
openapi.json    generated API schema
```

## Develop

```sh
pip install -e ".[test]"
python -m pytest -q
python scripts/export_openapi.py
```

Never commit `config.yaml`, the vault, keys or addresses: this repository is public.
