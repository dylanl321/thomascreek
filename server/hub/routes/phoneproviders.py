"""Provider status for the phone's "My playlists" sheet (Managed mode).

The hub already plays every provider as one library; this says how each provider is doing: working, expiring,
expired, full, disabled or not answering, its expiry day, connections in use out of the maximum, and how many of
its addresses answer. No address, username or login id leaves the hub.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from .. import iptvaddr
from ..iptvplay import KV_USAGE, accounts, providers
from ..state import Hub
from .deps import Caller, caller, hub
from .tvguide import _media

router = APIRouter(prefix="/v1/tv/phone", tags=["tv"])
log = logging.getLogger(__name__)
REFRESH_S = 30 * 60
_last_refresh = {"at": 0.0}

WARN_DAYS = 7
# Worst first: the sheet's dot shows the worst login of a provider only when no login works.
_RANK = {"ok": 0, "expiring": 1, "full": 2, "unknown": 3, "down": 4, "disabled": 5, "expired": 6}
_SAY = {
    "ok": "Working",
    "expiring": "Expires soon",
    "full": "All connections in use",
    "unknown": "Not checked yet",
    "down": "Not answering",
    "disabled": "Turned off by the provider",
    "expired": "Expired",
}


def _days(day: str | None, now: float) -> int | None:
    if not day:
        return None
    try:
        return (date.fromisoformat(day) - datetime.fromtimestamp(now, timezone.utc).date()).days
    except ValueError:
        return None


def provider_status(h: Hub, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    usage = h.db.kv_get(KV_USAGE) or {}
    by_login: dict[str, list[dict[str, Any]]] = {}
    for row in accounts(h, now)["logins"]:
        by_login.setdefault(str(row["provider"]), []).append(row)
    out = []
    for p in providers(h):
        rows = by_login.get(p["id"], [])
        addrs = p.get("addresses") or []
        up = sum(1 for a in addrs if a.get("state") == "ok")
        down = sum(1 for a in addrs if a.get("state") == "down")
        backup = any(a.get("in_use") and not a.get("main") for a in addrs)
        checked = [float((usage.get(f"{p['id']}|{r['login']}") or {}).get("at") or 0) for r in rows]
        checked_at = max(checked) if checked else 0.0
        states = []
        for r in rows:
            s = r["status"]
            if s == "ok" and not checked_at:
                s = "unknown"
            d = _days(r.get("expires"), now)
            if s == "ok" and d is not None and d <= WARN_DAYS:
                s = "expiring"
            states.append(s)
        if addrs and down == len(addrs):
            state = "down"
        elif "ok" in states:
            state = "ok"
        elif states:
            state = min(states, key=lambda s: _RANK[s])
        else:
            state = "unknown"
        expiries = [r["expires"] for r in rows if r.get("expires")]
        expires = max(expiries) if expiries else None
        days = _days(expires, now)
        if state == "ok" and days is not None and days <= WARN_DAYS:
            state = "expiring"
        used = sum(int(r.get("active") or 0) for r in rows)
        most = sum(int(r.get("max_connections") or 0) for r in rows)
        message = _SAY[state]
        if state == "expiring" and days is not None:
            message = "Expires today" if days <= 0 else f"Expires in {days} day{'s' if days != 1 else ''}"
        elif state == "ok" and backup:
            message = "Working on a backup address"
        out.append({
            "id": p["id"], "name": p["name"], "main": bool(p.get("main")), "state": state, "message": message,
            "expires": expires, "days_left": days, "logins": len(rows),
            "connections": {"used": used, "max": most},
            "addresses": {"total": len(addrs), "up": up, "down": down, "using_backup": backup},
            "checked_at": datetime.fromtimestamp(checked_at, timezone.utc).isoformat() if checked_at else None,
        })
    used = sum(p["connections"]["used"] for p in out)
    most = sum(p["connections"]["max"] for p in out)
    working = sum(1 for p in out if p["state"] in ("ok", "expiring", "full"))
    unchecked = sum(1 for p in out if p["state"] == "unknown")
    if not out:
        summary = "No providers on the hub yet"
    else:
        parts = [f"{working} working"]
        if unchecked:
            parts.append(f"{unchecked} not checked yet")
        broken = len(out) - working - unchecked
        if broken:
            parts.append(f"{broken} not working")
        summary = " · ".join(parts)
    return {
        "providers": out,
        "all": {"name": "All playlists", "providers": len(out), "working": working, "unchecked": unchecked,
                "connections": {"used": used, "max": most}, "message": summary},
    }


async def _refresh(h: Hub) -> None:
    try:
        await iptvaddr.check_all(h, only_with_backups=False)
    except Exception as exc:  # noqa: BLE001 - a status refresh never fails the sheet
        log.info("provider status refresh failed (%s)", type(exc).__name__)


@router.get("/providers")
async def phone_providers(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """{providers:[{id, name, main, state, message, expires, days_left, logins, connections, addresses,
    checked_at}], all:{...}}. The hub already merges every provider into one library."""
    _media(h, c)
    body = provider_status(h)
    now = time.time()
    stale = any(p["state"] == "unknown" for p in body["providers"]) or now - _last_refresh["at"] > REFRESH_S
    if stale and now - _last_refresh["at"] > 60:
        # Ask each provider for its expiry and connections in the background; the next open shows them.
        _last_refresh["at"] = now
        asyncio.get_running_loop().create_task(_refresh(h))
        body["refreshing"] = True
    return body
