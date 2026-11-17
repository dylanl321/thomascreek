"""Thomas Creek TV enrolment: install links, approving a TV, its signed requests, tokens and the subject it acts as."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any


from .crypto import verify_signature
from .policy import ALL_CAPABILITIES, Subject
from .tvapp_common import (
    TvAppParts,
    CONFIG_SKEW_S,
    INSTALL_TTL_S,
    KV_INSTALL,
    KV_TVS,
    MAX_TVS,
    PROFILES,
    TV_KEEP,
    TvAppError,
    _CODE_ALPHABET,
    _MEDIA,
    _PERSON_ID,
    _ROOM,
    _UNSET,
    token_message,
)


class EnrolMixin(TvAppParts):
    """Part of TvApp (hub/tvapp.py); uses its attributes (cfg, vault, db, dir, adb, ...)."""

    # -- TVs and their source ------------------------------------------------------------------------------------------
    def tvs(self) -> dict[str, dict[str, Any]]:
        return dict(self.db.kv_get(KV_TVS) or {})

    def _save_tvs(self, tvs: dict[str, dict[str, Any]]) -> None:
        self.db.kv_set(KV_TVS, tvs)


    def _short_code(self) -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))

    def install_links(self) -> dict[str, dict[str, Any]]:
        raw = self.db.kv_get(KV_INSTALL) or {}
        now = time.time()
        live = {k: v for k, v in raw.items() if float(v.get("expires_at") or 0) > now}
        if live != raw:
            self.db.kv_set(KV_INSTALL, live)
        return live

    def create_install_link(self, *, name: str | None, room: str | None, profile: str) -> dict[str, Any]:
        """A short-lived code that lets Downloader or a browser fetch the signed APK on the home network."""
        if profile not in PROFILES:
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        if room:
            self._room(room)
        latest = self.latest()
        if not latest or not self.apk_path(int(latest["versionCode"])).exists():
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet")
        links = self.install_links()
        code = self._short_code()
        while code in links:
            code = self._short_code()
        rec = {
            "code": code, "name": (name or "TV")[:80], "room": room, "profile": profile,
            "created_at": time.time(), "expires_at": time.time() + INSTALL_TTL_S, "mode": "non-adb",
        }
        links[code] = rec
        self.db.kv_set(KV_INSTALL, links)
        boxes = self.boxes()
        boxes[f"code:{code}"] = {
            "added_at": rec["created_at"], "name": rec["name"], "profile": profile, "room": room,
            "auto": False, "setup": True, "mode": "non-adb", "phase": "install",
            "message": "On the TV, open Downloader or a browser and open the address from the phone.",
            "you_do": "Install Thomas Creek TV from the address on the phone",
            "install_code": code, "home": {"wanted": False},
            "capabilities": {"overlay": False, "home": False, "adb": False},
        }
        self._save_boxes(boxes)
        return rec

    def install_link(self, code: str) -> dict[str, Any] | None:
        return self.install_links().get((code or "").strip().upper())


    def approve_code(self, code: str, *, room: str | None = None, profile: str | None = None) -> dict[str, Any]:
        """Approve a TV that is showing this code. No credentials are typed on the TV."""
        want = (code or "").strip().upper()
        if not re.fullmatch(r"[A-Z2-9]{6}", want):
            raise TvAppError(400, "bad_code", "That code isn't one the TV would show")
        found = None
        for install_id, tv in self.tvs().items():
            if tv.get("approve_code") == want and tv.get("status") == "pending":
                found = install_id
                break
        if found is None:
            raise TvAppError(404, "not_found", "No TV is waiting with that code")
        if profile is not None and profile not in PROFILES:
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        if room:
            self._room(room)
        return self.set_tv(found, status="approved", profile=profile, room=room if room else _UNSET)

    async def enroll(self, ip: str, install_id: str, name: str, model: str | None, app_version: str | None,
                     public_key: str, code: str | None = None) -> dict[str, Any]:
        from .crypto import CryptoError, normalize_public_key

        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", install_id or ""):
            raise TvAppError(400, "bad_install_id", "install_id must be 16-64 letters, digits, - or _")
        try:
            pk = normalize_public_key(public_key)
        except CryptoError as exc:
            raise TvAppError(400, "bad_key", str(exc)) from None
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv and tv.get("public_key") != pk:
            raise TvAppError(409, "key_mismatch", "This TV id is already used with another key. Clear the app's data and try again.")
        if tv is None:
            if len(tvs) >= MAX_TVS:
                raise TvAppError(429, "too_many", "Too many TVs are registered. Remove old ones on the hub.")
            tv = {"public_key": pk, "status": "pending", "profile": "adult", "created_at": time.time()}
        tv.update(name=(name or "TV")[:80], model=(model or "")[:80] or None, app_version=(app_version or "")[:40] or None,
                  ip=ip, last_seen_at=time.time())
        link = self.install_link(code) if code else None
        box = self.boxes().get(ip)
        newly_pending = False
        if tv["status"] == "pending" and link is not None:
            # The phone already created this install link, so the TV is approved without anyone typing on it.
            tv.update(status="approved", profile=link.get("profile") or "adult", approved_at=time.time(),
                      approved_by="code", mode="non-adb", room=link.get("room") or tv.get("room"))
            if link.get("name"):
                tv["name"] = link["name"]
            boxes = self.boxes()
            manual = boxes.get(f"code:{link['code']}")
            if manual is not None:
                manual.update(phase="ready", message="This TV is registered and has its source.", you_do=None, ip=ip)
                boxes[f"code:{link['code']}"] = manual
                self._save_boxes(boxes)
        elif tv["status"] == "pending" and box is not None:
            # An admin added this box for the TV app already: that is the approval.
            tv.update(status="approved", profile=box.get("profile") or "adult", approved_at=time.time(), approved_by="box",
                      mode=box.get("mode") or "adb")
            if box.get("room") and not tv.get("room"):
                tv["room"] = box["room"]
        elif tv["status"] == "pending" and install_id not in tvs:
            newly_pending = True
        if tv["status"] == "pending" and not tv.get("approve_code"):
            tv["approve_code"] = self._short_code()
            tv["mode"] = tv.get("mode") or "non-adb"
        tvs[install_id] = tv
        self._save_tvs(tvs)
        self.db.log("tvapp.enroll", "ok", target=install_id, params={"name": tv["name"], "ip": ip, "status": tv["status"]},
                    source="tv")
        if newly_pending and self.notify:
            await self.notify("A TV wants the TV app source",
                              f"{tv['name']} is showing code {tv.get('approve_code')}. Approve it in the app.")
        out = {"status": tv["status"], "profile": tv["profile"], "mode": tv.get("mode") or "adb"}
        if tv["status"] == "pending" and tv.get("approve_code"):
            out["code"] = tv["approve_code"]
            out["approve_url"] = f"thomascreek://tv/{tv['approve_code']}"
        return out

    def set_tv(self, install_id: str, *, status: str | None = None, profile: str | None = None,
               name: str | None = None, room: Any = _UNSET, media_entity: Any = _UNSET) -> dict[str, Any]:
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv is None:
            raise TvAppError(404, "not_found", "No such TV")
        if status is not None:
            if status not in ("approved", "revoked", "pending"):
                raise TvAppError(400, "bad_status", "Status is approved, revoked or pending")
            tv["status"] = status
            if status == "approved":
                tv["approved_at"] = time.time()
                tv["approved_by"] = "admin"
        if profile is not None:
            if profile not in PROFILES:
                raise TvAppError(400, "bad_profile", "Profile is adult or kids")
            tv["profile"] = profile
        if name:
            tv["name"] = name[:80]
        if room is not _UNSET:
            tv["room"] = self._room(room)
        if media_entity is not _UNSET:
            tv["media_entity"] = self._media_entity(media_entity)
        tvs[install_id] = tv
        self._save_tvs(tvs)
        self.bump_config(install_id)
        return dict(tv, id=install_id)

    @staticmethod
    def _room(value: object) -> str | None:
        if value is None or value == "":
            return None
        text = str(value)
        if not _ROOM.fullmatch(text):
            raise TvAppError(400, "bad_room", "room is a house area id, such as living_room")
        return text

    @staticmethod
    def _media_entity(value: object) -> str | None:
        if value is None or value == "":
            return None
        text = str(value)
        if not _MEDIA.fullmatch(text):
            raise TvAppError(400, "bad_target", "media_entity is a media_player id")
        return text

    def remove_tv(self, install_id: str) -> None:
        tvs = self.tvs()
        if tvs.pop(install_id, None) is None:
            raise TvAppError(404, "not_found", "No such TV")
        self._save_tvs(tvs)

    def _verified(self, install_id: str, ts: int, signature: str, message: bytes, now: float, *,
                  log_action: str) -> dict[str, Any]:
        tv = self.tvs().get(install_id or "")
        if tv is None:
            raise TvAppError(404, "unknown_tv", "This TV isn't registered. Open the app at home to register it.")
        if abs(now - int(ts)) > CONFIG_SKEW_S:
            raise TvAppError(400, "clock", "The TV's clock is off by more than five minutes")
        if not verify_signature(tv["public_key"], message, signature or ""):
            self.db.log(log_action, "denied", target=install_id, detail="bad signature", source="tv")
            raise TvAppError(401, "bad_signature", "Signature didn't match this TV")
        if tv.get("status") == "revoked":
            raise TvAppError(403, "revoked", "This TV was removed on the hub")
        return tv


    # -- token, lineup, state, commands --------------------------------------------------------------------------------
    @staticmethod
    def key_fingerprint(tv: dict[str, Any]) -> str:
        return hashlib.sha256(str(tv.get("public_key") or "").encode()).hexdigest()[:16]

    def begin_token(self, install_id: str, ts: int, signature: str, person_id: str | None,
                    ip: str | None, now: float | None = None) -> tuple[Subject, dict[str, Any], str]:
        """Check the device-key signature and return (subject, device, key fingerprint).

        The same timestamp cannot be used twice, so a captured sign-in cannot mint
        another bearer. The caller still has to put the fingerprint in the token
        and re-check it on every later request.
        """
        now = time.time() if now is None else now
        person_id = person_id or None
        if person_id is not None and not _PERSON_ID.fullmatch(person_id):
            raise TvAppError(400, "bad_request", "person_id is not a person id")
        tv = self._verified(install_id, ts, signature, token_message(install_id, ts, person_id), now,
                            log_action="tvapp.token")
        if tv.get("status") != "approved":
            raise TvAppError(403, "pending", "This TV isn't approved yet")
        if int(ts) <= int(tv.get("last_token_ts") or 0):
            raise TvAppError(401, "replay", "This sign-in was already used. Ask again.")
        subject = self.subject_for(tv, install_id, person_id)
        device = self._device(tv, install_id)
        fingerprint = self.key_fingerprint(tv)
        tvs = self.tvs()
        tvs[install_id].update(last_token_ts=int(ts), last_seen_at=now, ip=ip or tv.get("ip"))
        self._save_tvs(tvs)
        self.db.log("tvapp.token", "ok", person_id=subject.person_id, device_id=device["id"], target=install_id,
                    params={"role": subject.role, "room": subject.areas[:1] or None}, source="tv")
        return subject, device, fingerprint

    def session(self, install_id: str, person_id: str | None, key_fp: str | None) -> tuple[Subject, dict[str, Any]]:
        """Rebuild the TV's subject from the current record. The token's role is not trusted."""
        tv = self.tvs().get(install_id or "")
        if tv is None:
            raise TvAppError(401, "unknown_tv", "This TV isn't registered. Open the app at home to register it.")
        if tv.get("status") == "revoked" or (key_fp and key_fp != self.key_fingerprint(tv)):
            raise TvAppError(401, "revoked", "This TV was removed on the hub")
        if tv.get("status") != "approved":
            raise TvAppError(403, "pending", "This TV isn't approved yet")
        if person_id is not None and not isinstance(person_id, str):
            person_id = str(person_id)
        subject = self.subject_for(tv, install_id, person_id or None)
        device = self._device(tv, install_id)
        tvs = self.tvs()
        if install_id in tvs:
            tvs[install_id]["last_seen_at"] = time.time()
            self._save_tvs(tvs)
        return subject, device

    def subject_for(self, tv: dict[str, Any], install_id: str, person_id: str | None) -> Subject:
        """The TV's rights: its profile and room, optionally narrowed to one person.

        A kids' profile is always a child. An adult profile starts as room-scoped
        everyday control (the tablet bundle, with whole-house caps removed).
        Naming a person can only narrow that. Grants on the person are not copied.
        Denies are. Doors, cameras and every whole-house capability are denied.
        """
        person = self._person(person_id) if person_id else None
        profile = tv.get("profile") or "adult"
        role = self._tv_role(profile, person)
        areas, shared = self._tv_areas(person, tv.get("room"), tv.get("media_entity"))
        denies = list(person.get("denies") or []) if person else []
        denies += [{"capability": cap} for cap in ALL_CAPABILITIES if cap not in TV_KEEP]
        return Subject(
            person_id=person["id"] if person else None,
            role=role,
            areas=areas,
            shared_areas=shared,
            grants=[{"capability": cap} for cap in ("lights.own_room", "climate.own_room", "media.own_tv")],
            denies=denies,
            tv_device=tv.get("media_entity"),
            at_home=True,
            device_id=f"tv:{install_id}",
            name=tv.get("name") or "TV",
            tv=True,
            tv_id=install_id,
        )

    def _person(self, person_id: str) -> dict[str, Any]:
        person = self.db.person(person_id)
        if person is None:
            raise TvAppError(404, "not_found", "No such person")
        if person.get("expires_at") and person["expires_at"] < time.time():
            raise TvAppError(403, "forbidden", "That person's access has ended")
        return person

    @staticmethod
    def _tv_role(profile: str, person: dict[str, Any] | None) -> str:
        if profile == "kids":
            if person is not None and person.get("role") != "child":
                raise TvAppError(403, "forbidden", "A kids' TV can't act as a grown-up")
            return "child"
        if person is None:
            return "tablet"
        role = person.get("role")
        if role == "child":
            return "child"
        if role == "guest":
            return "guest"
        if role in ("adult", "admin", "tablet"):
            # An admin or adult is narrowed to room-scoped everyday control.
            return "tablet"
        raise TvAppError(403, "forbidden", "That person can't use a TV")

    @staticmethod
    def _tv_areas(person: dict[str, Any] | None, room: str | None,
                  media_entity: str | None) -> tuple[list[str], list[str]]:
        if person is None:
            return ([room] if room else []), []
        role = person.get("role")
        if role in ("admin", "adult", "tablet"):
            return ([room] if room else []), []
        own = set(person.get("areas") or [])
        shared = set(person.get("shared_areas") or [])
        if room and room in own:
            return [room], []
        if room and room in shared:
            return [], [room]
        # No overlap with this room. Their own player still matches tv_device
        # when this TV is that player; otherwise the token can look but not act.
        if person.get("tv_device") and person.get("tv_device") == media_entity:
            return ([room] if room and room in own else []), []
        return [], []

    @staticmethod
    def _device(tv: dict[str, Any], install_id: str) -> dict[str, Any]:
        return {
            "id": f"tv:{install_id}",
            "name": tv.get("name") or "TV",
            "status": "active",
            "kind": "tv",
            "install_id": install_id,
        }
