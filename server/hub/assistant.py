"""Ask Thomas Creek: the chat and voice assistant.

The model never gets more power than the person asking. Every tool runs as a
real request to this hub with the caller's own token, so the same role checks,
Face ID tiers and logging apply as when they tap a button:

- `house` lists the rooms and devices this person can see (from the snapshot).
- `act` posts to /v1/actions.
- `hub_get` / `hub_change` call an allow-listed set of the hub's own endpoints.

A 428 (Face ID needed) or any delete is not carried out: it comes back to the
app as a confirm card, and the app sends the same request itself after the
person confirms. A 403 comes back as "not allowed" (with ask_parent for kids).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .policy import Subject

log = logging.getLogger("hub.assistant")

CONV_IDLE_S = 20 * 60
MAX_TURNS = 24  # user turns kept in one conversation before it starts fresh
RESULT_CHARS = 12000
RATE_WINDOW_S = 600
RATE_MAX = 40

# Never reachable from the assistant: sign-in, keys, raw media/camera bytes, device plumbing.
DENY = re.compile(
    r"^/v1/(admin|auth|enroll|hello|ping|events|assistant|vpn|push|devices/(push|prefs|activity)|app/)"
    r"|/(stream|hls|file|mjpeg|webrtc|player|pass)(/|$)|\.(jpg|mp4)$|/(art|image|assets|player-art)(/|$)"
)
# Catalog sections hidden from people without the matching capability (the endpoints refuse anyway).
TAG_CAPS: dict[str, tuple[str, ...]] = {
    "cameras": ("cameras",), "network": ("network.manage", "network.ask"), "people": ("people",),
    "trips": ("trips.view",), "recipes": ("family.view", "shopping.add"), "kitchen": ("appliances",),
    "holiday lights": ("holiday.full", "holiday.looks", "holiday.pick"),
}

SYSTEM = """You are Thomas Creek, the assistant inside the family's home app. People talk to you by voice or quick chat to control and check on the house and to change things in the app: lights, climate, doors, TVs and speakers, cameras, chores and rewards, calendar, meals and recipes, shopping lists, media requests, trips, the kids' internet time, alerts and settings.

How to work:
- You act as the person asking. Each tool call goes through the house's own permission checks, so try what they ask; if the hub says no, tell them plainly in one short sentence. Never look for a way around a refusal.
- For a child, a refused ask can usually become a request to a parent (POST /v1/requests). Offer that, or just do it when they clearly want it ("can I have more iPad time").
- When the hub says Face ID is needed, the app will show a confirm card. Say in a few words what will happen once they confirm, and never say it is done.
- Deletes always wait for the person to tap Confirm. Same rule.
- Find exact ids with `house` (devices, rooms, scenes, modes) or a search endpoint before acting. Do several independent things in parallel.
- Replies are often read aloud: one or two short sentences, plain words, no lists or markdown unless they asked for detail. Use the room and device names people use. Times in the house's time zone.
- If something is ambiguous and getting it wrong matters (which door, which kid), ask one short question. Otherwise pick the obvious reading and do it.
- You can't change the app's code, network firewall rules, storage, or anyone's permissions."""


@dataclass
class Conversation:
    id: str
    device_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    used_at: float = field(default_factory=time.time)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # confirm id -> request


@dataclass
class Turn:
    """What one request collected for the app."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    confirms: list[dict[str, Any]] = field(default_factory=list)
    changed: bool = False


class AssistantError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


class Assistant:
    def __init__(self, hub: Any):
        self.hub = hub
        self.convs: dict[str, Conversation] = {}
        self._hits: dict[str, list[float]] = {}
        self._catalog: dict[str, str] = {}
        self._client: Any = None
        self._client_key: str | None = None
        # Tests swap this for a fake model.
        self.create = self._create

    # -- setup -------------------------------------------------------------
    @property
    def cfg(self) -> Any:
        return self.hub.config.assistant

    def ready(self) -> bool:
        return bool(self.cfg.enabled and self.hub.vault.has(self.cfg.key_secret))

    def _anthropic(self) -> Any:
        key = self.hub.vault.get(self.cfg.key_secret)
        if not key:
            raise AssistantError(503, "assistant_off", "The assistant needs a Claude API key on the hub's admin page")
        if self._client is None or self._client_key != str(key):
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=str(key), max_retries=2, timeout=60)
            self._client_key = str(key)
        return self._client

    async def _create(self, **kw: Any) -> Any:
        client = self._anthropic()
        return await client.beta.messages.create(**kw)

    def catalog(self, app: Any, s: Subject) -> str:
        """The hub endpoints this person may call, one line each, from the app's own OpenAPI."""
        caps = set(self.hub.policy.capabilities_for(s))
        key = ",".join(sorted(caps))
        if key in self._catalog:
            return self._catalog[key]
        spec = app.openapi()
        schemas = spec.get("components", {}).get("schemas", {})
        lines = []
        for path, ops in sorted(spec.get("paths", {}).items()):
            if not path.startswith("/v1/") or DENY.search(path) or path == "/v1/actions":
                continue
            for method, op in ops.items():
                tags = op.get("tags") or []
                need = [c for t in tags for c in TAG_CAPS.get(t, ())]
                if need and not caps & set(need):
                    continue
                q = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"]
                body = ""
                ref = (op.get("requestBody", {}).get("content", {}).get("application/json", {})
                       .get("schema", {}))
                ref = ref.get("$ref") or next((x.get("$ref") for x in ref.get("anyOf", []) if x.get("$ref")), None)
                if ref:
                    props = schemas.get(ref.rsplit("/", 1)[-1], {}).get("properties", {})
                    body = " body{" + ", ".join(props) + "}"
                doc = (op.get("description") or "").strip().split("\n")[0][:110]
                summ = op.get("summary", "")
                lines.append(f"{method.upper()} {path}" + (f" ?{','.join(q)}" if q else "") + body
                             + f"  # {doc or summ}")
        text = "\n".join(lines)
        self._catalog[key] = text
        return text

    def tools(self, app: Any, s: Subject) -> list[dict[str, Any]]:
        cat = self.catalog(app, s)
        return [
            {"name": "house", "description": (
                "Rooms, devices, scenes, modes, cameras and what is playing that this person can see, with ids, "
                "current state and the actions each device allows. Filter by words in a room or device name "
                "(e.g. 'basement', 'garage') or by kind (light, climate, garage, lock, media, speaker, fan, switch, "
                "camera, oven, vacuum, valve, scene)."),
             "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                 "query": {"type": "string", "description": "Words to match in room or device names. Empty for everything."},
                 "kind": {"type": "string"}}}},
            {"name": "act", "description": (
                "Control a device or run a scene or mode (POST /v1/actions). Actions and params: "
                "light.set {on, brightness 0-100, color '#RRGGBB'}; fan.set {on, speed 0-100}; switch.set {on}; "
                "scene.run (target scene.x); mode.run (target mode id); climate.set {target_temp, hvac heat|cool|auto|off}; "
                "lock.set {locked}; garage.set {open}; media.command {command power|play|pause|next|prev|stop|mute|"
                "vol_up|vol_down|volume|source|up|down|left|right|select|back|home, volume, source}; "
                "media.volume {volume}; speaker.group {members[]}; announce {text, rooms[]} (no target); "
                "vacuum.start; valve.run {minutes}; oven.set {mode, temp, minutes}; oven.timer {minutes}; "
                "shower.preheat; network.pause / network.grant {minutes} (target = child's person id); "
                "system.app_restart (target app name). Use ids from `house`."),
             "input_schema": {"type": "object", "additionalProperties": False, "required": ["action", "summary"],
                              "properties": {
                                  "action": {"type": "string"}, "target": {"type": "string"},
                                  "params": {"type": "object"},
                                  "summary": {"type": "string", "description": "Short label for the person, e.g. 'Turn off basement lights'."}}}},
            {"name": "hub_get", "description": "Read from the hub as this person. Endpoints:\n" + cat
                + "\nOnly GET lines apply. Fill {path} parts yourself.",
             "input_schema": {"type": "object", "additionalProperties": False, "required": ["path"],
                              "properties": {"path": {"type": "string"}, "query": {"type": "object"}}}},
            {"name": "hub_change", "description": (
                "Change something through the hub as this person (POST, PUT, PATCH or DELETE from the endpoint "
                "list in hub_get). Deletes and Face ID steps come back as a confirm card instead of running."),
             "input_schema": {"type": "object", "additionalProperties": False, "required": ["method", "path", "summary"],
                              "properties": {
                                  "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
                                  "path": {"type": "string"}, "body": {"type": "object"},
                                  "summary": {"type": "string", "description": "Short label for the person."}}}},
        ]

    # -- the request -------------------------------------------------------
    def _rate(self, device_id: str) -> None:
        now = time.time()
        hits = [t for t in self._hits.get(device_id, []) if now - t < RATE_WINDOW_S]
        if len(hits) >= RATE_MAX:
            raise AssistantError(429, "slow_down", "That's a lot of questions. Try again in a few minutes.")
        hits.append(now)
        self._hits[device_id] = hits

    def _conversation(self, cid: str | None, device_id: str) -> Conversation:
        now = time.time()
        for k in [k for k, c in self.convs.items() if now - c.used_at > CONV_IDLE_S]:
            del self.convs[k]
        c = self.convs.get(cid or "")
        if c is None or c.device_id != device_id or c.turns >= MAX_TURNS:
            c = Conversation(id=secrets.token_urlsafe(12), device_id=device_id)
            self.convs[c.id] = c
        c.used_at = now
        return c

    def _who(self, s: Subject, screen: str | None, now_text: str) -> str:
        parts = [f"Person asking: {s.name or 'unknown'} (role {s.role}"
                 + (", a child" if s.role == "child" else "") + ")."]
        parts.append("They are at home." if s.at_home else "They are away from home.")
        if s.areas:
            parts.append(f"Their own room(s): {', '.join(s.areas)}.")
        if screen:
            parts.append(f"Screen open in the app: {screen[:200]}.")
        parts.append(f"Now: {now_text}.")
        return " ".join(parts)

    async def ask(self, *, app: Any, subject: Subject, device_id: str, auth: str, ip: str | None,
                  text: str, conversation_id: str | None = None, screen: str | None = None,
                  confirmed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self.cfg.enabled:
            raise AssistantError(503, "assistant_off", "The assistant is turned off")
        self._anthropic()
        self._rate(device_id)
        conv = self._conversation(conversation_id, device_id)
        turn = Turn()

        notes = []
        for r in confirmed or []:
            req = conv.pending.pop(str(r.get("id")), None)
            if req:
                notes.append(f"[{req['summary']}: {'done' if r.get('ok') else 'not done'}"
                             + (f" ({str(r.get('message'))[:120]})" if r.get("message") else "") + "]")
        user_text = (" ".join(notes) + "\n" if notes else "") + text.strip()[:2000]
        now_text = self.hub.policy.now().strftime("%A %Y-%m-%d %H:%M %Z")
        start = len(conv.messages)
        conv.messages.append({"role": "user", "content": user_text})
        # Who is asking and when goes in a mid-conversation system message, so the
        # tools and system prompt above it stay cached.
        conv.messages.append({"role": "system", "content": self._who(subject, screen, now_text)})
        conv.turns += 1

        system = [{"type": "text", "text": SYSTEM + "\nThe house is called " + self.cfg.house_name + "."}]
        tools = self.tools(app, subject)
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        ctx = {"app": app, "auth": auth, "ip": ip, "subject": subject, "conv": conv, "turn": turn}

        reply = ""
        usage = {"in": 0, "out": 0, "cached": 0}
        for _ in range(self.cfg.max_steps):
            kw: dict[str, Any] = dict(
                model=self.cfg.model, max_tokens=4096, system=system, tools=tools, messages=conv.messages,
                output_config={"effort": self.cfg.effort},
                cache_control={"type": "ephemeral"},
            )
            if self.cfg.fallbacks:
                kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            try:
                resp = await self.create(**kw)
            except AssistantError:
                raise
            except Exception as exc:  # anthropic errors, network
                log.warning("assistant model call failed: %s", type(exc).__name__)
                del conv.messages[start:]
                conv.turns -= 1
                raise AssistantError(502, "assistant_unavailable", _friendly(exc)) from None
            u = getattr(resp, "usage", None)
            if u is not None:
                usage["in"] += getattr(u, "input_tokens", 0) or 0
                usage["out"] += getattr(u, "output_tokens", 0) or 0
                usage["cached"] += getattr(u, "cache_read_input_tokens", 0) or 0
            content = [_dump(b) for b in resp.content]
            conv.messages.append({"role": "assistant", "content": content})
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if resp.stop_reason == "refusal":
                reply = "I can't help with that one."
                break
            uses = [b for b in content if b.get("type") == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                reply = "\n".join(t for t in texts if t).strip()
                break
            results = []
            for b in uses:
                out = await self._tool(ctx, b["name"], b.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": json.dumps(out, separators=(",", ":"), default=str)[:RESULT_CHARS],
                                **({"is_error": True} if out.get("error") else {})})
            conv.messages.append({"role": "user", "content": results})
        else:
            reply = reply or "That took too many steps. Try asking in smaller pieces."
        if not reply:
            reply = "Done." if turn.changed and not turn.confirms else "Okay."

        self.hub.db.log("assistant.ask", "ok", person_id=subject.person_id, device_id=device_id,
                        detail=f"in={usage['in']} cached={usage['cached']} out={usage['out']} steps={len(turn.steps)}")
        return {"conversation_id": conv.id, "reply": reply, "steps": turn.steps, "confirms": turn.confirms,
                "changed": turn.changed}

    # -- tools -------------------------------------------------------------
    async def _tool(self, ctx: dict[str, Any], name: str, inp: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "house":
                return self._house(ctx["subject"], str(inp.get("query") or ""), str(inp.get("kind") or ""))
            if name == "act":
                body = {"action": str(inp.get("action", "")), "target": inp.get("target"),
                        "params": inp.get("params") or {}}
                return await self._call(ctx, "POST", "/v1/actions", None, body, str(inp.get("summary") or body["action"]))
            if name == "hub_get":
                return await self._call(ctx, "GET", str(inp.get("path", "")), inp.get("query") or None, None, None)
            if name == "hub_change":
                return await self._call(ctx, str(inp.get("method", "POST")).upper(), str(inp.get("path", "")),
                                        None, inp.get("body"), str(inp.get("summary") or "Change"))
        except AssistantError as exc:
            return {"error": exc.message}
        return {"error": f"unknown tool {name}"}

    def _house(self, s: Subject, query: str, kind: str) -> dict[str, Any]:
        snap = self.hub.home.snapshot(s)
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        rooms = {r["id"]: r["name"] for r in snap.get("rooms", [])}

        def hit(*names: str) -> bool:
            hay = " ".join(n.lower() for n in names if n)
            return all(w in hay or w.rstrip("s") in hay for w in words)

        devices = []
        for d in snap.get("devices", []):
            if kind and d["kind"] != kind:
                continue
            if words and not hit(d["name"], rooms.get(d["room_id"], ""), d["id"], d["kind"]):
                continue
            st = {k: v for k, v in (d.get("state") or {}).items() if v not in (None, "", [], {})}
            devices.append({"id": d["id"], "name": d["name"], "room": rooms.get(d["room_id"]), "kind": d["kind"],
                            "state": st, "actions": d.get("actions", []),
                            **({} if d.get("available", True) else {"offline": True})})
        scenes = [{"id": eid, "name": st.get("attributes", {}).get("friendly_name", eid),
                   "room": rooms.get(self.hub.home.entity_area.get(eid) or "")}
                  for eid, st in self.hub.home.states.items() if eid.startswith("scene.")
                  and (not words or hit(st.get("attributes", {}).get("friendly_name", eid), eid))
                  and self.hub.policy.has(s, "lights.all")]
        out: dict[str, Any] = {"devices": devices[:150]}
        if len(devices) > 150:
            out["more"] = f"{len(devices) - 150} more; narrow the query"
        if not kind or kind == "scene":
            out["scenes"] = scenes[:40]
        if not words and not kind:
            out["rooms"] = [{"id": r["id"], "name": r["name"], "summary": r.get("summary")} for r in snap.get("rooms", [])]
            out["modes"] = snap.get("modes", [])
            out["now_playing"] = snap.get("now_playing", [])
            out["status"] = snap.get("status")
        if (not kind or kind == "camera") and snap.get("cameras"):
            out["cameras"] = [{"id": c.get("id"), "name": c.get("name")} for c in snap["cameras"]
                              if not words or hit(c.get("name", ""), c.get("id", ""))]
        return out

    async def _call(self, ctx: dict[str, Any], method: str, path: str, query: dict[str, Any] | None,
                    body: Any, summary: str | None) -> dict[str, Any]:
        path = "/" + path.lstrip("/")
        if not path.startswith("/v1/"):
            path = "/v1" + path
        if ".." in path or "?" in path or DENY.search(path):
            raise AssistantError(403, "not_allowed", "That part of the hub isn't available to the assistant")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise AssistantError(400, "bad_method", "Unknown method")
        conv: Conversation = ctx["conv"]
        turn: Turn = ctx["turn"]
        label = summary or f"{method} {path}"
        if method == "DELETE":
            return self._confirm(conv, turn, "tap", method, path, query, body, label)

        transport = httpx.ASGITransport(app=ctx["app"], client=(ctx["ip"] or "127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="https://hub.internal", timeout=30) as cl:
            r = await cl.request(method, path, params=query or None,
                                 json=body if method != "GET" else None,
                                 headers={"authorization": ctx["auth"], "x-tc-via": "ask"})
        try:
            data: Any = r.json()
        except ValueError:
            data = {"text": r.text[:500]}
        if r.status_code == 428:
            return self._confirm(conv, turn, "faceid", method, path, query, body, label)
        if r.status_code == 403:
            ask = bool(isinstance(data, dict) and data.get("ask_parent"))
            if method != "GET":
                turn.steps.append({"label": label, "status": "denied"})
            return {"error": "not allowed for this person", "ask_parent": ask,
                    "message": data.get("message") if isinstance(data, dict) else None}
        if r.status_code >= 400:
            if method != "GET":
                turn.steps.append({"label": label, "status": "failed"})
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
            return {"error": f"{r.status_code}: {msg}"}
        if method != "GET":
            turn.steps.append({"label": label, "status": "done"})
            turn.changed = True
        return {"ok": True, "data": data}

    def _confirm(self, conv: Conversation, turn: Turn, kind: str, method: str, path: str,
                 query: dict[str, Any] | None, body: Any, label: str) -> dict[str, Any]:
        cid = secrets.token_urlsafe(8)
        req = {"id": cid, "kind": kind, "summary": label, "method": method, "path": path,
               "query": query or None, "body": body}
        conv.pending[cid] = req
        turn.confirms.append(req)
        turn.steps.append({"label": label, "status": "confirm"})
        who = "confirm with Face ID" if kind == "faceid" else "tap Confirm"
        return {"waiting": True, "message": f"Not done yet: a card in the app asks the person to {who}. "
                                            "It runs when they do. Don't say it's done."}


def _dump(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_none=True)


def _friendly(exc: Exception) -> str:
    name = type(exc).__name__
    if "Authentication" in name or "PermissionDenied" in name:
        return "The Claude API key on the hub was refused. Check it on the admin page."
    if "RateLimit" in name:
        return "The assistant is busy. Try again in a minute."
    if "Connection" in name or "Timeout" in name:
        return "The hub can't reach the assistant service right now."
    return "The assistant had a problem. Try again."
