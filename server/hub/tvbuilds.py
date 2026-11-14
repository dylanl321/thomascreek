"""Thomas Creek TV builds the hub can hand to a TV.

These are the APKs already kept for overnight updates. /tv always points at
the newest one. A numeric Downloader code is shown only after Dylan registers
that address himself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import segno

from .tvapp import APK_NAME, KV_BUILDS, TvApp, TvAppError

KV_CODE = "tv.downloader_code"
INBOX = "inbox"


def builds(tv: TvApp) -> list[dict[str, Any]]:
    """Every APK on disk, newest first. The first row is the one TVs install."""
    catalog = tv.db.kv_get(KV_BUILDS) or {}
    if not isinstance(catalog, dict):
        catalog = {}
    latest = tv.latest() or {}
    latest_code = int(latest.get("versionCode") or 0)
    rows: list[dict[str, Any]] = []
    if not tv.dir.is_dir():
        return []
    changed = False
    for path in tv.dir.glob("*.apk"):
        if not path.stem.isdigit() or not path.is_file():
            continue
        code = int(path.stem)
        meta = catalog.get(str(code)) if isinstance(catalog.get(str(code)), dict) else {}
        if not meta and code == latest_code:
            meta = latest
        sha = meta.get("sha256") if isinstance(meta.get("sha256"), str) else None
        if not sha or len(sha) != 64:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            meta = {**meta, "versionCode": code, "sha256": sha, "fetched_at": path.stat().st_mtime}
            catalog[str(code)] = meta
            changed = True
        when = meta.get("fetched_at") or path.stat().st_mtime
        name = meta.get("versionName") or (latest.get("versionName") if code == latest_code else None) or code
        rows.append({
            "version_code": code,
            "version_name": str(name),
            "sha256": sha,
            "size": path.stat().st_size,
            "at": float(when) if isinstance(when, (int, float)) else path.stat().st_mtime,
            "latest": False,
            "path": f"/tv/{code}",
            "notes": meta.get("notes") or None,
        })
    if changed:
        tv.db.kv_set(KV_BUILDS, catalog)
    rows.sort(key=lambda row: row["version_code"], reverse=True)
    for row in rows:
        row["latest"] = row["version_code"] == latest_code
    if rows and not any(row["latest"] for row in rows):
        rows[0]["latest"] = True
    return rows


def adopt_inbox(tv: TvApp) -> str | None:
    """Take ThomasCreekTV.apk and update.json from tvapp/inbox, when both are there."""
    folder = tv.dir / INBOX
    if not folder.is_dir():
        return None
    apk_path = folder / APK_NAME
    info_path = folder / "update.json"
    if not apk_path.is_file() and not info_path.is_file():
        return None
    if not apk_path.is_file() or not info_path.is_file():
        return "The inbox needs both ThomasCreekTV.apk and update.json."
    try:
        info = json.loads(info_path.read_text())
        if not isinstance(info, dict):
            raise ValueError("not an object")
        notes = info.get("notes")
        tv.store_build(apk_path.read_bytes(), info, tag=notes if isinstance(notes, str) else None)
    except (OSError, ValueError, TvAppError) as exc:
        message = exc.message if isinstance(exc, TvAppError) else "That update.json isn't the one from the TV build."
        return friendly(message)
    apk_path.unlink(missing_ok=True)
    info_path.unlink(missing_ok=True)
    return None


def downloader_code(config_value: str | None, saved: Any) -> str | None:
    """The number Dylan registered. A value saved on the page wins. Anything else is ignored."""
    if saved is not None:
        return _digits(saved)
    return _digits(config_value)


def save_code(raw: str) -> str:
    """The code to store, or a sentence when it is not a Downloader code."""
    text = _digits(raw)
    if text is None:
        raise TvAppError(400, "bad_code", "That code is the number from aftv.news.")
    return text


def qr_svg(text: str) -> str:
    """An inline drawing of the address, so the page does not fetch anything."""
    svg = segno.make(text, error="m").svg_inline(scale=6, border=2, omitsize=True)
    start = svg.find("<svg")
    return svg[start:] if start >= 0 else svg


def when(ts: float, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("America/New_York")
    moment = datetime.fromtimestamp(ts, zone)
    return f"{moment.strftime('%b')} {moment.day}, {moment.strftime('%Y')}"


def size_label(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def friendly(message: str) -> str:
    if "different key" in message:
        return "That build is signed by a different key, so it was not kept."
    if "doesn't match" in message or "checksum" in message:
        return "That file doesn't match its checksum, so it was not kept."
    if "isn't properly signed" in message or "isn't signed" in message:
        return "That build isn't signed, so it was not kept."
    if "too large" in message:
        return "That build is too large."
    if "update.json" in message:
        return "That update.json isn't the one from the TV build."
    return message


def _digits(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit() and 4 <= len(text) <= 12:
        return text
    return None


def inbox_dir(tv: TvApp) -> Path:
    return tv.dir / INBOX
