"""The Hub object: wires config, vault, database, adapters and services."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from .actions import ActionRunner
from .assistant import Assistant
from .alerts import Alerts
from .adapters.cameras import Frigate
from .settings import PersonSettings
from .smart import Smart
from .adapters.family import FamilyBoard, Homebox, Mealie, ShowPlayer
from .adapters.firewalla import Firewalla
from .adapters.homeassistant import HomeAssistant
from .adapters.media import Arr, Jellyseerr, Plex
from .adapters.trips import Trek
from .adapters.truenas import TrueNAS
from .appupdate import AppUpdates
from .config import Config
from .crypto import TokenIssuer
from .db import DB
from .downloads import Downloads
from .sends import Sends
from .youtube import YouTube
from .events import EventBus
from .home import HomeModel
from .household import Household
from .notify import Notifier
from .policy import Policy, Subject
from .push import APNs
from .onepassword import OnePasswordSync
from .kitchen import Kitchen
from .bambu import Workshop
from .bambu_cloud import BambuCloud
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
        self.trek = Trek(config.trek, vault)
        self.show_player = ShowPlayer(config.show_player, vault)
        self.firewalla = Firewalla(config.firewalla, vault, db)
        self.apns = APNs(config.apns, vault)
        self.onepassword = OnePasswordSync(config.onepassword, vault)
        self.vpn = Vpn(config.vpn, vault, db)
        self.home = HomeModel(self)
        self.app_update = AppUpdates(config.app_update, vault,
                                     Path(db._path).parent / "app_update.json" if db._path != ":memory:" else None)
        self.app_update.on_change = lambda info: self.bus.publish({"type": "app_update", "data": info})
        self.downloads = Downloads(self, (Path(db._path).parent if db._path != ":memory:" else Path("/tmp")) / "downloads")
        data = Path(db._path).parent if db._path != ":memory:" else Path("/tmp")
        self.youtube = YouTube(self, data / "youtube")
        self.sends = Sends(self)
        self.notify = Notifier(self)
        self.alerts = Alerts(self)
        self.actions = ActionRunner(self)
        self.timers = Timers(self)
        self.kitchen = Kitchen(self)
        self.bambu_cloud = BambuCloud(self)
        self.workshop = Workshop(self, config.printers, data / "printers")
        self.smart = Smart(self)
        self.settings = PersonSettings(self)
        self.assistant = Assistant(self)
        self.household = Household(self)
        self._home_nets = [ipaddress.ip_network(n, strict=False) for n in config.hub.home_networks]
        self._proxies = [ipaddress.ip_network(n, strict=False) for n in config.hub.trusted_proxies]
        self.started_at = time.time()

    @property
    def adapters(self) -> list[Any]:
        return [self.ha, self.frigate, self.plex, self.jellyseerr, self.sonarr, self.radarr, self.truenas, self.mealie,
                self.homebox, self.family_board, self.trek, self.show_player, self.firewalla]

    def health(self) -> list[dict[str, Any]]:
        return [a.health() for a in self.adapters] + self.workshop.health()

    # -- network position -------------------------------------------------
    def client_ip(self, peer: str | None, forwarded_for: str | None) -> str | None:
        """The caller's address. X-Forwarded-For is believed only from a listed proxy."""
        if not peer:
            return None
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return None
        if forwarded_for and self._trusted_proxy(ip):
            # Walk from the right: each trusted proxy appends the address it saw, so the
            # first untrusted entry from the right is the real caller. Entries further left
            # came from the caller itself and can say anything (a stranger could claim
            # 10.0.0.5 and look like they're at home).
            for part in reversed([p.strip() for p in forwarded_for.split(",")]):
                try:
                    hop = ipaddress.ip_address(part)
                except ValueError:
                    return None
                if not self._trusted_proxy(hop):
                    return str(hop)
            return None
        return str(ip)

    def _trusted_proxy(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(ip in n for n in self._proxies)

    def is_home(self, ip: str | None) -> bool:
        if not ip:
            return False
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback:
            return False
        # A phone on the hub's own WireGuard tunnel is away, not home.
        if self.vpn.is_tunnel_address(addr):
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

    def subject_for_person(self, person: dict[str, Any], at_home: bool = True, device_id: str | None = None) -> Subject:
        """What a person may do, without a device: "View as", and a parent editing a child's favorites."""
        return Subject(
            person_id=person["id"], role=person["role"], areas=person.get("areas") or [],
            shared_areas=person.get("shared_areas") or [], grants=person.get("grants") or [],
            denies=person.get("denies") or [], tv_device=person.get("tv_device"), at_home=at_home,
            device_id=device_id, name=person["name"],
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

    SEED_FIELDS = ("ui_style", "areas", "shared_areas", "grants", "denies", "tv_device", "presence_entity")

    def seed_people(self) -> list[str]:
        """See HubSettings.seed_people. Returns the ids of the people it created."""
        from .policy import ROLES

        have = {(p.get("name") or "").strip().lower() for p in self.db.people()}
        made = []
        for entry in self.config.hub.seed_people or []:
            name = str(entry.get("name") or "").strip()
            role = entry.get("role")
            if not name or role not in ROLES or role in ("admin", "pending"):
                log.warning("seed_people: skipping %r (needs a name and a role other than admin/pending)", entry)
                continue
            if name.lower() in have:
                continue
            extra = {k: entry[k] for k in self.SEED_FIELDS if entry.get(k) is not None}
            if extra.get("ui_style") not in (None, "standard", "kid", "little"):
                extra.pop("ui_style")
            p = self.db.add_person(name, role, **extra)
            self.db.log("people.create", "ok", target=p["id"], params={"role": role}, source="config")
            log.info("seed_people: added %s (%s)", name, role)
            have.add(name.lower())
            made.append(p["id"])
        return made

    async def start(self) -> None:
        self.bootstrap_admin()
        self.seed_people()
        if self.onepassword.enabled:
            await self.onepassword.sync_safely()
            self.onepassword.start()
        try:
            self.vpn.bring_up()
        except Exception as exc:  # noqa: BLE001
            log.error("VPN did not come up: %s", exc)
        self.home.start()
        self.timers.start()
        self.youtube.start()
        self.sends.start()
        self.kitchen.start()
        self.workshop.start()
        self.smart.start()
        self.alerts.start()
        self.household.start()
        from .dropbox import watch

        self._dropbox = asyncio.create_task(watch(self.vault))
        self._frigate = asyncio.create_task(self._watch_frigate()) if self.frigate.enabled else None
        self._prober = asyncio.create_task(self._probe_services())
        self.app_update.start()

    async def probe_once(self) -> None:
        """Touch each enabled service so the status page shows a real answer, not "never checked"."""
        async def one(a: Any) -> None:
            try:
                await asyncio.wait_for(a.ping(), timeout=20)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                a.last_error = f"{a.title} did not answer"
            except Exception:  # noqa: BLE001
                pass  # the adapter recorded why

        await asyncio.gather(*(one(a) for a in self.adapters if a.enabled and hasattr(a, "ping")))

    async def _probe_services(self, interval: float = 120.0) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.probe_once()
                self.downloads.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("service probe: %s", exc)
            await asyncio.sleep(interval)

    async def _watch_frigate(self, interval: float = 10.0) -> None:
        """Keep the camera list fresh and send each new Frigate event to the app (SSE camera_event)."""
        from .routes.cameras import event_item

        seen: set[str] = set()
        after: float | None = None
        while True:
            try:
                await self.frigate.camera_info()
                rows = await self.frigate.events(limit=50)
                for e in sorted(rows, key=lambda r: r.get("start_time") or 0):
                    cam = e.get("camera")
                    item = event_item(self, e)
                    prev = self.frigate.last_event.get(cam)
                    if cam and (prev is None or (prev.get("start") or "") <= (item["start"] or "")):
                        self.frigate.last_event[cam] = item
                    if after is not None and e["id"] not in seen and (e.get("start_time") or 0) >= after:
                        self.bus.publish({"type": "camera_event", "data": item})
                        try:
                            await self.alerts.on_frigate(e)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("camera alert failed: %s", exc)
                    seen.add(e["id"])
                if rows:
                    after = max([after or 0] + [r.get("start_time") or 0 for r in rows])
                elif after is None:
                    after = time.time()
                if len(seen) > 2000:
                    seen = {r["id"] for r in rows}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("Frigate watch: %s", exc)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        await self.home.stop()
        await self.timers.stop()
        await self.youtube.stop()
        await self.sends.stop()
        await self.kitchen.stop()
        await self.workshop.stop()
        await self.smart.stop()
        await self.alerts.stop()
        await self.household.stop()
        await self.onepassword.stop()
        if getattr(self, "_dropbox", None):
            self._dropbox.cancel()
        if getattr(self, "_prober", None):
            self._prober.cancel()
        if getattr(self, "_frigate", None):
            self._frigate.cancel()
        for a in self.adapters:
            await a.close()
        await self.apns.close()
        await self.app_update.stop()
