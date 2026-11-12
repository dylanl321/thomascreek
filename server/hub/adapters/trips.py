"""TREK (github.com/mauriceboe/TREK), the family's self-hosted trip planner.

The hub reads TREK's versioned, read-only public API (`/api/v1`) with an API
key that Dylan mints in TREK under Settings → Integrations and stores as the
vault secret `trek.api_key`. The key belongs to one TREK user, so the hub sees
every trip that user owns or was invited to. Planning and editing stay in
TREK; the app only links to it.

TREK redirects plain http to https unless a proxy says the request was https,
so the hub sends `X-Forwarded-Proto: https` when it talks to TREK on the LAN.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from .base import Adapter

CACHE_S = 60.0
INCLUDE_ALL = "days,places,notes,reservations,accommodations,travellers"


class Trek(Adapter):
    name = "trek"
    title = "Trips"
    ping_path = "/api/v1/trips"

    def __init__(self, cfg, vault):
        super().__init__(cfg, vault)
        self._cache: dict[str, tuple[float, Any]] = {}

    def auth_headers(self) -> dict[str, str]:
        key = self.secret(self.cfg.token_secret)
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if (self.cfg.url or "").startswith("http://"):
            headers["X-Forwarded-Proto"] = "https"
        return headers

    async def _cached(self, key: str, path: str, **kw: Any) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        data = await self.get_json(path, **kw)
        self._cache[key] = (time.time(), data)
        return data

    async def trips(self) -> list[dict[str, Any]]:
        data = await self._cached("trips", "/api/v1/trips")
        return list((data or {}).get("trips", []))

    async def trip(self, trip_id: int) -> dict[str, Any]:
        return await self._cached(f"trip:{trip_id}", f"/api/v1/trips/{int(trip_id)}", params={"include": INCLUDE_ALL})

    def plan_url(self, trip_id: int | None = None) -> str | None:
        base = (self.cfg.web_url or "").rstrip("/")
        if not base:
            return None
        return f"{base}/trips/{int(trip_id)}" if trip_id else f"{base}/dashboard"


# -- shaping, pure functions so they are easy to test --------------------------

def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def timing(t: dict[str, Any], today: date) -> dict[str, Any]:
    """upcoming / now / past, days until it starts, and which day it is."""
    start, end = _d(t.get("start_date")), _d(t.get("end_date"))
    end = end or start
    if start is None:
        return {"status": "someday", "days_until": None, "day_number": None, "day_count": None}
    count = (end - start).days + 1 if end else None
    if today < start:
        return {"status": "upcoming", "days_until": (start - today).days, "day_number": None, "day_count": count}
    if end and today > end:
        return {"status": "past", "days_until": None, "day_number": None, "day_count": count}
    return {"status": "now", "days_until": 0, "day_number": (today - start).days + 1, "day_count": count}


def summary(t: dict[str, Any], today: date) -> dict[str, Any]:
    out = {
        "id": t.get("id"),
        "title": t.get("title") or "Trip",
        "description": t.get("description"),
        "start_date": t.get("start_date"),
        "end_date": t.get("end_date"),
        "archived": bool(t.get("archived")),
        **timing(t, today),
    }
    if out["archived"] and out["status"] != "past":
        out["status"] = "past"
    return out


def sort_trips(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Now first, then upcoming soonest first, then someday, then past newest first."""
    rank = {"now": 0, "upcoming": 1, "someday": 2}
    live = sorted((t for t in trips if t["status"] in rank),
                  key=lambda t: (rank[t["status"]], t.get("start_date") or ""))
    past = sorted((t for t in trips if t["status"] not in rank),
                  key=lambda t: t.get("start_date") or "", reverse=True)
    return live + past


def next_trip(trips: list[dict[str, Any]]) -> dict[str, Any] | None:
    for t in sort_trips(trips):
        if t["status"] in ("now", "upcoming"):
            return t
    return None


CHILD_DROP = {"notes"}


def for_child(trip: dict[str, Any]) -> dict[str, Any]:
    """Children see what happens and when, never booking notes (codes, prices, door codes)."""
    t = dict(trip)

    def strip(items: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if items is None:
            return None
        return [{k: v for k, v in r.items() if k not in CHILD_DROP} for r in items]

    if "days" in t:
        t["days"] = [{**d, "reservations": strip(d.get("reservations")) or []} for d in t["days"]]
    for key in ("accommodations", "unscheduled_reservations"):
        if key in t:
            t[key] = strip(t[key])
    return t


def detail(raw: dict[str, Any], today: date, child: bool) -> dict[str, Any]:
    out = {**summary(raw, today)}
    for key in ("days", "accommodations", "travellers", "unplanned_places", "unscheduled_reservations"):
        out[key] = raw.get(key) or []
    out["today"] = today.isoformat() if out["status"] == "now" else None
    return for_child(out) if child else out


def calendar_events(trips: list[dict[str, Any]], start: date, days: int) -> list[dict[str, Any]]:
    """One all-day entry per trip day inside the window, for the family calendar."""
    end = start + timedelta(days=days - 1)
    events = []
    for t in trips:
        s, e = _d(t.get("start_date")), _d(t.get("end_date"))
        if s is None:
            continue
        e = e or s
        name = t.get("title") or "Trip"
        cur, n = max(s, start), max((max(s, start) - s).days + 1, 1)
        while cur <= min(e, end):
            total = (e - s).days + 1
            events.append({
                "id": f"trek-{t.get('id')}-{cur.isoformat()}",
                "title": name if total == 1 else f"{name} (day {n} of {total})",
                "start": cur.isoformat(),
                "all_day": True,
                "who": None,
                "location": None,
            })
            cur += timedelta(days=1)
            n += 1
    return events
