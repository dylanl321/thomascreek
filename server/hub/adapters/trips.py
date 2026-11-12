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

import html
import json
import time
from datetime import date, timedelta
from typing import Any

import httpx

from .base import Adapter, AdapterError

CACHE_S = 60.0
# No ?include=: TREK then returns every section the key may read, instead of refusing a narrow key.


def unescape(v: Any) -> Any:
    """TREK stores some text HTML-escaped, sometimes twice ("&amp;amp;"). Give the app plain text."""
    if isinstance(v, str):
        for _ in range(3):
            if "&" not in v:
                break
            nv = html.unescape(v)
            if nv == v:
                break
            v = nv
        return v
    if isinstance(v, list):
        return [unescape(x) for x in v]
    if isinstance(v, dict):
        return {k: unescape(x) for k, x in v.items()}
    return v


class TrekRefused(Exception):
    """TREK said no to a change (bad arguments, missing scope); the message says why."""


def parse_rpc(text: str, want_id: int) -> dict[str, Any]:
    """A JSON-RPC answer from an MCP Streamable HTTP POST: plain JSON, or server-sent events."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    found: dict[str, Any] = {}
    for block in text.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
        found = msg if isinstance(msg, dict) else found
    return found


class Trek(Adapter):
    name = "trek"
    title = "Trips"
    ping_path = "/api/v1/trips"

    def __init__(self, cfg, vault):
        super().__init__(cfg, vault)
        self._cache: dict[str, tuple[float, Any]] = {}

    def api_key(self) -> str | None:
        key = (self.secret(self.cfg.token_secret) or "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        return key or None

    async def get_json(self, path: str, **kw: Any) -> Any:
        return unescape(await super().get_json(path, **kw))

    def auth_headers(self) -> dict[str, str]:
        key = self.api_key()
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if (self.cfg.url or "").startswith("http://"):
            headers["X-Forwarded-Proto"] = "https"
        return headers

    async def request(self, method: str, path: str, **kw: Any):
        key = self.api_key()
        if not key:
            self._fail("the TREK API key is not saved yet")
            raise AdapterError(self.last_error)
        if not key.startswith("trek_"):
            # TREK only looks up keys that start with trek_ (Settings, Integrations, API keys).
            self._fail("the saved TREK key is not a TREK API key (those start with trek_)")
            raise AdapterError(self.last_error)
        return await super().request(method, path, **kw)

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
        return await self._cached(f"trip:{trip_id}", f"/api/v1/trips/{int(trip_id)}")

    # -- changing trips: TREK's MCP server, signed in as a machine client ---------------------------
    def mcp_ready(self) -> bool:
        return bool(self.secret(self.cfg.mcp_client_id_secret) and self.secret(self.cfg.mcp_client_secret_secret))

    def _proto(self) -> dict[str, str]:
        return {"X-Forwarded-Proto": "https"} if (self.cfg.url or "").startswith("http://") else {}

    async def _mcp_token(self) -> str:
        tok = self.__dict__.get("_mcp_tok")
        if tok and tok[1] > time.time() + 60:
            return tok[0]
        cid = (self.secret(self.cfg.mcp_client_id_secret) or "").strip()
        csec = (self.secret(self.cfg.mcp_client_secret_secret) or "").strip()
        if not (cid and csec):
            raise AdapterError("TREK's machine client isn't saved on the hub yet (admin page, Trips)")
        headers = {"Accept": "application/json", **self._proto()}
        try:
            # client_secret_post first; some servers only take HTTP Basic (RFC 6749 2.3.1)
            r = await self.client().post("/oauth/token", data={"grant_type": "client_credentials",
                                                               "client_id": cid, "client_secret": csec},
                                         headers=headers)
            if r.status_code in (400, 401):
                r = await self.client().post("/oauth/token", data={"grant_type": "client_credentials"},
                                             headers=headers, auth=(cid, csec))
        except httpx.HTTPError as exc:
            raise AdapterError(f"cannot reach TREK: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            self._fail(f"TREK refused the hub's machine client ({r.status_code})")
            raise AdapterError(self.last_error)
        j = r.json()
        self.__dict__["_mcp_tok"] = (j["access_token"], time.time() + int(j.get("expires_in") or 3600))
        self.__dict__.pop("_mcp_sid", None)  # a new token starts a new session
        return j["access_token"]

    async def _rpc(self, method: str, params: dict[str, Any] | None = None, *, retry: bool = True) -> Any:
        if method != "initialize" and not self.__dict__.get("_mcp_sid"):
            await self._mcp_init()
        token = await self._mcp_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", **self._proto()}
        sid = self.__dict__.get("_mcp_sid")
        if sid:
            headers["Mcp-Session-Id"] = sid
        n = self.__dict__["_mcp_n"] = self.__dict__.get("_mcp_n", 0) + 1
        body = {"jsonrpc": "2.0", "id": n, "method": method, **({"params": params} if params is not None else {})}
        try:
            r = await self.client().post("/mcp", json=body, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            raise AdapterError(f"cannot reach TREK: {type(exc).__name__}") from exc
        if r.status_code in (400, 404) and sid and retry:  # the session expired: start over once
            self.__dict__.pop("_mcp_sid", None)
            return await self._rpc(method, params, retry=False)
        if r.status_code == 401 and retry:
            self.__dict__.pop("_mcp_tok", None)
            return await self._rpc(method, params, retry=False)
        if r.status_code >= 400:
            self._fail(f"TREK's MCP answered {r.status_code}")
            raise AdapterError(self.last_error)
        if r.headers.get("mcp-session-id"):
            self.__dict__["_mcp_sid"] = r.headers["mcp-session-id"]
        msg = parse_rpc(r.text, n)
        if msg.get("error"):
            raise TrekRefused(str(msg["error"].get("message") or msg["error"]))
        return msg.get("result")

    async def _mcp_init(self) -> None:
        await self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "thomascreek-hub", "version": "1"}})
        token = await self._mcp_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", **self._proto()}
        if self.__dict__.get("_mcp_sid"):
            headers["Mcp-Session-Id"] = self.__dict__["_mcp_sid"]
        try:
            await self.client().post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                     headers=headers)
        except httpx.HTTPError:
            pass

    async def mcp_tools(self) -> list[dict[str, Any]]:
        hit = self.__dict__.get("_mcp_tools")
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.__dict__["_mcp_tools"] = (time.time(), tools)
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        out: Any = res.get("structuredContent")
        if out is None:
            joined = "\n".join(texts)
            try:
                out = json.loads(joined)
            except ValueError:
                out = joined
        if res.get("isError"):
            raise TrekRefused(out if isinstance(out, str) else json.dumps(out)[:500])
        if not name.startswith(("list_", "get_", "search_")):
            self._cache.clear()  # the app's trip views pick up the change
        return out

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
