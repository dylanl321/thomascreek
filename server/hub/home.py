"""The home model: one merged, role-filtered picture of the house.

Home Assistant states are polled (and diffed into live events); slower sources
(NAS, meals, calendar) refresh on their own cadence. Every device carries
`updated_at` and `available` so the app can show freshness honestly.
"""

from __future__ import annotations

import asyncio
import logging
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
    "valve": ["valve.run"], "shower": ["shower.preheat"], "oven": ["oven.set"], "camera": ["camera.view"],
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
    return DOMAIN_KIND.get(domain)


def device_state(kind: str, st: dict[str, Any]) -> dict[str, Any]:
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
        out.update(current_temp=a.get("current_temperature"), target_temp=a.get("temperature"),
                   hvac=s, hvac_action=a.get("hvac_action"))
    elif kind == "lock":
        out["locked"] = s == "locked"
    elif kind == "garage":
        out["open"] = s in ("open", "opening")
        out["position"] = a.get("current_position")
    elif kind in ("media", "speaker"):
        out.update(on=s not in ("off", "standby", "unavailable"), playing=s == "playing",
                   volume=round(a["volume_level"] * 100) if a.get("volume_level") is not None else None,
                   source=a.get("source") or a.get("app_name"))
    elif kind == "vacuum":
        out["value"] = s
    elif kind == "sensor":
        out.update(value=s, unit=a.get("unit_of_measurement"))
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

    async def _refresh_areas(self) -> None:
        areas = await self.hub.ha.areas()
        floors = await self.hub.ha.floors()
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

    async def _refresh_slow(self) -> None:
        if self.hub.truenas.enabled:
            try:
                self.systems = await self.hub.truenas.summary()
            except Exception as exc:  # noqa: BLE001
                log.info("NAS summary failed: %s", exc)
        if self.hub.mealie.enabled:
            try:
                today = await self.hub.mealie.today()
                dinner = next((m for m in today if m.get("meal") == "dinner"), today[0] if today else None)
                self.dinner = {"title": dinner["title"], "image_url": dinner.get("image_url")} if dinner else None
            except Exception as exc:  # noqa: BLE001
                log.info("meal read failed: %s", exc)
        if self.hub.family_board.enabled:
            try:
                self.calendar = (await self.hub.family_board.calendar(1))["events"]
            except Exception as exc:  # noqa: BLE001
                log.info("calendar read failed: %s", exc)

    def _apply_states(self, states: list[dict[str, Any]]) -> None:
        fresh = {s["entity_id"]: s for s in states}
        changed = [eid for eid, s in fresh.items()
                   if eid in self.states and (self.states[eid].get("last_updated") != s.get("last_updated"))]
        first = not self.states
        self.states = fresh
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

    def device(self, eid: str, s: Subject) -> dict[str, Any] | None:
        st = self.states.get(eid)
        if st is None:
            return None
        kind = kind_for(st)
        if kind is None:
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
        return {
            "id": eid,
            "kind": kind,
            "name": st.get("attributes", {}).get("friendly_name", eid),
            "room_id": area,
            "state": device_state(kind, st),
            "updated_at": st.get("last_updated"),
            "available": st.get("state") not in ("unavailable", "unknown"),
            "actions": actions,
        }

    def snapshot(self, s: Subject) -> dict[str, Any]:
        cfg = self.hub.config
        policy = self.hub.policy
        devices = [d for eid in self.states if (d := self.device(eid, s)) is not None and d["room_id"]]
        vis = self.visible_areas(s)
        rooms = []
        for a in self.areas.values():
            if vis is not None and a["id"] not in vis:
                continue
            in_room = [d for d in devices if d["room_id"] == a["id"]]
            if not in_room:
                continue
            rooms.append({
                "id": a["id"], "name": a["name"], "floor_id": a["floor_id"], "symbol": a["symbol"],
                "summary": self._summary(in_room), "favorite": a["id"] in cfg.favorites,
                "device_ids": [d["id"] for d in in_room],
            })
        room_ids = {r["id"] for r in rooms}
        floors = [{"id": f["id"], "name": f["name"], "room_ids": [r["id"] for r in rooms if r["floor_id"] == f["id"]]}
                  for f in self.floors]
        floors = [f for f in floors if f["room_ids"]]

        cameras = []
        if policy.has(s, "cameras"):
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
        }
        if policy.has(s, "systems"):
            snap["systems"] = self.systems_card()
        return snap

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
            if p.get("presence_entity"):  # presence is opt-in per person
                st = self.states.get(p["presence_entity"])
                row["home"] = (st or {}).get("state") == "home" if st else None
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
