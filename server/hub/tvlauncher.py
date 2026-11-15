"""Kids launcher: a TV that starts in Thomas Creek TV, signed in as one person, with only the apps the hub lists.

Per TV, stored on the TV record under `launcher`:
    {"on": bool, "person_id": str | None, "apps": [package, ...], "guard": bool}

`apps` is the ordered tile row and the only other apps that TV may open. `guard` brings the TV back
home as soon as an app that isn't on the list comes to the front.

What the hub does over network debugging when a box at the TV's address is on its list:
  on:  enable the HOME alias, make it the home screen (remembering the previous one), allow Usage
       access and turn on the app's accessibility guard (window changes only, no screen content).
  off: put the previous home screen back, disable the alias, and turn the guard off.
Without network debugging the TV asks once on screen to become the home app, and the guard falls
back to Usage access when someone allowed it.

The TV reports the apps it can open (`POST /v1/tv/apps/installed`) so the admin page can offer them by name.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

ALIAS = "com.ipvio.stream.Home"
GUARD = "com.thomascreek.tv.device.GuardService"
MAX_APPS = 40
MAX_INSTALLED = 200
_PKG = re.compile(r"[a-zA-Z][\w]*(\.[\w]+)+")
_jobs: set[asyncio.Task[Any]] = set()


class LauncherError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def view(tv: dict[str, Any]) -> dict[str, Any]:
    """The launcher block for a TV record, filled with defaults."""
    raw = tv.get("launcher") or {}
    return {
        "on": bool(raw.get("on")),
        "person_id": raw.get("person_id") or None,
        "apps": [p for p in (raw.get("apps") or []) if isinstance(p, str) and _PKG.fullmatch(p)][:MAX_APPS],
        "guard": raw.get("guard", True) is not False,
    }


def config_block(tv: dict[str, Any]) -> dict[str, Any]:
    """What the TV gets in its config. Off is sent too, so a TV that was a launcher lets go."""
    block = view(tv)
    if not block["on"]:
        return {"on": False}
    return block


def allowed(tv: dict[str, Any]) -> list[str] | None:
    """The app list for `/v1/tv/apps` when the launcher is on, else None (the profile's usual list)."""
    block = view(tv)
    return block["apps"] if block["on"] else None


def status(tv: dict[str, Any]) -> dict[str, Any]:
    """What the hub last managed to do on the box."""
    return dict(tv.get("launcher_state") or {})


def installed(tv: dict[str, Any]) -> list[dict[str, str]]:
    return list(tv.get("launchable") or [])


def clean(body: dict[str, Any], people: list[dict[str, Any]]) -> dict[str, Any]:
    on = bool(body.get("on"))
    person_id = body.get("person_id") or None
    if person_id is not None:
        person_id = str(person_id)
        if not any(str(p.get("id")) == person_id for p in people):
            raise LauncherError(400, "That person isn't in the household.")
    apps: list[str] = []
    for name in body.get("apps") or []:
        text = str(name or "").strip()
        if not text:
            continue
        if not _PKG.fullmatch(text) or len(text) > 200:
            raise LauncherError(400, "That doesn't look like an app.")
        if text not in apps:
            apps.append(text)
    if len(apps) > MAX_APPS:
        raise LauncherError(400, f"A launcher shows up to {MAX_APPS} apps.")
    return {"on": on, "person_id": person_id, "apps": apps, "guard": body.get("guard", True) is not False}


def set_launcher(h: Any, install_id: str, body: dict[str, Any], *, apply: bool = True) -> dict[str, Any]:
    """Save the launcher for one TV, tell the TV, and set the box's home screen in the background."""
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    if tv is None or tv.get("status") != "approved":
        raise LauncherError(404, "That TV isn't in the house.")
    people = _people(h)
    block = clean(body, people)
    package = h.tv_app.cfg.package
    if block["apps"] and package in block["apps"]:
        block["apps"].remove(package)
    tv["launcher"] = block
    person = next((p for p in people if str(p.get("id")) == block["person_id"]), None)
    if block["on"] and person is not None:
        # A child's launcher is a kids' TV everywhere on the hub: bedtime, rights, lists.
        tv["profile"] = "kids" if person.get("role") == "child" else tv.get("profile") or "adult"
    tvs[install_id] = tv
    h.tv_app._save_tvs(tvs)
    h.tv_app.bump_config(install_id)
    if apply:
        start_apply(h, install_id)
    return {**view(tv), "status": status(tv)}


def save_installed(h: Any, install_id: str, apps: Any) -> dict[str, Any]:
    """The apps a TV can open, as it reported them ({package, label})."""
    if not isinstance(apps, list):
        raise LauncherError(400, "Send the apps as a list.")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in apps[:MAX_INSTALLED]:
        if not isinstance(item, dict):
            continue
        pkg = str(item.get("package") or "").strip()
        if not _PKG.fullmatch(pkg) or pkg in seen or len(pkg) > 200:
            continue
        seen.add(pkg)
        label = str(item.get("label") or pkg).strip()[:60] or pkg
        out.append({"package": pkg, "label": label})
    out.sort(key=lambda a: a["label"].lower())
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    if tv is None:
        raise LauncherError(404, "That TV isn't in the house.")
    tv["launchable"] = out
    tv["launchable_at"] = time.time()
    tvs[install_id] = tv
    h.tv_app._save_tvs(tvs)
    return {"ok": True, "count": len(out)}


def start_apply(h: Any, install_id: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(apply_box(h, install_id))
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


def box_for(h: Any, tv: dict[str, Any]) -> str | None:
    ip = tv.get("ip")
    if ip and ip in h.tv_app.boxes():
        return str(ip)
    return None


async def apply_box(h: Any, install_id: str) -> dict[str, Any]:
    """Make the box match the launcher setting over network debugging. Never raises."""
    try:
        return await _apply_box(h, install_id)
    except Exception as exc:  # noqa: BLE001 - background work reports, it doesn't crash the hub
        log.warning("tvlauncher: %s: %s", install_id, exc)
        return _note(h, install_id, ok=False, note="The hub couldn't reach this TV over network debugging.")


async def _apply_box(h: Any, install_id: str) -> dict[str, Any]:
    tv = h.tv_app.tvs().get(install_id) or {}
    block = view(tv)
    ip = box_for(h, tv)
    if ip is None:
        note = ("The TV asks once to become the home app. For the Home button and the guard without asking, "
                "add this TV under Thomas Creek TVs so the hub can reach it.") if block["on"] else "Off."
        return _note(h, install_id, ok=block["on"] is False, note=note, adb=False)
    refreshed = await h.tv_app.refresh_box(ip)
    if refreshed.get("state") != "ok":
        return _note(h, install_id, ok=False, adb=True,
                     note="The TV didn't answer over network debugging. It will be set the next time it does.")
    adb = h.tv_app.adb
    serial = h.tv_app._serial(ip)
    package = h.tv_app.cfg.package
    alias = f"{package}/{ALIAS}"
    guard = f"{package}/{GUARD}"
    previous_state = status(tv)
    if block["on"]:
        await adb.run(["-s", serial, "shell", "pm", "enable", alias], 15)
        current = await adb.current_home(serial)
        previous = previous_state.get("previous")
        if current and current != alias and not current.startswith(package + "/"):
            previous = current
        result = await adb.set_home(serial, alias)
        home_now = await adb.current_home(serial)
        home_ok = home_now == alias or (result.code == 0 and "error" not in result.out.lower() and home_now is None)
        await adb.run(["-s", serial, "shell", "appops", "set", package, "GET_USAGE_STATS", "allow"], 15)
        guard_ok = False
        if block["guard"]:
            guard_ok = await _guard(adb, serial, guard, True)
        else:
            await _guard(adb, serial, guard, False)
        note = "Home screen and guard are set." if home_ok and (guard_ok or not block["guard"]) else (
            "The guard is on. This TV keeps its own Home button, so the guard brings it back." if guard_ok
            else "The TV didn't take the home screen. The app asks on screen instead.")
        return _note(h, install_id, ok=home_ok or guard_ok, adb=True, home=home_ok, guard=guard_ok, previous=previous, note=note)
    previous = previous_state.get("previous")
    if previous:
        await adb.set_home(serial, previous)
    await adb.run(["-s", serial, "shell", "pm", "disable", alias], 15)
    await _guard(adb, serial, guard, False)
    return _note(h, install_id, ok=True, adb=True, home=False, guard=False, previous=previous,
                 note="The previous home screen is back.")


async def _guard(adb: Any, serial: str, component: str, on: bool) -> bool:
    got = await adb.run(["-s", serial, "shell", "settings", "get", "secure", "enabled_accessibility_services"], 15)
    raw = got.out.strip()
    listed = [] if raw in ("", "null") else [s for s in raw.split(":") if s]
    others = [s for s in listed if s != component]
    wanted = others + [component] if on else others
    if wanted != listed:
        if wanted:
            await adb.run(["-s", serial, "shell", "settings", "put", "secure", "enabled_accessibility_services", ":".join(wanted)], 15)
        else:
            await adb.run(["-s", serial, "shell", "settings", "delete", "secure", "enabled_accessibility_services"], 15)
    if on:
        await adb.run(["-s", serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1"], 15)
        check = await adb.run(["-s", serial, "shell", "settings", "get", "secure", "enabled_accessibility_services"], 15)
        return component in check.out
    return True


def _note(h: Any, install_id: str, *, ok: bool, note: str, adb: bool = False, home: bool | None = None,
          guard: bool | None = None, previous: str | None = None) -> dict[str, Any]:
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    state = {"ok": ok, "note": note, "adb": adb, "home": home, "guard": guard, "at": time.time()}
    if previous:
        state["previous"] = previous
    elif tv and (tv.get("launcher_state") or {}).get("previous"):
        state["previous"] = tv["launcher_state"]["previous"]
    if tv is not None:
        tv["launcher_state"] = state
        tvs[install_id] = tv
        h.tv_app._save_tvs(tvs)
    return state


def _people(h: Any) -> list[dict[str, Any]]:
    from .tvapp import _household

    return _household(h.db)
