"""What the app lists: one entry per real TV, appliances grouped by device, and the house layout.

Home Assistant reports the same TV three or four times (Cast, Android TV Remote, DLNA, an AirPlay
"+" copy), plus wall tablets, voice assistants, laptops and dead Plex sessions as media players. It
also splits each appliance into a dozen sensors. This module decides what the app shows:

* ``pick_screens`` keeps one media player per TV or streaming box, preferring the copy the remote
  can drive, and gives it the room of any copy that has one.
* ``appliances`` groups appliance entities (GE fridge, ovens, dishwasher, washer, dryer, vacuums)
  into one card per device with a plain status.
* ``HomeLayout`` is the house-wide layout the admin edits: devices hidden for everyone, per-room
  name, icon, order, hidden rooms, and devices moved into a different room. It lives in the hub's
  key-value table, so every phone sees the same house.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import Hub

STALE_DAYS = 3

# -- TVs ------------------------------------------------------------------------------------------

# Integrations that drive a TV or box directly; the higher, the better the remote.
SCREEN_PLATFORMS = {
    "androidtv_remote": 6, "apple_tv": 6, "roku": 5, "webostv": 5, "samsungtv": 5, "vizio": 5, "braviatv": 5,
    "androidtv": 4, "firetv": 4, "xbox": 4, "ps4": 3, "cast": 2, "kodi": 2,
}
# Integrations that are never a TV on their own (speakers, voice, tablets, phones, media servers).
NOT_SCREEN_PLATFORMS = {
    "plex", "sonos", "music_assistant", "esphome", "wyoming", "fully_kiosk", "browser_mod", "spotify",
    "squeezebox", "heos", "denonavr", "linkplay", "mobile_app", "jellyfin", "emby", "bluesound",
    "yamaha_musiccast", "playstation_network",
}
SCREEN_WORDS = re.compile(
    r"\btv\b|television|shield|chromecast|google ?tv|roku|apple ?tv|fire ?tv|\bonn\b|xbox|playstation|\bps[45]\b|"
    r"projector|\bframe\b|oled|bravia|webos|vizio|hisense|\btcl\b", re.I)
NOT_SCREEN_WORDS = re.compile(
    r"tablet|wall ?panel|kiosk|dashboard|voice|assist|satellite|macbook|imac|\bmac\b|iphone|ipad|\bpixel\b|"
    r"\bplex\b|airplay|volumio|upnp/av|music assistant|spotify|sonos|\becho\b|alexa|nest (mini|audio|hub)|"
    r"home ?pod|soundbar|receiver|\bavr\b|speaker", re.I)
# Words a TV's copies differ by ("Adeline's TV SmartTV 4K Television" is "Adeline's TV").
NAME_NOISE = re.compile(r"\(airplay\)|\(\d+\)|smart ?tv|\b4k\b|television|\btv\b|\+", re.I)


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", NAME_NOISE.sub(" ", (name or "").replace("’", "'")).lower())


def _since(st: dict[str, Any]) -> float | None:
    v = st.get("last_changed") or st.get("last_updated")
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_dead(st: dict[str, Any], now: float | None = None, days: float = STALE_DAYS) -> bool:
    """Unavailable (or unknown) for days: a device that's gone or was replaced."""
    if st.get("state") not in ("unavailable", "unknown"):
        return False
    t = _since(st)
    return t is not None and (now or time.time()) - t > days * 86400


def screen_score(st: dict[str, Any], platform: str | None, dev: dict[str, Any] | None) -> int | None:
    """None when this media player isn't a TV or box; otherwise how good a copy of it this is."""
    a = st.get("attributes", {})
    if "group_members" in a:  # Sonos and other speaker groups
        return None
    name = str(a.get("friendly_name") or "").strip()
    dev = dev or {}
    text = " ".join(str(x) for x in (name, dev.get("name"), dev.get("manufacturer"), dev.get("model")) if x)
    if not name or name.endswith("+") or platform in NOT_SCREEN_PLATFORMS or dev.get("integration") in NOT_SCREEN_PLATFORMS:
        return None
    if NOT_SCREEN_WORDS.search(text) and not (platform in SCREEN_PLATFORMS and platform != "cast"):
        return None
    dc = a.get("device_class")
    if dc in ("speaker", "receiver") and platform not in SCREEN_PLATFORMS and not SCREEN_WORDS.search(text):
        return None
    if not (dc == "tv" or platform in SCREEN_PLATFORMS or SCREEN_WORDS.search(text)):
        return None
    score = SCREEN_PLATFORMS.get(platform or "", 1)
    if platform == "dlna_dmr":
        score = 0
    return score


def pick_screens(states: dict[str, dict[str, Any]], platforms: dict[str, str], entity_device: dict[str, str],
                 devices: dict[str, dict[str, Any]], entity_area: dict[str, str], keep: set[str],
                 now: float | None = None) -> tuple[set[str], dict[str, str]]:
    """The media players to list as TVs, and rooms borrowed from another copy of the same TV."""
    now = now or time.time()
    groups: dict[str, list[tuple[int, str]]] = {}
    chosen: set[str] = set()
    for eid, st in states.items():
        if not eid.startswith("media_player."):
            continue
        if eid in keep:
            chosen.add(eid)
            continue
        score = screen_score(st, platforms.get(eid), devices.get(entity_device.get(eid, "")))
        if score is None or is_dead(st, now):
            continue
        key = norm_name(str(st.get("attributes", {}).get("friendly_name") or eid)) or eid
        live = st.get("state") not in ("unavailable", "unknown")
        groups.setdefault(key, []).append((score + (20 if live else 0) + (1 if eid in entity_area else 0), eid))
    borrowed: dict[str, str] = {}
    for members in groups.values():
        members.sort(key=lambda m: (-m[0], m[1]))
        best = members[0][1]
        chosen.add(best)
        if best not in entity_area:
            room = next((entity_area[e] for _, e in members if e in entity_area), None)
            if room:
                borrowed[best] = room
    # Kept players (a kid's TV, favourites) already stand for their group.
    for eid in keep:
        k = norm_name(str(states.get(eid, {}).get("attributes", {}).get("friendly_name") or ""))
        for _, e in groups.get(k, []):
            if e != eid:
                chosen.discard(e)
    return chosen, borrowed


# -- Appliances -----------------------------------------------------------------------------------

APPLIANCE_INTEGRATIONS = {
    "ge_home", "home_connect", "miele", "lg_thinq", "smartthinq_sensors", "whirlpool", "electrolux",
    "roborock", "ecovacs", "roomba", "neato", "sharkiq", "dreame_vacuum", "xiaomi_miio", "bosch_shc", "smartthings",
}
TYPES = [  # first match wins; tested against device and entity names
    ("dishwasher", re.compile(r"dish ?washer", re.I)),
    ("washer", re.compile(r"(?<!dish)(?<!dish )washer|washing machine|laundry", re.I)),
    ("dryer", re.compile(r"dryer", re.I)),
    ("fridge", re.compile(r"fridge|refrigerator|freezer", re.I)),
    ("microwave", re.compile(r"microwave", re.I)),
    ("oven", re.compile(r"oven|range|cooktop|stove", re.I)),
    ("vacuum", re.compile(r"vacuum|roomba|roborock|robot", re.I)),
    ("water_heater", re.compile(r"water heater", re.I)),
]
TYPE_NAME = {"dishwasher": "Dishwasher", "washer": "Washer", "dryer": "Dryer", "fridge": "Fridge",
             "microwave": "Microwave", "oven": "Oven", "vacuum": "Vacuum", "water_heater": "Water heater",
             "appliance": "Appliance"}
TYPE_SYMBOL = {"dishwasher": "dishwasher.fill", "washer": "washer.fill", "dryer": "dryer.fill",
               "fridge": "refrigerator.fill", "microwave": "microwave.fill", "oven": "oven.fill",
               "vacuum": "fan.fill", "water_heater": "water.waves", "appliance": "powerplug.fill"}
# A smart plug, leak sensor or switch named after an appliance is not the appliance.
NOT_APPLIANCE = re.compile(r"leak|water sensor|plug|outlet|socket|switch|relay|light|motion|contact|"
                           r"temperature sensor|humidity|meter|energy|power strip|button|remote", re.I)
STATUS_WORDS = re.compile(r"cycle|operation|status|state|phase|program|activity|mode", re.I)
IDLE = {"off", "idle", "standby", "docked", "ready", "none", "unavailable", "unknown", "", "complete", "finished",
        "done", "clean", "end", "paused", "sleeping", "charging"}
SERIAL = re.compile(r"^[A-Z]{1,4}\d{4,}[A-Z0-9]*\s*", re.I)


def is_fridge(st: dict[str, Any]) -> bool:
    name = f"{st.get('entity_id', '')} {st.get('attributes', {}).get('friendly_name') or ''}"
    return st.get("entity_id", "").startswith("water_heater.") and bool(TYPES[3][1].search(name))


def _clean(name: str) -> str:
    """'VV142830 Freezer' -> 'Freezer'."""
    out = SERIAL.sub("", name or "").strip()
    return out or name


def appliance_type(dev: dict[str, Any], states: dict[str, dict[str, Any]]) -> str | None:
    ents = [e for e in dev.get("entities") or [] if e in states]
    names = [str(states[e].get("attributes", {}).get("friendly_name") or e) for e in ents]
    head = " ".join(str(x) for x in (dev.get("name"), dev.get("model")) if x)
    domains = {e.split(".", 1)[0] for e in ents}
    known = dev.get("integration") in APPLIANCE_INTEGRATIONS or "vacuum" in domains or "water_heater" in domains
    if not known and (NOT_APPLIANCE.search(head) or not head):
        return None
    for t, rx in TYPES:
        if rx.search(head):
            return t
    for t, rx in TYPES:  # GE names its devices by serial number; the entity names say what it is
        if any(rx.search(n) for n in names):
            return t
    if "vacuum" in domains:
        return "vacuum"
    return "appliance" if known and dev.get("integration") in APPLIANCE_INTEGRATIONS else None


def _title(v: str) -> str:
    v = str(v).replace("_", " ").strip()
    return v[:1].upper() + v[1:] if v else v


def _temp(v: Any) -> str | None:
    try:
        return f"{round(float(v))}°"
    except (TypeError, ValueError):
        return None


def appliance_card(dev: dict[str, Any], kind: str, states: dict[str, dict[str, Any]],
                   area: str | None) -> dict[str, Any]:
    from .kitchen import is_oven, oven_state

    ents = [e for e in dev.get("entities") or [] if e in states]
    live = [e for e in ents if states[e].get("state") not in ("unavailable", "unknown")]
    parts: list[dict[str, Any]] = []
    status: str | None = None
    running = False
    oven_id = None
    for e in ents:
        st = states[e]
        a = st.get("attributes", {})
        nm = _clean(str(a.get("friendly_name") or e))
        if e.startswith("water_heater.") and is_oven(st):
            o = oven_state(st)
            oven_id = oven_id or e
            if o["on"]:
                running = True
                v = f"{_title(o['mode'] or 'On')} {_temp(o['target_temp']) or ''}".strip()
            else:
                v = "Off" if o["available"] else "Not connected"
            parts.append({"id": e, "name": nm, "value": v})
        elif e.startswith("water_heater."):
            t = _temp(a.get("current_temperature")) or _temp(a.get("temperature"))
            parts.append({"id": e, "name": nm, "value": t or ("Not connected" if e not in live else _title(st["state"]))})
        elif e.startswith("vacuum."):
            status = _title(st.get("state", ""))
            running = st.get("state") in ("cleaning", "returning")
    if status is None and kind not in ("fridge",):
        cands = []
        for e in live:
            if not (e.startswith("sensor.") or e.startswith("select.")):
                continue
            st = states[e]
            val = str(st.get("state"))
            nm = str(st.get("attributes", {}).get("friendly_name") or e)
            if not STATUS_WORDS.search(nm) or re.fullmatch(r"-?\d+(\.\d+)?", val):
                continue
            rank = 0 if re.search(r"cycle|operation|state|status", nm, re.I) else 1
            cands.append((rank, e, val))
        if cands:
            cands.sort()
            status = _title(cands[0][2])
            running = cands[0][2].strip().lower() not in IDLE
    door_open = any(
        states[e].get("attributes", {}).get("device_class") == "door" and states[e].get("state") == "on"
        for e in live if e.startswith("binary_sensor."))
    if not live:
        status, running = "Not connected", False
    elif status is None:
        status = " · ".join(f"{p['name']} {p['value']}" for p in parts[:2]) if parts else ("On" if running else "Off")
    if door_open:
        status = f"{status} · door open" if status else "Door open"
    name = dev.get("name") or TYPE_NAME[kind]
    if SERIAL.match(name) or name == dev.get("model"):
        name = TYPE_NAME[kind]
    return {
        "id": dev["id"], "type": kind, "name": name, "symbol": TYPE_SYMBOL.get(kind, "powerplug.fill"),
        "room_id": area, "status": status, "running": running, "available": bool(live), "door_open": door_open,
        "parts": parts, "oven_id": oven_id, "entity_ids": ents,
        "maker": dev.get("manufacturer"), "model": dev.get("model"),
    }


# -- House layout ---------------------------------------------------------------------------------

KV_KEY = "home.layout"
_ID = re.compile(r"^(?:[a-z_]+\.[A-Za-z0-9_\-]{1,180}|device:[A-Za-z0-9]{6,64})$")
_AREA = re.compile(r"^[A-Za-z0-9_\-]{1,120}$")
_SYMBOL = re.compile(r"^[a-z0-9.]{1,60}$")


class LayoutError(ValueError):
    def __init__(self, error: str, message: str, status: int = 400):
        super().__init__(message)
        self.error, self.message, self.status = error, message, status


class HomeLayout:
    """{"hidden": [entity ids], "moves": {entity id: area id}, "rooms": {area id: {name, symbol, order, hidden}}}"""

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._data: dict[str, Any] | None = None

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            raw = self.hub.db.kv_get(KV_KEY) or {}
            self._data = {"hidden": list(raw.get("hidden") or []), "moves": dict(raw.get("moves") or {}),
                          "rooms": dict(raw.get("rooms") or {})}
        return self._data

    def _save(self) -> None:
        self.hub.db.kv_set(KV_KEY, self.data)

    @property
    def hidden(self) -> set[str]:
        return set(self.data["hidden"])

    @property
    def moves(self) -> dict[str, str]:
        return self.data["moves"]

    def room(self, area_id: str) -> dict[str, Any]:
        return self.data["rooms"].get(area_id) or {}

    def room_hidden(self, area_id: str) -> bool:
        return bool(self.room(area_id).get("hidden"))

    def order(self, area_id: str, ids: list[str]) -> list[str]:
        saved = self.room(area_id).get("order") or []
        pos = {e: i for i, e in enumerate(saved)}
        base = {e: i for i, e in enumerate(ids)}
        return sorted(ids, key=lambda e: (pos.get(e, len(saved)), base[e]))

    # -- changes --------------------------------------------------------------------------------
    def set_hidden(self, ids: list[str], hidden: bool) -> list[str]:
        for e in ids:
            if not isinstance(e, str) or not _ID.match(e):
                raise LayoutError("bad_id", f"{e!r} isn't an entity id")
        cur = [e for e in self.data["hidden"] if e not in ids]
        if hidden:
            cur += ids
        self.data["hidden"] = cur[-2000:]
        self._save()
        return self.data["hidden"]

    def set_room(self, area_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not _AREA.match(area_id):
            raise LayoutError("bad_room", "Unknown room")
        r = dict(self.room(area_id))
        if "name" in patch:
            name = (patch["name"] or "").strip()
            if len(name) > 40:
                raise LayoutError("bad_name", "Room names are at most 40 characters")
            r["name"] = name or None
        if "symbol" in patch:
            sym = patch["symbol"]
            if sym is not None and not _SYMBOL.match(str(sym)):
                raise LayoutError("bad_symbol", "That icon isn't available")
            r["symbol"] = sym
        if "hidden" in patch:
            r["hidden"] = bool(patch["hidden"])
        if "order" in patch:
            order = patch["order"] or []
            if not isinstance(order, list) or len(order) > 400 or not all(isinstance(e, str) and _ID.match(e) for e in order):
                raise LayoutError("bad_order", "order must be a list of entity ids")
            r["order"] = list(dict.fromkeys(order))
        for e in patch.get("add") or []:
            if not isinstance(e, str) or not _ID.match(e):
                raise LayoutError("bad_id", f"{e!r} isn't an entity id")
            self.data["moves"][e] = area_id
        for e in patch.get("remove") or []:
            if self.data["moves"].get(e) == area_id:
                del self.data["moves"][e]
        r = {k: v for k, v in r.items() if v not in (None, False, [], "")}
        if r:
            self.data["rooms"][area_id] = r
        else:
            self.data["rooms"].pop(area_id, None)
        self._save()
        return r
