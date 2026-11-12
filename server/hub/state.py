"""The Hub object: wires config, vault, database, adapters and services."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from typing import Any

from .actions import ActionRunner
from .adapters.cameras import Frigate
from .adapters.family import FamilyBoard, Homebox, Mealie, ShowPlayer
from .adapters.firewalla import Firewalla
from .adapters.homeassistant import HomeAssistant
from .adapters.media import Arr, Jellyseerr, Plex
from .adapters.truenas import TrueNAS
from .config import Config
from .crypto import TokenIssuer
from .db import DB
from .events import EventBus
from .home import HomeModel
from .notify import Notifier
from .policy import Policy, Subject
from .push import APNs
from .onepassword import OnePasswordSync
from .timers import Timers
from .vault import Vault
from .vpn import Vpn

log = logging.getLogger("hub")

SIGNING_SECRET = "hub.token_signing_key"


class Hub:
    def __init__(self, config: Config, vault: Vault, db: DB):
        self.config = config
        self.vault = vault
        self.db = db
        if not vault.has(SIGNING_SECRET):
            vault.set(SIGNING_SECRET, secrets.token_urlsafe(48))
        self.tokens = TokenIssuer(str(vault.require(SIGNING_SECRET)), config.hub.id, config.hub.access_token_ttl_s)
        cp = config.child_policy
        self.policy = Policy(timezone=config.hub.timezone, bedtime=cp.bedtime,
                             climate_min=cp.climate_min, climate_max=cp.climate_max)
        self.bus = EventBus()
        self.ha = HomeAssistant(config.home_assistant, vault)
        self.frigate = Frigate(config.frigate, vault)
        self.plex = Plex(config.plex, vault)
        self.jellyseerr = Jellyseerr(config.jellyseerr, vault)
        self.sonarr = Arr("sonarr", config.sonarr, vault)
        self.radarr = Arr("radarr", config.radarr, vault)
        self.truenas = TrueNAS(config.truenas, vault)
        self.mealie = Mealie(config.mealie, vault)
        self.homebox = Homebox(config.homebox, vault)
        self.family_board = FamilyBoard(config.family_board, vault)
        self.show_player = ShowPlayer(config.show_player, vault)
        self.firewalla = Firewalla(config.firewalla, vault, db)
        self.apns = APNs(config.apns, vault)
        self.onepassword = OnePasswordSync(config.onepassword, vault)
        self.vpn = Vpn(config.vpn, vault, db)
        self.home = HomeModel(self)
        self.notify = Notifier(self)
        self.actions = ActionRunner(self)
        self.timers = Timers(self)
        self._home_nets = [ipaddress.ip_network(n, strict=False) for n in config.hub.home_networks]
        self._proxies = [ipaddress.ip_network(n, strict=False) for n in config.hub.trusted_proxies]
        self.started_at = time.time()

    @property
    def adapters(self) -> list[Any]:
        return [self.ha, self.frigate, self.plex, self.jellyseerr, self.sonarr, self.radarr, self.truenas, self.mealie,
                self.homebox, self.family_board, self.show_player, self.firewalla]

    def health(self) -> list[dict[str, Any]]:
        return [a.health() for a in self.adapters]

    # -- network position -------------------------------------------------
    def client_ip(self, peer: str | None, forwarded_for: str | None) -> str | None:
        """The caller's address. X-Forwarded-For is believed only from a listed proxy."""
        if not peer:
            return None
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return None
        if forwarded_for and any(ip in n for n in self._proxies):
            first = forwarded_for.split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(first))
            except ValueError:
                return None
        return str(ip)

    def is_home(self, ip: str | None) -> bool:
        if not ip:
            return False
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback:
            return False
        # A phone on the hub's own WireGuard tunnel is away, not home.
        if addr in self.vpn.net:
            return False
        return any(addr in n for n in self._home_nets)

    # -- identity --------------------------------------------------------
    def subject_for(self, device: dict[str, Any], at_home: bool) -> Subject:
        person = self.db.person(device.get("person_id")) if device.get("status") == "active" else None
        if person is None:
            return Subject(person_id=None, role="pending", at_home=at_home, device_id=device["id"], name=device.get("name"))
        if person.get("expires_at") and person["expires_at"] < time.time():
            return Subject(person_id=None, role="pending", at_home=at_home, device_id=device["id"])
        return Subject(
            person_id=person["id"], role=person["role"], areas=person.get("areas") or [],
            shared_areas=person.get("shared_areas") or [], grants=person.get("grants") or [],
            denies=person.get("denies") or [], tv_device=person.get("tv_device"), at_home=at_home,
            device_id=device["id"], name=f"{person['name']}'s {device.get('model') or 'device'}",
        )

    def revoke_device(self, device: dict[str, Any], reason: str = "revoked") -> None:
        self.vpn.revoke(device)
        self.db.update("devices", "id", device["id"], {"status": "revoked", "apns_token": None})
        self.db.execute("DELETE FROM nonces WHERE device_id=?", [device["id"]])
        self.db.log("device.revoke", "ok", device_id=device["id"], detail=reason, source="hub")

    # -- lifecycle -------------------------------------------------------
    def bootstrap_admin(self) -> str | None:
        """See HubSettings.bootstrap_admin. Returns the device id it promoted."""
        b = self.config.hub.bootstrap_admin
        if not b or not b.get("name") or self.db.people_with_role("admin"):
            return None
        after = float(b.get("enrolled_after", 0))
        pending = [d for d in self.db.devices() if d["status"] == "pending" and d["created_at"] >= after]
        if len(pending) != 1:
            log.warning("bootstrap_admin: %d pending phones since the cut-off; need exactly one", len(pending))
            return None
        d = pending[0]
        p = self.db.add_person(str(b["name"]), "admin")
        self.db.update("devices", "id", d["id"], {"person_id": p["id"], "status": "active"})
        self.db.log("people.bootstrap_admin", "ok", target=p["id"], params={"device": d["id"]}, source="config")
        log.info("bootstrap_admin: %s is admin on %s", b["name"], d["name"])
        return d["id"]

    async def start(self) -> None:
        self.bootstrap_admin()
        if self.onepassword.enabled:
            await self.onepassword.sync_safely()
            self.onepassword.start()
        try:
            self.vpn.bring_up()
        except Exception as exc:  # noqa: BLE001
            log.error("VPN did not come up: %s", exc)
        self.home.start()
        self.timers.start()
        from .dropbox import watch

        self._dropbox = asyncio.create_task(watch(self.vault))

    async def stop(self) -> None:
        await self.home.stop()
        await self.timers.stop()
        await self.onepassword.stop()
        if getattr(self, "_dropbox", None):
            self._dropbox.cancel()
        for a in self.adapters:
            await a.close()
        await self.apns.close()
