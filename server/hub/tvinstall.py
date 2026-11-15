"""Every Android TV in the house, whether it has Thomas Creek TV, and installing it on one or many at once.

The phone's TV fleet screen reads `inventory`. It merges four lists by IP address:
- the network scan the remote setup already does (every Android TV announcing the remote service),
- `adb mdns services` (TVs with Wireless debugging on, and their random ports),
- the boxes the hub manages over ADB (`tvapp.boxes`),
- the TVs that run the app and registered (`tvapp.tvs`), with their health from the fleet check-ins.

Each row gets one `state` and one `next` sentence, so the phone can show a single button per TV.

`Installer.start` runs `tvsetup.Setup.run` in the background, so the phone never waits on a five-minute
request. It keeps asking a TV for up to ten minutes while the TV still needs debugging turned on or the Allow
tap, and sends a push when the TV needs someone or is done. Starting it again for a TV that is already
running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .tvapp import TvAppError, clean_ip
from .tvsetup import Setup, classify, parse_mdns, status_of

log = logging.getLogger(__name__)

WAIT_S = 600
RETRY_S = 5
BUSY = ("queued", "pairing", "identifying", "installing", "permissions", "home")
NOT_ANDROID = "Thomas Creek TV runs on Android TV and Google TV. This one can still use the remote."


def kind_hint(name: str | None, model: str | None) -> dict[str, Any]:
    """Shield, Google TV, Fire TV or another Android TV, from what the network scan says."""
    return classify({"ro.product.model": model or "", "ro.product.manufacturer": "", "ro.product.brand": name or ""})


def guide(kind: str, *, wireless: bool) -> list[str]:
    """What to press on this kind of TV to turn debugging on. Shown on the phone, one line per step."""
    if kind == "shield":
        return [
            "On the TV open Settings, Device Preferences, About.",
            "Click Build 7 times, until it says you are a developer.",
            "Go back to Device Preferences, Developer options.",
            "Turn on Network debugging.",
        ]
    if kind == "fire_tv":
        return [
            "On the TV open Settings, My Fire TV, About.",
            "Click the device name 7 times, until it says you are a developer.",
            "Go back to My Fire TV, Developer options.",
            "Turn on ADB debugging.",
        ]
    steps = [
        "On the TV open Settings, System, About.",
        "Click Android TV OS build 7 times, until it says you are a developer.",
        "Go back to System, Developer options.",
    ]
    if wireless:
        steps += ["Turn on Wireless debugging.", "Open Wireless debugging, then Pair device with pairing code."]
    else:
        steps += ["Turn on USB debugging. If there is a Wireless debugging switch, turn that on too."]
    return steps


async def _adb_mdns(h: Any) -> dict[str, dict[str, Any]]:
    adb = h.tv_app.adb
    if not adb.available:
        return {}
    try:
        return {row["ip"]: row for row in parse_mdns(await adb.mdns())}
    except Exception as exc:  # noqa: BLE001
        log.info("adb mdns: %s", exc)
        return {}


async def _scan(h: Any, fresh: bool) -> list[Any]:
    remotes = getattr(h, "remotes", None)
    if remotes is None:
        return []
    try:
        return list(await remotes._scan_network(fresh))
    except Exception as exc:  # noqa: BLE001
        log.info("tv scan: %s", exc)
        return []


async def inventory(h: Any, *, fresh: bool = False, installer: Installer | None = None,
                    now: float | None = None) -> dict[str, Any]:
    from . import tvs as tvs_mod

    now = time.time() if now is None else now
    tv_app = h.tv_app
    latest = tv_app.latest() or {}
    latest_code = int(latest.get("versionCode") or 0)
    scan, wireless = await asyncio.gather(_scan(h, fresh), _adb_mdns(h))
    boxes = tv_app.boxes()
    rows: dict[str, dict[str, Any]] = {}
    others: list[dict[str, Any]] = []

    def row(ip: str) -> dict[str, Any]:
        return rows.setdefault(ip, {"ip": ip, "name": None, "model": None, "room": None, "sources": []})

    for found in scan:
        if found.kind != "android":
            others.append({"ip": found.host, "name": found.name, "kind": found.kind, "note": NOT_ANDROID})
            continue
        r = row(found.host)
        r["name"] = r["name"] or found.name
        r["model"] = r["model"] or found.model
        r["sources"].append("network")
    for ip, w in wireless.items():
        r = row(ip)
        r["sources"].append("wireless_debugging")
        r["wireless"] = {"connect_port": w.get("connect_port"), "pairing_port": w.get("pairing_port")}
        if ip in boxes and w.get("connect_port") and boxes[ip].get("connect_port") != w["connect_port"]:
            tv_app._update_box(ip, connect_port=int(w["connect_port"]), wireless=True)
    for ip, box in boxes.items():
        if ip.startswith("code:"):
            continue
        r = row(ip)
        r["sources"].append("hub")
        r["box"] = box
        r["name"] = box.get("name") if box.get("name") and box.get("name") != ip else r["name"]
        r["room"] = box.get("room") or r["room"]
        device = box.get("device") or {}
        r["model"] = device.get("model") or r["model"]
    for install_id, tv in tv_app.tvs().items():
        if tv.get("status") == "revoked":
            continue
        ip = tv.get("ip")
        if not ip:
            continue
        r = row(ip)
        r["sources"].append("app")
        summary = tvs_mod.summary(h, install_id, tv, now)
        summary["approve_code"] = tv.get("approve_code") if tv.get("status") == "pending" else None
        r["tv"] = summary
        r["name"] = summary.get("name") or r["name"]
        r["room"] = summary.get("room") or r["room"]
        r["model"] = r["model"] or tv.get("model")

    out = [_shape(r, latest_code, latest.get("versionName"), installer) for r in rows.values()]
    out.sort(key=lambda x: (_ORDER.get(x["state"], 9), str(x.get("name") or x["ip"]).lower()))
    counts: dict[str, int] = {}
    for x in out:
        counts[x["state"]] = counts.get(x["state"], 0) + 1
    return {
        "tvs": out,
        "others": sorted(others, key=lambda x: str(x["name"]).lower()),
        "counts": counts,
        "latest_version": latest.get("versionName"),
        "adb_available": tv_app.adb.available,
        "updates_waiting": sum(1 for x in out if x["state"] == "update"),
    }


_ORDER = {"needs_allow": 0, "pairing": 0, "installing": 1, "failed": 2, "approve": 2, "update": 3,
          "not_installed": 4, "not_set_up": 5, "ready": 6, "offline": 7}


def _shape(r: dict[str, Any], latest_code: int, latest_name: str | None, installer: Installer | None) -> dict[str, Any]:
    ip = r["ip"]
    box = r.get("box") or {}
    tv = r.get("tv") or {}
    wireless = r.get("wireless") or {}
    device = box.get("device") or kind_hint(r.get("name"), r.get("model"))
    kind = device.get("kind") or "generic"
    phase = box.get("phase")
    installed_code = box.get("installed")
    running = bool(installer and installer.running(ip))
    base = {
        "ip": ip,
        "name": r.get("name") or ip,
        "model": r.get("model"),
        "room": r.get("room"),
        "kind": kind,
        "profile": tv.get("profile") or box.get("profile"),
        "tv_id": tv.get("id"),
        "online": tv.get("online"),
        "app_version": tv.get("app_version"),
        "managed": bool(box),
        "wireless": bool(wireless or box.get("wireless")),
        "pairing_port": wireless.get("pairing_port"),
        "warning": device.get("warning"),
        "guide": guide(kind, wireless=bool(wireless or box.get("wireless"))),
        "sources": sorted(set(r.get("sources") or [])),
    }
    message = box.get("message")
    approve_code = None
    if running or phase in BUSY:
        state, label = "installing", message or "Installing Thomas Creek TV."
    elif phase == "needs_allow":
        state, label = "needs_allow", "On the TV, tap Allow and tick Always allow."
    elif wireless.get("pairing_port") and not box.get("paired") and not tv:
        state, label = "pairing", "The TV is showing a pairing code. Type it here."
    elif phase == "failed":
        state, label = "failed", message or "Setup stopped."
    elif tv and tv.get("status") == "pending":
        state, label = "approve", "Waiting for approval."
        approve_code = tv.get("approve_code")
    elif tv:
        stale = tv.get("update_available") or (installed_code and latest_code and installed_code < latest_code)
        if stale:
            state, label = "update", f"Update to {latest_name}." if latest_name else "An update is waiting."
        elif tv.get("online"):
            state, label = "ready", "Up to date." if tv.get("app_version") else "Running Thomas Creek TV."
        else:
            state, label = "offline", "Has the app. Off or asleep right now."
    elif box.get("state") == "ok" and installed_code:
        if latest_code and installed_code < latest_code:
            state, label = "update", f"Update to {latest_name}." if latest_name else "An update is waiting."
        else:
            state, label = "ready", "Installed. Open it once so it registers."
    elif box.get("state") == "ok":
        state, label = "not_installed", "Ready to install."
    else:
        state, label = "not_set_up", "Turn on debugging on the TV, then install."
    base.update(state=state, next=label, you_do=box.get("you_do") if state in ("needs_allow", "failed", "installing") else None,
                approve_code=approve_code, steps=status_of(dict(box, ip=ip))["steps"] if box else None)
    return base


class Installer:
    """Background setups, one per TV address."""

    def __init__(self, h: Any):
        self.h = h
        self.jobs: dict[str, asyncio.Task] = {}

    def running(self, ip: str) -> bool:
        task = self.jobs.get(ip)
        return bool(task and not task.done())

    async def start(self, ip: str, *, name: str | None = None, room: str | None = None, profile: str = "adult",
                    home: bool = False, pairing_code: str | None = None, pairing_port: int | None = None,
                    wait_s: float = WAIT_S) -> dict[str, Any]:
        ip = clean_ip(ip)
        if profile not in ("adult", "kids"):
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        tv_app = self.h.tv_app
        if not tv_app.adb.available:
            raise TvAppError(409, "no_adb", "The hub can't run adb, so it can't install over the network.")
        latest = tv_app.latest()
        if not latest or not tv_app.apk_path(int(latest["versionCode"])).exists():
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet.")
        if room:
            tv_app._room(room)
        if self.running(ip) and not pairing_code:
            return status_of(dict(tv_app.boxes().get(ip) or {}, ip=ip))
        boxes = tv_app.boxes()
        box = boxes.get(ip, {"added_at": time.time()})
        box.update(name=(name or box.get("name") or ip)[:80], profile=profile, auto=True, overnight_only=True,
                   setup=True, mode="adb", phase="queued", message="Starting.", you_do=None)
        if room:
            box["room"] = room
        boxes[ip] = box
        tv_app._save_boxes(boxes)
        old = self.jobs.get(ip)
        if old and not old.done():
            old.cancel()
        self.jobs[ip] = asyncio.create_task(self._run(ip, name=name, room=room, profile=profile, home=home,
                                                      pairing_code=pairing_code, pairing_port=pairing_port,
                                                      wait_s=wait_s))
        return status_of(dict(box, ip=ip))

    async def _run(self, ip: str, *, name: str | None, room: str | None, profile: str, home: bool,
                   pairing_code: str | None, pairing_port: int | None, wait_s: float) -> None:
        tv_app = self.h.tv_app
        label = name or (tv_app.boxes().get(ip) or {}).get("name") or ip
        deadline = time.monotonic() + wait_s
        told_allow = False
        out: dict[str, Any] = {}
        try:
            while True:
                code, port = pairing_code, pairing_port
                if code and not port:
                    port = await self._pairing_port(ip)
                    if not port:
                        tv_app._update_box(ip, phase="failed",
                                           message="The hub can't see the pairing screen. On the TV, open Wireless debugging, then Pair device with pairing code, and try again.",
                                           you_do="Open Pair device with pairing code")
                        return
                out = await Setup(tv_app).run(ip, name=name, room=room, profile=profile,
                                              pairing_port=port if code else None,
                                              pairing_code=code if port else None, home=home)
                pairing_code = pairing_port = None  # a code works once
                phase = out.get("phase")
                if phase == "needs_allow" and not told_allow:
                    told_allow = True
                    await self._notify(f"{label} needs one tap", "On the TV, tap Allow and tick Always allow.")
                if phase not in ("needs_allow", "offline") or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(RETRY_S)
            phase = out.get("phase")
            if phase == "ready":
                await self._notify(f"{label} has Thomas Creek TV", out.get("message") or "It's installed.")
            elif phase in ("needs_allow", "offline"):
                tv_app._update_box(ip, phase=phase,
                                   message="Stopped waiting after 10 minutes. Turn on debugging, then tap Install again."
                                   if phase == "offline" else "Stopped waiting for Allow. Tap Install again, then Allow on the TV.")
        except asyncio.CancelledError:
            raise
        except TvAppError as exc:
            tv_app._update_box(ip, phase="failed", message=exc.message, you_do=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("tv install %s: %s", ip, exc)
            tv_app._update_box(ip, phase="failed", message="Setup hit an error on the hub. Try again.", you_do=None)

    async def _pairing_port(self, ip: str) -> int | None:
        for _ in range(3):
            found = (await _adb_mdns(self.h)).get(ip) or {}
            if found.get("pairing_port"):
                return int(found["pairing_port"])
            await asyncio.sleep(1)
        return None

    async def _notify(self, title: str, body: str) -> None:
        notify = self.h.tv_app.notify
        if notify:
            try:
                await notify(title, body)
            except Exception as exc:  # noqa: BLE001
                log.info("tv install notify: %s", exc)

    async def start_many(self, ips: list[str], **opts: Any) -> dict[str, Any]:
        started, problems = [], []
        for raw in ips[:50]:
            try:
                started.append(await self.start(raw, **opts))
            except TvAppError as exc:
                problems.append({"ip": raw, "code": exc.code, "message": exc.message})
        return {"started": started, "problems": problems}
