"""Built-in VPN: WireGuard peers handed out at enrollment.

The phone makes its own key pair and sends only its public key; the private
key stays in the iOS keychain. The hub allocates a tunnel address, adds the
peer to its WireGuard interface and returns the settings the app's tunnel
extension needs. Revoking a device removes its peer.

Providers:
  none    VPN off. /v1/vpn/config answers 503.
  wg-cli  The hub owns a kernel WireGuard interface (container needs
          NET_ADMIN and host networking) and manages peers with `wg set`.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import subprocess
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from .config import VpnConfig
from .db import DB
from .vault import Vault

log = logging.getLogger("hub.vpn")


class VpnError(RuntimeError):
    pass


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

    @property
    def enabled(self) -> bool:
        return self.cfg.provider != "none" and bool(self.cfg.endpoint)

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

    def config_for(self, device: dict[str, Any], wg_public_key: str) -> dict[str, Any]:
        if not self.enabled:
            raise VpnError("the built-in VPN is not set up yet")
        if not valid_wg_key(wg_public_key):
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
        if device.get("wg_public_key"):
            try:
                self._wg("set", self.cfg.interface, "peer", device["wg_public_key"], "remove")
            except VpnError as exc:
                log.warning("could not remove peer: %s", exc)
        self.db.update("devices", "id", device["id"], {"wg_public_key": None, "wg_address": None})
