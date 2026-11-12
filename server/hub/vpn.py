"""Built-in VPN: WireGuard peers handed out at enrollment.

The phone makes its own key pair and sends only its public key; the private
key stays in the iOS keychain. The hub allocates a tunnel address, adds the
peer to its WireGuard interface and returns the settings the app's tunnel
extension needs. Revoking a device removes its peer.

Providers:
  none    VPN off. /v1/vpn/config answers 503.
  wg-cli  The hub owns a kernel WireGuard interface (container needs
          NET_ADMIN and host networking) and manages peers with `wg set`.
  pool    Another WireGuard server (the Firewalla) is the VPN. Dylan makes a few
          clients there and drops each `.conf` into the vault as
          `vpn.pool.<n>`. Each approved phone is given one free client, private
          key included, over the hub's pinned TLS channel. Removing a phone
          deletes its client from the vault, since the old phone still holds
          that key: Dylan deletes it on the Firewalla, makes a new one and drops
          it in under the same name.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import subprocess
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from .config import VpnConfig
from .db import DB
from .vault import Vault

log = logging.getLogger("hub.vpn")


class VpnError(RuntimeError):
    pass


def parse_wg_conf(text: str) -> dict[str, dict[str, str]]:
    """A wg-quick .conf as {"interface": {...}, "peer": {...}} with lower-case keys (first peer only)."""
    out: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().lower()
            section = name if name not in out else None
            if section:
                out[section] = {}
            continue
        if section and "=" in line:
            k, v = line.split("=", 1)
            out[section][k.strip().lower()] = v.strip()
    return out


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def valid_wg_key(b64: str) -> bool:
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:  # noqa: BLE001
        return False
    if len(raw) != 32:
        return False
    try:
        X25519PublicKey.from_public_bytes(raw)
    except Exception:  # noqa: BLE001
        return False
    return True


def new_private_key() -> str:
    raw = X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return base64.b64encode(raw).decode()


def public_from_private(priv_b64: str) -> str:
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


class Vpn:
    def __init__(self, cfg: VpnConfig, vault: Vault, db: DB, runner=subprocess.run):
        self.cfg = cfg
        self.vault = vault
        self.db = db
        self._run = runner
        self.net = ipaddress.ip_network(cfg.subnet, strict=False)
        self._pool: tuple[float, tuple[str, ...], dict] | None = None

    @property
    def enabled(self) -> bool:
        if self.cfg.provider == "pool":
            return bool(self.pool())
        return self.cfg.provider != "none" and bool(self.cfg.endpoint)

    # -- pool provider ---------------------------------------------------
    def pool(self) -> dict[str, dict[str, dict[str, str]]]:
        """Usable pooled client configs by vault name; unparseable ones are skipped."""
        names = tuple(sorted(n for n in self.vault.names() if n.startswith(self.cfg.pool_prefix)))
        if self._pool and self._pool[1] == names and time.monotonic() - self._pool[0] < 60:
            return self._pool[2]
        out = {}
        for name in names:
            conf = parse_wg_conf(str(self.vault.get(name) or ""))
            i, p = conf.get("interface", {}), conf.get("peer", {})
            if not (i.get("privatekey") and i.get("address") and p.get("publickey") and (p.get("endpoint") or self.cfg.endpoint)):
                log.warning("VPN pool: %s is not a complete WireGuard client config", name)
                continue
            try:
                conf["_pub"] = {"key": public_from_private(i["privatekey"])}
            except Exception:  # noqa: BLE001
                log.warning("VPN pool: %s has an invalid private key", name)
                continue
            out[name] = conf
        self._pool = (time.monotonic(), names, out)
        return out

    def _pool_slot(self, device: dict[str, Any]) -> tuple[str, dict[str, dict[str, str]]]:
        pool = self.pool()
        mine = device.get("wg_public_key")
        for name, conf in pool.items():
            if mine and conf["_pub"]["key"] == mine:
                return name, conf
        taken = {r["wg_public_key"] for r in self.db.all(
            "SELECT wg_public_key FROM devices WHERE wg_public_key IS NOT NULL AND status != 'revoked' AND id != ?",
            [device["id"]])}
        for name in sorted(pool, key=lambda n: (len(n), n)):
            if pool[name]["_pub"]["key"] not in taken:
                return name, pool[name]
        raise VpnError("every VPN client is in use; add another one on the Firewalla")

    def _pool_config(self, device: dict[str, Any]) -> dict[str, Any]:
        name, conf = self._pool_slot(device)
        i, p = conf["interface"], conf["peer"]
        addresses = _split(i.get("address"))
        self.db.update("devices", "id", device["id"], {"wg_public_key": conf["_pub"]["key"], "wg_address": addresses[0]})
        log.info("VPN pool: %s given to device %s", name, device["id"])
        dns = _split(i.get("dns")) or self.cfg.dns
        allowed = self.cfg.allowed_ips or _split(p.get("allowedips")) or ["0.0.0.0/0"]
        if self.cfg.allowed_ips:
            # Split tunnel: home ranges only, plus the VPN's own DNS servers so names still resolve.
            allowed = allowed + [f"{d}/32" for d in dns if "." in d and f"{d}/32" not in allowed]
        peer = {
            "public_key": p["publickey"],
            "endpoint": p.get("endpoint") or self.cfg.endpoint,
            "allowed_ips": allowed,
            "persistent_keepalive": int(p.get("persistentkeepalive") or self.cfg.persistent_keepalive),
        }
        if p.get("presharedkey"):
            peer["preshared_key"] = p["presharedkey"]
        return {
            "interface": {"addresses": addresses, "dns": dns, "private_key": i["privatekey"]},
            "peer": peer,
            "home_ssids": self.cfg.home_ssids,
        }

    def is_tunnel_address(self, addr: ipaddress._BaseAddress) -> bool:
        if addr in self.net:
            return True
        if self.cfg.provider == "pool":
            for conf in self.pool().values():
                for a in _split(conf["interface"].get("address")):
                    try:
                        if addr in ipaddress.ip_interface(a).network or addr == ipaddress.ip_interface(a).ip:
                            return True
                    except ValueError:
                        continue
        return False

    def server_public_key(self) -> str:
        priv = self.vault.get(self.cfg.server_key_secret)
        if priv is None:
            priv = new_private_key()
            self.vault.set(self.cfg.server_key_secret, priv)
        return public_from_private(str(priv))

    def server_address(self) -> str:
        return str(next(self.net.hosts()))

    def _allocate(self) -> str:
        used = {r["wg_address"] for r in self.db.all("SELECT wg_address FROM devices WHERE wg_address IS NOT NULL")}
        hosts = self.net.hosts()
        next(hosts)  # .1 is the hub
        for ip in hosts:
            if str(ip) not in used:
                return str(ip)
        raise VpnError("no tunnel addresses left")

    def _wg(self, *args: str, stdin: str | None = None) -> None:
        if self.cfg.provider != "wg-cli":
            return
        res = self._run(["wg", *args], input=stdin, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            # stderr from wg never contains key material for these subcommands.
            raise VpnError(f"wg {args[0]} failed: {res.stderr.strip()[:200]}")

    def _ip(self, *args: str, check: bool = False) -> None:
        res = self._run(["ip", *args], capture_output=True, text=True, check=False)
        if check and res.returncode != 0:
            raise VpnError(f"ip {' '.join(args[:2])} failed: {res.stderr.strip()[:200]}")

    def bring_up(self) -> None:
        """Create the interface and re-add every active peer (idempotent)."""
        if self.cfg.provider != "wg-cli" or not self.cfg.endpoint:
            return
        iface = self.cfg.interface
        port = self.cfg.endpoint.rsplit(":", 1)[-1]
        self.server_public_key()  # make sure a key exists
        self._ip("link", "add", iface, "type", "wireguard")
        self._wg("set", iface, "listen-port", port, "private-key", "/dev/stdin",
                 stdin=str(self.vault.require(self.cfg.server_key_secret)))
        self._ip("address", "replace", f"{self.server_address()}/{self.net.prefixlen}", "dev", iface)
        self._ip("link", "set", iface, "up", check=True)
        for d in self.db.all("SELECT wg_public_key, wg_address FROM devices WHERE status='active' AND wg_public_key IS NOT NULL"):
            self._wg("set", iface, "peer", d["wg_public_key"], "allowed-ips", f"{d['wg_address']}/32")
        log.info("WireGuard interface %s is up", iface)

    def config_for(self, device: dict[str, Any], wg_public_key: str | None) -> dict[str, Any]:
        if not self.enabled:
            raise VpnError("the built-in VPN is not set up yet")
        if self.cfg.provider == "pool":
            return self._pool_config(device)
        if not wg_public_key or not valid_wg_key(wg_public_key):
            raise VpnError("wg_public_key must be a base64 Curve25519 public key")
        old = device.get("wg_public_key")
        address = device.get("wg_address") or self._allocate()
        if old and old != wg_public_key:
            self._wg("set", self.cfg.interface, "peer", old, "remove")
        self._wg("set", self.cfg.interface, "peer", wg_public_key, "allowed-ips", f"{address}/32")
        self.db.update("devices", "id", device["id"], {"wg_public_key": wg_public_key, "wg_address": address})
        return {
            "interface": {"addresses": [f"{address}/32"], "dns": self.cfg.dns},
            "peer": {
                "public_key": self.server_public_key(),
                "endpoint": self.cfg.endpoint,
                "allowed_ips": self.cfg.allowed_ips or [str(self.net)],
                "persistent_keepalive": self.cfg.persistent_keepalive,
            },
            "home_ssids": self.cfg.home_ssids,
        }

    def revoke(self, device: dict[str, Any]) -> None:
        if self.cfg.provider == "pool" and device.get("wg_public_key"):
            for name, conf in self.pool().items():
                if conf["_pub"]["key"] == device["wg_public_key"]:
                    # The removed phone still has this key, so it must not go to anyone else.
                    self.vault.delete(name)
                    log.warning("VPN pool: %s retired with its device; delete that client on the Firewalla "
                                "and drop a new one in as %s", name, name)
        elif device.get("wg_public_key"):
            try:
                self._wg("set", self.cfg.interface, "peer", device["wg_public_key"], "remove")
            except VpnError as exc:
                log.warning("could not remove peer: %s", exc)
        self.db.update("devices", "id", device["id"], {"wg_public_key": None, "wg_address": None})
