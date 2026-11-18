"""Provider status for the phone's "My playlists" sheet (Managed mode).

The hub already plays every provider as one library; this says how each provider is doing: working, expiring,
expired, full, disabled or not answering, its expiry day, connections in use out of the maximum, and how many of
its addresses answer. No address, username or login id leaves the hub.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from ..iptvplay import KV_USAGE, accounts, providers
from ..state import Hub
from .deps import Caller, caller, hub
from .tvguide import _media

router = APIRouter(prefix="/v1/tv/phone", tags=["tv"])

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
    return {
        "providers": out,
        "all": {"name": "All playlists", "providers": len(out), "working": working,
                "connections": {"used": used, "max": most},
                "message": f"{working} of {len(out)} working" if out else "No providers on the hub yet"},
    }


@router.get("/providers")
async def phone_providers(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """{providers:[{id, name, main, state, message, expires, days_left, logins, connections, addresses,
    checked_at}], all:{...}}. The hub already merges every provider into one library."""
    _media(h, c)
    return provider_status(h)
