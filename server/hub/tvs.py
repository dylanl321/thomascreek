"""Every Thomas Creek TV as the phone sees it: one list, one detail, settings and a few actions.

This wraps what the hub already keeps about a TV (the registry in tvapp, the fleet check-ins in
tvfleet, now-playing from /v1/tv/state) into one shape for the iPhone app and the assistant.

Settings that should be the same on every screen live in the TV's hub record under `settings`, with
`settings_rev` counting changes. Picture and audio stay on each TV. The last write wins, from the phone
or from the TV. Every change sends `config_changed` to the TV, which pulls its config at once.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from . import tvwho, tvcec, tvfleet
from .home import iso
from .tvapp import TvAppError
from .tvlineup import valid_key

# A TV counts as online when it has checked in within this window (fleet report, state or config pull).
ONLINE_S = tvfleet.ONLINE_S
ACTIONS = ("restart_app", "update", "open_app", "screenshot", "play") + tvcec.ACTIONS
# Settings that stay on each TV and never come from the hub.
LOCAL_ONLY = ("picture", "audio", "display", "sound")

_SLUG = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,59}")
_LANG = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})?")
_RATINGS = ("G", "PG", "PG-13", "R", "NC-17", "TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14", "TV-MA")
_SUBTITLES = ("off", "auto")


class TvsError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# -- reading ---------------------------------------------------------------------------------------------------------

def listing(h: Any, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    rows = [summary(h, install_id, tv, now) for install_id, tv in h.tv_app.tvs().items() if tv.get("status") != "revoked"]
    rows.sort(key=lambda row: (row["status"] != "approved", str(row.get("name") or "").lower()))
    latest = h.tv_app.latest() or {}
    return {"tvs": rows, "latest_version": latest.get("versionName")}


def detail(h: Any, install_id: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    tv = _tv(h, install_id)
    out = summary(h, install_id, tv, now)
    fleet = tvfleet._devices(h).get(install_id)
    if fleet:
        view = tvfleet._public(h, install_id, fleet, now)
        out["device"] = {key: view.get(key) for key in (
            "cpu_pct", "ram_pct", "storage_pct", "temperature_c", "network", "ping_ms", "uptime_s",
            "remote_battery_pct", "android")}
        out["last_command"] = tvfleet._last_command(h, install_id)
    else:
        out["device"] = None
        out["last_command"] = None
    out["settings"] = settings_of(tv)
    out["cec"] = tvcec.view(tv)
    out["actions"] = list(ACTIONS)
    return out


def summary(h: Any, install_id: str, tv: dict[str, Any], now: float) -> dict[str, Any]:
    fleet = tvfleet._devices(h).get(install_id) or {}
    health = tv.get("health") or {}
    seen = max(float(fleet.get("seen") or 0), float(tv.get("state_at") or 0), float(tv.get("last_seen_at") or 0),
               float(tv.get("last_config_at") or 0))
    version = fleet.get("app_version") or tv.get("app_version") or health.get("app_version")
    latest = (h.tv_app.latest() or {}).get("versionName")
    online = bool(seen and now - seen < ONLINE_S)
    return {
        "id": install_id,
        "name": tv.get("name") or "TV",
        "room": tv.get("room"),
        "profile": tv.get("profile") or "adult",
        "status": tv.get("status") or "pending",
        "online": online,
        "last_seen": iso(seen) if seen else None,
        "health": _health(h, install_id, fleet, seen, now, online),
        "app_version": version,
        "update_available": bool(latest and version and latest != version),
        "foreground": bool(tv.get("foreground", True)) if online else False,
        "now_playing": _now_playing(tv.get("playing")) if online else None,
        "last_error": health.get("last_error"),
        "model": fleet.get("model") or tv.get("model"),
        "platform": fleet.get("platform") or "unknown",
        "capability": fleet.get("capability") or "basic",
        "ip": tv.get("ip"),
        "screenshot": _screenshot(h, install_id),
        "can_screenshot": _can_screenshot(h, install_id),
        "can_cec": _can_adb(h, tv),
        "settings_rev": int(tv.get("settings_rev") or 0),
        "settings_by": tv.get("settings_by"),
        "settings_at": iso(float(tv["settings_at"])) if tv.get("settings_at") else None,
        # Who the hub thinks is watching (see hub/tvwho.py). person_id is None when nobody is named.
        "watching": tvwho.current(h, install_id, now),
    }


def settings_of(tv: dict[str, Any]) -> dict[str, Any]:
    raw = tv.get("settings")
    return dict(raw) if isinstance(raw, dict) else {}


# -- writing ---------------------------------------------------------------------------------------------------------

def change(h: Any, install_id: str, body: dict[str, Any], *, by: str, now: float | None = None) -> dict[str, Any]:
    """Apply a change from the phone (`by="phone"`) or the TV (`by="tv"`). The last write wins.

    The phone may also rename the TV, move it to a room, or switch its profile. Each change bumps the
    config once, which sends `config_changed` to that TV.
    """
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        raise TvsError(400, "bad_request", "Send the settings to change.")
    _tv(h, install_id)
    record = {key: body[key] for key in ("name", "room", "profile") if by == "phone" and key in body}
    changes = body.get("settings") if "settings" in body else {}
    if not isinstance(changes, dict):
        raise TvsError(400, "bad_request", "settings is an object of the values to change.")
    clean = {key: _setting(key, value) for key, value in changes.items()}
    if record.get("profile") is not None and record["profile"] not in ("adult", "kids"):
        raise TvsError(400, "bad_profile", "Profile is adult or kids.")
    if clean:
        tvs = h.tv_app.tvs()
        tv = tvs[install_id]
        current = settings_of(tv)
        for key, value in clean.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        tv.update(settings=current, settings_rev=int(tv.get("settings_rev") or 0) + 1, settings_at=now, settings_by=by)
        tvs[install_id] = tv
        h.tv_app._save_tvs(tvs)
    if record:
        try:
            h.tv_app.set_tv(install_id, profile=record.get("profile"),
                            name=str(record.get("name") or "")[:80] or None,
                            **({"room": record["room"]} if "room" in record else {}))
        except TvAppError as exc:
            raise TvsError(exc.status, exc.code, exc.message) from None
    elif clean:
        h.tv_app.bump_config(install_id)
    if record or clean:
        h.db.log("tvs.settings", "ok", target=install_id, source=by,
                 params={"settings": sorted(clean), "record": sorted(record)})
    tv = h.tv_app.tvs()[install_id]
    return {"ok": True, "settings": settings_of(tv), "settings_rev": int(tv.get("settings_rev") or 0)}


def copy(h: Any, install_id: str, source_id: str, *, by: str = "phone", now: float | None = None) -> dict[str, Any]:
    """Give one TV another TV's settings, the device set included, so a new TV starts like the old one."""
    if source_id == install_id:
        raise TvsError(400, "bad_request", "Pick another TV to copy from.")
    source = settings_of(_tv(h, source_id))
    target = settings_of(_tv(h, install_id))
    if not source:
        raise TvsError(409, "nothing_to_copy", "That TV hasn't saved any settings on the hub yet.")
    changes: dict[str, Any] = {key: None for key in target if key not in source}
    changes.update(source)
    return change(h, install_id, {"settings": changes}, by=by, now=now)


def announce(h: Any, install_id: str | None, version: int | None = None) -> None:
    """Tell one TV (or every TV when install_id is None) to pull its config now. The TV answers with
    POST /v1/tv/config and gets 304 when nothing it caches changed."""
    data: dict[str, Any] = {"install_id": install_id, "version": version or h.tv_app.config_version()}
    if install_id:
        tv = h.tv_app.tvs().get(install_id) or {}
        data["settings_rev"] = int(tv.get("settings_rev") or 0)
        h.bus.publish({"type": "config_changed", "data": data}, device_id=f"tv:{install_id}")
    else:
        h.bus.publish({"type": "config_changed", "data": data}, tvs=True)


async def act(h: Any, install_id: str, body: dict[str, Any], who: str | None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    body = body if isinstance(body, dict) else {}
    action = str(body.get("action") or "")
    if action not in ACTIONS:
        raise TvsError(400, "bad_action", "That isn't something the phone can ask a TV to do.")
    tv = _tv(h, install_id)
    if tv.get("status") != "approved":
        raise TvsError(409, "not_approved", "Approve this TV first.")
    online = summary(h, install_id, tv, now)["online"]
    if action == "screenshot":
        return await _screenshot_now(h, install_id, tv, online, who)
    if action in tvcec.ACTIONS:
        return await _cec(h, install_id, tv, body, online, who)
    if action == "restart_app":
        if online:
            sent = _fleet(h, install_id, "restart_app", {}, who)
            return {"ok": True, "via": "app", "id": sent["id"], "message": "Restarting Thomas Creek TV."}
        await _adb_launch(h, tv, restart=True)
        return {"ok": True, "via": "adb", "message": "Restarted Thomas Creek TV over the network."}
    if action == "open_app":
        try:
            await _adb_launch(h, tv, restart=False)
            return {"ok": True, "via": "adb", "message": "Opened Thomas Creek TV."}
        except TvsError:
            if not online:
                raise
        # No network debugging: the app brings itself back to the front by restarting.
        sent = _fleet(h, install_id, "restart_app", {}, who)
        return {"ok": True, "via": "app", "id": sent["id"], "message": "Brought Thomas Creek TV to the front."}
    if action == "update":
        return await _update(h, install_id, tv, online, who)
    return _play(h, install_id, tv, body, online)


# -- helpers ---------------------------------------------------------------------------------------------------------

def _tv(h: Any, install_id: str) -> dict[str, Any]:
    tv = h.tv_app.tvs().get(install_id or "")
    if not tv or tv.get("status") == "revoked":
        raise TvsError(404, "not_found", "That TV isn't in the house.")
    return tv


def _health(h: Any, install_id: str, fleet: dict[str, Any], seen: float, now: float, online: bool) -> str:
    if not online:
        return "offline"
    if fleet:
        state = tvfleet._health(h, fleet, seen, now)
        if state != "offline":
            return state
    return "ok"


def _root(h: Any, install_id: str) -> bool:
    return (tvfleet._devices(h).get(install_id) or {}).get("capability") == "root"


def _can_adb(h: Any, tv: dict[str, Any]) -> bool:
    """The hub can reach this box over network debugging: power, input and volume over HDMI-CEC."""
    return bool(tv.get("ip") and h.tv_app.adb.available)


async def _cec(h: Any, install_id: str, tv: dict[str, Any], body: dict[str, Any], online: bool,
               who: str | None) -> dict[str, Any]:
    """HDMI-CEC power, input and volume (hub/tvcec.py). Over adb; volume falls back to the app."""
    try:
        serial = await _adb_ready(h, tv)
    except TvsError as exc:
        volume = str(body.get("volume") or "")
        if body.get("action") == "tv_volume" and online and volume in ("up", "down", "mute") and exc.code != "needs_allow":
            params = {"mute": True} if volume == "mute" else {"delta": 1 if volume == "up" else -1}
            sent = _fleet(h, install_id, "tv_volume", params, who)
            return {"ok": True, "via": "app", "id": sent["id"], "message": "Sent the volume to the TV app."}
        raise
    try:
        return await tvcec.act(h, install_id, tv, body, serial)
    except tvcec.CecError as exc:
        raise TvsError(exc.status, exc.code, exc.message) from None


def _can_screenshot(h: Any, install_id: str) -> bool:
    """The hub takes the picture over network debugging; a rooted box can also send its own.

    Without either, the app has no way to capture the screen, so the phone hides the button.
    """
    tv = h.tv_app.tvs().get(install_id) or {}
    return _root(h, install_id) or bool(tv.get("ip") and h.tv_app.adb.available)


async def _screenshot_now(h: Any, install_id: str, tv: dict[str, Any], online: bool, who: str | None) -> dict[str, Any]:
    name = tv.get("name") or "The TV"
    problem: TvsError | None = None
    try:
        serial = await _adb_ready(h, tv)
        png = await h.tv_app.adb.screencap(serial)
        if png and len(png) <= tvfleet.SHOT_MAX:
            path = tvfleet._shot_path(h, install_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            return {"ok": True, "via": "adb", "message": "Here's the screen."}
        problem = TvsError(502, "no_picture", f"{name} didn't send a picture. Try again in a moment.")
    except TvsError as exc:
        problem = exc
    if online and _root(h, install_id):
        sent = _fleet(h, install_id, "screenshot", {}, who)
        return {"ok": True, "via": "app", "id": sent["id"], "message": "Taking a picture of the screen."}
    if not online and problem.code == "no_address":
        raise TvsError(409, "offline", f"{name} is offline, so it can't take a picture.")
    raise problem


def _now_playing(playing: Any) -> dict[str, Any] | None:
    if not isinstance(playing, dict):
        return None
    keys = ("kind", "title", "name", "number", "channel_key", "movie_id", "series_id", "episode_id", "position_s")
    out = {key: playing.get(key) for key in keys if playing.get(key) is not None}
    return out or None


def _screenshot(h: Any, install_id: str) -> dict[str, Any] | None:
    path = tvfleet._shot_path(h, install_id)
    if not path.is_file():
        return None
    at = path.stat().st_mtime
    return {"url": f"/v1/tvs/{install_id}/screenshot?t={int(at)}", "at": iso(at)}


def _fleet(h: Any, install_id: str, action: str, params: dict[str, Any], who: str | None) -> dict[str, Any]:
    try:
        return tvfleet.command(h, install_id, action, params, who)
    except tvfleet.FleetError as exc:
        raise TvsError(exc.status, "fleet", exc.message) from None


async def _adb_ready(h: Any, tv: dict[str, Any]) -> str:
    ip = tv.get("ip")
    name = tv.get("name") or "The TV"
    if not ip:
        raise TvsError(409, "no_address", f"The hub doesn't know {name}'s address yet.")
    adb = h.tv_app.adb
    if not adb.available:
        raise TvsError(409, "no_adb", "The hub can't reach TVs over the network (adb is missing).")
    serial = h.tv_app._serial(ip)
    state = await adb.connect(serial)
    if state == "needs_allow":
        raise TvsError(409, "needs_allow", f"{name} needs one Allow for the hub: press Allow on the TV, then try again.")
    if state != "ok":
        raise TvsError(409, "offline", f"{name} didn't answer. Is it on, with network debugging turned on?")
    return serial


async def _adb_launch(h: Any, tv: dict[str, Any], *, restart: bool) -> None:
    serial = await _adb_ready(h, tv)
    package = h.tv_app.cfg.package
    adb = h.tv_app.adb
    if restart:
        await adb.run(["-s", serial, "shell", "am", "force-stop", package], 15)
    result = await adb.run(["-s", serial, "shell", "monkey", "-p", package,
                            "-c", "android.intent.category.LEANBACK_LAUNCHER", "1"], 20)
    if result.code != 0 or "No activities found" in result.out or "aborted" in result.out.lower():
        raise TvsError(502, "launch_failed", "The TV didn't open Thomas Creek TV. Is it installed?")


async def _update(h: Any, install_id: str, tv: dict[str, Any], online: bool, who: str | None) -> dict[str, Any]:
    latest = h.tv_app.latest()
    if not latest or not h.tv_app.apk_path(int(latest["versionCode"])).is_file():
        raise TvsError(409, "no_build", "The hub doesn't have a TV app build yet.")
    name = latest.get("versionName") or str(latest["versionCode"])
    current = summary(h, install_id, tv, time.time())["app_version"]
    if current and current == latest.get("versionName"):
        return {"ok": True, "via": "none", "message": f"Already on {name}."}
    ip = tv.get("ip")
    if ip and ip in h.tv_app.boxes():
        try:
            await h.tv_app.install(ip, force=True)
            return {"ok": True, "via": "adb", "message": f"Installed {name}. The app restarts on its own."}
        except TvAppError as exc:
            if not online:
                raise TvsError(exc.status, exc.code, exc.message) from None
    elif ip and not online:
        serial = await _adb_ready(h, tv)
        result = await h.tv_app.adb.install(serial, h.tv_app.apk_path(int(latest["versionCode"])))
        if result.code == 0 and "Success" in result.out:
            return {"ok": True, "via": "adb", "message": f"Installed {name}."}
        raise TvsError(502, "install_failed", "The TV refused the update.")
    if not online:
        raise TvsError(409, "offline", f"{tv.get('name') or 'The TV'} is offline, so it can't update now.")
    path = h.tv_app.apk_path(int(latest["versionCode"]))
    params = {"url": "/v1/tv/apk", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "version_code": int(latest["versionCode"]), "version_name": name}
    sent = _fleet(h, install_id, "install_apk", params, who)
    return {"ok": True, "via": "app", "id": sent["id"],
            "message": f"Sent {name} to the TV. If it asks, press Install on the TV."}


def _play(h: Any, install_id: str, tv: dict[str, Any], body: dict[str, Any], online: bool) -> dict[str, Any]:
    if not online:
        raise TvsError(409, "offline", f"{tv.get('name') or 'The TV'} is offline.")
    command: dict[str, Any] = {"action": "play"}
    key = body.get("channel_key")
    if key:
        if not valid_key(str(key)):
            raise TvsError(400, "bad_key", "That channel isn't one the hub knows.")
        command.update(kind="live", channel_key=str(key))
    elif body.get("movie_id"):
        command.update(kind="movie", movie_id=str(body["movie_id"])[:80])
    elif body.get("episode_id"):
        command.update(kind="episode", episode_id=str(body["episode_id"])[:80],
                       series_id=str(body.get("series_id") or "")[:80] or None)
    else:
        raise TvsError(400, "bad_request", "Say what to play: a channel_key, movie_id or episode_id.")
    if isinstance(body.get("title"), str):
        command["title"] = body["title"][:120]
    sent = h.tv_app.enqueue_command(install_id, command)
    h.bus.publish({"type": "tv_command", "data": sent}, device_id=f"tv:{install_id}")
    return {"ok": True, "via": "app", "id": sent["id"], "message": "Playing on the TV."}


def _setting(key: str, value: Any) -> Any:
    """Validate one setting. None removes it, so the TV falls back to its own default."""
    if key in LOCAL_ONLY:
        raise TvsError(400, "local_setting", "Picture and audio stay on each TV.")
    if value is None:
        if key not in _VALIDATORS:
            raise TvsError(400, "bad_setting", f"{key} isn't a TV setting the hub keeps.")
        return None
    check = _VALIDATORS.get(key)
    if check is None:
        raise TvsError(400, "bad_setting", f"{key} isn't a TV setting the hub keeps.")
    return check(value)


def _strings(value: Any, *, limit: int, length: int, pattern: re.Pattern[str] | None = None,
             keys: bool = False, what: str) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise TvsError(400, "bad_setting", f"{what} is a list of at most {limit}.")
    out: list[str] = []
    for item in value:
        text = str(item).strip() if isinstance(item, (str, int)) else ""
        ok = bool(text) and len(text) <= length
        if ok and pattern is not None:
            ok = bool(pattern.fullmatch(text))
        if ok and keys:
            ok = valid_key(text)
        if not ok:
            raise TvsError(400, "bad_setting", f"{what} has an entry the hub can't use.")
        if text not in out:
            out.append(text)
    return out


def _home_rows(value: Any) -> list[str]:
    return _strings(value, limit=30, length=60, pattern=_SLUG, what="home_rows")


def _favorites(value: Any) -> list[str]:
    return _strings(value, limit=500, length=140, keys=True, what="favorites")


def _hidden(value: Any) -> list[str]:
    return _strings(value, limit=500, length=80, what="hidden_categories")


def _language(value: Any) -> str:
    if not isinstance(value, str) or not _LANG.fullmatch(value):
        raise TvsError(400, "bad_setting", "language is a code such as en or en-US.")
    return value


def _parental(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TvsError(400, "bad_setting", "parental is an object.")
    out: dict[str, Any] = {}
    if "locked" in value:
        if not isinstance(value["locked"], bool):
            raise TvsError(400, "bad_setting", "parental.locked is true or false.")
        out["locked"] = value["locked"]
    if "max_rating" in value:
        rating = value["max_rating"]
        if rating is not None and rating not in _RATINGS:
            raise TvsError(400, "bad_setting", "parental.max_rating is a rating such as PG or TV-Y7.")
        out["max_rating"] = rating
    unknown = set(value) - {"locked", "max_rating"}
    if unknown:
        raise TvsError(400, "bad_setting", "parental takes locked and max_rating. A PIN stays on the TV.")
    return out


def _player(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TvsError(400, "bad_setting", "player is an object.")
    out: dict[str, Any] = {}
    for flag in ("autoplay_next", "resume"):
        if flag in value:
            if not isinstance(value[flag], bool):
                raise TvsError(400, "bad_setting", f"player.{flag} is true or false.")
            out[flag] = value[flag]
    if "subtitles" in value:
        sub = value["subtitles"]
        if not isinstance(sub, str) or not (sub in _SUBTITLES or _LANG.fullmatch(sub)):
            raise TvsError(400, "bad_setting", "player.subtitles is off, auto or a language code.")
        out["subtitles"] = sub
    if "audio_language" in value:
        lang = value["audio_language"]
        if lang is not None and (not isinstance(lang, str) or not _LANG.fullmatch(lang)):
            raise TvsError(400, "bad_setting", "player.audio_language is a language code.")
        out["audio_language"] = lang
    unknown = set(value) - {"autoplay_next", "resume", "subtitles", "audio_language"}
    if unknown:
        raise TvsError(400, "bad_setting", "player takes autoplay_next, resume, subtitles and audio_language.")
    return out


START_PAGES = ("home", "movies", "series", "live", "search")
# Side menu entries, the same ids on both TVs (tc-shared RailOrder). Home and Settings can't be hidden.
MENU_ITEMS = ("home", "live", "movies", "series", "sport", "guide", "favorites", "mylist", "history", "search", "settings")
_MENU_KEEP = ("home", "settings")
_DEVICE_PLATFORMS = ("android", "appletv")
_DEVICE_KEY = re.compile(r"[A-Za-z0-9_.:/-]{1,120}")
DEVICE_MAX_KEYS = 800
DEVICE_MAX_BYTES = 256_000


def _start_page(value: Any) -> str:
    if value not in START_PAGES:
        raise TvsError(400, "bad_setting", "start_page is one of " + ", ".join(START_PAGES) + ".")
    return value


def _flags(name: str, value: Any, flags: tuple[str, ...]) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise TvsError(400, "bad_setting", f"{name} is an object.")
    unknown = set(value) - set(flags)
    if unknown:
        raise TvsError(400, "bad_setting", f"{name} takes " + ", ".join(flags) + ".")
    for flag, v in value.items():
        if not isinstance(v, bool):
            raise TvsError(400, "bad_setting", f"{name}.{flag} is true or false.")
    return dict(value)


def _menu(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) - {"order", "hidden"}:
        raise TvsError(400, "bad_setting", "menu takes order and hidden.")
    out: dict[str, list[str]] = {}
    for part in ("order", "hidden"):
        if part in value:
            items = _strings(value[part], limit=len(MENU_ITEMS), length=20, what=f"menu.{part}")
            if any(i not in MENU_ITEMS for i in items):
                raise TvsError(400, "bad_setting", f"menu.{part} uses " + ", ".join(MENU_ITEMS) + ".")
            out[part] = items
    if any(i in _MENU_KEEP for i in out.get("hidden", [])):
        raise TvsError(400, "bad_setting", "Home and Settings always stay in the menu.")
    return out


def _look(value: Any) -> dict[str, bool]:
    return _flags("look", value, ("sidebar_labels", "motion"))


def _startup(value: Any) -> dict[str, bool]:
    return _flags("startup", value, ("autostart", "resume_live", "resume_vod", "confirm_exit"))


def _phone_page(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TvsError(400, "bad_setting", "phone_page is true or false.")
    return value


def _device_value(value: Any, depth: int = 0) -> Any:
    """A plain value, or a list of them two levels deep at most (Android sends ["t", ["a", "b"]] for a string set)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 4000:
            raise TvsError(400, "bad_setting", "A device setting is at most 4000 characters.")
        return value
    if isinstance(value, list) and len(value) <= 600 and depth < 2:
        return [_device_value(v, depth + 1) for v in value]
    raise TvsError(400, "bad_setting", "A device setting is a plain value or a list of them.")


def _device(value: Any) -> dict[str, dict[str, Any]]:
    """Everything else a TV app keeps for itself, per platform, so it lives on the hub too.

    The TV sends the whole set for its platform; the hub keeps it as given (no meaning attached) and hands it
    back, for example to a reinstalled TV or when the admin copies one TV's setup to another.
    """
    if not isinstance(value, dict) or set(value) - set(_DEVICE_PLATFORMS):
        raise TvsError(400, "bad_setting", "device has android and appletv.")
    out: dict[str, dict[str, Any]] = {}
    for platform, prefs in value.items():
        if prefs is None:
            continue
        if not isinstance(prefs, dict) or len(prefs) > DEVICE_MAX_KEYS:
            raise TvsError(400, "bad_setting", f"device.{platform} is an object of at most {DEVICE_MAX_KEYS} settings.")
        clean: dict[str, Any] = {}
        for key, v in prefs.items():
            if not isinstance(key, str) or not _DEVICE_KEY.fullmatch(key):
                raise TvsError(400, "bad_setting", f"device.{platform} has a name the hub can't use.")
            clean[key] = _device_value(v)
        out[platform] = clean
    if len(json.dumps(out)) > DEVICE_MAX_BYTES:
        raise TvsError(400, "bad_setting", "device is too large.")
    return out


_VALIDATORS = {
    "home_rows": _home_rows,
    "favorites": _favorites,
    "hidden_categories": _hidden,
    "language": _language,
    "parental": _parental,
    "player": _player,
    "start_page": _start_page,
    "menu": _menu,
    "look": _look,
    "startup": _startup,
    "phone_page": _phone_page,
    "device": _device,
}
SETTING_KEYS = tuple(_VALIDATORS)
# Settings a person can carry from TV to TV ("tv.<key>" in their person settings). They sit on top of the TV's own
# when that person is watching. Device-wide things (parental lock, phone page, startup, the device set) stay per TV.
PERSONAL_KEYS = ("start_page", "home_rows", "menu", "look", "player", "language", "hidden_categories")


def personal(key: str, value: Any) -> Any:
    """Validate one person-level TV setting (the part after `tv.`)."""
    if key not in PERSONAL_KEYS:
        raise TvsError(400, "bad_setting", f"{key} isn't a TV setting a person carries.")
    return None if value is None else _VALIDATORS[key](value)


def person_layers(db: Any) -> dict[str, dict[str, Any]]:
    """Every person's TV settings, keyed by person id, for the TV config. A TV applies the one who is watching."""
    try:
        rows = db.all("SELECT person_id, key, value FROM person_settings WHERE key LIKE 'tv.%'")
    except Exception:  # noqa: BLE001 - a TV config must not fail on the settings table
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["key"])[3:]
        if key not in PERSONAL_KEYS:
            continue
        try:
            out.setdefault(str(row["person_id"]), {})[key] = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
    return out


# What the iPhone and the admin page draw for each setting. The hub owns the labels and choices, so every editor
# shows the same thing. `scope` is "tv" (this TV only) or "person" (a person can carry it).
SCHEMA: list[dict[str, Any]] = [
    {"key": "start_page", "label": "Opens on", "type": "choice", "choices": list(START_PAGES), "scope": "person"},
    {"key": "menu.hidden", "label": "Hidden from the menu", "type": "multi", "choices": [m for m in MENU_ITEMS if m not in _MENU_KEEP],
     "scope": "person"},
    {"key": "look.sidebar_labels", "label": "Menu labels always shown", "type": "bool", "scope": "person"},
    {"key": "look.motion", "label": "Animations", "type": "bool", "scope": "person"},
    {"key": "player.autoplay_next", "label": "Play the next episode", "type": "bool", "scope": "person"},
    {"key": "player.resume", "label": "Resume where you left off", "type": "bool", "scope": "person"},
    {"key": "player.subtitles", "label": "Subtitles", "type": "choice", "choices": ["off", "auto"], "scope": "person"},
    {"key": "startup.autostart", "label": "Open when the TV turns on (Android)", "type": "bool", "scope": "tv"},
    {"key": "startup.resume_live", "label": "Play the last channel at start", "type": "bool", "scope": "tv"},
    {"key": "startup.resume_vod", "label": "Continue the last film at start", "type": "bool", "scope": "tv"},
    {"key": "startup.confirm_exit", "label": "Press Back twice to leave", "type": "bool", "scope": "tv"},
    {"key": "parental.locked", "label": "Parental lock", "type": "bool", "scope": "tv"},
    {"key": "phone_page", "label": "Phone page on this TV (local web remote)", "type": "bool", "scope": "tv"},
]


def schema() -> dict[str, Any]:
    return {"settings": SCHEMA, "menu_items": list(MENU_ITEMS), "start_pages": list(START_PAGES),
            "personal_keys": list(PERSONAL_KEYS)}
