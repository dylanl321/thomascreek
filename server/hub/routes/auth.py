"""Enrollment, sign-in and "who am I"."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..crypto import CryptoError, normalize_public_key, verify_signature
from ..db import new_id
from ..home import iso
from ..state import Hub
from .deps import Caller, caller, caller_ip, consume_nonce, error, hub, issue_nonce

log = logging.getLogger("hub.auth")
router = APIRouter(prefix="/v1", tags=["auth"])

MAX_PENDING = 10


class EnrollBody(BaseModel):
    device_name: str = Field(max_length=80)
    model: str | None = Field(default=None, max_length=80)
    platform: str | None = Field(default=None, max_length=40)
    app_version: str | None = Field(default=None, max_length=40)
    public_key: str = Field(max_length=200)
    secure_public_key: str | None = Field(default=None, max_length=200)
    apns_token: str | None = Field(default=None, max_length=200)
    apns_env: str | None = Field(default=None, pattern="^(sandbox|production)$")


@router.post("/enroll")
async def enroll(body: EnrollBody, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """First open on home Wi-Fi: register this phone. Nothing to scan or type.

    Being on the home network is the proof of belonging. A new phone is
    `pending` (the child's limits, no room) until Dylan says whose it is.
    """
    ip = caller_ip(request, h)
    if not h.is_home(ip):
        raise error(403, "not_home", "Open the app at home on Wi-Fi to set it up")
    try:
        pk = normalize_public_key(body.public_key)
        spk = normalize_public_key(body.secure_public_key) if body.secure_public_key else None
    except CryptoError as exc:
        raise error(400, "bad_key", str(exc)) from None

    existing = h.db.one("SELECT * FROM devices WHERE public_key=?", [pk])
    if existing and existing["status"] != "revoked":
        updates: dict[str, Any] = {"app_version": body.app_version, "last_seen_at": time.time()}
        if spk and not existing.get("secure_public_key"):
            updates["secure_public_key"] = spk
        if body.apns_token:
            updates.update(apns_token=body.apns_token, apns_env=body.apns_env or "production")
        h.db.update("devices", "id", existing["id"], updates)
        return {"device_id": existing["id"], "status": existing["status"]}
    if existing:
        raise error(403, "revoked", "This device was removed. Ask Dylan to let it back in.")

    pending = h.db.one("SELECT COUNT(*) AS n FROM devices WHERE status='pending'")["n"]  # type: ignore[index]
    if pending >= MAX_PENDING:
        raise error(429, "too_many_pending", "Too many devices are waiting to be approved")

    did = new_id("dev")
    h.db.insert("devices", {
        "id": did, "name": body.device_name, "model": body.model, "platform": body.platform,
        "app_version": body.app_version, "public_key": pk, "secure_public_key": spk, "status": "pending",
        "apns_token": body.apns_token, "apns_env": body.apns_env or "production", "enrolled_ip": ip,
        "created_at": time.time(), "last_seen_at": time.time(),
    })
    h.db.log("device.enroll", "ok", device_id=did, target=did, params={"name": body.device_name, "model": body.model}, source="app")
    await h.notify.send(
        roles=("admin",), level="decision", cat="devices",
        title=f"New {body.model or 'device'} joined", body=f"“{body.device_name}” is on home Wi-Fi. Whose is it?",
        actions=[{"id": "assign", "title": "Choose person"}, {"id": "revoke", "title": "Remove", "destructive": True}],
        ref=f"thomascreek://people/devices/{did}", group_key=f"device:{did}", push_category="NEW_DEVICE",
    )
    return {"device_id": did, "status": "pending"}


class ChallengeBody(BaseModel):
    device_id: str


@router.post("/auth/challenge")
async def challenge(body: ChallengeBody, h: Hub = Depends(hub)) -> dict[str, Any]:
    device = h.db.device(body.device_id)
    if device is None or device["status"] == "revoked":
        # Same answer shape either way, so ids can't be probed.
        raise error(401, "unknown_device", "Set the app up again at home")
    return {"nonce": issue_nonce(h, device["id"]), "expires_in": 120}


class TokenBody(BaseModel):
    device_id: str
    nonce: str
    signature: str


@router.post("/auth/token")
async def token(body: TokenBody, h: Hub = Depends(hub)) -> dict[str, Any]:
    device = h.db.device(body.device_id)
    if device is None or device["status"] == "revoked":
        raise error(401, "unknown_device", "Set the app up again at home")
    if not consume_nonce(h, device["id"], body.nonce):
        raise error(401, "bad_nonce", "Challenge expired, ask for a new one")
    if not verify_signature(device["public_key"], body.nonce.encode(), body.signature):
        h.db.log("auth.token", "denied", device_id=device["id"], detail="bad signature", source="app")
        raise error(401, "bad_signature", "Signature didn't match this device")
    s = h.subject_for(device, at_home=False)
    access, exp = h.tokens.issue(device["id"], s.person_id, s.role)
    h.db.update("devices", "id", device["id"], {"last_seen_at": time.time()})
    person = h.db.person(s.person_id)
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_at": iso(exp),
        "person": _person_out(person),
        "role": s.role,
    }


def _person_out(p: dict[str, Any] | None) -> dict[str, Any] | None:
    if p is None:
        return None
    out = {"id": p["id"], "name": p["name"], "role": p["role"], "avatar": p.get("avatar"),
           "ui_style": p.get("ui_style") or ("kid" if p["role"] == "child" else "standard")}
    if p["role"] == "guest":
        out["access_until"] = iso(p.get("expires_at"))
        out["area"] = (p.get("areas") or [None])[0]
    return out


@router.get("/me")
async def me(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    s = c.subject
    person = h.db.person(s.person_id)
    out: dict[str, Any] = {
        "person": _person_out(person),
        "role": s.role,
        "capabilities": h.policy.capabilities_for(s),
        "device": {"id": c.device["id"], "name": c.device["name"], "status": c.device["status"]},
        "at_home": s.at_home,
        "dry_run": h.config.hub.dry_run,
    }
    if h.config.hub.remote_url:
        out["remote_url"] = h.config.hub.remote_url
    if s.role == "guest":
        g = h.config.guest
        pw = h.vault.get(g.wifi_password_secret)
        out["guest"] = {"wifi_ssid": g.wifi_ssid, "wifi_password": str(pw) if pw else None,
                        "guidance": g.guidance, "host_name": g.host_name}
    if s.role == "child" and person:
        internet: dict[str, Any] = {"paused": False}
        if h.firewalla.enabled and h.firewalla.rules_for(person["id"]):
            try:
                st = await h.firewalla.internet_state(person["id"])
                internet["paused"] = st["paused"]
            except Exception:  # noqa: BLE001
                internet["unknown"] = True
        restore = next((t for t in h.timers.pending("network.restore") if t["payload"].get("person_id") == person["id"]), None)
        if restore:
            internet["until"] = iso(restore["due_at"])
        out["child"] = {"room_id": (person.get("areas") or [None])[0], "tv_device_id": person.get("tv_device"),
                        "internet": internet}
    return out


def b64(data: bytes) -> str:  # small helper for tests
    return base64.b64encode(data).decode()
