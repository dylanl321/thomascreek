"""The home model: one merged, role-filtered picture of the house.

Home Assistant states are polled (and diffed into live events); slower sources
(NAS, meals, calendar) refresh on their own cadence. Every device carries
`updated_at` and `available` so the app can show freshness honestly.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .policy import Subject, Target

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.home")

DOMAIN_KIND = {
    "light": "light", "fan": "fan", "switch": "switch", "climate": "climate", "lock": "lock",
    "camera": "camera", "vacuum": "vacuum", "valve": "valve", "water_heater": "shower",
}

KIND_ACTIONS = {
    "light": ["light.set"], "fan": ["fan.set"], "switch": ["switch.set"], "climate": ["climate.set"],
    "lock": ["lock.set"], "garage": ["garage.set"], "media": ["media.command", "media.volume", "media.play"],
    "speaker": ["media.command", "media.volume", "speaker.group"], "vacuum": ["vacuum.start"],
    "valve": ["valve.run"], "shower": ["shower.preheat"], "oven": ["oven.set", "oven.timer"], "camera": ["camera.view"],
}

SYMBOLS = {
    "kitchen": "fork.knife", "living": "sofa", "bed": "bed.double", "master": "bed.double", "garage": "car",
    "office": "desktopcomputer", "basement": "stairs", "sunroom": "sun.max", "workshop": "hammer",
    "apartment": "building", "porch": "leaf", "bath": "shower", "guest": "person.crop.circle",
}


def iso(ts: float | str | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _symbol(name: str) -> str:
    low = name.lower()
    for key, sym in SYMBOLS.items():
        if key in low:
            return sym
    return "square.split.2x2"


# Sprinkler zones that Home Assistant exposes as plain switches (Rachio, Rain Bird, OpenSprinkler):
# they show as valves so they run for a set time. The controller's schedules, rain delay and standby
# switches stay switches.
IRRIGATION = re.compile(r"irrigation|sprinkler|rachio|rain ?bird|opensprinkler", re.I)
NOT_A_ZONE = re.compile(r"schedule|rain ?delay|standby|enabled?$", re.I)


def is_irrigation_zone(state: dict[str, Any]) -> bool:
    name = str(state.get("attributes", {}).get("friendly_name") or "")
    eid = state["entity_id"]
    return bool(IRRIGATION.search(eid) or IRRIGATION.search(name)) and not (
        NOT_A_ZONE.search(name) or NOT_A_ZONE.search(eid.replace("_", " ")))


def kind_for(state: dict[str, Any]) -> str | None:
    eid: str = state["entity_id"]
    domain = eid.split(".", 1)[0]
    attrs = state.get("attributes", {})
    if domain == "cover":
        return "garage" if attrs.get("device_class") in ("garage", "gate") else None
    if domain == "media_player":
        return "speaker" if "group_members" in attrs else "media"
    if domain == "sensor" and attrs.get("device_class") in ("temperature", "humidity", "moisture"):
        return "sensor"
    if domain == "switch" and is_irrigation_zone(state):
        return "valve"
    if domain == "water_heater":
        from .catalog import is_fridge
        from .kitchen import is_oven

        if is_oven(state):
            return "oven"
        # GE reports the fridge and freezer as water heaters: they live on the Appliances page, not as showers.
        return None if is_fridge(state) else "shower"
    return DOMAIN_KIND.get(domain)


# media_player supported_features bits (MediaPlayerEntityFeature)
MP_PAUSE, MP_VOLUME_SET, MP_VOLUME_MUTE, MP_PREV, MP_NEXT = 1, 4, 8, 16, 32
MP_TURN_ON, MP_TURN_OFF, MP_VOLUME_STEP, MP_SELECT_SOURCE, MP_STOP, MP_PLAY = 128, 256, 1024, 2048, 4096, 16384
# Remote buttons per integration: hub name -> (how, command). "remote" = remote.send_command
# on the linked remote entity; anything else is "<domain>.<service>:<field>" on the media player.
REMOTE_KEYS: dict[str, dict[str, str]] = {
    "apple_tv": {"up": "up", "down": "down", "left": "left", "right": "right", "select": "select",
                 "back": "menu", "home": "home"},
    "androidtv_remote": {"up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT",
                         "select": "DPAD_CENTER", "back": "BACK", "home": "HOME"},
    "roku": {"up": "up", "down": "down", "left": "left", "right": "right", "select": "select", "back": "back",
             "home": "home"},
    "samsungtv": {"up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
                  "select": "KEY_ENTER", "back": "KEY_RETURN", "home": "KEY_HOME"},
    "braviatv": {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "select": "Confirm",
                 "back": "Return", "home": "Home"},
    "webostv": {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "select": "ENTER",
                "back": "BACK", "home": "HOME"},
    "androidtv": {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "select": "CENTER",
                  "back": "BACK", "home": "HOME"},
}
# Integrations whose buttons go to a service on the media player itself, not a remote entity.
PLAYER_KEY_SERVICES = {"webostv": ("webostv", "button", "button"), "androidtv": ("androidtv", "adb_command", "command")}
DPAD = ("up", "down", "left", "right", "select", "back", "home")


def media_commands(st: dict[str, Any], link: dict[str, Any] | None) -> list[str]:
    """The remote buttons this TV or player really supports through Home Assistant."""
    f = int(st.get("attributes", {}).get("supported_features") or 0)
    cmds = []
    if f & (MP_TURN_ON | MP_TURN_OFF):
        cmds.append("power")
    if f & MP_PLAY:
        cmds.append("play")
    if f & MP_PAUSE:
        cmds.append("pause")
    if f & MP_STOP:
        cmds.append("stop")
    if f & MP_PREV:
        cmds.append("prev")
    if f & MP_NEXT:
        cmds.append("next")
    if f & (MP_VOLUME_STEP | MP_VOLUME_SET):
        cmds += ["vol_up", "vol_down"]
    if f & MP_VOLUME_SET:
        cmds.append("volume")
    if f & MP_VOLUME_MUTE:
        cmds.append("mute")
    if f & MP_SELECT_SOURCE and st.get("attributes", {}).get("source_list"):
        cmds.append("source")
    link = link or {}
    plat = link.get("platform")
    if plat in REMOTE_KEYS and (link.get("remote") or plat in PLAYER_KEY_SERVICES):
        cmds += list(DPAD)
    return cmds


def _num(v: Any) -> float | None:
    """HA attributes are numbers, but some integrations send them as text."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def device_state(kind: str, st: dict[str, Any], link: dict[str, Any] | None = None) -> dict[str, Any]:
    s = st.get("state")
    a = st.get("attributes", {})
    out: dict[str, Any] = {}
    if kind in ("light", "fan", "switch", "valve"):
        out["on"] = s == "on" or s == "open"
        if "brightness" in a and a["brightness"] is not None:
            out["brightness"] = round(a["brightness"] * 100 / 255)
        if a.get("rgb_color"):
            r, g, b = a["rgb_color"]
            out["color"] = f"#{r:02x}{g:02x}{b:02x}"
        if kind == "fan" and a.get("percentage") is not None:
            out["speed"] = a["percentage"]
    elif kind == "climate":
        low, high = _num(a.get("target_temp_low")), _num(a.get("target_temp_high"))
        target = _num(a.get("temperature"))
        out.update(current_temp=_num(a.get("current_temperature")), target_temp=target,
                   target_low=low, target_high=high, humidity=_num(a.get("current_humidity") or a.get("humidity")),
                   hvac=s, hvac_action=a.get("hvac_action"), unit=a.get("temperature_unit"),
                   on=s not in ("off", "unavailable", "unknown"), available=s not in ("unavailable", "unknown"))
        if target is None and low is not None and high is not None:
            # Heat/cool keeps two set points; one "target" is the middle of the range.
            out["target_temp"] = round((low + high) / 2, 1)
    elif kind == "lock":
        out["locked"] = s == "locked"
    elif kind == "garage":
        out["open"] = s in ("open", "opening")
        out["position"] = a.get("current_position")
    elif kind in ("media", "speaker"):
        out.update(on=s not in ("off", "standby", "unavailable"), playing=s == "playing",
                   volume=round(a["volume_level"] * 100) if a.get("volume_level") is not None else None,
                   muted=bool(a.get("is_volume_muted")) if "is_volume_muted" in a else None,
                   source=a.get("source") or a.get("app_name"),
                   sources=list(a.get("source_list") or []),
                   commands=media_commands(st, link))
    elif kind == "vacuum":
        out["value"] = s
    elif kind == "sensor":
        out.update(value=s, unit=a.get("unit_of_measurement"))
    elif kind == "oven":
        from .kitchen import oven_state

        o = oven_state(st)
        out.update(on=o["on"], current_temp=o["current_temp"], target_temp=o["target_temp"], mode=o["mode"],
                   modes=o["modes"], remote_ready=o["remote_ready"], available=o["available"])
    elif kind == "shower":
        out.update(current_temp=a.get("current_temperature"), target_temp=a.get("temperature"), on=s not in ("off",))
    return out


class HomeModel:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.states: dict[str, dict[str, Any]] = {}
        self.areas: dict[str, dict[str, Any]] = {}
        self.floors: list[dict[str, Any]] = []
        self.entity_area: dict[str, str] = {}
        # media_player -> {"remote": remote entity or None, "platform": integration}
        self.media_links: dict[str, dict[str, Any]] = {}
        # Home Assistant's device registry: device id -> {name, manufacturer, model, integration, entities}
        self.ha_devices: dict[str, dict[str, Any]] = {}
        self.entity_device: dict[str, str] = {}
        self.platforms: dict[str, str] = {}
        # Rooms as Home Assistant has them, before the layout's moves and borrowed TV rooms.
        self.ha_area: dict[str, str] | None = None
        self.base_areas: dict[str, dict[str, Any]] = {}
        # One media player per real TV or box; None until worked out (then every player is listed).
        self.screens: set[str] | None = None
        from .catalog import HomeLayout

        self.layout = HomeLayout(hub)
        self.systems: dict[str, Any] | None = None
        self.dinner: dict[str, Any] | None = None
        self.calendar: list[dict[str, Any]] = []
        self.ha_ok_at: float | None = None
        self.slow_at = 0.0
        self._task: asyncio.Task | None = None

    # -- refresh loop ------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh failed: %s", exc)
            await asyncio.sleep(self.hub.config.hub.poll_interval_s)

    async def refresh(self) -> None:
        ha = self.hub.ha
        was_ok = self.ha_ok_at is not None and ha.last_error is None
        if ha.enabled:
            try:
                if not self.areas or time.time() - self.slow_at > 300:
                    await self._refresh_areas()
                states = await ha.states()
                self._apply_states(states)
                self.ha_ok_at = time.time()
            except Exception as exc:  # noqa: BLE001
                log.warning("Home Assistant read failed: %s", exc)
        if time.time() - self.slow_at > 120:
            self.slow_at = time.time()
            await self._refresh_slow()
        now_ok = ha.last_error is None and self.ha_ok_at is not None
        if was_ok != now_ok:
            self.hub.bus.publish({"type": "status", "data": {}})

    def mark_nas_dirty(self) -> None:
        """Re-read the NAS on the next loop, after an app was started, stopped or updated."""
        self.slow_at = 0.0

    async def _nas_crash_notice(self, before: dict[str, Any] | None, now: dict[str, Any]) -> None:
        """Tell the admins when an app newly crashes (not on the first read after the hub starts)."""
        if before is None:
            return
        was = set((before.get("apps") or {}).get("crashed_names") or [])
        for name in (now.get("apps") or {}).get("crashed_names") or []:
            if name in was:
                continue
            await self.hub.notify.send(
                title=f"{name} crashed on the NAS", body="Open it to see the logs or restart it.",
                cat="systems", level="maintenance", roles=["admin"], ref=f"thomascreek://nas/app/{name}",
                group_key=f"nas.crash.{name}",
            )

    async def _refresh_areas(self) -> None:
        areas = await self.hub.ha.areas()
        floors = await self.hub.ha.floors()
        links = await self.hub.ha.media_links()
        reg = await self.hub.ha.devices()
        if reg.get("devices"):
            self.ha_devices = {d["id"]: d for d in reg["devices"] if d.get("id")}
            self.entity_device = {e: d["id"] for d in reg["devices"] for e in d.get("entities") or []}
            self.platforms = {p[0]: p[1] for p in reg.get("platforms") or [] if isinstance(p, list) and len(p) == 2}
        extra = self.hub.config.home_assistant.extra.get("remotes", {})
        self.media_links = {m["entity_id"]: {"remote": extra.get(m["entity_id"]) or m.get("remote"),
                                             "platform": m.get("platform")} for m in links if m.get("entity_id")}
        overrides = self.hub.config.areas
        self.areas = {}
        self.entity_area = {}
        for a in areas:
            o = overrides.get(a["id"])
            self.areas[a["id"]] = {
                "id": a["id"],
                "name": (o.name if o and o.name else a["name"]),
                "floor_id": (o.floor if o and o.floor else a.get("floor")),
                "symbol": (o.symbol if o and o.symbol else _symbol(a["name"])),
                "shared": bool(o.shared) if o else False,
            }
            for e in a.get("entities", []):
                self.entity_area.setdefault(e, a["id"])
        for area_id, o in overrides.items():
            if area_id not in self.areas:
                self.areas[area_id] = {"id": area_id, "name": o.name or area_id, "floor_id": o.floor,
                                       "symbol": o.symbol or _symbol(o.name or area_id), "shared": o.shared}
            for e in o.entities:
                self.entity_area[e] = area_id
        self.floors = floors or self.hub.config.floors
        self.base_areas = {k: dict(v) for k, v in self.areas.items()}
        self.ha_area = dict(self.entity_area)
        self.recompute()

    def recompute(self) -> None:
        """Apply the TV picks and the admin's layout (moves, room names and icons) to the raw HA rooms."""
        from .catalog import pick_screens

        if self.ha_area is None:
            self.ha_area = dict(self.entity_area)
            self.base_areas = {k: dict(v) for k, v in self.areas.items()}
        ea = dict(self.ha_area)
        keep = {p["tv_device"] for p in self.hub.db.people() if p.get("tv_device")}
        keep |= {e for e in self.hub.config.favorites if e.startswith("media_player.")}
        keep &= set(self.states)
        if self.states:
            self.screens, borrowed = pick_screens(self.states, self.platforms, self.entity_device, self.ha_devices,
                                                  ea, keep)
            for e, a in borrowed.items():
                if a in self.base_areas:
                    ea.setdefault(e, a)
        for e, a in self.layout.moves.items():
            if a in self.base_areas:
                ea[e] = a
        self.entity_area = ea
        areas = {}
        for k, v in self.base_areas.items():
            r = self.layout.room(k)
            areas[k] = {**v, "name": r.get("name") or v["name"], "symbol": r.get("symbol") or v["symbol"],
                        "hidden": bool(r.get("hidden"))}
        self.areas = areas

    async def _refresh_slow(self) -> None:
        if self.hub.truenas.enabled:
            try:
                before = self.systems
                self.systems = await self.hub.truenas.summary()
                await self._nas_crash_notice(before, self.systems)
            except Exception as exc:  # noqa: BLE001
                log.info("NAS summary failed: %s", exc)
        if self.hub.mealie.enabled:
            try:
                today = await self.hub.mealie.today()
                dinner = next((m for m in today if m.get("meal") == "dinner"), today[0] if today else None)
                self.dinner = ({"title": dinner["title"], "image_url": dinner.get("image_url"), "slug": dinner.get("slug")}
                               if dinner else None)
            except Exception as exc:  # noqa: BLE001
                log.info("meal read failed: %s", exc)
        if self.hub.family_board.enabled:
            try:
                self.calendar = (await self.hub.family_board.calendar(1, self.hub.config.hub.timezone))["events"]
            except Exception as exc:  # noqa: BLE001
                log.info("calendar read failed: %s", exc)

    def _apply_states(self, states: list[dict[str, Any]]) -> None:
        fresh = {s["entity_id"]: s for s in states}
        changed = [eid for eid, s in fresh.items()
                   if eid in self.states and (self.states[eid].get("last_updated") != s.get("last_updated"))]
        first = not self.states
        before = {e for e in self.states if e.startswith("media_player.")}, \
            {e for e, st in self.states.items() if e.startswith("media_player.") and st.get("state") == "unavailable"}
        self.states = fresh
        after = {e for e in fresh if e.startswith("media_player.")}, \
            {e for e, st in fresh.items() if e.startswith("media_player.") and st.get("state") == "unavailable"}
        if first or before != after or self.screens is None:
            self.recompute()
        if first:
            return
        for eid in changed:
            if eid in self.entity_area or eid in self.hub.config.favorites:
                self.hub.bus.publish({"type": "device", "entity_id": eid})

    # -- shaping for one person -------------------------------------------
    def visible_areas(self, s: Subject) -> set[str] | None:
        """None means every area."""
        if s.role in ("admin", "adult", "tablet"):
            return None
        return set(s.areas) | set(s.shared_areas)

    def listed(self, eid: str, s: Subject | None = None) -> bool:
        """False for devices the admin hid and for media players that aren't the chosen copy of a TV."""
        if eid in self.layout.hidden:
            return False
        if eid.startswith("media_player.") and self.screens is not None and eid not in self.screens:
            st = self.states.get(eid) or {}
            if "group_members" not in st.get("attributes", {}) and not (s and s.tv_device == eid):
                return False
        return True

    def device(self, eid: str, s: Subject, include_hidden: bool = False) -> dict[str, Any] | None:
        st = self.states.get(eid)
        if st is None:
            return None
        kind = kind_for(st)
        if kind is None:
            return None
        if not self.listed(eid, s) and not (include_hidden and eid in self.layout.hidden):
            return None
        area = self.entity_area.get(eid)
        vis = self.visible_areas(s)
        if vis is not None and area not in vis and not (s.tv_device and eid == s.tv_device):
            return None
        policy = self.hub.policy
        target = Target(entity_id=eid, area=area, kind=kind)
        actions = [a for a in KIND_ACTIONS.get(kind, []) if policy.decide(s, a, target, {}).allowed]
        if kind == "camera" and not actions:
            return None
        if kind == "garage" and not policy.has(s, "doors"):
            return None
        if kind == "lock" and not policy.has(s, "doors"):
            return None
        state = device_state(kind, st, self.media_links.get(eid))
        if kind == "valve" and state.get("on"):
            left = self.hub.actions.valve_remaining(eid)
            if left is not None:
                state["remaining_s"] = left
        out = {
            "id": eid,
            "kind": kind,
            "name": st.get("attributes", {}).get("friendly_name", eid),
            "room_id": area,
            "state": state,
            "updated_at": st.get("last_updated"),
            "available": st.get("state") not in ("unavailable", "unknown"),
            "actions": actions,
        }
        if include_hidden:
            out["hidden"] = eid in self.layout.hidden
        return out

    def snapshot(self, s: Subject) -> dict[str, Any]:
        cfg = self.hub.config
        policy = self.hub.policy
        starred = set(self.hub.settings.favorite_ids(s, "room"))
        devices = [d for eid in self.states if (d := self.device(eid, s)) is not None and d["room_id"]]
        vis = self.visible_areas(s)
        rooms = []
        for a in self.areas.values():
            if vis is not None and a["id"] not in vis:
                continue
            if a.get("hidden"):
                continue
            in_room = [d for d in devices if d["room_id"] == a["id"]]
            scenes = self.scenes(a["id"], s)
            if not in_room and not scenes:
                continue
            rooms.append({
                "id": a["id"], "name": a["name"], "floor_id": a["floor_id"], "symbol": a["symbol"],
                "summary": self._summary(in_room), "favorite": a["id"] in starred,
                "device_ids": self.layout.order(a["id"], [d["id"] for d in in_room]),
                "scenes": scenes,
            })
        room_ids = {r["id"] for r in rooms}
        floors = [{"id": f["id"], "name": f["name"], "room_ids": [r["id"] for r in rooms if r["floor_id"] == f["id"]]}
                  for f in self.floors]
        floors = [f for f in floors if f["room_ids"]]

        cameras = []
        if policy.has(s, "cameras") and self.hub.frigate.enabled:
            from .routes.cameras import camera_list

            cameras = camera_list(self.hub)
        elif policy.has(s, "cameras"):
            for eid, st in self.states.items():
                if eid.startswith("camera."):
                    cameras.append({
                        "id": eid.split(".", 1)[1], "name": st.get("attributes", {}).get("friendly_name", eid),
                        "room_id": self.entity_area.get(eid), "snapshot_url": f"/v1/cameras/{eid.split('.', 1)[1]}/snapshot.jpg",
                        "updated_at": st.get("last_updated"),
                    })

        now_playing = []
        for d in devices:
            if d["kind"] in ("media", "speaker") and d["state"].get("playing"):
                a = self.states[d["id"]].get("attributes", {})
                now_playing.append({
                    "device_id": d["id"], "room_id": d["room_id"], "title": a.get("media_title"),
                    "subtitle": a.get("media_artist") or a.get("media_series_title") or a.get("app_name"),
                    "art_url": f"/v1/media/player-art/{d['id']}" if a.get("entity_picture") else None, "state": "playing",
                })

        modes = [{"id": m.id, "name": m.name, "symbol": m.symbol, "active": False}
                 for m in cfg.modes if s.role in m.roles]
        month = policy.now().month
        snap: dict[str, Any] = {
            "generated_at": iso(time.time()),
            "status": self.status(s, devices),
            "people": self.people(s),
            "modes": modes,
            "floors": floors,
            "rooms": rooms,
            "devices": [d for d in devices if d["room_id"] in room_ids],
            "cameras": cameras,
            "now_playing": now_playing,
            "today": {"events": self.calendar if policy.has(s, "family.view") else [],
                      "dinner": self.dinner},
            "seasonal": {"holiday_lights": month in (11, 12, 1)},
            "weather": self._weather_now(s),
            "smart": self._smart_cards(s),
        }
        if policy.has(s, "appliances"):
            snap["appliances"] = self.appliances()
        if self.hub.workshop.enabled and (policy.has(s, "appliances") or s.role == "child"):
            snap["printers"] = self.hub.workshop.summaries()
        if policy.has(s, "systems"):
            snap["systems"] = self.systems_card()
        return snap

    def scenes(self, area_id: str, s: Subject, include_hidden: bool = False) -> list[dict[str, Any]]:
        """The room's Home Assistant scenes this person may run, in the room's saved order."""
        out = []
        hidden = self.layout.hidden
        for eid, ar in self.entity_area.items():
            if ar != area_id or not eid.startswith("scene.") or eid not in self.states:
                continue
            if eid in hidden and not include_hidden:
                continue
            st = self.states[eid]
            if st.get("state") == "unavailable":
                continue
            if not self.hub.policy.decide(s, "scene.run", Target(entity_id=eid, area=area_id, kind="scene"), {}).allowed:
                continue
            item = {"id": eid, "name": st.get("attributes", {}).get("friendly_name") or eid,
                    "icon": st.get("attributes", {}).get("icon")}
            if include_hidden:
                item["hidden"] = eid in hidden
            out.append(item)
        order = self.layout.order(area_id, [x["id"] for x in out])
        by = {x["id"]: x for x in out}
        return [by[e] for e in order]

    def appliances(self, include_hidden: bool = False) -> list[dict[str, Any]]:
        """One card per real appliance (fridge, ovens, dishwasher, washer, dryer, vacuum, steam shower)."""
        from .catalog import appliance_card, appliance_type

        devs = self.ha_devices
        if not devs:  # no device registry: each appliance entity stands on its own
            devs = {e: {"id": e, "name": st.get("attributes", {}).get("friendly_name"), "entities": [e]}
                    for e, st in self.states.items() if e.startswith(("water_heater.", "vacuum."))}
        hidden = self.layout.hidden
        cards = []
        for d in devs.values():
            kind = appliance_type(d, self.states)
            if kind is None:
                continue
            key = f"device:{d['id']}" if self.ha_devices else d["id"]
            if key in hidden and not include_hidden:
                continue
            area = next((self.entity_area[e] for e in d.get("entities") or [] if e in self.entity_area), None)
            card = appliance_card(d, kind, self.states, area)
            card["id"] = key
            if include_hidden:
                card["hidden"] = key in hidden
            cards.append(card)
        order = {"oven": 0, "fridge": 1, "dishwasher": 2, "microwave": 3, "washer": 4, "dryer": 5, "vacuum": 6}
        cards.sort(key=lambda c: (not c["available"], order.get(c["type"], 9), c["name"]))
        return cards

    def _smart_cards(self, s: Subject) -> list[dict[str, Any]]:
        # Suggestions must never take Home down with them.
        try:
            return self.hub.smart.cards(s)
        except Exception as exc:  # noqa: BLE001
            log.warning("smart cards failed: %s", exc)
            return []

    def _summary(self, devices: list[dict[str, Any]]) -> str:
        lights_on = sum(1 for d in devices if d["kind"] == "light" and d["state"].get("on"))
        climate = next((d for d in devices if d["kind"] == "climate"), None)
        parts = []
        if lights_on:
            parts.append(f"{lights_on} light{'s' if lights_on != 1 else ''} on")
        if climate and climate["state"].get("current_temp") is not None:
            parts.append(f"{round(climate['state']['current_temp'])}°")
        playing = next((d for d in devices if d["kind"] in ("media", "speaker") and d["state"].get("playing")), None)
        if playing:
            parts.append("playing")
        return ", ".join(parts) or "All off"

    def people(self, s: Subject) -> list[dict[str, Any]]:
        out = []
        for p in self.hub.db.people():
            if p["role"] in ("tablet", "pending"):
                continue
            if s.role == "guest" and p["id"] != s.person_id:
                continue
            row: dict[str, Any] = {"id": p["id"], "name": p["name"], "role": p["role"], "avatar": p.get("avatar")}
            home = self.hub.household.presence(p)["home"]
            if home is not None:
                row["home"] = home
            out.append(row)
        return out

    def status(self, s: Subject, devices: list[dict[str, Any]]) -> dict[str, Any]:
        ha = self.hub.ha
        if ha.enabled and (self.ha_ok_at is None or ha.last_error):
            since = iso(self.ha_ok_at) if self.ha_ok_at else None
            line = "Can't reach the house controls" + (f" since {since}" if since else "")
            if s.is_child_like:
                line = "The house is napping. Try again in a bit."
            return {"level": "offline", "line": line, "since": since}
        issues = []
        now = time.time()
        for d in devices:
            if d["kind"] == "garage" and d["state"].get("open"):
                changed = self.states[d["id"]].get("last_changed")
                mins = int((now - _ts(changed)) / 60) if changed else None
                issues.append(f"{d['name']} open" + (f" {mins} min" if mins else ""))
        if s.role == "admin":
            for h in self.hub.health():
                if h["enabled"] and h["error"]:
                    issues.append(h["error"])
        if issues:
            return {"level": "attention", "line": "; ".join(issues[:3])}
        return {"level": "ok", "line": "Everything looks good"}

    def _weather_now(self, s) -> dict[str, Any] | None:
        """The date-line weather, plus the most serious official alert for the house (warnings only for kids)."""
        w = self.hub.smart.weather.now()
        fresh = self.hub.forecasts.cached_home_now()
        if fresh:  # the same forecast as the Weather page, so the two always agree
            w = {**(w or {}), **fresh, "source": "open-meteo"}
        alerts = self.hub.forecasts.cached_home_alerts()
        if s.role == "child":
            alerts = [a for a in alerts if a["level"] == "warning"]
        alerts = [a for a in alerts if a["level"] != "statement"]
        if alerts:
            w = dict(w or {})
            w["alert"] = {"id": alerts[0]["id"], "event": alerts[0]["event"], "level": alerts[0]["level"]}
            if len(alerts) > 1:
                w["alert_count"] = len(alerts)
        return w

    def systems_card(self) -> dict[str, Any]:
        s = self.systems or {}
        return {
            "storage": s.get("storage", []),
            "apps": {k: v for k, v in s.get("apps", {"total": 0, "crashed": 0, "updates": 0}).items() if k != "items"},
            "internet": {"up": self.hub.firewalla.last_error is None, "latency_ms": None},
            "services": self.hub.health(),
        }


def _ts(v: str) -> float:
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return time.time()
