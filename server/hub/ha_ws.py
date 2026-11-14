"""One persistent connection to Home Assistant's websocket API, shared by the whole hub.

It carries:
  - state changes as they happen (`subscribe_events` state_changed): folded into the home model and sent to
    phones within a moment, and handed to the alerts (doorbell, garage, hazards),
  - the starting states (`get_states`) after every (re)connect, so nothing missed while it was down is lost,
  - the room, floor, device and entity registries (`config/*_registry/list`), cheap to read, instead of the
    Jinja templates the REST API needs (those run on Home Assistant's own event loop and slow it down),
  - service calls (`call_service`), without a new HTTP request each time.

A ping every 30 s notices a dead connection; it reconnects with a growing pause. While it is down the hub falls
back to REST for everything (the 15 s poll and REST service calls), so nothing stops working.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import ssl
import time
from typing import TYPE_CHECKING, Any

from .adapters.base import NoAnswer, ServiceError, Unreachable

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.ha_ws")

PING_EVERY_S = 30.0
PONG_WITHIN_S = 10.0
# Websocket error codes that mean the request itself was wrong, not the device.
BAD_REQUEST_CODES = {"invalid_format", "not_found", "service_validation_error", "unknown_command", "unauthorized"}


class NotConnected(Unreachable):
    """The websocket isn't up; the caller should use REST."""


class HAConnection:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future] = {}
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self.connected = False
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.error: str | None = None
        self.reconnects = 0

    @property
    def enabled(self) -> bool:
        return bool(self.hub.ha.enabled and getattr(self.hub.config.home_assistant, "websocket", True))

    @property
    def ready(self) -> bool:
        return self.connected and self._ws is not None

    def url(self) -> str:
        url = (self.hub.config.home_assistant.url or "").rstrip("/")
        return re.sub(r"^http", "ws", url) + "/api/websocket"

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._down("stopped")

    def health(self) -> dict[str, Any]:
        return {"connected": self.connected, "since": self.connected_at, "last_event": self.last_event_at,
                "error": self.error, "reconnects": self.reconnects}

    # -- requests ------------------------------------------------------------
    async def request(self, msg: dict[str, Any], timeout: float | None = None) -> Any:
        """Send one command and wait for its result. Raises NotConnected (nothing sent), NoAnswer (sent, no reply
        in time) or ServiceError (Home Assistant said no, with its reason)."""
        ws = self._ws
        if not self.connected or ws is None:
            raise NotConnected("Home Assistant's websocket isn't connected")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[mid] = fut
        try:
            try:
                await ws.send(json.dumps({**msg, "id": mid}))
            except Exception as exc:  # noqa: BLE001
                raise NotConnected(f"Home Assistant's websocket closed: {type(exc).__name__}") from exc
            try:
                reply = await asyncio.wait_for(asyncio.shield(fut), timeout or self.hub.config.home_assistant.timeout_s)
            except asyncio.TimeoutError:
                raise NoAnswer("no answer from Home Assistant in time") from None
        finally:
            self._waiting.pop(mid, None)
        if reply.get("success", True):
            return reply.get("result")
        err = reply.get("error") or {}
        code, message = str(err.get("code") or ""), (str(err.get("message") or "")[:160] or None)
        if code == "_closed":
            raise NoAnswer("Home Assistant's websocket closed before it answered")
        status = 400 if code in BAD_REQUEST_CODES else 500
        raise ServiceError(f"Home Assistant answered {code or 'an error'}" + (f": {message}" if message else ""),
                           status, message)

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        data = dict(data)
        msg: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service, "service_data": data}
        if "entity_id" in data:
            msg["target"] = {"entity_id": data.pop("entity_id")}
        await self.request(msg)
        self.hub.ha.last_ok = time.time()
        self.hub.ha.last_error = None

    async def registry(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for name in ("area", "floor", "device", "entity"):
            try:
                out[name] = await self.request({"type": f"config/{name}_registry/list"}, timeout=20) or []
            except ServiceError:
                out[name] = []  # floors need HA 2024.4+
        return out

    # -- connection ------------------------------------------------------------
    def _down(self, why: str | None) -> None:
        was = self.connected
        self.connected, self._ws = False, None
        if why:
            self.error = why
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_result({"success": False, "error": {"code": "_closed"}})
        self._waiting.clear()
        if was:
            self.hub.home.ws_changed(False)

    async def _run(self) -> None:
        import websockets

        delay = 2.0
        while True:
            try:
                ctx = None
                if self.url().startswith("wss://"):
                    ctx = ssl.create_default_context()
                    if not self.hub.config.home_assistant.verify_tls:
                        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                token = self.hub.ha.secret(self.hub.config.home_assistant.token_secret)
                if not token:
                    raise RuntimeError("no Home Assistant token")
                async with websockets.connect(self.url(), ssl=ctx, max_size=64 * 2**20, open_timeout=10,
                                              ping_interval=None) as ws:
                    await asyncio.wait_for(ws.recv(), 10)  # auth_required
                    await ws.send(json.dumps({"type": "auth", "access_token": token}))
                    if json.loads(await asyncio.wait_for(ws.recv(), 10)).get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant refused the token")
                    self._ws = ws
                    self.connected, self.error, self.connected_at = True, None, time.time()
                    delay = 2.0
                    reader = asyncio.create_task(self._read(ws))
                    pinger = asyncio.create_task(self._ping())
                    try:
                        await self.request({"type": "subscribe_events", "event_type": "state_changed"})
                        # Everything missed while down, then the rooms: the poll and the templates aren't needed.
                        states = await self.request({"type": "get_states"}, timeout=30)
                        if isinstance(states, list):
                            self.hub.home.apply_full_states(states)
                        self.hub.home.ws_changed(True)
                        done, _ = await asyncio.wait({reader, pinger}, return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            if t.exception():
                                raise t.exception()  # type: ignore[misc]
                    finally:
                        reader.cancel()
                        pinger.cancel()
            except asyncio.CancelledError:
                self._down(None)
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("Home Assistant websocket: %s: %s", type(exc).__name__, exc)
                self._down(f"{type(exc).__name__}: {exc}"[:200])
            else:
                self._down("closed")
            self.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(PING_EVERY_S)
            try:
                await self.request({"type": "ping"}, timeout=PONG_WITHIN_S)
            except NoAnswer:
                raise ConnectionError("Home Assistant stopped answering pings") from None

    async def _read(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "event":
                self._on_event(msg.get("event") or {})
            elif kind in ("result", "pong"):
                fut = self._waiting.get(msg.get("id"))
                if fut is not None and not fut.done():
                    fut.set_result(msg if kind == "result" else {"success": True, "result": "pong"})
        raise ConnectionError("Home Assistant closed the websocket")

    def _on_event(self, event: dict[str, Any]) -> None:
        d = event.get("data") or {}
        eid = d.get("entity_id") or ""
        self.last_event_at = time.time()
        new = d.get("new_state")
        if isinstance(new, dict):
            self.hub.home.publish_soon(self.hub.home.merge_states([new]))
        # Alerts may send pushes or fetch snapshots: never hold up the next event for them.
        task = asyncio.create_task(self.hub.alerts.on_state(eid, d.get("old_state"), new))
        task.add_done_callback(_log_alert_failure(eid))


def _log_alert_failure(eid: str):
    def done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            log.warning("alert for %s failed: %s", eid, t.exception())
    return done


# -- registries -> the shapes the home model already uses (same as the REST templates gave) ---------------------
def areas_from(reg: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    device_area = {d["id"]: d.get("area_id") for d in reg.get("device", []) if d.get("id")}
    members: dict[str, list[str]] = {}
    for e in reg.get("entity", []):
        area = e.get("area_id") or device_area.get(e.get("device_id") or "")
        if area and e.get("entity_id"):
            members.setdefault(area, []).append(e["entity_id"])
    return [{"id": a["area_id"], "name": a.get("name") or a["area_id"], "floor": a.get("floor_id"),
             "entities": list(dict.fromkeys(members.get(a["area_id"], [])))}
            for a in reg.get("area", []) if a.get("area_id")]


def floors_from(reg: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"id": f["floor_id"], "name": f.get("name") or f["floor_id"]} for f in reg.get("floor", []) if f.get("floor_id")]


def devices_from(reg: dict[str, list[dict[str, Any]]], media_integrations: tuple[str, ...],
                 known: set[str]) -> dict[str, Any]:
    by_device: dict[str, list[str]] = {}
    platforms: list[list[str]] = []
    for e in reg.get("entity", []):
        eid = e.get("entity_id")
        if not eid:
            continue
        if e.get("device_id"):
            by_device.setdefault(e["device_id"], []).append(eid)
        if eid.startswith("media_player.") and e.get("platform") in media_integrations:
            platforms.append([eid, e["platform"]])
    devices = []
    for d in reg.get("device", []):
        ents = by_device.get(d.get("id") or "", [])
        if not d.get("id") or not any(x in known for x in ents):
            continue  # only devices with a live entity, like the template's walk over states
        ids = d.get("identifiers") or []
        devices.append({"id": d["id"], "name": d.get("name_by_user") or d.get("name"),
                        "manufacturer": d.get("manufacturer"), "model": d.get("model"),
                        "integration": ids[0][0] if ids and ids[0] else None, "entities": ents})
    return {"devices": devices, "platforms": platforms}


def media_links_from(reg: dict[str, list[dict[str, Any]]], states: dict[str, Any]) -> list[dict[str, Any]]:
    device_of = {e["entity_id"]: e.get("device_id") for e in reg.get("entity", []) if e.get("entity_id")}
    remotes: dict[str, str] = {}
    for e in reg.get("entity", []):
        if (e.get("entity_id") or "").startswith("remote.") and e.get("device_id"):
            remotes.setdefault(e["device_id"], e["entity_id"])
    ident = {d["id"]: (d.get("identifiers") or []) for d in reg.get("device", []) if d.get("id")}
    out = []
    for eid in states:
        if not eid.startswith("media_player."):
            continue
        dev = device_of.get(eid)
        ids = ident.get(dev or "", [])
        out.append({"entity_id": eid, "remote": remotes.get(dev) if dev else None,
                    "platform": ids[0][0] if ids and ids[0] else None})
    return out
