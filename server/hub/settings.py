"""Per-person settings and favorites that follow a person to every device they use.

One row per (person, key) in ``person_settings``, so two devices changing
different things never overwrite each other. Favorites are kept per kind
(``fav.room``, ``fav.device`` ...) as an ordered list, and the hub applies a
star or un-star to the list as it stands, so a star on the iPhone and an
un-star on the iPad both survive. Only a reorder replaces a whole list.

Each change bumps the person's version and sends a ``settings`` event to that
person's other devices, which re-read ``GET /v1/me/settings``.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from .policy import Subject

if TYPE_CHECKING:
    from .state import Hub

FAVORITE_KINDS = ("room", "device", "camera", "mode", "media", "recipe", "youtube")
MAX_FAVORITES = 60
HOME_SECTIONS = ("whos_home", "rightnow", "status", "favorites", "modes", "kids", "playing", "cameras", "holiday",
                 "trip", "today", "systems")

_ID = re.compile(r"^[A-Za-z0-9_.:\-/]{1,200}$")
_APP_KEY = re.compile(r"^app\.[a-z0-9_.]{1,60}$")


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _choice(*opts: str):
    def check(v: Any) -> str:
        if v not in opts:
            raise ValueError(f"must be one of {', '.join(opts)}")
        return v
    return check


def _entity(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _ID.match(v):
        raise ValueError("must be an id")
    return v


def _sections(v: Any) -> list[str]:
    if not isinstance(v, list) or len(v) > len(HOME_SECTIONS):
        raise ValueError("must be a list of Home sections")
    out = []
    for s in v:
        if s not in HOME_SECTIONS:
            raise ValueError(f"unknown Home section {s!r}")
        if s not in out:
            out.append(s)
    return out


# Settings that follow the person. Anything device-only (VPN mode, hub address,
# downloaded files) never comes here.
SETTINGS: dict[str, tuple[Any, Any]] = {
    # key: (default, validator)
    "home.order": ([], _sections),
    "home.hidden": ([], _sections),
    "appearance": ("system", _choice("system", "light", "dark")),
    "simple_mode": (False, _bool),
    "text_size": ("standard", _choice("standard", "large", "larger")),
    "defaults.play_on": (None, _entity),
    "defaults.speaker": (None, _entity),
    "defaults.oven": (None, _entity),
    "cameras.layout": ("grid", _choice("grid", "list")),
    "cameras.home": ("all", _choice("all", "starred")),
    "downloads.quality": ("original", _choice("original", "high", "medium", "small")),
    "downloads.cellular": (False, _bool),
    "downloads.remove_watched": (True, _bool),
    "live_activity.auto": (True, _bool),
}


class SettingsError(ValueError):
    def __init__(self, error: str, message: str, status: int = 400):
        super().__init__(message)
        self.error, self.message, self.status = error, message, status


class PersonSettings:
    def __init__(self, hub: "Hub"):
        self.hub = hub

    # -- storage -------------------------------------------------------------
    def _rows(self, pid: str) -> dict[str, Any]:
        rows = self.hub.db.all("SELECT key, value FROM person_settings WHERE person_id=?", [pid])
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def _put(self, pid: str, key: str, value: Any, device_id: str | None) -> None:
        if value is None and not key.startswith("defaults."):
            self.hub.db.execute("DELETE FROM person_settings WHERE person_id=? AND key=?", [pid, key])
            return
        self.hub.db.execute(
            "INSERT INTO person_settings (person_id, key, value, updated_at, device_id) VALUES (?,?,?,?,?) "
            "ON CONFLICT(person_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
            "device_id=excluded.device_id",
            [pid, key, json.dumps(value), time.time(), device_id])

    def version(self, pid: str | None) -> int:
        if not pid:
            return 0
        return int(self.hub.db.kv_get(f"settings_version:{pid}") or 0)

    def _bump(self, pid: str, device_id: str | None) -> int:
        v = self.version(pid) + 1
        self.hub.db.kv_set(f"settings_version:{pid}", v)
        self.hub.bus.publish({"type": "settings", "data": {"version": v, "device_id": device_id}}, person_id=pid)
        return v

    def forget(self, pid: str) -> None:
        self.hub.db.execute("DELETE FROM person_settings WHERE person_id=?", [pid])
        self.hub.db.execute("DELETE FROM hub_kv WHERE key=?", [f"settings_version:{pid}"])

    # -- reading ------------------------------------------------------------
    def get(self, s: Subject) -> dict[str, Any]:
        pid = s.person_id
        rows = self._rows(pid) if pid else {}
        settings = {k: rows.get(k, default) for k, (default, _) in SETTINGS.items()}
        settings.update({k: v for k, v in rows.items() if _APP_KEY.match(k)})
        favorites = {kind: self._visible(s, kind, rows.get(f"fav.{kind}", self._default_favorites(s, kind)))
                     for kind in FAVORITE_KINDS}
        return {"version": self.version(pid), "settings": settings, "favorites": favorites}

    def favorite_ids(self, s: Subject, kind: str) -> list[str]:
        """Starred ids of one kind, in the person's order (rooms on Home, cameras first...)."""
        if not s.person_id:
            return []
        rows = self._rows(s.person_id)
        return [f["id"] for f in self._visible(s, kind, rows.get(f"fav.{kind}", self._default_favorites(s, kind)))]

    def _default_favorites(self, s: Subject, kind: str) -> list[dict[str, Any]]:
        # Before anyone stars a room, everyone starts from the house-wide list in the config.
        if kind != "room":
            return []
        return [{"id": a} for a in self.hub.config.favorites if a in self.hub.home.areas]

    def _visible(self, s: Subject, kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # A favorite someone can no longer reach simply drops out; it comes back if access does.
        return [f for f in items if self._allowed(s, kind, f["id"], strict=False)]

    def _allowed(self, s: Subject, kind: str, item_id: str, strict: bool = True) -> bool:
        h, pol = self.hub, self.hub.policy
        if s.role in ("pending",) or not s.person_id:
            return False
        if kind == "room":
            if item_id not in h.home.areas:
                return not strict and not h.home.areas  # keep stars while the house list is still loading
            vis = h.home.visible_areas(s)
            return vis is None or item_id in vis
        if kind == "device":
            if item_id not in h.home.states:
                return not strict and not h.home.states
            return h.home.device(item_id, s) is not None
        if kind == "camera":
            return pol.has(s, "cameras")
        if kind == "mode":
            return any(m.id == item_id and s.role in m.roles for m in h.config.modes)
        if kind == "media":
            return pol.has(s, "media.all") or pol.has(s, "media.own_tv") or pol.has(s, "media.request")
        if kind == "recipe":
            return pol.has(s, "family.view")
        if kind == "youtube":
            return s.role in ("admin", "adult", "child")
        return False

    # -- writing ------------------------------------------------------------
    def patch(self, s: Subject, changes: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        pid = self._pid(s)
        clean: dict[str, Any] = {}
        for key, value in changes.items():
            if key in SETTINGS:
                default, check = SETTINGS[key]
                try:
                    clean[key] = default if value is None else check(value)
                except ValueError as exc:
                    raise SettingsError("bad_setting", f"{key}: {exc}") from None
            elif _APP_KEY.match(key):
                if value is not None and len(json.dumps(value)) > 2000:
                    raise SettingsError("bad_setting", f"{key}: too large")
                clean[key] = value
            else:
                raise SettingsError("unknown_setting", f"Unknown setting {key}")
        for key, value in clean.items():
            if key in SETTINGS and value == SETTINGS[key][0]:
                self.hub.db.execute("DELETE FROM person_settings WHERE person_id=? AND key=?", [pid, key])
            else:
                self._put(pid, key, value, device_id)
        if clean:
            self._bump(pid, device_id)
        return self.get(s)

    def star(self, s: Subject, kind: str, item_id: str, on: bool, label: dict[str, Any] | None,
             device_id: str | None) -> dict[str, Any]:
        pid = self._pid(s)
        if kind not in FAVORITE_KINDS:
            raise SettingsError("unknown_kind", f"Can't star a {kind}", 404)
        if not _ID.match(item_id):
            raise SettingsError("bad_id", "That id isn't valid")
        if on and not self._allowed(s, kind, item_id):
            raise SettingsError("not_allowed", "That isn't something this person can use", 403)
        with self.hub.db._lock:  # read-modify-write as one step
            rows = self._rows(pid)
            items = list(rows.get(f"fav.{kind}", self._default_favorites(s, kind)))
            have = next((f for f in items if f["id"] == item_id), None)
            if on and have is None:
                if len(items) >= MAX_FAVORITES:
                    raise SettingsError("too_many", f"Up to {MAX_FAVORITES} favorites of each kind")
                items.append({"id": item_id, **_label(label)})
            elif on and have is not None and label:
                have.update(_label(label))
            elif not on and have is not None:
                items = [f for f in items if f["id"] != item_id]
            else:
                return self.get(s)
            self._put(pid, f"fav.{kind}", items, device_id)
        self._bump(pid, device_id)
        return self.get(s)

    def reorder(self, s: Subject, kind: str, ids: list[str], device_id: str | None) -> dict[str, Any]:
        pid = self._pid(s)
        if kind not in FAVORITE_KINDS:
            raise SettingsError("unknown_kind", f"Can't star a {kind}", 404)
        with self.hub.db._lock:
            items = list(self._rows(pid).get(f"fav.{kind}", self._default_favorites(s, kind)))
            by_id = {f["id"]: f for f in items}
            # Keep anything starred elsewhere since this list was shown, at the end.
            ordered = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
            ordered += [f for f in items if f["id"] not in set(ids)]
            self._put(pid, f"fav.{kind}", ordered, device_id)
        self._bump(pid, device_id)
        return self.get(s)

    def migrate(self, s: Subject, local: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        """A device's own choices from before settings synced. The first device to send a key wins."""
        pid = self._pid(s)
        rows = self._rows(pid)
        fresh = {k: v for k, v in local.items() if k in SETTINGS and k not in rows}
        if not fresh:
            return self.get(s)
        try:
            return self.patch(s, fresh, device_id)
        except SettingsError:
            return self.get(s)

    @staticmethod
    def _pid(s: Subject) -> str:
        if not s.person_id or s.role == "pending":
            raise SettingsError("not_assigned", "This device isn't assigned to anyone yet", 403)
        return s.person_id


def _label(label: dict[str, Any] | None) -> dict[str, Any]:
    """Optional words the app shows without another fetch (a show's title, a recipe's picture)."""
    out: dict[str, Any] = {}
    for k in ("title", "subtitle", "image"):
        v = (label or {}).get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:300]
    return out
