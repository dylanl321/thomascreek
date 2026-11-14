"""A TV on the home network asks to join. A parent taps once.

The install id is the stable identity. A TV that was already approved, and comes
back wiped with a new key, keeps its room, profile and name. A new id waits
fifteen minutes. The same address cannot ask more than eight times in ten minutes.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .crypto import CryptoError, normalize_public_key
from .db import new_id
from .home import iso
from .tvapp import TvAppError

KV = "tv.pair"
TTL_S = 15 * 60
RATE_N = 8
RATE_WINDOW_S = 600
_INSTALL = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class TvPair:
    def __init__(self, hub: Any):
        self.hub = hub
        self._hits: dict[str, list[float]] = {}

    def request(self, ip: str | None, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        self._limit(ip or "", now)
        install_id = str(body.get("install_id") or "")
        if not _INSTALL.fullmatch(install_id):
            raise TvAppError(400, "bad_install_id", "install_id must be 16-64 letters, digits, - or _")
        try:
            public_key = normalize_public_key(str(body.get("public_key") or ""))
        except CryptoError as exc:
            raise TvAppError(400, "bad_key", "The TV key wasn't readable") from exc
        name = str(body.get("name") or "TV").strip()[:80] or "TV"
        model = str(body.get("model") or "").strip()[:80] or None
        version = str(body.get("app_version") or "").strip()[:40] or None
        room, room_name = _room(self.hub, body.get("suggested_room"))
        tvs = self.hub.tv_app.tvs()
        existing = tvs.get(install_id)
        if existing and existing.get("status") == "approved":
            existing.update(public_key=public_key, name=existing.get("name") or name, model=model or existing.get("model"),
                            app_version=version or existing.get("app_version"), last_seen_at=now, ip=ip)
            tvs[install_id] = existing
            self.hub.tv_app._save_tvs(tvs)
            return self._approved(install_id)
        if existing is None and len(tvs) >= 40:
            raise TvAppError(429, "too_many", "Too many TVs are registered. Remove old ones on the hub.")
        record = dict(existing or {"status": "pending", "profile": "adult", "created_at": now})
        record.update(public_key=public_key, name=name, model=model, app_version=version, last_seen_at=now, ip=ip,
                      status="pending")
        if room and not record.get("room"):
            record["room"] = room
        tvs[install_id] = record
        self.hub.tv_app._save_tvs(tvs)
        pending = self._live(now)
        current = next((row for row in pending if row.get("install_id") == install_id), None)
        fresh = current is None
        if current is None:
            current = {"pair_id": new_id("pair"), "install_id": install_id, "notified": False}
        current.update(name=name, model=model, suggested_room=room, suggested_room_name=room_name,
                       expires_at=now + TTL_S)
        pending = [row for row in pending if row.get("install_id") != install_id]
        pending.append(current)
        self._save(pending)
        if fresh or not current.get("notified"):
            current["notified"] = True
            self._save(pending)
            self._tell(current)
        return _pending_body(current)

    def poll(self, pair_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self._one(pair_id, now)
        if row is None:
            return {"status": "expired"}
        if row.get("status") == "denied":
            return {"status": "denied"}
        if row.get("status") == "approved":
            return self._approved(str(row["install_id"]))
        return _pending_body(row)

    def pending(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        rows = []
        for row in self._live(now):
            if row.get("status") in ("approved", "denied"):
                continue
            rows.append({
                "pair_id": row["pair_id"], "install_id": row["install_id"], "name": row.get("name"),
                "model": row.get("model"), "suggested_room": row.get("suggested_room"),
                "suggested_room_name": row.get("suggested_room_name"), "expires_at": iso(row.get("expires_at")),
            })
        return {"pending": rows}

    def approve(self, pair_id: str, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self._one(pair_id, now)
        if row is None or row.get("status") in ("denied",):
            return {"ok": False, "status": "expired"}
        if row.get("status") == "approved":
            return {"ok": True, "status": "approved"}
        profile = body.get("profile") or "adult"
        if profile not in ("adult", "kids"):
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        room_id, _room_name = _room(self.hub, str(body.get("room") or row.get("suggested_room") or ""))
        name = str(body.get("name") or row.get("name") or "TV").strip()[:80]
        try:
            self.hub.tv_app.set_tv(str(row["install_id"]), status="approved", profile=profile, name=name,
                                   **({"room": room_id} if room_id else {}))
        except TvAppError:
            return {"ok": False, "status": "expired"}
        row["status"] = "approved"
        self._replace(row, now)
        self.hub.bus.publish({"type": "tv_pair", "data": {**_public(row), "status": "approved"}})
        return {"ok": True, "status": "approved"}

    def deny(self, pair_id: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self._one(pair_id, now)
        if row is None:
            return {"ok": False, "status": "expired"}
        row["status"] = "denied"
        self._replace(row, now)
        install_id = str(row["install_id"])
        tv = self.hub.tv_app.tvs().get(install_id)
        if tv and tv.get("status") == "pending":
            self.hub.tv_app.remove_tv(install_id)
        self.hub.bus.publish({"type": "tv_pair", "data": {**_public(row), "status": "denied"}})
        return {"ok": True, "status": "denied"}

    def _approved(self, install_id: str) -> dict[str, Any]:
        tv = self.hub.tv_app.tvs().get(install_id) or {}
        subject = self.hub.tv_app.subject_for(tv, install_id, None)
        device = self.hub.tv_app._device(tv, install_id)
        fingerprint = self.hub.tv_app.key_fingerprint(tv)
        access, exp = self.hub.tokens.issue(device["id"], subject.person_id, subject.role, extra={
            "kind": "tv", "tv": install_id, "key": fingerprint,
        })
        return {
            "status": "approved",
            "access_token": access,
            "token_type": "bearer",
            "expires_at": iso(exp),
            "expires_in": max(0, int(exp - time.time())),
            "role": subject.role,
            "profile": tv.get("profile") or "adult",
            "room": tv.get("room"),
            "capabilities": self.hub.policy.capabilities_for(subject),
            "tv": {"id": install_id, "name": tv.get("name") or "TV"},
            "config": self.hub.tv_app.approved_config(install_id),
        }

    def _tell(self, row: dict[str, Any]) -> None:
        data = _public(row)
        data["status"] = "pending"
        self.hub.bus.publish({"type": "tv_pair", "data": data})
        notify = getattr(self.hub, "notify", None)
        if notify is None:
            return
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(notify.send(
                title=f"{row.get('name') or 'A TV'} wants to join",
                body="Open Thomas Creek to allow it.",
                cat="systems", level="decision", roles=("admin",),
                ref="thomascreek://tv", group_key=f"tv-pair:{row['pair_id']}",
            ))
        except RuntimeError:
            pass

    def _limit(self, ip: str, now: float) -> None:
        hits = [stamp for stamp in self._hits.get(ip, []) if now - stamp < RATE_WINDOW_S]
        if len(hits) >= RATE_N:
            raise TvAppError(429, "rate_limited", "Wait a moment and try again.")
        hits.append(now)
        self._hits[ip] = hits

    def _live(self, now: float) -> list[dict[str, Any]]:
        raw = self.hub.db.kv_get(KV) or []
        if not isinstance(raw, list):
            return []
        kept = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            if float(row.get("expires_at") or 0) <= now and row.get("status") not in ("approved", "denied"):
                continue
            kept.append(row)
        return kept

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.hub.db.kv_set(KV, rows[-40:])

    def _one(self, pair_id: str, now: float) -> dict[str, Any] | None:
        for row in self._live(now):
            if row.get("pair_id") == pair_id:
                return row
        return None

    def _replace(self, row: dict[str, Any], now: float) -> None:
        rows = [item for item in self._live(now) if item.get("pair_id") != row.get("pair_id")]
        rows.append(row)
        self._save(rows)


def _pending_body(row: dict[str, Any]) -> dict[str, Any]:
    return {"status": "pending", "pair_id": row["pair_id"], "expires_at": iso(row.get("expires_at")), "poll_after_s": 2}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair_id": row.get("pair_id"), "install_id": row.get("install_id"), "name": row.get("name"),
        "model": row.get("model"), "suggested_room": row.get("suggested_room"),
    }


def _room(hub: Any, value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    areas = hub.home.areas or {}
    if text in areas:
        return text, areas[text].get("name")
    folded = text.casefold()
    for area_id, area in areas.items():
        name = str(area.get("name") or "")
        if area_id.casefold() == folded or name.casefold() == folded or area_id.replace("_", " ") == folded:
            return area_id, name or None
    if re.fullmatch(r"[a-z0-9_]{1,40}", text):
        return text, None
    return None, None
