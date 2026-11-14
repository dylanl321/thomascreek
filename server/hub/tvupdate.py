"""Overnight updates for the TV app.

The hub already stores one signed build. A TV that is idle, inside the
night window, installs it without asking. Afterwards it says whether it
booted, reached the hub, and played. That note stays on the admin view.
"""

from __future__ import annotations

import time
from typing import Any

from .home import iso
from .tvapp import TvApp, TvAppError

KV = "tv.update_health"
_FLAGS = ("boot_ok", "hub_ok", "playback_ok", "rollback")
_INSTALL = (
    "Download the APK with this TV's bearer, check sha256 and signer_sha256, "
    "then hand the file to PackageInstaller. The person confirms on the TV."
)


def manifest(tv: TvApp, current: str | None) -> dict[str, Any]:
    """The build to install, or a short note when this TV already has it."""
    body = tv.update_manifest()
    if body.get("install") != _INSTALL:
        body["install"] = _INSTALL
    have = _number(current)
    if have is not None and have >= int(body["version_code"]):
        return {"version_code": int(body["version_code"]), "up_to_date": True}
    body["idle_only"] = True
    body["silent"] = True
    body["window"] = _window(tv)
    return body


def record(tv: TvApp, install_id: str, body: dict[str, Any]) -> None:
    """Keep the latest report for this TV. Nothing is pushed."""
    if not isinstance(body, dict):
        raise TvAppError(400, "bad_request", "Say how the update went.")
    code = body.get("version_code")
    if isinstance(code, bool) or not isinstance(code, int) or code < 0:
        raise TvAppError(400, "bad_request", "Say how the update went.")
    flags: dict[str, bool] = {}
    for key in _FLAGS:
        value = body.get(key)
        if not isinstance(value, bool):
            raise TvAppError(400, "bad_request", "Say how the update went.")
        flags[key] = value
    saved = dict(tv.db.kv_get(KV) or {})
    saved[install_id] = {
        "version_code": code,
        "boot_ok": flags["boot_ok"],
        "hub_ok": flags["hub_ok"],
        "playback_ok": flags["playback_ok"],
        "rollback": flags["rollback"],
        "ok": flags["boot_ok"] and flags["hub_ok"] and flags["playback_ok"] and not flags["rollback"],
        "at": time.time(),
    }
    tv.db.kv_set(KV, saved)


def listing(tv: TvApp) -> list[dict[str, Any]]:
    """Reports for the admin view. A TV that has not checked in is absent. Problems come first."""
    saved = tv.db.kv_get(KV) or {}
    if not isinstance(saved, dict):
        return []
    names = tv.tvs()
    rows = []
    for install_id, row in saved.items():
        if not isinstance(row, dict):
            continue
        tv_row = names.get(install_id) or {}
        rows.append({
            "id": install_id,
            "name": tv_row.get("name") or "TV",
            "version_code": row.get("version_code"),
            "boot_ok": bool(row.get("boot_ok")),
            "hub_ok": bool(row.get("hub_ok")),
            "playback_ok": bool(row.get("playback_ok")),
            "rollback": bool(row.get("rollback")),
            "ok": bool(row.get("ok")),
            "at": iso(row.get("at")),
        })
    rows.sort(key=lambda row: (bool(row["ok"]), str(row["name"]).casefold()))
    return rows


def _number(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _window(tv: TvApp) -> dict[str, str]:
    timezone = "America/New_York"
    clock = getattr(tv, "now", None)
    if clock is not None:
        moment = clock()
        key = getattr(getattr(moment, "tzinfo", None), "key", None)
        if isinstance(key, str) and key:
            timezone = key
    return {"from": "01:00", "to": "06:00", "timezone": timezone}
