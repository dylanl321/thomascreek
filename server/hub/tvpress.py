"""One remote for every TV, for the phone media apps.

A phone used to need three routes and two device ids to drive one TV: `/v1/tvs/{id}/actions` (play, stop and
HDMI-CEC), `/v1/tv/remote` (the TV app's own buttons) and `media.command` on a Home Assistant device (arrows, OK,
apps, volume). Here the phone gets one list and one press:

- `listing`: every TV this person may use, with what is on it and `controls`, the buttons that work on that TV
  right now. The phone draws those and nothing else, so no button is dead.
- `press`: one button, or `send` with a film, an episode or a channel to play there. The hub picks the way to the
  TV (the TV app's event stream, Home Assistant's remote, or a key over network debugging), tries the next way when
  one fails, and answers with one sentence either way.

Who may use a TV is the same rule as the old remote (`tv.stop` for that room), so a child reaches their own TV and
nobody else's. A kids' TV and a child are only tuned to kids' channels (`tvcontrol._fields`).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import tvcontrol, tvfleet, tvs, tvwho
from .actions import ActionError
from .home import media_commands
from .policy import Subject, Target
from .tvapp import TvAppError
from .tvremote import remote_info

log = logging.getLogger("hub.tvpress")

# The phone's name for a button -> the `media.command` name Home Assistant's remote takes.
HA_KEYS = {
    "up": "up", "down": "down", "left": "left", "right": "right", "select": "select", "back": "back", "home": "home",
    "menu": "menu", "play_pause": "play_pause", "rewind": "rewind", "fast_forward": "fast_forward",
    "channel_up": "ch_up", "channel_down": "ch_down", "volume_up": "vol_up", "volume_down": "vol_down",
    "mute": "mute", "text": "text",
}
# The same buttons as Android key codes, for a box the hub reaches over network debugging.
ADB_KEYS = {
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER", "back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "menu": "KEYCODE_MENU",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "rewind": "KEYCODE_MEDIA_REWIND",
    "fast_forward": "KEYCODE_MEDIA_FAST_FORWARD", "channel_up": "KEYCODE_CHANNEL_UP",
    "channel_down": "KEYCODE_CHANNEL_DOWN", "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE", "power_off": "KEYCODE_SLEEP", "power_on": "KEYCODE_WAKEUP",
}
# What the TV app does itself when it is on screen. Both TV apps: play, pause, skip and stop. The rest is the
# Android TV app only (the Apple TV app doesn't act on them yet), so an Apple TV is never offered them here.
APP_BOTH = ("play", "pause", "seek", "stop", "tune")
APP_ANDROID = ("channel_up", "channel_down", "back", "home", "text")
# The order the phone shows them in. `controls` is a subset of this, in this order.
ORDER = ("power_on", "power_off", "input", "open_app", "up", "down", "left", "right", "select", "back", "home",
         "menu", "play_pause", "play", "pause", "rewind", "fast_forward", "seek", "stop", "channel_up",
         "channel_down", "tune", "send", "volume_up", "volume_down", "mute", "volume_set", "text", "app", "source")
KEYS = frozenset(ORDER)
# For these the TV app comes first when it is on screen: it knows the channel list and the player.
APP_FIRST = frozenset({"play", "pause", "seek", "stop", "tune", "channel_up", "channel_down", "text"})

WINDOW_S = 5.0
MAX_IN_WINDOW = 40  # a held arrow repeats about six times a second
ADB_READY_S = 30.0
ONLINE_S = 120

SAID = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "select": "OK", "back": "Back", "home": "Home",
    "menu": "Menu", "play_pause": "Play or pause", "play": "Playing", "pause": "Paused", "rewind": "Back a bit",
    "fast_forward": "Forward a bit", "seek": "Skipped", "stop": "Stopped", "channel_up": "Channel up",
    "channel_down": "Channel down", "tune": "Tuned", "send": "Playing", "volume_up": "Louder", "volume_down": "Quieter",
    "mute": "Mute", "volume_set": "Volume set", "text": "Typed", "power_on": "Turned on", "power_off": "Turned off",
    "input": "Switched the TV to this box", "open_app": "Opened Thomas Creek TV", "app": "Opened", "source": "Switched",
}


class PressError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# -- the list ---------------------------------------------------------------------------------------------------------

def listing(h: Any, subject: Subject, now: float | None = None) -> dict[str, Any]:
    """Every TV this person may use: approved, in a room they may use, the one they used last first."""
    now = time.time() if now is None else now
    from .tvhandoff import _order

    names = {area["id"]: area.get("name") for area in h.home.areas.values()}
    rows = []
    for install_id, tv in h.tv_app.tvs().items():
        if tv.get("status") != "approved" or not _allowed(h, subject, tv, install_id):
            continue
        rows.append(_row(h, subject, install_id, tv, names, now))
    return {"tvs": _order(h, subject.person_id, rows)}


def _row(h: Any, subject: Subject, install_id: str, tv: dict[str, Any], names: dict[str, Any],
         now: float) -> dict[str, Any]:
    online = _online(h, install_id, tv, now)
    ways = _ways(h, subject, install_id, tv, online)
    row: dict[str, Any] = {
        "id": install_id,
        "name": tv.get("name") or "TV",
        "room": tv.get("room"),
        "room_name": names.get(tv.get("room") or ""),
        "kids": tv.get("profile") == "kids",
        "online": online,
        "foreground": bool(tv.get("foreground", True)) if online else False,
        "now_playing": tvs._now_playing(h, tv.get("playing")) if online else None,
        "watching": _watching(h, install_id, tv, now),
        "controls": [key for key in ORDER if ways.get(key)],
    }
    st = _state(h, tv)
    if st is not None:
        attrs = st.get("attributes", {})
        if "volume_set" in ways and attrs.get("volume_level") is not None:
            row["volume"] = round(float(attrs["volume_level"]) * 100)
        if "mute" in ways and "is_volume_muted" in attrs:
            row["muted"] = bool(attrs.get("is_volume_muted"))
        info = remote_info(st, h.home.media_links.get(tv.get("media_entity"))) or {}
        if "app" in ways:
            apps = [a for a in info.get("apps") or [] if a.get("kid") or not subject.is_child_like]
            row["apps"] = [{"id": a["id"], "name": a["name"]} for a in apps]
            if info.get("app"):
                row["app"] = info["app"]
        if "source" in ways:
            row["inputs"] = list(info.get("inputs") or [])
    if not row["controls"]:
        row["note"] = _why_nothing(tv, online)
    return row


def _why_nothing(tv: dict[str, Any], online: bool) -> str:
    name = tv.get("name") or "This TV"
    if not online:
        return f"{name} is off or asleep, and the hub has no way to wake it yet."
    return f"{name} can't be driven from a phone yet."


def _watching(h: Any, install_id: str, tv: dict[str, Any], now: float) -> str | None:
    """The name of the person the TV plays for, when the hub knows one."""
    held = tv.get("watcher")  # a person id, kept by the TV's own check-in
    person = h.db.person(held) if isinstance(held, str) and held else None
    if person and person.get("name"):
        return str(person["name"])
    return tvwho.current(h, install_id, now).get("name")


def _online(h: Any, install_id: str, tv: dict[str, Any], now: float) -> bool:
    seen = tvs._seen(h, install_id, tv)
    return bool(seen and now - seen < ONLINE_S)


def _allowed(h: Any, subject: Subject, tv: dict[str, Any], install_id: str) -> bool:
    target = Target(entity_id=tv.get("media_entity") or install_id, area=tv.get("room"), kind="tv")
    return bool(h.policy.decide(subject, "tv.stop", target, {}).allowed)


def _state(h: Any, tv: dict[str, Any]) -> dict[str, Any] | None:
    eid = tv.get("media_entity")
    st = h.home.states.get(eid) if eid else None
    return st if isinstance(st, dict) and st.get("state") != "unavailable" else None


def _android(h: Any, install_id: str) -> bool:
    """An Android or Google TV, by its own check-in. An Apple TV checks in as `unknown` with no Android version; a TV
    that hasn't checked in yet is treated the same, so it is never offered a button only the Android app acts on."""
    fleet = tvfleet._devices(h).get(install_id) or {}
    return fleet.get("platform") in ("google_tv", "android_tv") or bool(fleet.get("android"))


# -- which way each button goes -----------------------------------------------------------------------------------

def _ways(h: Any, subject: Subject, install_id: str, tv: dict[str, Any], online: bool) -> dict[str, list[str]]:
    """Button -> the ways the hub can send it to this TV now, best first. A button with no way is left out."""
    ha = _ha_keys(h, subject, tv)
    android = _android(h, install_id)
    adb = android and tvs._can_adb(h, tv)
    app = online and bool(tv.get("foreground", True))
    out: dict[str, list[str]] = {}

    def add(key: str, way: str) -> None:
        if way not in out.setdefault(key, []):
            out[key].append(way)

    if app:
        for key in APP_BOTH + (APP_ANDROID if android else ()):
            add(key, "app")
    if online:
        add("send", "app")  # a film, an episode or a channel from the phone: the TV app takes it when it is running
    for key in ha:
        add(key, "ha")
    if adb:
        for key in ADB_KEYS:
            add(key, "adb")
        add("input", "adb")
        add("text", "adb")
    if online and android:
        for key in ("volume_up", "volume_down", "mute"):
            add(key, "app")
    if adb or (online and android):
        add("open_app", "adb" if adb else "app")
    # The TV app knows its own player and channel list, so its way comes first for those buttons.
    for key in APP_FIRST:
        ways = out.get(key)
        if ways and "app" in ways:
            out[key] = ["app"] + [w for w in ways if w != "app"]
    return out


def _ha_keys(h: Any, subject: Subject, tv: dict[str, Any]) -> list[str]:
    """The buttons this TV has through Home Assistant, in the phone's names."""
    st = _state(h, tv)
    eid = tv.get("media_entity")
    if st is None or not eid:
        return []
    target = Target(entity_id=eid, area=h.home.entity_area.get(eid) or tv.get("room"), kind="media")
    if not h.policy.decide(subject, "media.command", target, {}).allowed:
        return []
    have = set(media_commands(st, h.home.media_links.get(eid)))
    keys = [key for key, cmd in HA_KEYS.items() if cmd in have]
    if "power" in have:
        keys += ["power_on", "power_off"]
    if "volume" in have:
        keys.append("volume_set")
    if "app" in have:
        keys.append("app")
    if "source" in have:
        keys.append("source")
    return keys


# -- one press -------------------------------------------------------------------------------------------------

async def press(h: Any, subject: Subject, install_id: str, body: Any) -> dict[str, Any]:
    """Send one button to one TV. Answers {ok, via, message}; raises PressError with a sentence to show."""
    body = body if isinstance(body, dict) else {}
    key = body.get("key")
    if not isinstance(key, str) or key not in KEYS:
        raise PressError(400, "bad_request", "That button isn't on this remote.")
    try:
        install_id, tv = h.tv_app.resolve(str(install_id))
    except TvAppError as exc:
        raise PressError(exc.status, exc.code, exc.message) from None
    if tv.get("status") != "approved":
        raise PressError(403, "pending", "That TV isn't ready yet")
    if not _allowed(h, subject, tv, install_id):
        raise PressError(403, "forbidden", "That room isn't one you can use")
    name = tv.get("name") or "The TV"
    online = _online(h, install_id, tv, time.time())
    ways = _ways(h, subject, install_id, tv, online).get(key) or []
    if not ways:
        raise PressError(409, "not_now", _why_not(key, name, online))
    if not _pace(h, subject.person_id, install_id):
        return {"ok": False, "via": None, "message": "That's as fast as it goes."}
    last: PressError | None = None
    for way in ways:
        try:
            said = await _send(h, subject, install_id, tv, key, body, way)
        except PressError as exc:
            if exc.status in (400, 403):
                raise  # a refusal is the answer; another way must not get round it
            last = exc
            log.info("press %s on %s failed by %s: %s", key, install_id, way, exc.code)
            continue
        if key in ("play", "tune", "send"):
            tvwho.claim_for_play(h, install_id, subject.person_id, subject.role)
        return {"ok": True, "via": way, "message": said or f"{SAID[key]} on {name}."}
    assert last is not None
    raise last


def _why_not(key: str, name: str, online: bool) -> str:
    if not online:
        return f"{name} is off or asleep, so it can't take that. Turn it on first."
    if key in APP_FIRST or key in ("back", "home"):
        return f"{name} isn't showing Thomas Creek TV right now. Open it on the TV, then try again."
    return f"{name} doesn't have that button from a phone."


async def _send(h: Any, subject: Subject, install_id: str, tv: dict[str, Any], key: str, body: dict[str, Any],
                way: str) -> str | None:
    if way == "app":
        return await _by_app(h, subject, install_id, tv, key, body)
    if way == "ha":
        return await _by_ha(h, subject, tv, key, body)
    return await _by_adb(h, install_id, tv, key, body, subject.name)


async def _by_app(h: Any, subject: Subject, install_id: str, tv: dict[str, Any], key: str,
                  body: dict[str, Any]) -> str | None:
    """The TV app's own buttons, on its event stream (`tvcontrol`), and its queued stop and volume."""
    if key == "send":
        _send_title(h, subject, install_id, tv, body)
        return None
    if key in ("stop", "open_app"):
        try:
            out = await tvs.act(h, install_id, {"action": key}, subject.name)
        except tvs.TvsError as exc:
            raise PressError(exc.status, exc.code, exc.message) from None
        return str(out.get("message") or "") or None
    if key in ("volume_up", "volume_down", "mute"):
        params = {"mute": True} if key == "mute" else {"delta": 1 if key == "volume_up" else -1}
        try:
            tvs._fleet(h, install_id, "tv_volume", params, subject.name)
        except tvs.TvsError as exc:
            raise PressError(exc.status, exc.code, exc.message) from None
        return None
    command = {"play": "play", "pause": "pause", "seek": "seek", "tune": "tune", "channel_up": "channel_up",
               "channel_down": "channel_down", "back": "back", "home": "home", "text": "text"}[key]
    relay = {"device": install_id, "command": command}
    for field in ("position_s", "channel_key", "text"):
        if field in body:
            relay[field] = body[field]
    try:
        out = tvcontrol.send(h, subject, relay)
    except tvcontrol.RemoteError as exc:
        raise PressError(exc.status if exc.status != 200 else 409, exc.code, exc.message) from None
    if not out.get("ok"):
        raise PressError(429, "slow_down", str(out.get("message") or "That's as fast as it goes."))
    return None


def _send_title(h: Any, subject: Subject, install_id: str, tv: dict[str, Any], body: dict[str, Any]) -> None:
    """Play a film, an episode or a channel from the phone on this TV. What may play is the old send's rule
    (`tvs._kids_check`), and a child's phone is held to it on every TV, not only on a kids' TV."""
    fields = ("channel_key", "movie_id", "episode_id", "series_id", "title")
    wanted: dict[str, Any] = {k: body[k] for k in fields if k in body}
    kids = tv.get("profile") == "kids" or subject.is_child_like
    judged = {**tv, "profile": "kids" if kids else tv.get("profile")}
    key = wanted.get("channel_key")
    try:
        if kids and isinstance(key, str) and tvs.valid_key(key):
            # The lineup knows which channels are kids' channels, the same way the remote's tune does.
            if not tvcontrol._channel(h, key).get("kids"):
                name = tv.get("name") or "That TV"
                raise PressError(403, "kids_tv", f"{name} is a kids' TV, and this isn't on its kids' list."
                                 if tv.get("profile") == "kids" else "That channel isn't on the kids' list.")
            judged = {**tv, "profile": "adult"}
        tvs._play(h, install_id, judged, wanted, True)
    except tvs.TvsError as exc:
        raise PressError(exc.status, exc.code, exc.message) from None
    except tvcontrol.RemoteError as exc:
        raise PressError(exc.status, exc.code, exc.message) from None


async def _by_ha(h: Any, subject: Subject, tv: dict[str, Any], key: str, body: dict[str, Any]) -> str | None:
    """Home Assistant's remote for this TV, through the same action the house app's remote uses."""
    eid = tv.get("media_entity")
    params: dict[str, Any]
    if key in ("power_on", "power_off"):
        params = {"command": "power", "on": key == "power_on"}
    elif key == "volume_set":
        level = body.get("volume")
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 100:
            raise PressError(400, "bad_request", "Say how loud, from 0 to 100.")
        params = {"command": "volume", "volume": int(level)}
    elif key == "app":
        params = {"command": "app", "app": str(body.get("app") or "")[:80]}
    elif key == "source":
        params = {"command": "source", "source": str(body.get("source") or "")[:80]}
    elif key == "text":
        params = {"command": "text", "text": str(body.get("text") or "")[:120]}
    else:
        params = {"command": HA_KEYS[key]}
    try:
        plan = h.actions.plan(subject, "media.command", eid, params)
        if not plan.decision.allowed:
            raise PressError(403, "forbidden", "Ask a parent" if plan.decision.ask_parent else "Not allowed")
        out = await h.actions.run(subject, plan, source="app")
    except ActionError as exc:
        raise PressError(exc.status, exc.error, exc.message) from None
    if out.get("dry_run"):
        return str(out.get("message") or "Practice mode: nothing was changed")
    return None


async def _by_adb(h: Any, install_id: str, tv: dict[str, Any], key: str, body: dict[str, Any],
                  who: str | None) -> str | None:
    """A key over network debugging, or HDMI-CEC for the TV's own power and input (`tvcec`)."""
    name = tv.get("name") or "The TV"
    if key in ("input", "power_on", "power_off", "open_app"):
        action = {"input": {"action": "tv_input"}, "power_on": {"action": "tv_power", "power": "on"},
                  "power_off": {"action": "tv_power", "power": "off"}, "open_app": {"action": "open_app"}}[key]
        try:
            out = await tvs.act(h, install_id, action, who)
        except tvs.TvsError as exc:
            raise PressError(exc.status, exc.code, exc.message) from None
        return str(out.get("message") or "") or None
    serial = await _serial(h, tv)
    if key == "text":
        text = " ".join(str(body.get("text") or "").split())
        if not text or len(text) > 80 or "://" in text:
            raise PressError(400, "bad_request", "Say what to type.")
        # Only what the box's shell can't read as anything else; a space is `%s` to `input text`.
        safe = "".join(ch for ch in text if (ch.isascii() and ch.isalnum()) or ch in " .,-@+=").replace(" ", "%s")
        if not safe:
            raise PressError(400, "bad_request", "Say what to type.")
        args = ["-s", serial, "shell", "input", "text", safe]
    else:
        args = ["-s", serial, "shell", "input", "keyevent", ADB_KEYS[key]]
    result = await h.tv_app.adb.run(args, 15)
    if result.code != 0:
        _ready(h).pop(serial, None)
        raise PressError(502, "key_failed", f"{name} didn't take that. Try again in a moment.")
    return None


def _ready(h: Any) -> dict[str, float]:
    store: dict[str, float] | None = getattr(h, "_press_adb", None)
    if store is None:
        store = {}
        h._press_adb = store
    return store


async def _serial(h: Any, tv: dict[str, Any]) -> str:
    """The box's adb address, connected. A box that answered in the last half minute isn't asked again, so a held
    arrow costs one command per press, not three."""
    ip = tv.get("ip")
    ready = _ready(h)
    serial = h.tv_app._serial(ip) if ip else None
    if serial and ready.get(serial, 0) > time.time():
        return serial
    try:
        serial = await tvs._adb_ready(h, tv)
    except tvs.TvsError as exc:
        raise PressError(exc.status, exc.code, exc.message) from None
    ready[serial] = time.time() + ADB_READY_S
    return serial


def _pace(h: Any, person_id: str | None, install_id: str) -> bool:
    now = time.time()
    store: dict[tuple[str, str], list[float]] | None = getattr(h, "_press_times", None)
    if store is None:
        store = {}
        h._press_times = store
    key = (person_id or "", install_id)
    recent = [stamp for stamp in store.get(key, []) if now - stamp < WINDOW_S]
    if len(recent) >= MAX_IN_WINDOW:
        store[key] = recent
        return False
    recent.append(now)
    store[key] = recent
    return True
