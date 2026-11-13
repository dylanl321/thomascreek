"""Find and add TVs and streaming boxes for the remote: Android / Google TV and Apple TV.

Pairing happens once, on the hub, through Home Assistant's own setup flows
(androidtv_remote and apple_tv), so the pairing keys live in Home Assistant and
every phone, iPad and Watch in the house can use the TV straight away. Nobody
pairs anything on their own device.

Where devices come from:
- Home Assistant: devices already added (config entries) and devices its
  discovery found but nobody set up yet (setup flows in progress).
- A short mDNS query from the hub itself (host networking), so a TV shows up
  even if Home Assistant's discovery card was dismissed long ago.

The hub reaches Home Assistant's setup API with its own token, which therefore
has to belong to an admin user in Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import AdapterError

log = logging.getLogger("hub.remotes")

# Home Assistant integration (setup flow handler) per kind of device.
HANDLERS = {"android": "androidtv_remote", "apple": "apple_tv"}
KIND_OF = {v: k for k, v in HANDLERS.items()}
FLOW_ID = re.compile(r"[A-Za-z0-9]{8,64}")

# What each setup step asks for, in the app's words. HA sends step ids and field names, not text.
STEP_TEXT = {
    "user": "Looking for the device…",
    "zeroconf_confirm": "Add {name} to the house?",
    "confirm": "Add {name} to the house?",
    "pair": "Type the code showing on {name}.",
    "pair_with_pin": "Type the code showing on {name}.",
    "pair_no_pin": "On {name}, enter {pin}, then tap Continue.",
    "protocol_disabled": "{name} has {protocol} turned off. Tap Continue to skip it.",
    "password": "{name} asks for an AirPlay password. Turn that off in its settings, then try again.",
    "reauth_confirm": "{name} needs pairing again. Tap Continue, then type the code on the TV.",
}
ABORT_TEXT = {
    "already_configured": "That one is already added.",
    "already_in_progress": "Someone is adding it right now. Try again in a minute.",
    "cannot_connect": "The hub couldn't reach it. Check that it's on and on home Wi-Fi.",
    "no_devices_found": "The hub couldn't find it on the network. Check that it's on.",
    "device_not_found": "The hub couldn't find it on the network. Check that it's on.",
    "backoff": "It asked the hub to wait. Try again in a few minutes.",
    "ignored_model": "Home Assistant doesn't support this model.",
    "unknown": "Something went wrong while pairing. Try again.",
}
ERROR_TEXT = {
    "invalid_auth": "That code didn't match. Check the TV and try again.",
    "cannot_connect": "The hub couldn't reach it. Check that it's on.",
    "no_usable_service": "This device doesn't allow remote control from the network.",
    "unknown": "Something went wrong. Try again.",
}

# Rebuilt by the template engine so the app can open the remote of each added device.
ENTITIES_TEMPLATE = """
{%- set ns = namespace(out=[]) -%}
{%- for dom in ['androidtv_remote', 'apple_tv'] -%}
  {%- for e in integration_entities(dom) | select('match', 'media_player\\.') -%}
    {%- set ns.out = ns.out + [{'entity_id': e, 'entry_id': config_entry_id(e), 'domain': dom}] -%}
  {%- endfor -%}
{%- endfor -%}
{{ ns.out | tojson }}
"""


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("’", "'"))


# ---------------------------------------------------------------------------------------------
# mDNS: one multicast query from an unprivileged port. Responders answer it directly (legacy
# unicast), which works next to the NAS's own Avahi on UDP 5353.

MDNS_SERVICES = ("_androidtvremote2._tcp", "_companion-link._tcp", "_googlecast._tcp", "_airplay._tcp")


@dataclass
class Found:
    kind: str
    host: str
    name: str
    model: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _qname(n: str) -> bytes:
    out = b""
    for p in n.split("."):
        if p:
            b = p.encode()
            out += bytes([len(b)]) + b
    return out + b"\0"


def _read_name(d: bytes, o: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped, end, hops = False, o, 0
    while hops < 30:
        ln = d[o]
        if ln == 0:
            o += 1
            break
        if ln & 0xC0 == 0xC0:
            if not jumped:
                end = o + 2
            jumped, o, hops = True, ((ln & 0x3F) << 8) | d[o + 1], hops + 1
            continue
        labels.append(d[o + 1:o + 1 + ln].decode("utf-8", "replace"))
        o += 1 + ln
    return ".".join(labels), (end if jumped else o)


def parse_mdns(d: bytes) -> list[tuple[str, str, Any]]:
    """(type, name, value) for PTR and TXT records in one mDNS answer; [] if it doesn't parse."""
    out: list[tuple[str, str, Any]] = []
    try:
        _, _, qd, an, ns, ar = struct.unpack("!6H", d[:12])
        o = 12
        for _ in range(qd):
            _, o = _read_name(d, o)
            o += 4
        for _ in range(an + ns + ar):
            name, o = _read_name(d, o)
            t, _, _, rl = struct.unpack("!HHIH", d[o:o + 10])
            o += 10
            if t == 12:
                out.append(("PTR", name, _read_name(d, o)[0]))
            elif t == 16:
                txt, i, rd = {}, 0, d[o:o + rl]
                while i < len(rd):
                    ln = rd[i]
                    k, _, v = rd[i + 1:i + 1 + ln].decode("utf-8", "replace").partition("=")
                    if k:
                        txt[k] = v
                    i += 1 + ln
                out.append(("TXT", name, txt))
            o += rl
    except (struct.error, IndexError):
        return out
    return out


def _instance(full: str, service: str) -> str:
    """'Living Room._companion-link._tcp.local' -> 'Living Room'."""
    i = full.find("." + service)
    return full[:i] if i > 0 else full


def remote_port_open(ip: str, port: int = 6466) -> bool:
    """Android TV Remote's control port. Some Google TV boxes (onn. 4K) answer on it without advertising it."""
    try:
        socket.create_connection((ip, port), timeout=0.4).close()
        return True
    except OSError:
        return False


def devices_from_answers(answers: list[tuple[str, list[tuple[str, str, Any]]]],
                         has_remote: Any = remote_port_open) -> list[Found]:
    """Turn (source ip, records) pairs into Android TV and Apple TV devices, one per address."""
    by_ip: dict[str, dict[str, Any]] = {}
    for ip, recs in answers:
        h = by_ip.setdefault(ip, {"svc": {}, "txt": {}})
        for t, name, v in recs:
            for svc in MDNS_SERVICES:
                if t == "PTR" and name.startswith(svc):
                    h["svc"].setdefault(svc, _instance(v, svc))
                if t == "TXT" and ("." + svc) in name:
                    h["txt"].setdefault(svc, v)
    out: list[Found] = []
    for ip, h in by_ip.items():
        svc, txt = h["svc"], h["txt"]
        cast = txt.get("_googlecast._tcp") or {}
        if "_androidtvremote2._tcp" in svc:
            out.append(Found("android", ip, cast.get("fn") or svc["_androidtvremote2._tcp"], cast.get("md")))
        elif cast.get("fn") and "_companion-link._tcp" not in svc and has_remote(ip):
            out.append(Found("android", ip, cast["fn"], cast.get("md")))
        elif "_companion-link._tcp" in svc:
            model = (txt.get("_companion-link._tcp") or {}).get("rpMd") or (txt.get("_airplay._tcp") or {}).get("model")
            if model and model.startswith("AppleTV"):
                out.append(Found("apple", ip, svc["_companion-link._tcp"], model))
    return sorted(out, key=lambda f: (f.kind, f.name.lower()))


def mdns_scan(timeout: float = 2.5) -> list[Found]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.bind(("", 0))
        s.settimeout(0.3)
        for svc in MDNS_SERVICES:
            q = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + _qname(svc + ".local") + struct.pack("!HH", 12, 0x8001)
            s.sendto(q, ("224.0.0.251", 5353))
        answers: list[tuple[str, list[tuple[str, str, Any]]]] = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                d, a = s.recvfrom(9000)
            except socket.timeout:
                continue
            answers.append((a[0], parse_mdns(d)))
        return devices_from_answers(answers)
    finally:
        s.close()


APPLE_MODELS = {"AppleTV5,3": "Apple TV HD", "AppleTV6,2": "Apple TV 4K", "AppleTV11,1": "Apple TV 4K (2nd gen)",
                "AppleTV14,1": "Apple TV 4K (3rd gen)"}


def model_name(kind: str, model: str | None) -> str | None:
    if kind == "apple":
        return APPLE_MODELS.get(model or "", "Apple TV")
    return model


# ---------------------------------------------------------------------------------------------


class RemoteSetup:
    """Lists TVs and boxes and drives Home Assistant's pairing flows for them."""

    CACHE_S = 20

    def __init__(self, hub: Any):
        self.hub = hub
        self._scan: tuple[float, list[Found]] | None = None
        self.scanner = mdns_scan  # tests swap this out

    # -- Home Assistant setup API ----------------------------------------------------------------
    async def _ha(self, method: str, path: str, body: Any = None) -> Any:
        ha = self.hub.ha
        try:
            resp = await ha.request(method, path, json=body) if body is not None else await ha.request(method, path)
        except AdapterError as exc:
            msg = str(exc)
            if "refused the hub's credential" in msg:
                raise SetupError(409, "ha_admin_needed",
                                 "Home Assistant only lets an admin add devices. Give the hub's Home Assistant "
                                 "user admin rights, then try again.") from None
            if " 404 " in msg or msg.endswith(" 404"):
                raise SetupError(404, "not_found", "That pairing ended. Start again.") from None
            raise SetupError(502, "ha_error", msg) from None
        return resp.json() if resp.content else None

    async def _entries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for dom in HANDLERS.values():
            rows = await self._ha("GET", f"/api/config/config_entries/entry?domain={dom}") or []
            out += [r for r in rows if r.get("domain") == dom]
        return out

    async def _entities(self) -> dict[str, str]:
        try:
            rows = json.loads(await self.hub.ha.render(ENTITIES_TEMPLATE))
        except Exception:  # noqa: BLE001 - older HA without config_entry_id(): no links, still usable
            return {}
        out: dict[str, str] = {}
        for r in rows:
            if r.get("entry_id") and r["entry_id"] not in out:
                out[r["entry_id"]] = r["entity_id"]
        return out

    async def _scan_network(self, fresh: bool) -> list[Found]:
        if not fresh and self._scan and time.time() - self._scan[0] < self.CACHE_S:
            return self._scan[1]
        try:
            found = await asyncio.to_thread(self.scanner)
        except OSError as exc:
            log.warning("mDNS scan failed: %s", exc)
            found = []
        self._scan = (time.time(), found)
        return found

    # -- listing --------------------------------------------------------------------------------
    async def overview(self, fresh: bool = False) -> dict[str, Any]:
        entries, flows, entities, scan = await asyncio.gather(
            self._entries(), self._ha("GET", "/api/config/config_entries/flow"), self._entities(),
            self._scan_network(fresh))
        home = self.hub.home
        added: list[dict[str, Any]] = []
        taken: set[str] = set()
        for e in entries:
            eid = entities.get(e.get("entry_id", ""))
            area_id = home.entity_area.get(eid) if eid else None
            st = home.states.get(eid or "", {})
            added.append({
                "id": e["entry_id"], "kind": KIND_OF[e["domain"]], "name": e.get("title") or "TV",
                "entity_id": eid, "room": (home.areas.get(area_id) or {}).get("name") if area_id else None,
                "working": e.get("state") == "loaded", "state": st.get("state"),
                "disabled": bool(e.get("disabled_by")),
            })
            taken.add(_norm(e.get("title")))
        found: list[dict[str, Any]] = []
        for f in flows or []:
            dom = f.get("handler")
            if dom not in KIND_OF or (f.get("context") or {}).get("source") in ("ignore", "user", "reauth"):
                continue
            name = ((f.get("context") or {}).get("title_placeholders") or {}).get("name") or "TV"
            if _norm(name) in taken:
                continue
            taken.add(_norm(name))
            found.append({"id": f"flow:{f['flow_id']}", "kind": KIND_OF[dom], "name": name, "host": None,
                          "model": None, "via": "home_assistant"})
        hosts = {x["host"] for x in found}
        for d in scan:
            if _norm(d.name) in taken or d.host in hosts:
                for x in found:  # fill in what Home Assistant's discovery card didn't say
                    if _norm(x["name"]) == _norm(d.name):
                        x["host"], x["model"] = d.host, model_name(d.kind, d.model)
                continue
            taken.add(_norm(d.name))
            found.append({"id": f"net:{d.kind}:{d.host}", "kind": d.kind, "name": d.name, "host": d.host,
                          "model": model_name(d.kind, d.model), "via": "network"})
        added.sort(key=lambda x: (x["kind"], x["name"].lower()))
        found.sort(key=lambda x: (x["kind"], x["name"].lower()))
        return {"added": added, "found": found, "scanned_at": self._scan[0] if self._scan else None}

    # -- pairing --------------------------------------------------------------------------------
    async def start(self, item_id: str, name: str | None = None) -> dict[str, Any]:
        """Begin adding a found device (or one typed by address: 'host:<kind>:<address>')."""
        if item_id.startswith("flow:"):
            fid = item_id[5:]
            flow = await self._flow(fid)
            return self._shape(flow, name)
        m = re.fullmatch(r"(?:net|host):(android|apple):([0-9A-Za-z.\-:]{2,64})", item_id)
        if not m:
            raise SetupError(400, "bad_target", "Unknown device")
        kind, host = m.groups()
        flow = await self._ha("POST", "/api/config/config_entries/flow",
                              {"handler": HANDLERS[kind], "show_advanced_options": False})
        if flow.get("type") == "form" and flow.get("step_id") == "user":
            key = "host" if kind == "android" else "device_input"
            flow = await self._ha("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {key: host})
        return self._shape(flow, name)

    async def answer(self, flow_id: str, pin: str | None) -> dict[str, Any]:
        """Continue a pairing: the code from the TV, or just Continue on a confirm step."""
        flow = await self._flow(flow_id)
        fields = [f.get("name") for f in flow.get("data_schema") or []]
        body: dict[str, Any] = {}
        if "pin" in fields:
            body["pin"] = clean_pin(flow.get("handler", ""), pin)
        name = ((flow.get("description_placeholders") or {}).get("name"))
        out = self._shape(await self._ha("POST", f"/api/config/config_entries/flow/{flow_id}", body), name)
        if out["state"] == "done":
            await self._after_added(out)
        return out

    async def cancel(self, flow_id: str) -> None:
        if not FLOW_ID.fullmatch(flow_id):
            raise SetupError(400, "bad_target", "Unknown pairing")
        try:
            await self._ha("DELETE", f"/api/config/config_entries/flow/{flow_id}")
        except SetupError as exc:
            if exc.code != "not_found":
                raise

    async def _flow(self, flow_id: str) -> dict[str, Any]:
        if not FLOW_ID.fullmatch(flow_id):
            raise SetupError(400, "bad_target", "Unknown pairing")
        flow = await self._ha("GET", f"/api/config/config_entries/flow/{flow_id}")
        if (flow or {}).get("handler") not in KIND_OF:
            raise SetupError(404, "not_found", "That pairing ended. Start again.")
        return flow

    def _shape(self, flow: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        """Home Assistant's flow step, reduced to what the app shows."""
        typ = flow.get("type")
        ph = {k: str(v) for k, v in (flow.get("description_placeholders") or {}).items() if v is not None}
        name = ph.get("name") or name or "the TV"
        out: dict[str, Any] = {"flow_id": flow.get("flow_id"), "kind": KIND_OF.get(flow.get("handler", ""), None),
                               "name": name}
        if typ == "create_entry":
            res = flow.get("result") or {}
            entry_id = res.get("entry_id") if isinstance(res, dict) else None
            return {**out, "state": "done", "entry_id": entry_id, "name": flow.get("title") or name,
                    "message": f"{flow.get('title') or name} is added. Everyone's remote has it now."}
        if typ == "abort":
            reason = flow.get("reason") or "unknown"
            return {**out, "state": "failed", "reason": reason, "message": ABORT_TEXT.get(reason, ABORT_TEXT["unknown"])}
        if typ != "form":
            return {**out, "state": "failed", "reason": typ or "unknown", "message": ABORT_TEXT["unknown"]}
        step = flow.get("step_id") or ""
        fields = [f.get("name") for f in flow.get("data_schema") or []]
        errs = flow.get("errors") or {}
        err = next(iter(errs.values()), None) if errs else None
        text = STEP_TEXT.get(step, "Tap Continue.")
        try:
            text = text.format_map({"name": name, "pin": ph.get("pin", ""), "protocol": ph.get("protocol", "a feature")})
        except (KeyError, ValueError):
            pass
        return {**out, "state": "form", "step": step, "needs_pin": "pin" in fields,
                "show_code": ph.get("pin") if step == "pair_no_pin" else None,
                "protocol": ph.get("protocol"), "message": text,
                "error": ERROR_TEXT.get(err, ERROR_TEXT["unknown"]) if err else None}

    async def _after_added(self, out: dict[str, Any]) -> None:
        self._scan = None
        home = self.hub.home
        try:  # pick up the new media player and its remote now, not in five minutes
            await home._refresh_areas()
            await home.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh after adding a TV failed: %s", exc)
        self.hub.bus.publish({"type": "remotes", "data": {"added": out.get("entry_id")}})


def clean_pin(handler: str, pin: str | None) -> str:
    """Android TV shows 6 letters and numbers (hex); Apple TV shows 4 digits."""
    if handler == "androidtv_remote":
        code = re.sub(r"[^0-9A-Fa-f]", "", pin or "").upper()
        if len(code) != 6:
            raise SetupError(400, "bad_params", "Type the 6 letters and numbers shown on the TV")
        return code
    digits = re.sub(r"\D", "", pin or "")
    if not 4 <= len(digits) <= 8:
        raise SetupError(400, "bad_params", "Type the code shown on the TV")
    return digits


class SetupError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
