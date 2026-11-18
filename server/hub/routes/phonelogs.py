"""Logs a phone sends to the hub for a look afterwards ("Send logs to hub" on the Android phone).

The phone sends its own recent lines (player, cast, update, errors) with a short note. The hub scrubs anything that
looks like a login or a provider address before keeping it, keeps the last few per house, and logs the upload in
the activity log. Admins read them at `/v1/diagnostics/phone-logs` (the app, the admin page or an AI connection
over the hub's MCP, which reaches `/v1/diagnostics/...` like any other hub read).

Update outcomes ("downloaded, verified, installed" or why it stopped) come from the phone too, as one activity
line each (`/v1/tv/phone/update/report`).
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(tags=["diagnostics"])

KV_INDEX = "phone.logs"
KV_ONE = "phone.log."
KEEP = 30
MAX_LINES = 3000
MAX_LINE = 500
MAX_BYTES = 512 * 1024

# Scrubbing: a phone must never send these, but a stray line must not keep them either.
_XTREAM = re.compile(r"/(live|movie|series|timeshift)/[^/\s]+/[^/\s]+/", re.I)
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_QUERY = re.compile(r"(?i)([?&](?:username|password|pass|user|token|key|auth|code)=)[^&\s]+")
_BEARER = re.compile(r"(?i)\b(bearer|authorization:?)\s+[A-Za-z0-9._~+/=-]{8,}")
_PASS = re.compile(r"/v1/tv/(phone|stream)/(file|live|cast)/[A-Za-z0-9._~-]{16,}")
_HOST = re.compile(r"(?i)\b(https?://)([^/\s:]+)")


def scrub(line: str) -> str:
    """No logins, passes, bearers or provider hosts. The hub's own address may stay; others become `host`."""
    out = _USERINFO.sub(r"\1***@", line)
    out = _XTREAM.sub(r"/\1/***/***/", out)
    out = _QUERY.sub(r"\1***", out)
    out = _BEARER.sub(r"\1 ***", out)
    out = _PASS.sub(r"/v1/tv/\1/\2/***", out)
    return out


def _hosts_off(line: str, keep: str) -> str:
    def one(m: re.Match[str]) -> str:
        return m.group(0) if keep and m.group(2).lower() == keep else f"{m.group(1)}host"
    return _HOST.sub(one, line)


class LogsBody(BaseModel):
    version_name: str = Field(default="", max_length=40)
    version_code: int | None = None
    model: str = Field(default="", max_length=80)
    android: str = Field(default="", max_length=20)
    mode: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=500)
    # Plain strings, or {t: unix ms, level: I|W|E, tag, msg} from the phone's own log.
    lines: list[str | dict[str, Any]] = Field(default_factory=list, max_length=MAX_LINES)


def _text(raw: str | dict[str, Any]) -> str:
    if isinstance(raw, str):
        return raw
    when = ""
    if isinstance(raw.get("t"), (int, float)):
        ms = int(raw["t"])
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) + f".{ms % 1000:03d} "
    level = str(raw.get("level") or "I")[:1]
    return f"{when}{level}/{str(raw.get('tag') or '')[:24]} {raw.get('msg') or ''}"


def _admin(c: Caller) -> None:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only an admin can read phone logs")


@router.post("/v1/tv/phone/logs")
async def phone_logs(body: LogsBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """{lines, note?, version_name, version_code, model, android, mode} -> {id, lines}. Kept scrubbed."""
    keep = (h.config.hub.remote_url or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    lines, size = [], 0
    for raw in body.lines[-MAX_LINES:]:
        line = _hosts_off(scrub(_text(raw)[:MAX_LINE]), keep)
        size += len(line) + 1
        if size > MAX_BYTES:
            break
        lines.append(line)
    log_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    person = h.db.person(c.subject.person_id) if c.subject.person_id else None
    meta = {"id": log_id, "at": time.time(), "device_id": c.device["id"], "device": c.device.get("name"),
            "person": (person or {}).get("name"), "version": body.version_name, "version_code": body.version_code,
            "model": body.model, "android": body.android, "mode": body.mode,
            "note": _hosts_off(scrub(body.note), keep), "lines": len(lines)}
    h.db.kv_set(KV_ONE + log_id, {**meta, "text": lines})
    index = [m for m in (h.db.kv_get(KV_INDEX) or []) if isinstance(m, dict)]
    index.insert(0, meta)
    for gone in index[KEEP:]:
        h.db.kv_set(KV_ONE + str(gone.get("id")), None)
    h.db.kv_set(KV_INDEX, index[:KEEP])
    h.db.log("phone.logs", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=log_id,
             params={"lines": len(lines), "version": body.version_name, "note": meta["note"][:120]})
    return {"id": log_id, "lines": len(lines)}


@router.get("/v1/diagnostics/phone-logs")
async def list_logs(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Newest first: who sent it, from which phone and version, the note and the line count."""
    _admin(c)
    return {"logs": [m for m in (h.db.kv_get(KV_INDEX) or []) if isinstance(m, dict)]}


@router.get("/v1/diagnostics/phone-logs/{log_id}")
async def one_log(log_id: str, grep: str | None = None, c: Caller = Depends(caller),
                  h: Hub = Depends(hub)) -> dict[str, Any]:
    """One upload with its lines. `grep` keeps only lines containing it (case-insensitive)."""
    _admin(c)
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", log_id):
        raise error(404, "not_found", "No such log")
    row = h.db.kv_get(KV_ONE + log_id)
    if not isinstance(row, dict):
        raise error(404, "not_found", "No such log (only the last 30 are kept)")
    if grep:
        word = grep.casefold()
        row = {**row, "text": [line for line in row.get("text") or [] if word in line.casefold()]}
    return row


@router.get("/v1/diagnostics/phone-activity")
async def phone_activity(limit: int = 200, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The hub's own lines about phones: plays, casts, conversions, updates and log uploads, newest first."""
    _admin(c)
    rows = h.db.all(
        "SELECT ts, action, outcome, target, params, device_id FROM activity WHERE action LIKE 'tv.phone.%'"
        " OR action LIKE 'phoneapp.%' OR action = 'phone.logs' ORDER BY ts DESC LIMIT ?",
        [max(1, min(int(limit), 1000))])
    return {"activity": rows}


class UpdateReport(BaseModel):
    version_code: int | None = None
    stage: str = Field(max_length=24)
    ok: bool = True
    reason: str = Field(default="", max_length=200)


@router.post("/v1/tv/phone/update/report")
async def update_report(body: UpdateReport, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """One line per step of a self-update: checked, downloaded, verified, installing, installed, or why it stopped
    (bad checksum, wrong signer, Android refused, waiting for "Install unknown apps")."""
    h.db.log("phoneapp.update", "ok" if body.ok else "error", person_id=c.subject.person_id,
             device_id=c.device["id"], target=str(body.version_code or ""),
             params={"stage": body.stage, "reason": scrub(body.reason)})
    return {"ok": True}
