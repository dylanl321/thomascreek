"""What an idle TV shows.

Eight scenes ship with the hub, tagged by season and holiday. The date picks
one. A channel in the lineup whose name matches is used on its own. Dylan can
pin one, or point any of them at a channel key or a stream, from the phone or
from the hub config.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .tvlineup import valid_key

KV = "tv.ambient"
_ID = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_CAM = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "fireplace", "title": "Fireplace", "seasons": ("winter",), "holidays": (), "words": ("fireplace", "hearth", "fire")},
    {"id": "snowfall", "title": "Snowfall", "seasons": ("winter",), "holidays": (), "words": ("snow", "snowfall")},
    {"id": "christmas", "title": "Christmas lights", "seasons": ("winter",), "holidays": ("christmas",), "words": ("christmas",)},
    {"id": "leaves", "title": "Autumn leaves", "seasons": ("autumn",), "holidays": (), "words": ("autumn", "leaves", "fall")},
    {"id": "halloween", "title": "Halloween", "seasons": ("autumn",), "holidays": ("halloween",), "words": ("halloween",)},
    {"id": "rain", "title": "Spring rain", "seasons": ("spring",), "holidays": (), "words": ("rain",)},
    {"id": "beach", "title": "Summer beach", "seasons": ("summer",), "holidays": (), "words": ("beach", "ocean")},
    {"id": "aquarium", "title": "Aquarium", "seasons": ("winter", "spring", "summer", "autumn"), "holidays": (), "words": ("aquarium", "fish")},
)
_BY_ID = {row["id"]: row for row in CATALOG}


def season_of(moment: datetime) -> str:
    month = moment.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def holiday_of(moment: datetime) -> str | None:
    if moment.month == 10:
        return "halloween"
    if moment.month == 12 and moment.day <= 26:
        return "christmas"
    return None


def pick_id(moment: datetime, pin: str | None) -> str:
    if pin in _BY_ID:
        return str(pin)
    holiday = holiday_of(moment)
    if holiday:
        for row in CATALOG:
            if holiday in row["holidays"]:
                return row["id"]
    season = season_of(moment)
    # Winter alternates the two quiet scenes by the day, so it isn't the same fire every night.
    if season == "winter":
        return "snowfall" if moment.day % 2 else "fireplace"
    for row in CATALOG:
        if season in row["seasons"] and not row["holidays"]:
            return row["id"]
    return "aquarium"


class Ambient:
    def __init__(self, hub: Any):
        self.hub = hub

    def view(self, device: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        moment = self._now(now)
        season = season_of(moment)
        holiday = holiday_of(moment)
        chosen = _BY_ID[pick_id(moment, self._pin(device))]
        ambience = self._ambience(chosen)
        return {
            "season": season,
            "holiday": holiday,
            "ambience": ambience,
            "widgets": {
                "weather": self._weather(),
                "calendar": self._calendar(),
                "cameras": self._cameras(),
                "art": self._art(moment),
            },
        }

    def library(self, device: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        moment = self._now(now)
        current = pick_id(moment, self._pin(device))
        return {
            "pin": self._pin(device),
            "current": current,
            "items": [self._library_row(row) for row in CATALOG],
        }

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        saved = self._saved()
        device = _device(body.get("device"))
        if "pin" in body:
            pin = body.get("pin")
            pin = pin if isinstance(pin, str) and pin in _BY_ID else None
            if device:
                pins = dict(saved.get("devices") or {})
                if pin:
                    pins[device] = pin
                else:
                    pins.pop(device, None)
                saved["devices"] = pins
            else:
                saved["pin"] = pin
        items = body.get("items")
        if isinstance(items, list):
            known = dict(saved.get("items") or {})
            for item in items:
                if not isinstance(item, dict) or item.get("id") not in _BY_ID:
                    continue
                known[item["id"]] = _assignment(item)
            saved["items"] = known
        self.hub.db.kv_set(KV, saved)
        return self.library(device)

    def _ambience(self, row: dict[str, Any]) -> dict[str, Any]:
        assigned = self._assignment_for(row["id"])
        out: dict[str, Any] = {"id": row["id"], "title": row["title"]}
        channel = assigned.get("channel_key") or self._match(row)
        if channel:
            out["channel_key"] = channel
        elif assigned.get("stream_url"):
            out["stream_url"] = assigned["stream_url"]
        poster = assigned.get("poster_url") or self._poster(channel)
        if poster:
            out["poster_url"] = poster
        return out

    def _library_row(self, row: dict[str, Any]) -> dict[str, Any]:
        assigned = self._assignment_for(row["id"])
        item = {
            "id": row["id"], "title": row["title"],
            "seasons": list(row["seasons"]), "holidays": list(row["holidays"]),
        }
        if assigned.get("channel_key"):
            item["channel_key"] = assigned["channel_key"]
        elif self._match(row):
            item["channel_key"] = self._match(row)
            item["matched"] = True
        if assigned.get("stream_url"):
            item["stream_url"] = assigned["stream_url"]
        if assigned.get("poster_url"):
            item["poster_url"] = assigned["poster_url"]
        return item

    def _assignment_for(self, item_id: str) -> dict[str, Any]:
        saved = (self._saved().get("items") or {}).get(item_id) or {}
        config = {}
        for row in self.hub.config.ambient.streams:
            if row.id == item_id:
                config = {"channel_key": row.channel_key, "stream_url": row.stream_url, "poster_url": row.poster_url}
                break
        merged = {k: v for k, v in config.items() if v}
        merged.update({k: v for k, v in saved.items() if v})
        return merged

    def _pin(self, device: str | None) -> str | None:
        saved = self._saved()
        if device:
            pinned = (saved.get("devices") or {}).get(device)
            if pinned in _BY_ID:
                return pinned
        pin = saved.get("pin") or self.hub.config.ambient.pin
        return pin if pin in _BY_ID else None

    def _match(self, row: dict[str, Any]) -> str | None:
        words = tuple(word.casefold() for word in row["words"])
        for channel in self._channels():
            name = str(channel.get("name") or "").casefold()
            if any(word in name for word in words) and valid_key(str(channel.get("key") or "")):
                return str(channel["key"])
        return None

    def _channels(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        doc = self.hub.tv_catalog.load() if self.hub.tv_catalog else {}
        for row in doc.get("channels") or []:
            if isinstance(row, dict):
                rows.append(row)
        try:
            for row in self.hub.tv_app._lineup().get("channels") or []:
                if isinstance(row, dict) and not row.get("hidden"):
                    rows.append(row)
        except Exception:  # noqa: BLE001
            pass
        return rows

    def _poster(self, channel: str | None) -> str | None:
        artwork = getattr(self.hub, "artwork", None)
        if artwork is None or not channel:
            return None
        return artwork.public_url("logo", channel)

    def _weather(self) -> dict[str, Any] | None:
        try:
            raw = self.hub.smart.weather.now() or {}
        except Exception:  # noqa: BLE001
            return None
        keep = {key: raw.get(key) for key in ("temp", "condition", "symbol", "high", "low") if raw.get(key) is not None}
        return keep or None

    def _calendar(self) -> list[dict[str, Any]]:
        events = getattr(self.hub.home, "calendar", None) or []
        out = []
        for event in events:
            if not isinstance(event, dict) or not event.get("title"):
                continue
            item = {"title": str(event["title"])[:120]}
            if event.get("start"):
                item["start"] = event["start"]
            out.append(item)
            if len(out) >= 6:
                break
        return out

    def _cameras(self) -> list[dict[str, Any]]:
        names: list[str] = []
        try:
            names.extend(self.hub.home_alerts.door_cameras())
        except Exception:  # noqa: BLE001
            pass
        for eid, state in self.hub.home.states.items():
            if eid.startswith("camera."):
                name = eid.split(".", 1)[1]
                if name not in names:
                    names.append(name)
        out = []
        for name in names:
            if not _CAM.match(name):
                continue
            label = ((self.hub.home.states.get(f"camera.{name}") or {}).get("attributes") or {}).get("friendly_name")
            out.append({"name": label or name.replace("_", " ").title(),
                        "snapshot_url": f"/v1/ambient/cameras/{name}.jpg"})
            if len(out) >= 4:
                break
        return out

    def _art(self, moment: datetime) -> list[str]:
        try:
            guide = self.hub.tv_catalog.guide(
                start=moment.timestamp() - 3600, end=moment.timestamp() + 3600,
                channel=None, kids_only=False, now=moment.timestamp())
        except Exception:  # noqa: BLE001
            return []
        found: list[str] = []
        for channel in guide.get("channels") or []:
            poster = (channel.get("now") or {}).get("poster_url")
            if isinstance(poster, str) and poster.startswith("/v1/") and poster not in found:
                found.append(poster)
            if len(found) >= 4:
                break
        return found

    def _saved(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV) or {}
        return raw if isinstance(raw, dict) else {}

    def _now(self, now: datetime | None) -> datetime:
        tz = ZoneInfo(self.hub.config.hub.timezone)
        if now is None:
            return datetime.now(tz)
        return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)


def _device(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 80 or "://" in text:
        return None
    return text


def _assignment(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    channel = item.get("channel_key")
    if isinstance(channel, str) and valid_key(channel):
        out["channel_key"] = channel
    stream = _stream(item.get("stream_url"))
    if stream and "channel_key" not in out:
        out["stream_url"] = stream
    poster = item.get("poster_url")
    if isinstance(poster, str) and poster.startswith("/v1/") and ".." not in poster:
        out["poster_url"] = poster[:200]
    return out


def _stream(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > 300 or "://" not in text:
        return None
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return None
    if parts.username or parts.password or "@" in parts.netloc:
        return None
    return text
