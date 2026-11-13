"""Zero-touch setup for a TV that already has network debugging turned on.

The person turns on Developer options and network debugging (or Wireless debugging) once.
After that the hub pairs if Android 11+ asks for a code, waits for the Allow prompt, reads
the model, installs the signed app, grants the overlay, and can make the app Home without
disabling the stock launcher. Running it again is safe: a box that already has this build
is not reinstalled.

Automatic updates for a box set up this way wait until overnight, and never while the app
is on screen. Two home-screen crashes reported by the app put the previous launcher back.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .tvapp import TvApp, TvAppError, clean_ip

_CODE = re.compile(r"^[A-Za-z0-9]{6,8}$")
_NOTIFY = "android.permission.POST_NOTIFICATIONS"


def classify(props: dict[str, str]) -> dict[str, Any]:
    """What kind of Android box this is, from getprop. Fire TV is called out and not made Home."""
    manufacturer = props.get("ro.product.manufacturer") or ""
    model = props.get("ro.product.model") or ""
    brand = props.get("ro.product.brand") or ""
    device = props.get("ro.product.device") or ""
    blob = f"{manufacturer} {model} {brand} {device}".lower()
    sdk_raw = props.get("ro.build.version.sdk") or "0"
    sdk = int(sdk_raw) if sdk_raw.isdigit() else 0
    warning = None
    if "amazon" in blob or device.lower().startswith("aft") or "fire tv" in blob:
        kind = "fire_tv"
        warning = "Fire TV ignores the Home button. The app can be installed, but it can't be the home screen."
    elif "nvidia" in blob or "shield" in blob:
        kind = "shield"
    elif any(word in blob for word in ("chromecast", "google tv streamer", "sabrina", "kirkwood", "boreal")):
        kind = "google_tv"
    elif any(word in blob for word in ("sony", "tcl", "hisense", "philips", "bravia")):
        kind = "android_tv"
    else:
        kind = "generic"
    return {
        "kind": kind, "model": model[:80] or None, "manufacturer": manufacturer[:80] or None,
        "sdk": sdk, "warning": warning, "home_ok": kind != "fire_tv",
    }


def parse_mdns(text: str) -> list[dict[str, Any]]:
    """`adb mdns services` lines into one row per IP, with the connect and pairing ports."""
    found: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", line)
        if not match:
            continue
        ip, port = match.group(1), int(match.group(2))
        row = found.setdefault(ip, {"ip": ip, "name": line.split()[0] if line.split() else ip})
        if "pairing" in line.lower():
            row["pairing_port"] = port
        else:
            row["connect_port"] = port
    return [found[ip] for ip in sorted(found)]


def _steps(phase: str, *, home: bool, fire: bool) -> list[dict[str, str]]:
    order = ["debug", "allow", "identify", "install", "permissions", "home", "enroll"]
    labels = {
        "debug": "Network debugging",
        "allow": "Allow this computer",
        "identify": "See what the TV is",
        "install": "Install Thomas Creek TV",
        "permissions": "Allow the overlay",
        "home": "Home screen",
        "enroll": "Register with the hub",
    }
    current = {
        "offline": "debug", "pairing": "debug", "needs_allow": "allow", "identifying": "identify",
        "installing": "install", "permissions": "permissions", "home": "home", "failed": "debug",
    }.get(phase, "debug")
    index = len(order) if phase == "ready" else order.index(current)
    out = []
    for i, step in enumerate(order):
        if step == "home" and (not home or fire):
            state = "skipped"
        elif phase == "failed" and step == current:
            state = "failed"
        elif i < index:
            state = "done"
        elif i == index:
            state = "current"
        else:
            state = "todo"
        out.append({"id": step, "title": labels[step], "state": state})
    return out


def status_of(box: dict[str, Any]) -> dict[str, Any]:
    phase = box.get("phase") or ("ready" if box.get("state") == "ok" else box.get("state") or "offline")
    device = box.get("device") or {}
    home = box.get("home") or {}
    return {
        "ip": box.get("ip"),
        "name": box.get("name"),
        "room": box.get("room"),
        "profile": box.get("profile"),
        "phase": phase,
        "message": box.get("message"),
        "you_do": box.get("you_do"),
        "warning": device.get("warning"),
        "device": device or None,
        "installed": box.get("installed"),
        "home": home or None,
        "capabilities": box.get("capabilities") or {},
        "steps": _steps(phase, home=bool(home.get("wanted")), fire=device.get("kind") == "fire_tv"),
        "overnight_only": bool(box.get("overnight_only")),
    }


class Setup:
    def __init__(self, tv: TvApp):
        self.tv = tv

    async def discover(self) -> dict[str, Any]:
        if not self.tv.adb.available:
            return {"devices": [], "hint": "The hub can't run adb, so it can't look for TVs. Type the address instead."}
        text = await self.tv.adb.mdns()
        devices = parse_mdns(text)
        hint = None if devices else "Nothing answered. On the TV, turn on network debugging, or type its address."
        return {"devices": devices, "hint": hint}

    async def run(self, ip: str, *, name: str | None, room: str | None, profile: str, pairing_port: int | None,
                  pairing_code: str | None, home: bool) -> dict[str, Any]:
        ip = clean_ip(ip)
        if profile not in ("adult", "kids"):
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        if pairing_code is not None and not _CODE.fullmatch(pairing_code):
            raise TvAppError(400, "bad_code", "The pairing code is the 6 characters on the TV")
        if pairing_port is not None and not 1 <= int(pairing_port) <= 65535:
            raise TvAppError(400, "bad_port", "That pairing port isn't valid")
        if (pairing_code is None) != (pairing_port is None):
            raise TvAppError(400, "bad_code", "Wireless debugging needs both the code and the pairing port")
        if room:
            self.tv._room(room)
        boxes = self.tv.boxes()
        box = boxes.get(ip, {"added_at": time.time()})
        box.update(name=(name or box.get("name") or ip)[:80], profile=profile, auto=True, overnight_only=True, setup=True)
        if room:
            box["room"] = room
        box["home"] = {**(box.get("home") or {}), "wanted": bool(home)}
        boxes[ip] = box
        self.tv._save_boxes(boxes)

        if pairing_code and pairing_port and not box.get("paired"):
            self._phase(ip, "pairing", "Pairing with the TV.", "Keep the pairing screen open")
            result = await self.tv.adb.pair(f"{ip}:{int(pairing_port)}", pairing_code)
            if result.code != 0 or "successfully paired" not in result.out.lower():
                return self._phase(ip, "failed", "That pairing code didn't work. On the TV, open Wireless debugging and use the new code.",
                                   "Enter the new pairing code")
            self.tv._update_box(ip, paired=True)

        refreshed = await self.tv.refresh_box(ip)
        if refreshed.get("state") == "no_adb":
            return self._phase(ip, "failed", "The hub can't run adb.", None)
        if refreshed.get("state") == "needs_allow":
            return self._phase(ip, "needs_allow",
                               "On the TV, tap Allow debugging from this computer, and tick Always allow.",
                               "Tap Allow on the TV")
        if refreshed.get("state") != "ok":
            return self._phase(ip, "offline",
                               "The TV didn't answer. Turn it on, with network debugging turned on.",
                               "Turn on network debugging")

        serial = self.tv._serial(ip)
        self._phase(ip, "identifying", "Checking what this TV is.", None)
        device = classify(await self.tv.adb.props(serial))
        self.tv._update_box(ip, device=device)
        if device["kind"] == "fire_tv":
            home = False
            self.tv._update_box(ip, home={**(self.tv.boxes()[ip].get("home") or {}), "wanted": False})

        latest = self.tv.latest()
        if not latest or not self.tv.apk_path(int(latest["versionCode"])).exists():
            return self._phase(ip, "failed", "The hub doesn't have a TV app build yet.", None)
        if (refreshed.get("installed") or 0) >= int(latest["versionCode"]):
            self.tv._update_box(ip, last_result="already installed")
        else:
            self._phase(ip, "installing", "Installing Thomas Creek TV.", None)
            try:
                installed = await self.tv.install(ip)
            except TvAppError as exc:
                return self._phase(ip, "failed", exc.message, None)
            if str(installed.get("last_result") or "").startswith("postponed"):
                return self._phase(ip, "installing",
                                   "The app is on screen. The hub will finish the install when it isn't, overnight.",
                                   "Leave the app, or wait until overnight")

        self._phase(ip, "permissions", "Allowing the overlay.", None)
        package = self.tv.cfg.package
        overlay = await self.tv.adb.grant_overlay(serial, package)
        notes = await self.tv.adb.grant(serial, package, _NOTIFY) if device.get("sdk", 0) >= 33 else None
        capabilities = {
            "overlay": overlay.code == 0,
            "notifications": notes is None or notes.code == 0,
            "home": bool(device.get("home_ok")),
            "fire_tv": device["kind"] == "fire_tv",
        }
        self.tv._update_box(ip, capabilities=capabilities)

        if home and device.get("home_ok"):
            self._phase(ip, "home", "Setting the home screen. The stock launcher stays installed.", None)
            previous = await self.tv.adb.current_home(serial)
            component = self.tv.cfg.home_activity
            result = await self.tv.adb.set_home(serial, component)
            home_state = {"wanted": True, "set": result.code == 0 and "error" not in result.out.lower(),
                          "component": component, "previous": previous if previous != component else (self.tv.boxes()[ip].get("home") or {}).get("previous")}
            self.tv._update_box(ip, home=home_state)
        return self._phase(ip, "ready",
                           "Thomas Creek TV is installed. The first time it opens, it registers itself. Leave the TV on.",
                           "Open Thomas Creek TV once" if not self._enrolled(ip) else None)

    def _enrolled(self, ip: str) -> bool:
        return any(tv.get("ip") == ip and tv.get("status") == "approved" for tv in self.tv.tvs().values())

    def _phase(self, ip: str, phase: str, message: str, you_do: str | None) -> dict[str, Any]:
        box = self.tv._update_box(ip, phase=phase, message=message, you_do=you_do)
        if phase == "ready" and self._enrolled(ip):
            box = self.tv._update_box(ip, message="This TV is registered and has its source.", you_do=None)
        return status_of(box)

    async def rollback_home(self, ip: str) -> dict[str, Any]:
        ip = clean_ip(ip)
        box = self.tv.boxes().get(ip)
        if box is None:
            raise TvAppError(404, "not_found", "That box isn't on the list")
        previous = (box.get("home") or {}).get("previous")
        if not previous:
            raise TvAppError(409, "no_previous", "The hub doesn't have the previous home screen for this TV")
        serial = self.tv._serial(ip)
        result = await self.tv.adb.set_home(serial, previous)
        if result.code != 0:
            raise TvAppError(502, "failed", "The TV didn't change its home screen")
        self.tv._update_box(ip, home={**(box.get("home") or {}), "set": False, "wanted": False, "rolled_back": True})
        return self._phase(ip, "ready", "The previous home screen is back. Thomas Creek TV stays installed.", None)

    async def note_crashes(self, ip: str | None, crashes: int) -> None:
        if not ip or crashes < 2:
            return
        box = self.tv.boxes().get(ip)
        home = (box or {}).get("home") or {}
        if box and home.get("set") and home.get("previous"):
            await self.rollback_home(ip)
            self.tv._update_box(ip, message="The app crashed at startup, so the previous home screen is back.")


async def reconcile(tv: TvApp) -> None:
    """Reconnect boxes after a reboot, and surface a new Allow prompt."""
    for ip, box in list(tv.boxes().items()):
        if not box.get("setup"):
            continue
        try:
            cur = await tv.refresh_box(ip)
        except TvAppError:
            continue
        phase = box.get("phase")
        if cur.get("state") == "needs_allow" and phase == "ready":
            tv._update_box(ip, phase="needs_allow",
                           message="After a restart the TV is asking to allow debugging again.",
                           you_do="Tap Allow on the TV")
        elif cur.get("state") == "ok" and phase == "needs_allow":
            tv._update_box(ip, phase="ready", message="Connected again.", you_do=None)
