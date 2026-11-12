"""Kids' internet through Firewalla MSP, managed directly by the hub."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..home import iso
from ..state import Hub
from .deps import Caller, caller, error, hub, need, require_confirm

router = APIRouter(prefix="/v1/network", tags=["network"])


def _fw(h: Hub):
    if not h.firewalla.enabled:
        raise error(503, "not_set_up", "Firewalla isn't connected to the hub yet")
    return h.firewalla


def _linked(h: Hub, person_id: str) -> list[dict[str, Any]]:
    return h.db.all("SELECT mac, device_name, rule_id FROM network_rules WHERE person_id=? ORDER BY created_at", [person_id])


@router.get("/kids")
async def kids(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "network.manage")
    fw = h.firewalla
    status: dict[str, str] = {}
    if fw.enabled:
        try:
            status = {str(r.get("id")): r.get("status") for r in await fw.rules()}
        except AdapterError:
            status = {}
    restores = {t["payload"].get("person_id"): t for t in h.timers.pending("network.restore")}
    out = []
    for p in h.db.people_with_role("child"):
        ids = fw.rules_for(p["id"])
        devices = [{"mac": d["mac"], "name": d["device_name"], "rule_id": d["rule_id"],
                    "paused": status.get(d["rule_id"]) not in (None, "paused")} for d in _linked(h, p["id"])]
        t = restores.get(p["id"])
        out.append({
            "id": p["id"], "name": p["name"], "managed": bool(ids),
            "paused": any(status.get(i) not in (None, "paused") for i in ids) if status else None,
            "until": iso(t["due_at"]) if t else None, "devices": devices,
        })
    return {"kids": out}


@router.get("/devices")
async def devices(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Admin: every device Firewalla sees, to pick a child's tablet and phone."""
    need(h, c, "people")
    try:
        rows = await _fw(h).devices()
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    owner = {r["mac"]: r["person_id"] for r in h.db.all("SELECT mac, person_id FROM network_rules")}
    for r in rows:
        r["linked_to"] = owner.get(r["mac"])
    return {"devices": rows}


class LinkBody(BaseModel):
    macs: list[str] = Field(min_length=1, max_length=10)


def _norm_mac(mac: str) -> str:
    m = mac.strip().upper().replace("-", ":")
    parts = m.split(":")
    if len(parts) != 6 or not all(len(x) == 2 and all(ch in "0123456789ABCDEF" for ch in x) for x in parts):
        raise error(400, "bad_mac", f"{mac} isn't a MAC address")
    return m


@router.post("/kids/{person_id}/devices")
async def link_devices(person_id: str, body: LinkBody, request: Request, c: Caller = Depends(caller),
                       h: Hub = Depends(hub)) -> dict[str, Any]:
    """Admin: give a child's devices a hub-managed "block internet" rule each.

    Each rule is created, then paused at once, so linking leaves the child's
    internet on until someone pauses it.
    """
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    person = h.db.person(person_id)
    if person is None or person["role"] != "child":
        raise error(404, "not_found", "Internet time is only managed for children")
    macs = [_norm_mac(m) for m in body.macs]
    if not h.actions.is_live("network.setup"):
        h.db.log("network.setup", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"],
                 target=person_id, params={"macs": macs})
        return {"ok": True, "dry_run": True, "message": "Practice mode: no Firewalla rules were created"}
    fw = _fw(h)
    try:
        known = {d["mac"]: d for d in await fw.devices()}
        gid = None
        created = []
        for mac in macs:
            if h.db.one("SELECT rule_id FROM network_rules WHERE mac=?", [mac]):
                continue  # already linked (to this or another child)
            dev = known.get(mac)
            if dev is None:
                raise error(404, "unknown_device", f"Firewalla doesn't know {mac}")
            gid = dev.get("gid") or gid
            if not gid:
                boxes = await fw.boxes()
                gid = boxes[0].get("gid") if boxes else None
            if not gid:
                raise error(409, "no_box", "Couldn't find the Firewalla box")
            rule_id = await fw.create_block_rule(mac, gid, f"Thomas Creek hub: {person['name']} internet ({dev['name']}). Paused = internet on.")
            await fw.pause_rule(rule_id)
            h.db.insert("network_rules", {"rule_id": rule_id, "person_id": person_id, "mac": mac,
                                          "device_name": dev["name"], "gid": gid, "created_at": time.time()})
            created.append({"mac": mac, "name": dev["name"], "rule_id": rule_id})
    except AdapterError as exc:
        h.db.log("network.setup", "error", person_id=c.subject.person_id, device_id=c.device["id"],
                 target=person_id, detail=str(exc)[:300])
        raise error(502, "failed", str(exc)) from None
    h.db.log("network.setup", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=person_id,
             params={"created": [d["rule_id"] for d in created]})
    return {"ok": True, "created": created, "devices": _linked(h, person_id)}


@router.delete("/kids/{person_id}/devices/{mac}")
async def unlink_device(person_id: str, mac: str, request: Request, c: Caller = Depends(caller),
                        h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    row = h.db.one("SELECT * FROM network_rules WHERE person_id=? AND mac=?", [person_id, _norm_mac(mac)])
    if row is None:
        raise error(404, "not_found", "That device isn't linked")
    if h.actions.is_live("network.setup"):
        try:
            await _fw(h).pause_rule(row["rule_id"])  # leave internet on
        except AdapterError as exc:
            raise error(502, "failed", str(exc)) from None
    h.db.execute("DELETE FROM network_rules WHERE rule_id=?", [row["rule_id"]])
    h.db.log("network.unlink", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=person_id,
             params={"rule_id": row["rule_id"]})
    return {"ok": True, "message": "Unlinked. The paused rule can be deleted in the Firewalla app."}
