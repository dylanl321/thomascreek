"""What each Thomas Creek TV can do, and how it is feeling.

The TV posts on its own. Commands wait on the same event stream as play and
stop, under `tv_manage`. A box that cannot do something says so in a sentence.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

from . import tvupdate
from .db import new_id
from .home import iso

KV_DEVICES = "tv.fleet.devices"
KV_HISTORY = "tv.fleet.history"
KV_NOTICES = "tv.fleet.notices"
KV_KILL = "tv.fleet.kill"
KV_POLICY = "tv.fleet.policy"
KV_COMMANDS = "tv.fleet.commands"
KV_ROLLOUT = "tv.fleet.rollout"
KV_AUDIT = "tv.fleet.audit"

ONLINE_S = 120
SAMPLE_S = 60
COMMAND_TTL_S = 120
OUTPUT_MAX = 16 * 1024
SHOT_MAX = 4_000_000
OURS = "me.dlewis.thomascreek.tv"

CAPABILITIES = ("basic", "owner", "root")
PLATFORMS = ("google_tv", "android_tv", "unknown")
METRICS = ("cpu", "ram", "storage", "temperature", "ping", "rssi")
SHELL_TEMPLATES = {
    "logcat": "logcat -d -t 200 -s ThomasCreekTV:V AndroidRuntime:E",
    "packages": "pm list packages",
    "storage": "df -h /data",
    "uptime": "uptime",
    "network": "ip -4 addr",
    "battery": "dumpsys battery",
}

ACTIONS = (
    "reboot", "restart_app", "install_apk", "uninstall", "disable", "set_kiosk",
    "apply_policy", "kill_list", "screenshot", "show_message", "tv_power", "tv_volume",
)

# Cited in server/docs/fleet-kill-list.md. A save replaces this, including an empty one.
STARTER = (
    "com.adups.fota",
    "com.adups.fota.sysoper",
    "com.fw.upgrade.sysoper",
    "com.swe.dgbluancher",
)

_WAITERS: dict[str, list[asyncio.Event]] = {}


class FleetError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def report(h: Any, install_id: str, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Store one check-in and hand back the kill list, the policy, and anything still waiting."""
    now = time.time() if now is None else now
    _known(h, install_id)
    parsed = _parse_report(body)
    devices = _devices(h)
    prev = devices.get(install_id) or {}
    special = _special(prev, parsed, h)
    latest = {**prev, **parsed, "seen": now}
    if parsed.get("packages") is None and (
        not parsed.get("packages_hash") or parsed.get("packages_hash") == prev.get("packages_hash")
    ):
        latest["packages"] = prev.get("packages")
        if not parsed.get("packages_hash"):
            latest["packages_hash"] = prev.get("packages_hash")
    devices[install_id] = latest
    h.db.kv_set(KV_DEVICES, devices)
    _touch(h, install_id, now)
    if special or _due(h, install_id, now):
        _remember(h, install_id, latest, now)
    _alerts(h, install_id, latest, now)
    need = bool(parsed.get("packages_hash")) and parsed.get("packages") is None and parsed.get("packages_hash") != prev.get("packages_hash")
    if parsed.get("packages") is not None:
        need = False
    return {
        "ok": True,
        "need_packages": need,
        "kill_list": kill_list(h)["packages"],
        "policy": policy(h),
        "commands": pending(h, install_id, now),
    }


def devices(h: Any, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    sweep(h, now)
    rows = [_public(h, install_id, row, now) for install_id, row in _devices(h).items()]
    rows.sort(key=lambda row: str(row.get("name") or ""))
    return {"devices": rows}


def device(h: Any, install_id: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    row = _devices(h).get(install_id)
    if row is None:
        raise FleetError(404, "That TV isn't in the house.")
    view = _public(h, install_id, row, now)
    view["packages_hash"] = row.get("packages_hash")
    view["packages"] = list(row.get("packages") or [])
    view["policy"] = policy(h)
    view["last_command"] = _last_command(h, install_id)
    return view


def history(h: Any, install_id: str, metric: str, hours: int, now: float | None = None) -> dict[str, Any]:
    if metric not in METRICS:
        raise FleetError(400, "That chart isn't one the hub keeps.")
    if install_id not in _devices(h):
        raise FleetError(404, "That TV isn't in the house.")
    now = time.time() if now is None else now
    hours = 24 if hours is None else hours
    if hours < 1 or hours > 720:
        raise FleetError(400, "That chart isn't one the hub keeps.")
    start = now - hours * 3600
    points = []
    for point in _history(h).get(install_id) or []:
        if point.get("t", 0) < start or metric not in point:
            continue
        points.append({"t": iso(point["t"]), "v": point[metric]})
    return {"metric": metric, "points": points}


def kill_list(h: Any) -> dict[str, Any]:
    saved = h.db.kv_get(KV_KILL)
    if not isinstance(saved, list):
        return {"packages": list(STARTER)}
    return {"packages": [str(name) for name in saved]}


def set_kill_list(h: Any, packages: list[Any]) -> dict[str, Any]:
    clean = [_package(name) for name in packages]
    if OURS in clean or h.config.tv_app.package in clean:
        raise FleetError(400, "That app stays on the TV.")
    h.db.kv_set(KV_KILL, clean)
    _fanout(h, "kill_list", {"packages": clean})
    return {"packages": clean}


def policy(h: Any) -> dict[str, Any]:
    saved = h.db.kv_get(KV_POLICY)
    base = {"blocked": [], "restrict_settings": False, "hide_status_bar": False, "disable_keyguard": False}
    if not isinstance(saved, dict):
        return base
    blocked = [_package(name) for name in (saved.get("blocked") or [])]
    base["blocked"] = blocked
    for key in ("restrict_settings", "hide_status_bar", "disable_keyguard"):
        base[key] = bool(saved.get(key))
    return base


def set_policy(h: Any, body: dict[str, Any]) -> dict[str, Any]:
    saved = _policy_params(body or {})
    h.db.kv_set(KV_POLICY, saved)
    _fanout(h, "apply_policy", saved)
    return saved


def start_rollout(h: Any, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Offer one stored build to a few TVs first, then the rest once those are fine."""
    now = time.time() if now is None else now
    try:
        code = int((body or {}).get("version_code"))
    except (TypeError, ValueError):
        raise FleetError(404, "That build isn't on the hub.") from None
    if isinstance((body or {}).get("version_code"), bool) or not h.tv_app.apk_path(code).is_file():
        raise FleetError(404, "That build isn't on the hub.")
    stage = [str(item) for item in ((body or {}).get("stage") or [])]
    if not stage:
        raise FleetError(400, "Name the first room.")
    for install_id in stage:
        _known(h, install_id)
    then = (body or {}).get("then")
    if then not in (None, "all"):
        raise FleetError(400, "Name the first room.")
    immediate = bool((body or {}).get("now"))
    row = {
        "version_code": code,
        "state": "staging",
        "now": immediate,
        "then": then,
        "stage": stage,
        "status": {install_id: "waiting" for install_id in stage},
        "rest": "held",
        "at": now,
    }
    h.db.kv_set(KV_ROLLOUT, row)
    if immediate:
        _push_install(h, stage, code, now)
    return rollout(h)


def rollout(h: Any) -> dict[str, Any]:
    row = h.db.kv_get(KV_ROLLOUT)
    if not isinstance(row, dict) or row.get("state") in (None, "done"):
        return {"rollout": None}
    names = h.tv_app.tvs()
    return {"rollout": {
        "version_code": row.get("version_code"),
        "state": row.get("state"),
        "now": bool(row.get("now")),
        "stage": [{
            "id": install_id,
            "name": (names.get(install_id) or {}).get("name") or "TV",
            "status": (row.get("status") or {}).get(install_id) or "waiting",
        } for install_id in row.get("stage") or []],
        "rest": row.get("rest") or "held",
    }}


def offered_version(h: Any, install_id: str) -> int | None:
    """The build this TV should be offered, when a rollout is holding the newest one back."""
    row = h.db.kv_get(KV_ROLLOUT)
    if not isinstance(row, dict) or row.get("state") in (None, "done"):
        return None
    code = int(row["version_code"])
    stage = set(row.get("stage") or [])
    if install_id in stage or (row.get("state") == "rolling" and row.get("then") == "all"):
        return code
    previous = _previous_code(h, code)
    return previous if previous is not None else 0


def manifest_for(h: Any, code: int, current: str | None) -> dict[str, Any]:
    """The update body for one stored build. `0` means there is nothing older to offer."""
    if code <= 0:
        have = tvupdate._number(current)
        return {"version_code": have or 0, "up_to_date": True}
    path = h.tv_app.apk_path(code)
    if not path.is_file():
        raise FleetError(404, "That build isn't on the hub.")
    latest = h.tv_app.latest() or {}
    if int(latest.get("versionCode") or 0) == code:
        return tvupdate.manifest(h.tv_app, current)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    have = tvupdate._number(current)
    if have is not None and have >= code:
        return {"version_code": code, "up_to_date": True}
    catalog = h.db.kv_get("tv.builds") or {}
    meta = catalog.get(str(code)) if isinstance(catalog, dict) and isinstance(catalog.get(str(code)), dict) else {}
    return {
        "version_code": code,
        "version_name": meta.get("versionName") or str(code),
        "sha256": sha,
        "signer_sha256": h.db.kv_get("tvapp.signer"),
        "apk_path": f"/v1/tv/{code}",
        "package": h.config.tv_app.package,
        "notes": meta.get("notes") or None,
        "install": tvupdate._INSTALL,
        "idle_only": True,
        "silent": True,
        "window": tvupdate._window(h.tv_app),
    }


def note_health(h: Any, install_id: str, body: dict[str, Any], now: float | None = None) -> None:
    """Move a rollout on when a staged TV says the new build plays."""
    row = h.db.kv_get(KV_ROLLOUT)
    if not isinstance(row, dict) or row.get("state") not in ("staging", "rolling"):
        return
    now = time.time() if now is None else now
    failed = body.get("rollback") is True or body.get("playback_ok") is False
    healthy = body.get("boot_ok") is True and body.get("hub_ok") is True and body.get("playback_ok") is True and body.get("rollback") is False
    status = dict(row.get("status") or {})
    if install_id in (row.get("stage") or []) and failed:
        status[install_id] = "stopped"
        row["status"] = status
        row["state"] = "stopped"
        row["rest"] = "held"
        h.db.kv_set(KV_ROLLOUT, row)
        return
    if not healthy:
        return
    status[install_id] = "ok"
    row["status"] = status
    stage = list(row.get("stage") or [])
    if row.get("state") == "staging" and all(status.get(item) == "ok" for item in stage):
        if row.get("then") == "all":
            row["state"] = "rolling"
            row["rest"] = "offered"
            h.db.kv_set(KV_ROLLOUT, row)
            if row.get("now"):
                others = [item for item in h.tv_app.tvs() if item not in stage and (h.tv_app.tvs().get(item) or {}).get("status") == "approved"]
                _push_install(h, others, int(row["version_code"]), now)
            return
        row["state"] = "done"
    elif row.get("state") == "rolling":
        approved = [item for item, tv in h.tv_app.tvs().items() if tv.get("status") == "approved"]
        if approved and all(status.get(item) == "ok" for item in approved):
            row["state"] = "done"
    h.db.kv_set(KV_ROLLOUT, row)


def shell(h: Any, install_id: str, body: dict[str, Any], who: str | None, at_home: bool, now: float | None = None) -> dict[str, Any]:
    """Queue one allowlisted command. Free-form stays off unless the flag is on."""
    if not at_home:
        raise FleetError(403, "The TV app talks to the hub only on the home network")
    now = time.time() if now is None else now
    _known(h, install_id)
    if (_devices(h).get(install_id) or {}).get("capability") != "root":
        raise FleetError(400, "This TV has no shell.")
    template = (body or {}).get("template")
    line = (body or {}).get("command")
    if template:
        if template not in SHELL_TEMPLATES:
            raise FleetError(400, "That isn't one of the shell commands.")
        params = {"template": template}
        audit_template, audit_command = template, None
    elif isinstance(line, str):
        if not h.config.tv_fleet.shell:
            return {"ok": False, "message": "Shell commands are off."}
        if "\n" in line or not line.strip() or len(line) > 200:
            raise FleetError(400, "That isn't one of the shell commands.")
        params = {"command": line.strip()}
        audit_template, audit_command = None, line.strip()
    else:
        raise FleetError(400, "That isn't one of the shell commands.")
    queued = _enqueue(h, install_id, "shell", params, who, now)
    _audit(h, install_id, queued["id"], audit_template, audit_command, who, now)
    return queued


def audit(h: Any, limit: int = 50) -> dict[str, Any]:
    rows = h.db.kv_get(KV_AUDIT) or []
    if not isinstance(rows, list):
        rows = []
    shown = []
    for row in rows[-max(1, min(limit, 200)):]:
        shown.append({
            "at": row.get("at"), "who": row.get("who"), "id": row.get("tv"),
            "action": "shell", "template": row.get("template"), "command": row.get("command"),
            "ok": row.get("ok"),
        })
    return {"items": list(reversed(shown))}


def command_detail(h: Any, install_id: str, command_id: str) -> dict[str, Any]:
    for row in _commands(h).get(install_id) or []:
        if row.get("id") == command_id:
            view = _wire(row)
            view["ok"] = row.get("ok")
            view["detail"] = row.get("detail")
            if row.get("output") is not None:
                view["output"] = row.get("output")
                view["truncated"] = bool(row.get("truncated"))
            return view
    raise FleetError(404, "That command has already finished.")


def command(h: Any, install_id: str, action: str, params: dict[str, Any] | None, who: str | None = None,
            now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    _known(h, install_id)
    if action not in ACTIONS:
        raise FleetError(400, "That isn't a command this TV knows.")
    clean = _params(action, params or {}, h.config.tv_app.package)
    return _enqueue(h, install_id, action, clean, who, now)


def ack(h: Any, install_id: str, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    wanted = str((body or {}).get("id") or "")
    ok = (body or {}).get("ok")
    if not isinstance(ok, bool):
        raise FleetError(400, "Say how that command went.")
    rows = _commands(h)
    found = None
    for row in rows.get(install_id) or []:
        if row.get("id") == wanted and not row.get("acked") and now - float(row.get("ts") or 0) < COMMAND_TTL_S:
            found = row
            break
    if found is None:
        return {"ok": False, "message": "That command has already finished."}
    found["acked"] = True
    found["ok"] = ok
    found["detail"] = _sentence((body or {}).get("detail"))
    hits = (body or {}).get("hits")
    if isinstance(hits, list):
        found["hits"] = [str(name) for name in hits if str(name).strip()][:50]
    output = (body or {}).get("output")
    if isinstance(output, str):
        found["truncated"] = bool((body or {}).get("truncated")) or len(output) > OUTPUT_MAX
        found["output"] = output[:OUTPUT_MAX]
    rows[install_id] = [row for row in rows.get(install_id) or [] if now - float(row.get("ts") or 0) < _retain(h)]
    h.db.kv_set(KV_COMMANDS, rows)
    if found.get("hits"):
        _notice(h, install_id, "kill", True, now)
    elif found.get("action") == "kill_list":
        _notice(h, install_id, "kill", False, now)
    if found.get("action") == "shell":
        _audit_ok(h, found["id"], ok)
    return {"ok": True}


def pending(h: Any, install_id: str, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    _expire(h, now)
    return [_wire(row) for row in _commands(h).get(install_id) or [] if not row.get("acked")]


async def wait(h: Any, install_id: str, timeout: float) -> list[dict[str, Any]]:
    event = asyncio.Event()
    _WAITERS.setdefault(install_id, []).append(event)
    try:
        found = pending(h, install_id)
        if found or timeout <= 0:
            return found
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        waiting = _WAITERS.get(install_id, [])
        if event in waiting:
            waiting.remove(event)
    return pending(h, install_id)


def save_shot(h: Any, install_id: str, command_id: str, data: bytes) -> None:
    if not data or len(data) > SHOT_MAX:
        raise FleetError(400, "That picture didn't come through.")
    rows = _commands(h).get(install_id) or []
    if not any(row.get("id") == command_id and row.get("action") == "screenshot" for row in rows):
        raise FleetError(404, "That picture isn't here.")
    path = _shot_path(h, install_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def shot_path(h: Any, install_id: str) -> Path:
    path = _shot_path(h, install_id)
    if not path.is_file():
        raise FleetError(404, "That picture isn't here.")
    return path


def sweep(h: Any, now: float | None = None) -> None:
    """Say once when a TV has been quiet longer than the window."""
    now = time.time() if now is None else now
    for install_id, row in _devices(h).items():
        quiet = now - float(row.get("seen") or 0) >= _cfg(h).offline_after_min * 60
        _notice(h, install_id, "offline", quiet, now)


def _parse_report(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise FleetError(400, "The TV didn't say what it can do.")
    capability = body.get("capability")
    if capability not in CAPABILITIES:
        raise FleetError(400, "The TV didn't say what it can do.")
    platform = body.get("platform") or "unknown"
    if platform not in PLATFORMS:
        platform = "unknown"
    packages = body.get("packages") if isinstance(body.get("packages"), dict) else {}
    names = packages.get("list") if packages.get("changed") is True else None
    stored_names = None
    digest = packages.get("hash")
    if isinstance(names, list):
        stored_names = sorted({str(name).strip() for name in names if str(name).strip()})
        digest = hashlib.sha256("\n".join(stored_names).encode()).hexdigest()
    elif not isinstance(digest, str) or len(digest) != 64:
        digest = None
    network = body.get("network") if isinstance(body.get("network"), dict) else {}
    kind = network.get("kind") if network.get("kind") in ("wifi", "ethernet", "none") else "none"
    return {
        "capability": capability,
        "model": _text(body.get("model"), 80),
        "android": _text(body.get("android"), 20),
        "platform": platform,
        "app_version": _text(body.get("app_version"), 40),
        "cpu_pct": _num(body.get("cpu_pct"), 0, 100),
        "ram": _pair(body.get("ram")),
        "storage": _pair(body.get("storage")),
        "temperature_c": _num(body.get("temperature_c"), -20, 120),
        "network": {
            "kind": kind,
            "ssid": _text(network.get("ssid"), 64) if kind == "wifi" else None,
            "rssi_dbm": _num(network.get("rssi_dbm"), -120, 0) if kind == "wifi" else None,
        },
        "ping_ms": _num(body.get("ping_ms"), 0, 60_000),
        "foreground": _text(body.get("foreground"), 120),
        "uptime_s": _num(body.get("uptime_s"), 0, 10**9),
        "remote_battery_pct": _num(body.get("remote_battery_pct"), 0, 100),
        "packages_hash": digest,
        "packages": stored_names,
    }


def _special(prev: dict[str, Any], parsed: dict[str, Any], h: Any) -> bool:
    if parsed.get("packages_hash") and parsed.get("packages_hash") != prev.get("packages_hash"):
        return True
    hot = _cfg(h).hot_c
    low = _cfg(h).low_battery_pct
    return _crossed(prev.get("temperature_c"), parsed.get("temperature_c"), hot) or _crossed(
        prev.get("remote_battery_pct"), parsed.get("remote_battery_pct"), low, below=True)


def _crossed(before: Any, after: Any, line: float, below: bool = False) -> bool:
    if not isinstance(after, (int, float)):
        return False
    was = isinstance(before, (int, float)) and (before <= line if below else before >= line)
    now = after <= line if below else after >= line
    return was != now


def _public(h: Any, install_id: str, row: dict[str, Any], now: float) -> dict[str, Any]:
    tv = (h.tv_app.tvs() or {}).get(install_id) or {}
    seen = float(row.get("seen") or tv.get("state_at") or tv.get("last_seen_at") or 0)
    ram, storage = row.get("ram") or {}, row.get("storage") or {}
    return {
        "id": install_id,
        "name": tv.get("name") or "TV",
        "room": tv.get("room"),
        "online": bool(seen and now - seen < ONLINE_S),
        "capability": row.get("capability") or "basic",
        "model": row.get("model"),
        "android": row.get("android"),
        "platform": row.get("platform") or "unknown",
        "app_version": row.get("app_version"),
        "health": _health(h, row, seen, now),
        "last_seen": iso(seen) if seen else None,
        "cpu_pct": row.get("cpu_pct"),
        "ram_pct": _pct(ram.get("used_mb"), ram.get("total_mb")),
        "storage_pct": _pct(storage.get("used_mb"), storage.get("total_mb")),
        "temperature_c": row.get("temperature_c"),
        "network": row.get("network") or {"kind": "none", "ssid": None, "rssi_dbm": None},
        "ping_ms": row.get("ping_ms"),
        "foreground": row.get("foreground"),
        "uptime_s": row.get("uptime_s"),
        "remote_battery_pct": row.get("remote_battery_pct"),
    }


def _health(h: Any, row: dict[str, Any], seen: float, now: float) -> str:
    cfg = _cfg(h)
    if not seen or now - seen >= cfg.offline_after_min * 60:
        return "offline"
    temp = row.get("temperature_c")
    if isinstance(temp, (int, float)) and temp >= cfg.hot_c:
        return "hot"
    battery = row.get("remote_battery_pct")
    if isinstance(battery, (int, float)) and battery <= cfg.low_battery_pct:
        return "battery"
    storage = row.get("storage") or {}
    used = _pct(storage.get("used_mb"), storage.get("total_mb"))
    if isinstance(used, int) and used >= 90:
        return "storage"
    return "ok"


def _alerts(h: Any, install_id: str, row: dict[str, Any], now: float) -> None:
    temp = row.get("temperature_c")
    battery = row.get("remote_battery_pct")
    _notice(h, install_id, "offline", False, now)
    _notice(h, install_id, "hot", isinstance(temp, (int, float)) and temp >= _cfg(h).hot_c, now)
    _notice(h, install_id, "battery", isinstance(battery, (int, float)) and battery <= _cfg(h).low_battery_pct, now)
    names = set(row.get("packages") or [])
    if names:
        _notice(h, install_id, "kill", bool(names & set(kill_list(h)["packages"])), now)


def _notice(h: Any, install_id: str, kind: str, active: bool, now: float) -> None:
    sent = h.db.kv_get(KV_NOTICES) or {}
    if not isinstance(sent, dict):
        sent = {}
    key = f"{install_id}|{kind}"
    if active and sent.get(key):
        return
    if not active:
        if sent.pop(key, None) is not None:
            h.db.kv_set(KV_NOTICES, sent)
        return
    sent[key] = True
    h.db.kv_set(KV_NOTICES, sent)
    tv = (h.tv_app.tvs() or {}).get(install_id) or {}
    name = tv.get("name") or "TV"
    sentence = {
        "offline": f"{name} has been quiet for a while.",
        "hot": f"{name} is running hot.",
        "battery": f"{name}'s remote is low.",
        "kill": f"{name} still has an app from the block list.",
    }[kind]
    try:
        asyncio.get_running_loop().create_task(h.notify.send(
            title=sentence, body=None, cat="systems", level="fyi", roles=("admin",),
            group_key=f"fleet:{key}", ref="thomascreek://tv/fleet",
        ))
    except RuntimeError:
        pass


def _remember(h: Any, install_id: str, row: dict[str, Any], now: float) -> None:
    point: dict[str, Any] = {"t": now}
    if isinstance(row.get("cpu_pct"), (int, float)):
        point["cpu"] = row["cpu_pct"]
    ram, storage = row.get("ram") or {}, row.get("storage") or {}
    ram_pct = _pct(ram.get("used_mb"), ram.get("total_mb"))
    storage_pct = _pct(storage.get("used_mb"), storage.get("total_mb"))
    if ram_pct is not None:
        point["ram"] = ram_pct
    if storage_pct is not None:
        point["storage"] = storage_pct
    if isinstance(row.get("temperature_c"), (int, float)):
        point["temperature"] = row["temperature_c"]
    if isinstance(row.get("ping_ms"), (int, float)):
        point["ping"] = row["ping_ms"]
    rssi = (row.get("network") or {}).get("rssi_dbm")
    if isinstance(rssi, (int, float)):
        point["rssi"] = rssi
    if len(point) == 1:
        return
    saved = _history(h)
    points = [item for item in saved.get(install_id) or [] if isinstance(item, dict)]
    points.append(point)
    saved[install_id] = _fold(points, now, _cfg(h).retain_days)
    h.db.kv_set(KV_HISTORY, saved)


def _due(h: Any, install_id: str, now: float) -> bool:
    points = _history(h).get(install_id) or []
    if not points:
        return True
    return now - float(points[-1].get("t") or 0) >= SAMPLE_S


def _fold(points: list[dict[str, Any]], now: float, days: int) -> list[dict[str, Any]]:
    cutoff = now - max(1, days) * 86400
    recent = now - 48 * 3600
    kept: list[dict[str, Any]] = []
    hourly: dict[int, dict[str, Any]] = {}
    for point in points:
        stamp = float(point.get("t") or 0)
        if stamp < cutoff:
            continue
        if stamp >= recent:
            kept.append(point)
        else:
            hourly[int(stamp // 3600)] = point
    return [hourly[key] for key in sorted(hourly)] + kept


def _fanout(h: Any, action: str, params: dict[str, Any]) -> None:
    known = _devices(h)
    for install_id, row in known.items():
        if row.get("capability") in ("owner", "root"):
            _enqueue(h, install_id, action, params, None, time.time())


def _enqueue(h: Any, install_id: str, action: str, params: dict[str, Any], who: str | None, now: float) -> dict[str, Any]:
    row = {
        "id": new_id("tvm"), "install_id": install_id, "action": action, "params": params,
        "at": iso(now), "ts": now, "acked": False, "ok": None, "detail": None, "who": who,
    }
    rows = _commands(h)
    kept = [item for item in rows.get(install_id) or [] if now - float(item.get("ts") or 0) < _retain(h)]
    kept.append(row)
    rows[install_id] = kept
    h.db.kv_set(KV_COMMANDS, rows)
    h.bus.publish({"type": "tv_manage", "data": _wire(row)}, device_id=f"tv:{install_id}")
    for event in _WAITERS.get(install_id, []):
        event.set()
    return {"ok": True, "id": row["id"]}


def _params(action: str, params: dict[str, Any], package: str) -> dict[str, Any]:
    if action in ("reboot", "restart_app", "screenshot"):
        return {}
    if action == "install_apk":
        url = str(params.get("url") or "")
        digest = str(params.get("sha256") or "")
        if not url.startswith("/") or len(digest) != 64:
            raise FleetError(400, "That command is missing a detail.")
        return {
            "url": url, "sha256": digest.lower(),
            "version_code": params.get("version_code"), "version_name": _text(params.get("version_name"), 40),
        }
    if action in ("uninstall", "disable"):
        name = _package(params.get("package"))
        if name in (OURS, package):
            raise FleetError(400, "That app stays on the TV.")
        return {"package": name}
    if action == "set_kiosk":
        if not isinstance(params.get("on"), bool):
            raise FleetError(400, "That command is missing a detail.")
        return {"on": params["on"]}
    if action == "apply_policy":
        return _policy_params(params)
    if action == "kill_list":
        return {"packages": [_package(name) for name in (params.get("packages") or [])]}
    if action == "show_message":
        style = params.get("style")
        text = _text(params.get("text"), 240)
        if style not in ("fullscreen", "toast") or not text:
            raise FleetError(400, "That command is missing a detail.")
        return {"style": style, "text": text}
    if action == "tv_power":
        if not isinstance(params.get("on"), bool):
            raise FleetError(400, "That command is missing a detail.")
        return {"on": params["on"]}
    if action == "tv_volume":
        level = params.get("level")
        if isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100:
            return {"level": level}
        if params.get("delta") in (-1, 1):
            return {"delta": params["delta"]}
        raise FleetError(400, "Say how loud.")
    raise FleetError(400, "That isn't a command this TV knows.")


def _policy_params(params: dict[str, Any]) -> dict[str, Any]:
    blocked = [_package(name) for name in (params.get("blocked") or [])]
    return {
        "blocked": blocked,
        "restrict_settings": bool(params.get("restrict_settings")),
        "hide_status_bar": bool(params.get("hide_status_bar")),
        "disable_keyguard": bool(params.get("disable_keyguard")),
    }


def _package(name: Any) -> str:
    text = str(name or "").strip()
    parts = text.split(".")
    if len(parts) < 2 or any(not part[:1].isalpha() or not all(ch.isalnum() or ch == "_" for ch in part) for part in parts):
        raise FleetError(400, "That doesn't look like an app.")
    return text


def _known(h: Any, install_id: str) -> None:
    tv = (h.tv_app.tvs() or {}).get(install_id)
    if not tv or tv.get("status") != "approved":
        raise FleetError(404, "That TV isn't in the house.")


def _touch(h: Any, install_id: str, now: float) -> None:
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    if not tv:
        return
    tv["last_seen_at"] = now
    tv["state_at"] = now
    tvs[install_id] = tv
    h.tv_app._save_tvs(tvs)


def _expire(h: Any, now: float) -> None:
    rows = _commands(h)
    changed = False
    for install_id, queued in list(rows.items()):
        kept = []
        for row in queued:
            age = now - float(row.get("ts") or 0)
            if row.get("acked") or age < COMMAND_TTL_S:
                if age < _retain(h):
                    kept.append(row)
            else:
                changed = True
        if kept != queued:
            changed = True
        rows[install_id] = kept
    if changed:
        h.db.kv_set(KV_COMMANDS, rows)


def _last_command(h: Any, install_id: str) -> dict[str, Any] | None:
    rows = [row for row in _commands(h).get(install_id) or [] if row.get("acked")]
    if not rows:
        return None
    row = max(rows, key=lambda item: float(item.get("ts") or 0))
    return {"id": row["id"], "action": row["action"], "ok": row.get("ok"), "detail": row.get("detail"), "at": row.get("at")}


def _wire(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "install_id": row["install_id"], "action": row["action"], "params": row.get("params") or {}, "at": row.get("at")}


def _devices(h: Any) -> dict[str, dict[str, Any]]:
    saved = h.db.kv_get(KV_DEVICES) or {}
    return saved if isinstance(saved, dict) else {}


def _history(h: Any) -> dict[str, list[dict[str, Any]]]:
    saved = h.db.kv_get(KV_HISTORY) or {}
    return saved if isinstance(saved, dict) else {}


def _commands(h: Any) -> dict[str, list[dict[str, Any]]]:
    saved = h.db.kv_get(KV_COMMANDS) or {}
    return saved if isinstance(saved, dict) else {}


def _shot_path(h: Any, install_id: str) -> Path:
    root = Path(h.db._path).parent if h.db._path != ":memory:" else Path("/tmp")
    return root / "fleet-shots" / f"{install_id}.png"


def _cfg(h: Any) -> Any:
    return h.config.tv_fleet


def _retain(h: Any) -> float:
    return max(1, _cfg(h).retain_days) * 86400


def _pair(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    used, total = _num(value.get("used_mb"), 0, 10**9), _num(value.get("total_mb"), 0, 10**9)
    if used is None or total is None:
        return None
    return {"used_mb": int(used), "total_mb": int(total)}


def _pct(used: Any, total: Any) -> int | None:
    if not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return None
    return int(round(100 * used / total))


def _num(value: Any, low: float, high: float) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < low or value > high:
        return None
    return int(value) if float(value).is_integer() else value


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] or None


def _push_install(h: Any, install_ids: list[str], code: int, now: float) -> None:
    path = h.tv_app.apk_path(code)
    if not path.is_file():
        return
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    latest = h.tv_app.latest() or {}
    name = latest.get("versionName") if int(latest.get("versionCode") or 0) == code else str(code)
    params = {"url": f"/v1/tv/{code}", "sha256": sha, "version_code": code, "version_name": name}
    for install_id in install_ids:
        if (h.tv_app.tvs().get(install_id) or {}).get("status") == "approved":
            _enqueue(h, install_id, "install_apk", params, None, now)


def _previous_code(h: Any, code: int) -> int | None:
    folder = h.tv_app.dir
    if not folder.is_dir():
        return None
    found = [int(path.stem) for path in folder.glob("*.apk") if path.stem.isdigit() and int(path.stem) < code]
    return max(found) if found else None


def _audit(h: Any, install_id: str, command_id: str, template: str | None, line: str | None, who: str | None, now: float) -> None:
    rows = h.db.kv_get(KV_AUDIT) or []
    if not isinstance(rows, list):
        rows = []
    rows.append({
        "cmd": command_id, "at": iso(now), "who": who or "Admin", "tv": install_id,
        "template": template, "command": line, "ok": None,
    })
    h.db.kv_set(KV_AUDIT, rows[-200:])


def _audit_ok(h: Any, command_id: str, ok: bool) -> None:
    rows = h.db.kv_get(KV_AUDIT) or []
    if not isinstance(rows, list):
        return
    for row in rows:
        if row.get("cmd") == command_id:
            row["ok"] = ok
    h.db.kv_set(KV_AUDIT, rows)


def _sentence(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:240] or None
