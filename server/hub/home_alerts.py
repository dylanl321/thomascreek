"""Doorbell and door-camera alerts for the TVs.

Nothing has to be configured. The hub finds the doorbell and the cameras by the
door from Home Assistant and Frigate, stays quiet overnight except for a press,
and hands the TV a picture and a stream that use the TV's own sign-in.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, time as dtime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .db import new_id

DOOR = re.compile(r"door|doorbell|front|porch|entry|driveway|gate|stoop", re.I)
_ID = re.compile(r"^ha_[0-9a-f]{8,32}$")
KV_SETTINGS = "tv.home_alerts.settings"
KV_RECENT = "tv.home_alerts.recent"
KEEP_S = 600
QUIET_FROM = "22:00"
QUIET_TO = "07:00"
TTL = {"doorbell": 90, "person": 45, "motion": 20}
GAP = {"doorbell": 20, "person": 45, "motion": 60}


class HomeAlerts:
    def __init__(self, hub: Any):
        self.hub = hub
        self._last: dict[str, float] = {}

    # -- what the house already has ---------------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV_SETTINGS) or {}
        if not isinstance(raw, dict):
            raw = {}
        extra = self.hub.config.home_assistant.extra or {}
        quiet = raw.get("quiet") if isinstance(raw.get("quiet"), dict) else extra.get("alert_quiet") or {}
        if not isinstance(quiet, dict):
            quiet = {}
        cameras = raw["cameras"] if "cameras" in raw else extra.get("door_cameras")
        rooms = raw["rooms"] if "rooms" in raw else extra.get("door_rooms")
        return {
            "cameras": _names(cameras),
            "rooms": _names(rooms),
            "quiet": {"from": _clock(quiet.get("from"), QUIET_FROM), "to": _clock(quiet.get("to"), QUIET_TO)},
            "motion": bool(raw.get("motion", extra.get("alert_motion", False))),
            "auto": "cameras" not in raw and not extra.get("door_cameras"),
        }

    def set_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        cur = dict(self.hub.db.kv_get(KV_SETTINGS) or {})
        if "cameras" in body:
            cur["cameras"] = _names(body.get("cameras"))
            if body.get("cameras") is None:
                cur.pop("cameras", None)
        if "rooms" in body:
            cur["rooms"] = _names(body.get("rooms"))
            if body.get("rooms") is None:
                cur.pop("rooms", None)
        if isinstance(body.get("quiet"), dict):
            cur["quiet"] = {"from": _clock(body["quiet"].get("from"), QUIET_FROM),
                            "to": _clock(body["quiet"].get("to"), QUIET_TO)}
        if "motion" in body:
            cur["motion"] = bool(body.get("motion"))
        self.hub.db.kv_set(KV_SETTINGS, cur)
        return self.view()

    def view(self) -> dict[str, Any]:
        chosen = self.settings()
        found = self._catalog()
        cameras = chosen["cameras"] if chosen["cameras"] is not None else [row["id"] for row in found if row["door"]]
        if chosen["cameras"] is None and not cameras and len(found) == 1:
            cameras = [found[0]["id"]]
        rooms = chosen["rooms"]
        return {
            "cameras": cameras,
            "rooms": rooms,
            "quiet": chosen["quiet"],
            "motion": chosen["motion"],
            "auto": chosen["auto"],
            "found": found,
        }

    def _catalog(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for eid, st in self.hub.home.states.items():
            if not eid.startswith("camera."):
                continue
            name = eid.split(".", 1)[1]
            attrs = st.get("attributes") or {}
            room = self.hub.home.entity_area.get(eid)
            title = attrs.get("friendly_name") or name.replace("_", " ").title()
            seen[name] = {"id": name, "name": title, "room": room,
                          "door": bool(DOOR.search(f"{eid} {title} {room or ''}"))}
        for name, info in self.hub.frigate._info.items():
            room = self.hub.home.entity_area.get(f"camera.{name}")
            title = info.get("name") or seen.get(name, {}).get("name") or name.replace("_", " ").title()
            door = bool(DOOR.search(f"{name} {title} {room or ''}")) or seen.get(name, {}).get("door", False)
            seen[name] = {"id": name, "name": title, "room": room, "door": door}
        src = (self.hub.alerts.sources() or {}).get("doorbell_camera")
        if src and src in seen:
            seen[src]["door"] = True
        elif src:
            seen[src] = {"id": src, "name": src.replace("_", " ").title(), "room": None, "door": True}
        return sorted(seen.values(), key=lambda row: (not row["door"], str(row["name"]).casefold()))

    def door_cameras(self) -> list[str]:
        return list(self.view()["cameras"])

    # -- raising an alert --------------------------------------------------------------------------------------
    def note_doorbell(self, entity_id: str, camera: str | None = None, now: float | None = None) -> dict[str, Any] | None:
        cam = camera or (self.hub.alerts.sources() or {}).get("doorbell_camera") or next(iter(self.door_cameras()), None)
        return self._raise("doorbell", cam, "Someone's at the door", now=now, key=entity_id or "doorbell")

    def note_frigate(self, event: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
        if event.get("false_positive"):
            return None
        camera = str(event.get("camera") or "")
        if not camera or camera not in self.door_cameras():
            return None
        if not self._room_ok(camera):
            return None
        label = str(event.get("label") or "")
        if label in ("person", "face"):
            kind = "person"
            title = f"Someone at the {self._title(camera)}"
        elif label == "motion" and self.settings()["motion"]:
            kind = "motion"
            title = f"Motion at the {self._title(camera)}"
        else:
            return None
        if self._quiet(now) and kind != "doorbell":
            return None
        return self._raise(kind, camera, title, now=now, key=camera)

    def _raise(self, kind: str, camera: str | None, title: str, *, now: float | None, key: str) -> dict[str, Any] | None:
        if kind != "doorbell" and self._quiet(now):
            return None
        if kind != "doorbell" and camera and not self._room_ok(camera):
            return None
        now = time.time() if now is None else now
        gap_key = f"{kind}:{key}"
        if now - self._last.get(gap_key, 0) < GAP[kind]:
            return None
        self._last[gap_key] = now
        ttl = TTL[kind]
        alert: dict[str, Any] = {
            "id": new_id("ha"), "kind": kind, "camera": camera, "title": title, "ttl_s": ttl, "at": now,
        }
        if camera:
            alert["snapshot_url"] = f"/v1/tv/alerts/{alert['id']}/snapshot.jpg"
            alert["stream_url"] = f"/v1/tv/alerts/{alert['id']}/stream"
            alert["stream"] = "hls" if self.hub.frigate.cfg.go2rtc_url else "mjpeg"
        self._remember(alert)
        data = {k: v for k, v in alert.items() if k != "at"}
        self.hub.bus.publish({"type": "home_alert", "data": data})
        return data

    def get(self, alert_id: str) -> dict[str, Any] | None:
        if not _ID.match(alert_id or ""):
            return None
        now = time.time()
        for row in self._recent():
            if row.get("id") == alert_id and now - float(row.get("at") or 0) < KEEP_S:
                return row
        return None

    def _remember(self, alert: dict[str, Any]) -> None:
        now = time.time()
        rows = [row for row in self._recent() if now - float(row.get("at") or 0) < KEEP_S]
        rows.append(alert)
        self.hub.db.kv_set(KV_RECENT, rows[-40:])

    def _recent(self) -> list[dict[str, Any]]:
        raw = self.hub.db.kv_get(KV_RECENT) or []
        return raw if isinstance(raw, list) else []

    def _room_ok(self, camera: str) -> bool:
        rooms = self.settings()["rooms"]
        if not rooms:
            return True
        room = self.hub.home.entity_area.get(f"camera.{camera}")
        info = self.hub.frigate._info.get(camera) or {}
        return room in rooms or info.get("room") in rooms

    def _title(self, camera: str) -> str:
        for row in self._catalog():
            if row["id"] == camera:
                return str(row["name"]).lower()
        return camera.replace("_", " ")

    def _quiet(self, now: float | None) -> bool:
        quiet = self.settings()["quiet"]
        start = _parse_clock(quiet.get("from"))
        end = _parse_clock(quiet.get("to"))
        if start is None or end is None or start == end:
            return False
        moment = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(self.hub.config.hub.timezone))
        clock = moment.time().replace(second=0, microsecond=0)
        if start < end:
            return start <= clock < end
        return clock >= start or clock < end


def rewrite_playlist(text: str, alert_id: str) -> str:
    """Point every HLS segment at the hub, so the TV never calls go2rtc itself."""
    lines = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if "://" in stripped or stripped.startswith("//") or ".." in stripped:
            continue
        lines.append(f"/v1/tv/alerts/{alert_id}/media?src={quote(stripped, safe='')}")
    return "\n".join(lines) + ("\n" if lines else "")


def _names(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        text = str(item).strip()
        if text and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text) and text not in out:
            out.append(text)
    return out[:40]


def _clock(value: object, default: str) -> str:
    text = str(value or "").strip()
    return text if _parse_clock(text) else default


def _parse_clock(value: object) -> dtime | None:
    text = str(value or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hour, minute = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dtime(hour, minute)
