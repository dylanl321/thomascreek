"""Every Thomas Creek TV as the phone sees it: one list, one detail, settings and a few actions.

This wraps what the hub already keeps about a TV (the registry in tvapp, the fleet check-ins in
tvfleet, now-playing from /v1/tv/state) into one shape for the iPhone app and the assistant.

Settings that should be the same on every screen live in the TV's hub record under `settings`, with
`settings_rev` counting changes. Picture and audio stay on each TV. The last write wins, from the phone
or from the TV. Every change sends `config_changed` to the TV, which pulls its config at once.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from . import tvcec, tvfleet
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


_VALIDATORS = {
    "home_rows": _home_rows,
    "favorites": _favorites,
    "hidden_categories": _hidden,
    "language": _language,
    "parental": _parental,
    "player": _player,
}
SETTING_KEYS = tuple(_VALIDATORS)
