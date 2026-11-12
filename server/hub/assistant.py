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

import base64
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
RESULT_CHARS = 20000
MAX_IMAGE_BYTES = 3_500_000  # the API takes up to 5 MB per image
RATE_WINDOW_S = 600
RATE_MAX = 40

# Never reachable from the assistant: sign-in, keys, raw media/camera bytes, device plumbing.
DENY = re.compile(
    r"^/v1/(admin|auth|enroll|hello|ping|events|assistant|vpn|push|devices/(push|prefs|activity)|app/)"
    r"|/(stream|hls|file|mjpeg|webrtc|player|pass)(/|$)|\.(jpg|mp4)$|/(art|image|assets|player-art|picture)(/|$)"
)
# binary_sensor device classes the assistant can read, and how to say on / off.
SENSOR_WORDS: dict[str, tuple[str, str]] = {
    "door": ("open", "closed"), "window": ("open", "closed"), "opening": ("open", "closed"),
    "garage_door": ("open", "closed"), "motion": ("motion", "clear"), "occupancy": ("occupied", "clear"),
    "presence": ("present", "away"), "moisture": ("wet", "dry"), "smoke": ("smoke", "clear"),
    "gas": ("gas", "clear"), "carbon_monoxide": ("CO detected", "clear"), "safety": ("unsafe", "safe"),
    "problem": ("problem", "ok"), "vibration": ("vibration", "still"), "sound": ("sound", "quiet"),
}

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
- Chores, routines and family calendar events live on the family board: list, add, change or remove them with /v1/family/items. To change a trip, read GET /v1/trip-tools for TREK's planning tools and their inputs, then POST /v1/trip-tools/{name} with {"arguments": {...}} (removals use DELETE).
- Use `look` to see a camera when the question is about what is there right now.
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


def _fit(out: dict[str, Any], limit: int = RESULT_CHARS) -> str:
    """Tool result as JSON under the size limit. Never cuts mid-JSON (the model then sees a broken,
    partial list): it drops whole items from the end of the longest list and says how many."""
    def dump(o: Any) -> str:
        return json.dumps(o, separators=(",", ":"), default=str)
    text = dump(out)
    if len(text) <= limit:
        return text
    out = dict(out)
    lists = [k for k, v in out.items() if isinstance(v, list)]
    if not lists:
        return dump({"error": "result too large; ask for something narrower"})
    key = max(lists, key=lambda k: len(dump(out[k])))
    items = out[key]
    lo, hi = 0, len(items)
    while lo < hi:  # most items that still fit
        mid = (lo + hi + 1) // 2
        trial = {**out, key: items[:mid], "more": f"{len(items) - mid} more {key} not shown; narrow by room or kind"}
        lo, hi = (mid, hi) if len(dump(trial)) <= limit else (lo, mid - 1)
    out[key] = items[:lo]
    out["more"] = f"{len(items) - lo} more {key} not shown; narrow by room or kind"
    text = dump(out)
    return text if len(text) <= limit else dump({"error": "result too large; ask for something narrower"})


def _forget_pictures(messages: list[dict[str, Any]]) -> None:
    """Pictures are sent once. Later turns keep a note instead, so a chat doesn't re-send megabytes."""
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                b["content"] = [{"type": "text", "text": "(picture shown earlier)"} if c.get("type") == "image" else c
                                for c in b["content"]]


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
                "camera, oven, vacuum, valve (sprinkler zones), sensor (door/window/motion/presence/leak/smoke, read-only), "
                "scene, weather). With no filter it also gives the weather."),
             "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                 "query": {"type": "string", "description": "Words to match in room or device names. Empty for everything."},
                 "kind": {"type": "string"},
                 "active": {"type": "boolean", "description": (
                     "true for only what is on, open, unlocked or playing right now. Use it for 'what is on'.")}}}},
            {"name": "look", "description": (
                "See a camera right now, or the snapshot of one camera event, as a picture. Use it for questions "
                "like 'is there a car in the driveway' or 'what set off the back camera'. Camera ids come from "
                "`house` (cameras) and event ids from GET /v1/cameras/events."),
             "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                 "camera": {"type": "string", "description": "Camera id for a live picture."},
                 "event_id": {"type": "string", "description": "Event id for that event's snapshot."}}}},
            {"name": "act", "description": (
                "Control a device or run a scene or mode (POST /v1/actions). Actions and params: "
                "light.set {on, brightness 0-100, color '#RRGGBB'}; fan.set {on, speed 0-100}; switch.set {on}; "
                "scene.run (target scene.x); mode.run (target mode id); climate.set {target_temp, hvac heat|cool|auto|off}; "
                "lock.set {locked}; garage.set {open}; media.command {command power|play|pause|next|prev|stop|mute|"
                "vol_up|vol_down|volume|source|up|down|left|right|select|back|home, volume, source}; "
                "media.volume {volume}; speaker.group {members[]}; announce {text, rooms[]} (no target); "
                "vacuum.start; valve.run {minutes 1-180 (default 10; it always stops itself), on:false to stop}; "
                "oven.set {mode, temp, minutes}; oven.timer {minutes}; "
                "shower.preheat; network.pause / network.grant {minutes} (target = child's person id); "
                "system.app_restart (target = NAS app name from GET /v1/systems); "
                "steps.run {label, steps:[{action, target, params}]} to do several light/switch/fan/lock/garage/TV "
                "changes as one (one confirmation). To play a Plex/library title on a TV use POST /v1/media/play "
                "(find it with GET /v1/media/search). Use ids from `house`."),
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
                img = out.pop("_image", None)
                content: Any = _fit(out) if img is None else [
                    {"type": "image", "source": {"type": "base64", "media_type": img[0], "data": img[1]}},
                    {"type": "text", "text": f"Picture: {out.get('about', '')}"}]
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": content,
                                **({"is_error": True} if out.get("error") else {})})
            conv.messages.append({"role": "user", "content": results})
        else:
            reply = reply or "That took too many steps. Try asking in smaller pieces."
        _forget_pictures(conv.messages)
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
                return self._house(ctx["subject"], str(inp.get("query") or ""), str(inp.get("kind") or ""),
                                   bool(inp.get("active")))
            if name == "look":
                return await self._look(ctx, str(inp.get("camera") or ""), str(inp.get("event_id") or ""))
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

    def _house(self, s: Subject, query: str, kind: str, active: bool = False) -> dict[str, Any]:
        snap = self.hub.home.snapshot(s)
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        rooms = {r["id"]: r["name"] for r in snap.get("rooms", [])}

        def hit(*names: str) -> bool:
            hay = " ".join(n.lower() for n in names if n)
            return all(w in hay or w.rstrip("s") in hay for w in words)

        devices: list[dict[str, Any]] = []
        kind_actions: dict[str, Any] = {}
        for d in snap.get("devices", []):
            if kind and d["kind"] != kind:
                continue
            if words and not hit(d["name"], rooms.get(d["room_id"], ""), d["id"], d["kind"]):
                continue
            raw = d.get("state") or {}
            if active and not (raw.get("on") or raw.get("open") or raw.get("playing") or raw.get("locked") is False):
                continue
            st = {k: v for k, v in raw.items() if v not in (None, "", [], {})}
            # Actions are the same for every device of a kind; list them once (below) to keep results small.
            acts = d.get("actions", [])
            if acts and kind_actions.setdefault(d["kind"], acts) != acts:
                st["actions"] = acts
            devices.append({"id": d["id"], "name": d["name"], "room": rooms.get(d["room_id"]), "kind": d["kind"],
                            "state": st, **({} if d.get("available", True) else {"offline": True})})
        # Devices Home Assistant hasn't put in a room: the app leaves them out, but adults can still ask.
        home = self.hub.home
        if home.visible_areas(s) is None:
            for eid in home.states:
                if home.entity_area.get(eid):
                    continue
                d = home.device(eid, s)
                if d is None or (kind and d["kind"] != kind) or (words and not hit(d["name"], d["id"], d["kind"])):
                    continue
                raw = d.get("state") or {}
                if active and not (raw.get("on") or raw.get("open") or raw.get("playing")):
                    continue
                devices.append({"id": d["id"], "name": d["name"], "room": None, "kind": d["kind"],
                                "state": {k: v for k, v in raw.items() if v not in (None, "", [], {})},
                                **({"actions": d["actions"]} if d.get("actions") else {}),
                                **({} if d.get("available", True) else {"offline": True})})
        sensors = self._sensors(s, rooms, hit if words else None, active) if not kind or kind == "sensor" else []
        scenes = [{"id": eid, "name": st.get("attributes", {}).get("friendly_name", eid),
                   "room": rooms.get(self.hub.home.entity_area.get(eid) or "")}
                  for eid, st in self.hub.home.states.items() if eid.startswith("scene.")
                  and (not words or hit(st.get("attributes", {}).get("friendly_name", eid), eid))
                  and self.hub.policy.has(s, "lights.all")]
        out: dict[str, Any] = {"devices": devices, "actions_by_kind": kind_actions}
        if sensors:
            out["sensors"] = sensors
        if not kind or kind == "scene":
            out["scenes"] = scenes[:40]
        if not words and not kind and not active:
            out["rooms"] = [{"id": r["id"], "name": r["name"], "summary": r.get("summary")} for r in snap.get("rooms", [])]
            out["modes"] = snap.get("modes", [])
            out["now_playing"] = snap.get("now_playing", [])
            out["status"] = snap.get("status")
        if (not words and not kind and not active) or kind == "weather":
            wx = getattr(getattr(self.hub, "smart", None), "weather", None)
            data = (wx.data if wx is not None else None) or snap.get("weather")
            if data:
                out["weather"] = data
        if (not kind or kind == "camera") and snap.get("cameras"):
            out["cameras"] = [{"id": c.get("id"), "name": c.get("name")} for c in snap["cameras"]
                              if not words or hit(c.get("name", ""), c.get("id", ""))]
        return out

    async def _look(self, ctx: dict[str, Any], camera: str, event_id: str) -> dict[str, Any]:
        """A JPEG from the hub's own camera route, as the caller (so the cameras permission applies)."""
        ref = event_id or camera
        if not ref or not re.fullmatch(r"[\w.\-]+", ref):
            return {"error": "give a camera id or an event id"}
        path = f"/v1/cameras/events/{ref}/snapshot.jpg" if event_id else f"/v1/cameras/{ref}/snapshot.jpg"
        transport = httpx.ASGITransport(app=ctx["app"], client=(ctx["ip"] or "127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="https://hub.internal", timeout=20) as cl:
            r = await cl.get(path, headers={"authorization": ctx["auth"], "x-tc-via": "ask"})
        if r.status_code == 403:
            return {"error": "not allowed for this person"}
        if r.status_code >= 400 or not r.content:
            return {"error": f"no picture ({r.status_code})"}
        if len(r.content) > MAX_IMAGE_BYTES:
            return {"error": "picture too large"}
        kind = r.headers.get("content-type", "image/jpeg").split(";")[0]
        return {"_image": (kind if kind.startswith("image/") else "image/jpeg", base64.b64encode(r.content).decode()),
                "about": f"event {event_id}" if event_id else f"camera {camera}, live"}

    def _sensors(self, s: Subject, rooms: dict[str, str], hit: Any, active: bool) -> list[dict[str, Any]]:
        """Door, window, motion, presence, leak and smoke sensors: read-only, in rooms this person can see."""
        home = self.hub.home
        vis = home.visible_areas(s)
        out = []
        for eid, st in home.states.items():
            if not eid.startswith("binary_sensor.") or st.get("state") in ("unavailable", "unknown", None):
                continue
            cls = st.get("attributes", {}).get("device_class")
            if cls not in SENSOR_WORDS:
                continue
            area = home.entity_area.get(eid)
            if vis is not None and area not in vis:
                continue
            name = st.get("attributes", {}).get("friendly_name", eid)
            on = st["state"] == "on"
            if active and not on:
                continue
            if hit is not None and not hit(name, rooms.get(area or "", ""), cls):
                continue
            out.append({"id": eid, "name": name, "room": rooms.get(area or ""), "type": cls,
                        "state": SENSOR_WORDS[cls][0 if on else 1], "since": st.get("last_changed")})
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
