"""HDMI-CEC for Thomas Creek TV boxes, run by the hub over network debugging (adb).

A box on Android 11 (the Shield) has no `cmd hdmi_control`, and an app can't reach the CEC service, so the
hub drives the framework's own CEC paths with keys:

* Turn on / switch to this box: waking the box sends One Touch Play (<Text View On> + <Active Source>),
  which turns the TV on and moves it to this input. A box that is already awake is put to sleep and woken
  again with "turn the TV off with the box" held off for that moment, so the TV is never sent <Standby>.
* Turn off: the box goes to sleep and, with "turn the TV off with the box" on, sends <Standby> to the TV.
* Volume: the volume keys. With CEC volume control on, the box sends them to the TV or receiver over CEC.

A playback box can't pick another of the TV's inputs; CEC lets the TV do that, not a source.

`probe` reads what the box knows: CEC on, CEC volume on, awake, and whether the TV has ever answered on the
CEC bus. A TV that never answers usually has CEC switched off in its own settings (Anynet+, Bravia Sync,
SimpLink, CEC). The result is kept on the TV's hub record as `cec`, so the phone can say why a button did
nothing.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .home import iso

class CecError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


ACTIONS = ("tv_power", "tv_input", "tv_volume", "cec_check")
VOLUME_KEYS = {"up": "KEYCODE_VOLUME_UP", "down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE"}
AUTO_OFF = "hdmi_control_auto_device_off_enabled"
# Between sleep and wake in the quiet cycle: long enough for the box to reach sleep and send its CEC messages.
CYCLE_S = 2.5

_PROBE = ("settings get global hdmi_control_enabled; "
          "settings get global hdmi_control_volume_control_enabled; "
          f"settings get global {AUTO_OFF}; "
          "dumpsys power | grep -m1 mWakefulness=; "
          "dumpsys hdmi_control | grep -E 'mAddress:|\\[R\\]|port_id: .*cec: '")
_HEADER = re.compile(r"\[R\].*message=<[^>]*>\s*([0-9A-Fa-f])[0-9A-Fa-f]:")
_ADDRESS = re.compile(r"mAddress:\s*(\d+)")


def parse(out: str) -> dict[str, Any]:
    """The probe's output as facts. Unknown values stay None, never a guess."""
    lines = [line.strip() for line in out.splitlines()]
    head = lines[:3] + [""] * (3 - len(lines[:3]))

    def flag(text: str, default: bool | None) -> bool | None:
        if text == "1":
            return True
        if text == "0":
            return False
        return default  # "null": the setting was never written, so the framework default holds

    awake: bool | None = None
    own: set[int] = set()
    heard: set[int] = set()
    port = False
    for line in lines:
        if "mWakefulness=" in line:
            state = line.split("mWakefulness=", 1)[1].strip().lower()
            awake = state.startswith("awake") or state.startswith("dream")
        elif (m := _ADDRESS.search(line)):
            own.add(int(m.group(1)))
        elif (m := _HEADER.search(line)):
            heard.add(int(m.group(1), 16))
        elif "cec: true" in line:
            port = True
    enabled = flag(head[0], True if port else None)
    return {
        "enabled": bool(enabled and (port or own)),
        "volume": bool(flag(head[1], False)),
        "auto_off": flag(head[2], True) is not False,
        "awake": awake,
        # Every [R] line from our own address is our broadcast echoed back. Anything else is another device.
        "tv_answers": bool(heard - own) if own else bool(heard),
    }


def note(cec: dict[str, Any], name: str) -> str | None:
    """One line on what stops CEC working here, or None when nothing does."""
    if not cec.get("enabled"):
        return f"HDMI-CEC is off on {name}. Turn on HDMI-CEC in the box's Settings, Display & Sound."
    if not cec.get("tv_answers"):
        return ("The TV hasn't answered on HDMI-CEC yet. Turn on CEC in the TV's own settings "
                "(Anynet+, Bravia Sync, SimpLink or HDMI-CEC), then try again.")
    return None


def view(tv: dict[str, Any]) -> dict[str, Any] | None:
    """What the phone sees under `cec` on a TV: the last probe plus a one-line note."""
    cec = tv.get("cec")
    if not isinstance(cec, dict):
        return None
    out = {key: cec.get(key) for key in ("enabled", "tv_answers", "volume", "auto_off", "awake")}
    out["checked"] = iso(float(cec.get("at") or 0)) if cec.get("at") else None
    out["note"] = note(cec, tv.get("name") or "this box")
    return out


async def probe(h: Any, install_id: str, serial: str, now: float | None = None) -> dict[str, Any]:
    result = await h.tv_app.adb.run(["-s", serial, "shell", _PROBE], 20)
    cec = parse(result.out)
    cec["at"] = time.time() if now is None else now
    tvs = h.tv_app.tvs()
    if install_id in tvs:
        tvs[install_id]["cec"] = cec
        h.tv_app._save_tvs(tvs)
    return cec


async def _key(h: Any, serial: str, key: str) -> bool:
    result = await h.tv_app.adb.run(["-s", serial, "shell", "input", "keyevent", key], 15)
    return result.code == 0


async def _announce(h: Any, serial: str, cec: dict[str, Any]) -> bool:
    """One Touch Play. Asleep: wake. Awake: sleep and wake without sending the TV <Standby>."""
    if cec.get("awake") is False:
        return await _key(h, serial, "KEYCODE_WAKEUP")
    adb = h.tv_app.adb
    held = cec.get("auto_off") is not False
    if held:
        await adb.run(["-s", serial, "shell", "settings", "put", "global", AUTO_OFF, "0"], 10)
    try:
        if not await _key(h, serial, "KEYCODE_SLEEP"):
            return False
        await asyncio.sleep(CYCLE_S)
        return await _key(h, serial, "KEYCODE_WAKEUP")
    finally:
        if held:
            await adb.run(["-s", serial, "shell", "settings", "put", "global", AUTO_OFF, "1"], 10)


async def act(h: Any, install_id: str, tv: dict[str, Any], body: dict[str, Any], serial: str) -> dict[str, Any]:
    """Run one CEC action over adb. Raises CecError with a sentence the phone can show."""
    action = str(body.get("action") or "")
    name = tv.get("name") or "The TV"
    power = str(body.get("power") or "")
    volume = VOLUME_KEYS.get(str(body.get("volume") or ""))
    if action == "tv_power" and power not in ("on", "off"):
        raise CecError(400, "bad_request", "Say on or off.")
    if action == "tv_volume" and volume is None:
        raise CecError(400, "bad_request", "Say up, down or mute.")
    cec = await probe(h, install_id, serial)
    problem = note(cec, name)
    if action == "cec_check":
        return {"ok": True, "via": "adb", "cec": view(h.tv_app.tvs()[install_id]),
                "message": problem or "HDMI-CEC works: the TV answers this box."}
    if action == "tv_volume":
        if not await _key(h, serial, volume):
            raise _failed(name)
        if not cec.get("volume"):
            message = (f"Sent the volume key to {name}. Volume over HDMI-CEC is off on this box, so it changes "
                       "only what the box itself controls.")
        else:
            message = "Sent the volume over HDMI-CEC." if not problem else f"Sent the volume over HDMI-CEC. {problem}"
        return {"ok": True, "via": "adb", "message": message}
    if action == "tv_power" and power == "off":
        if not await _key(h, serial, "KEYCODE_SLEEP"):
            raise _failed(name)
        if not cec.get("auto_off"):
            return {"ok": True, "via": "adb", "message": (
                f"{name} went to sleep. It doesn't turn the TV off with it: turn on \"Power off TV\" "
                "under HDMI-CEC in the box's settings.")}
        return {"ok": True, "via": "adb", "message": "Sent Standby over HDMI-CEC." + (f" {problem}" if problem else "")}
    # tv_power on, tv_input: One Touch Play.
    if not await _announce(h, serial, cec):
        raise _failed(name)
    done = "Turned the TV on over HDMI-CEC." if action == "tv_power" else "Switched the TV to this box over HDMI-CEC."
    return {"ok": True, "via": "adb", "message": done + (f" {problem}" if problem else "")}


def _failed(name: str) -> CecError:
    return CecError(502, "key_failed", f"{name} didn't take the key. Try again in a moment.")
