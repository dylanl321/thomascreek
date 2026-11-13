"""`hub` command line, run inside the container on the NAS.

  hub serve                          start the hub
  hub init-key PATH                  write a new 32-byte master key file
  hub secrets list|set NAME|delete NAME|sync   (sync: copy from 1Password now)
  hub check                          read-only connection test of every service
  hub people list | add NAME ROLE
  hub devices list | assign DEVICE PERSON | revoke DEVICE
  hub bootstrap-admin NAME DEVICE    make the first admin (Dylan) from an enrolled phone

Secret values are read from stdin without echo and are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from .config import data_dir, load_config, master_key_path
from .db import DB
from .policy import ROLES
from .vault import Vault, generate_master_key, load_master_key


def _vault() -> Vault:
    return Vault(data_dir() / "vault.json", load_master_key(master_key_path()))


def _db() -> DB:
    return DB(data_dir() / "hub.db")


def cmd_init_key(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists():
        print(f"{path} already exists; not overwriting", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(generate_master_key() + "\n")
    print(f"wrote a new master key to {path} (keep a copy somewhere safe, offline)")
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    v = _vault()
    if args.action == "list":
        for n in v.names():
            print(n)
        return 0
    if not args.name and args.action != "sync":
        print("secret name required", file=sys.stderr)
        return 2
    if args.action == "sync":
        return _sync_onepassword()
    if args.action == "delete":
        print("deleted" if v.delete(args.name) else "not found")
        return 0
    if sys.stdin.isatty():
        value = getpass.getpass(f"value for {args.name} (hidden): ")
    else:
        value = sys.stdin.read()
        # Keep multi-line values (a .p8 key) intact; trim a single trailing newline.
        if value.endswith("\n") and value.count("\n") == 1:
            value = value[:-1]
    if not value:
        print("empty value; nothing stored", file=sys.stderr)
        return 1
    v.set(args.name, value)
    print(f"stored {args.name}")
    return 0


def _sync_onepassword() -> int:
    from .onepassword import OnePasswordSync

    cfg = load_config()
    if not cfg.onepassword.enabled:
        print("onepassword.enabled is false in the config", file=sys.stderr)
        return 1
    sync = OnePasswordSync(cfg.onepassword, _vault())
    if not sync.enabled:
        print(f"store the service account token first: hub secrets set {cfg.onepassword.token_secret}", file=sys.stderr)
        return 1
    report = asyncio.run(sync.sync())
    for n in report.stored:
        print(f"updated {n}")
    for n, why in sorted(report.errors.items()):
        print(f"failed  {n}: {why}")
    print(report.summary())
    return 1 if report.errors else 0


def cmd_people(args: argparse.Namespace) -> int:
    db = _db()
    if args.action == "list":
        for p in db.people():
            print(f"{p['id']}  {p['role']:<7} {p['name']}")
        return 0
    if args.role not in ROLES:
        print(f"role must be one of {', '.join(ROLES)}", file=sys.stderr)
        return 2
    p = db.add_person(args.name, args.role)
    db.log("people.create", "ok", target=p["id"], source="cli")
    print(p["id"])
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    db = _db()
    if args.action == "list":
        for d in db.devices():
            print(f"{d['id']}  {d['status']:<8} {d.get('person_id') or '-':<26} {d['name']} ({d.get('model') or '?'})")
        return 0
    d = db.device(args.device)
    if d is None:
        print("no such device", file=sys.stderr)
        return 1
    if args.action == "assign":
        if db.person(args.person) is None:
            print("no such person", file=sys.stderr)
            return 1
        db.update("devices", "id", d["id"], {"person_id": args.person, "status": "active"})
        db.log("device.assign", "ok", target=d["id"], params={"person_id": args.person}, source="cli")
    else:
        db.update("devices", "id", d["id"], {"status": "revoked", "apns_token": None})
        db.log("device.revoke", "ok", target=d["id"], source="cli")
        print("revoked (the VPN peer is removed on the hub's next start)")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    db = _db()
    if db.people_with_role("admin"):
        print("an admin already exists; use the app to change people", file=sys.stderr)
        return 1
    d = db.device(args.device)
    if d is None or d["status"] == "revoked":
        print("enroll the phone first (open the app at home), then run `hub devices list`", file=sys.stderr)
        return 1
    p = db.add_person(args.name, "admin")
    db.update("devices", "id", d["id"], {"person_id": p["id"], "status": "active"})
    db.log("people.bootstrap_admin", "ok", target=p["id"], params={"device": d["id"]}, source="cli")
    print(f"{args.name} is admin on {d['name']}")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    from .main import default_hub

    async def run() -> int:
        h = default_hub()
        checks = [
            ("Home Assistant", h.ha, lambda: h.ha.get_json("/api/")),
            ("Frigate", h.frigate, lambda: h.frigate.stats()),
            ("Plex", h.plex, lambda: h.plex.get_json("/identity")),
            ("Jellyseerr", h.jellyseerr, lambda: h.jellyseerr.get_json("/api/v1/status")),
            ("Sonarr", h.sonarr, lambda: h.sonarr.get_json("/api/v3/system/status")),
            ("Radarr", h.radarr, lambda: h.radarr.get_json("/api/v3/system/status")),
            ("TrueNAS", h.truenas, lambda: h.truenas.summary()),
            ("Mealie", h.mealie, lambda: h.mealie.get_json("/api/app/about")),
            ("Homebox", h.homebox, lambda: h.homebox.search("")),
            ("Family board", h.family_board, lambda: h.family_board.calendar(1)),
            ("Show player", h.show_player, lambda: h.show_player.get_json("/status")),
            ("Firewalla", h.firewalla, lambda: h.firewalla.box()),
        ]
        bad = 0
        for title, adapter, fn in checks:
            if not adapter.enabled:
                print(f"  -   {title}: not set up")
                continue
            try:
                await fn()
                print(f"  ok  {title}")
            except Exception as exc:  # noqa: BLE001
                bad += 1
                print(f"  !!  {title}: {exc}")
        if h.onepassword.enabled:
            r = await h.onepassword.sync_safely()
            print(f"  {'ok' if r and not r.errors else '!!'}  1Password: {r.summary() if r else h.onepassword.last_error}")
        else:
            print("  -   1Password: not set up")
        print(f"  {'ok' if h.apns.enabled else '-'}   Push: {'ready' if h.apns.enabled else 'not set up'}")
        print(f"  {'ok' if h.vpn.enabled else '-'}   VPN: {h.config.vpn.provider}")
        print(f"  dry run: {h.config.hub.dry_run}; live actions: {', '.join(h.config.hub.live_actions) or 'none'}")
        await h.stop()
        return 1 if bad else 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="hub", description="Thomas Creek Hub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    k = sub.add_parser("init-key")
    k.add_argument("path")
    s = sub.add_parser("secrets")
    s.add_argument("action", choices=["list", "set", "delete", "sync"])
    s.add_argument("name", nargs="?")
    sub.add_parser("check")
    p = sub.add_parser("people")
    p.add_argument("action", choices=["list", "add"])
    p.add_argument("name", nargs="?")
    p.add_argument("role", nargs="?")
    d = sub.add_parser("devices")
    d.add_argument("action", choices=["list", "assign", "revoke"])
    d.add_argument("device", nargs="?")
    d.add_argument("person", nargs="?")
    b = sub.add_parser("bootstrap-admin")
    b.add_argument("name")
    b.add_argument("device")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .main import run

        run()
        return
    handlers = {"init-key": cmd_init_key, "secrets": cmd_secrets, "check": cmd_check, "people": cmd_people,
                "devices": cmd_devices, "bootstrap-admin": cmd_bootstrap}
    sys.exit(handlers[args.cmd](args))


_ = load_config  # re-exported for scripts
