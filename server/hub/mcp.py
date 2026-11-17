"""The hub's MCP server: outside AI apps (Claude Desktop, Claude Code, claude.ai) control the house.

One endpoint, `POST /mcp` (Streamable HTTP, JSON answers, no server-sent stream). It offers the
same tools as Ask Thomas Creek (hub/assistant.py: house, look, act, hub_get, hub_change), run the
same way: every call is an ordinary hub request made as the person the key belongs to, so the
policy, kids' rules and activity log apply exactly as they do for that person's buttons.

Callers sign in with an AI key (hub/aikeys.py), made on the admin page or by the OAuth sign-in
(hub/mcp_oauth.py) that claude.ai and the Claude apps use.

An AI app can never pass a Face ID step itself. Locks, the garage, other secure and admin steps,
deletes and changes to shared family records become an approval on that person's own iPhone
(a request of kind "ai"): Face ID there runs the stored call once. It expires after
`mcp.approval_minutes`. The `approval` tool tells the AI app how it ended.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from . import aikeys
from .actions import ActionError
from .assistant import Conversation, Turn, _fit
from .db import new_id

if TYPE_CHECKING:
    from fastapi import Request

    from .policy import Subject
    from .state import Hub

log = logging.getLogger("hub.mcp")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "thomas-creek-hub", "title": "Thomas Creek", "version": "1"}

INSTRUCTIONS = """Thomas Creek is a family home. These tools control and check on the house as the person this connection belongs to, with exactly their rights (a child's connection gets a child's limits).

- Find exact ids with `house` (rooms, devices, scenes, modes, cameras) or a search endpoint via `hub_get` before acting.
- `act` controls devices, scenes and TVs. `hub_get` reads, and `hub_change` changes, anything else the hub offers; the endpoint list is in the hub_get description.
- Locks, the garage, other Face ID steps, deletes and changes to chores, events, trips, people or phones are not done right away: they go to the person's iPhone for Face ID approval. Tell the person to approve it on their phone, never say it is done, and use `approval` with the id to see how it ended.
- When the hub says no, say so plainly. Never look for a way around a refusal.
- Times are in the house's time zone."""

APPROVAL_TOOL = {
    "name": "approval",
    "description": ("How an action that waited for Face ID on the person's iPhone ended: waiting, done, failed, "
                    "declined or expired, with the hub's answer once it ran."),
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"],
                    "properties": {"id": {"type": "string", "description": "The approval id an earlier tool returned."}}},
}


class McpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class McpServer:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._hits: dict[str, list[float]] = {}

    @property
    def cfg(self) -> Any:
        return self.hub.config.mcp

    # -- who is calling ----------------------------------------------------
    def where(self, ip: str | None) -> str:
        """"home", "vpn" (the home WireGuard tunnel) or "away"."""
        if self.hub.is_home(ip):
            return "home"
        try:
            if ip and self.hub.vpn.is_tunnel_address(ipaddress.ip_address(ip)):
                return "vpn"
        except ValueError:
            pass
        return "away"

    def sign_in(self, bearer: str | None, ip: str | None) -> dict[str, Any]:
        """The caller's key, device and subject; McpError when it can't be used from here."""
        if not self.cfg.enabled:
            raise McpError(503, "mcp_off", "The hub's MCP server is turned off on the admin page")
        found = aikeys.check(self.hub, bearer or "")
        if found is None:
            raise McpError(401, "unauthorized", "Sign in again: this AI key isn't valid any more")
        key, device = found
        where = self.where(ip)
        if where == "away" and not self.cfg.away:
            raise McpError(403, "home_only", "AI apps can reach the hub only from home or the home VPN")
        subject = self.hub.subject_for(device, where == "home")
        if subject.role == "pending" or not subject.person_id:
            raise McpError(403, "forbidden", "This AI key's person isn't in the house any more")
        now = time.time()
        hits = [t for t in self._hits.get(key["id"], []) if now - t < 60]
        if len(hits) >= self.cfg.calls_per_minute:
            raise McpError(429, "slow_down", "Too many calls; try again in a minute")
        hits.append(now)
        self._hits[key["id"]] = hits
        aikeys.touch(self.hub, key["id"])
        self.hub.db.update("devices", "id", device["id"], {"last_seen_at": now})
        return {"key": key, "device": device, "subject": subject, "ip": ip}

    # -- JSON-RPC ----------------------------------------------------------
    async def handle(self, app: Any, who: dict[str, Any], msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
        mid, method = msg.get("id"), msg["method"]
        params = msg.get("params") or {}
        if "id" not in msg:  # a notification (initialized, cancelled): nothing to answer
            return None
        try:
            if method == "initialize":
                asked = str(params.get("protocolVersion") or "")
                return _result(mid, {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS + f"\nThis connection belongs to {_person_name(self.hub, who)}.",
                })
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": self.tools(app, who["subject"])})
            if method == "tools/call":
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    return _error(mid, -32602, "tools/call needs a tool name")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    return _error(mid, -32602, "arguments must be an object")
                return _result(mid, await self.call(app, who, params["name"], args))
            if method in ("resources/list", "prompts/list"):
                return _result(mid, {method.split("/")[0]: []})
        except McpError as exc:
            return _error(mid, -32000, exc.message)
        return _error(mid, -32601, f"Unknown method {method}")

    def tools(self, app: Any, s: "Subject") -> list[dict[str, Any]]:
        out = []
        for t in self.hub.assistant.tools(app, s):
            desc = t["description"].replace("as this person", "as the person this connection belongs to")
            out.append({"name": t["name"], "description": desc, "inputSchema": t["input_schema"]})
        out.append(APPROVAL_TOOL)
        return out

    async def call(self, app: Any, who: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
        s: Subject = who["subject"]
        if name == "approval":
            out = self.approval_status(s, str(args.get("id") or ""))
        else:
            if name == "act":
                held = await self._secure_act(who, args)
                if held is not None:
                    return _content(held)
            auth = "Bearer " + self.hub.tokens.issue(who["device"]["id"], s.person_id, s.role)[0]
            conv = Conversation(id="mcp", device_id=who["device"]["id"])
            ctx = {"app": app, "auth": auth, "ip": who["ip"], "subject": s, "conv": conv, "turn": Turn(),
                   "via": "mcp", "on_confirm": self._on_confirm(who)}
            out = await self.hub.assistant._tool(ctx, name, args)
        img = out.pop("_image", None)
        if img is not None:
            return {"content": [{"type": "image", "mimeType": img[0], "data": img[1]},
                                {"type": "text", "text": f"Picture: {out.get('about', '')}"}]}
        return _content(out)

    async def _secure_act(self, who: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
        """A device action that needs Face ID goes to the phone before anything runs."""
        action = str(args.get("action") or "")
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        if not action or action.startswith("_") or any(str(k).startswith("_") for k in params):
            return None  # the hub's own route answers with the right error
        try:
            plan = self.hub.actions.plan(who["subject"], action, args.get("target"), params)
        except ActionError:
            return None
        if not plan.decision.allowed or plan.decision.tier not in ("secure", "admin"):
            return None
        body = {"action": action, "target": args.get("target"), "params": params}
        return await self.ask_phone(who, "POST", "/v1/actions", None, body, str(args.get("summary") or action))

    def _on_confirm(self, who: dict[str, Any]) -> Any:
        async def confirm(_kind: str, method: str, path: str, query: Any, body: Any, label: str) -> dict[str, Any]:
            return await self.ask_phone(who, method, path, query, body, label)
        return confirm

    # -- approvals on the iPhone -------------------------------------------
    async def ask_phone(self, who: dict[str, Any], method: str, path: str, query: Any, body: Any,
                        label: str) -> dict[str, Any]:
        s: Subject = who["subject"]
        label = (label or f"{method} {path}").strip()[:80]
        rid = new_id("req")
        payload = {"method": method, "path": path, "query": query, "body": body, "key_id": who["key"]["id"],
                   "device_id": who["device"]["id"], "ip": who["ip"], "app": who["key"]["label"]}
        self.hub.db.insert("requests", {"id": rid, "created_at": time.time(), "kind": "ai", "requester_id": s.person_id,
                                        "title": label, "payload": payload, "status": "waiting"})
        self.hub.db.log("mcp.approval", "waiting", person_id=s.person_id, device_id=who["device"]["id"], target=rid,
                        params={"method": method, "path": path}, source="mcp")
        await self.hub.notify.send(
            person_ids=[s.person_id], level="decision", cat="requests", title=f"{who['key']['label']}: {label}?",
            body="An AI app asked to do this as you. Approve with Face ID, or ignore it.",
            actions=[{"id": "approve", "title": "Approve"}, {"id": "deny", "title": "Not now", "destructive": True}],
            ref=f"thomascreek://requests/{rid}", group_key=f"request:{rid}", push_category="REQUEST",
        )
        mins = self.cfg.approval_minutes
        return {"waiting": True, "approval_id": rid,
                "message": (f"Not done yet: sent to {s.name or 'the person'}'s iPhone to approve with Face ID. "
                            f"It runs when they approve, within {mins} minutes. Check with the approval tool.")}

    def _expired(self, r: dict[str, Any]) -> bool:
        return r["status"] == "waiting" and r["created_at"] < time.time() - self.cfg.approval_minutes * 60

    def approval_status(self, s: "Subject", rid: str) -> dict[str, Any]:
        r = self.hub.db.one("SELECT * FROM requests WHERE id=? AND kind='ai'", [rid])
        if r is None or r["requester_id"] != s.person_id:
            return {"error": "no such approval"}
        if self._expired(r):
            self.hub.db.update("requests", "id", rid, {"status": "expired"})
            r["status"] = "expired"
        out: dict[str, Any] = {"id": rid, "what": r["title"],
                               "status": {"approved": "done", "denied": "declined"}.get(r["status"], r["status"])}
        if r["payload"].get("result") is not None:
            out["result"] = r["payload"]["result"]
        return out

    async def decide(self, request: "Request", c: Any, r: dict[str, Any], decision: str) -> None:
        """The person answered an AI approval on their iPhone (POST /v1/requests/{id}/approve or the alert)."""
        from .routes.deps import error, require_confirm

        h = self.hub
        if c.subject.person_id != r["requester_id"]:
            raise error(403, "forbidden", "Only the person the AI app acts for can approve this")
        if r["status"] != "waiting":
            raise error(409, "already_decided", "This was already answered")
        if self._expired(r):
            h.db.update("requests", "id", r["id"], {"status": "expired"})
            raise error(409, "expired", "This approval expired. Ask the AI app again.")
        if decision == "deny":
            h.db.update("requests", "id", r["id"], {"status": "denied", "decided_by": c.subject.person_id,
                                                    "decided_at": time.time()})
            h.db.log("mcp.approval", "denied", person_id=c.subject.person_id, device_id=c.device["id"], target=r["id"])
            return
        await require_confirm(request, h, c, "secure")
        ok, result = await self._run_approved(request.app, r)
        payload = {**r["payload"], "result": result}
        h.db.update("requests", "id", r["id"], {"status": "approved" if ok else "failed", "payload": payload,
                                                "decided_by": c.subject.person_id, "decided_at": time.time()})
        h.db.log("mcp.approval", "ok" if ok else "error", person_id=c.subject.person_id, device_id=c.device["id"],
                 target=r["id"], detail=None if ok else str(result)[:200])
        if not ok:
            raise error(502, "failed", f"Approved, but it didn't go through: {result}")

    async def _run_approved(self, app: Any, r: dict[str, Any]) -> tuple[bool, Any]:
        """Run the stored call once, as the AI key's device. Face ID was just given on the phone."""
        h = self.hub
        p = r["payload"]
        device = h.db.device(p["device_id"])
        key = h.db.one("SELECT * FROM ai_keys WHERE id=?", [p["key_id"]])
        if device is None or device.get("status") != "active" or key is None or key.get("revoked_at"):
            return False, "the AI key was turned off"
        subject = h.subject_for(device, self.where(p.get("ip")) == "home")
        if p["path"] == "/v1/actions" and p["method"] == "POST":
            b = p["body"] or {}
            try:
                plan = h.actions.plan(subject, str(b.get("action")), b.get("target"), b.get("params") or {})
                if not plan.decision.allowed:
                    return False, "not allowed for this person"
                return True, await h.actions.run(subject, plan, source="mcp")
            except ActionError as exc:
                return False, exc.message
        auth = "Bearer " + h.tokens.issue(device["id"], subject.person_id, subject.role)[0]
        transport = httpx.ASGITransport(app=app, client=(p.get("ip") or "127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="https://hub.internal", timeout=30) as cl:
            resp = await cl.request(p["method"], p["path"], params=p.get("query") or None,
                                    json=p["body"] if p["method"] != "GET" else None,
                                    headers={"authorization": auth, "x-tc-via": "mcp"})
        try:
            data: Any = resp.json()
        except ValueError:
            data = {"text": resp.text[:300]}
        if resp.status_code >= 400:
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
            if resp.status_code in (403, 428) and "Face ID" in str(msg):
                msg = "this step can only be done in the Thomas Creek app"
            return False, msg
        return True, data


def _person_name(h: "Hub", who: dict[str, Any]) -> str:
    p = h.db.person(who["subject"].person_id)
    return f"{p['name']} ({p['role']})" if p else "someone"


def _content(out: dict[str, Any]) -> dict[str, Any]:
    text = _fit(out)
    return {"content": [{"type": "text", "text": text if isinstance(text, str) else json.dumps(text)}],
            "isError": bool(out.get("error"))}


def _result(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
