"""Family life around the house: who's home, Today, arriving and leaving,
kids' bedtime, guest passes and "Tell Dylan" feedback.

Presence comes from, in order: the person's `presence_entity`, a Home Assistant
`person.<first name>` entity, or the last arrive/leave the person's own phone
reported (a geofence round the house). The hub, not the phone, decides whether
someone was the last one out, so a phone without location still gets the check
when Home Assistant sees everyone leave.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .db import new_id
from .home import iso, kind_for
from .kitchen import oven_state
from .smart import _bedtime, clock, hm

if TYPE_CHECKING:
    from .policy import Subject
    from .state import Hub

log = logging.getLogger("hub.household")

HOME_STATES = ("home",)
APP_PRESENCE_TTL_S = 18 * 3600  # a phone's own report counts this long without a newer one
INTERCOM_PHRASES = [
    {"id": "dinner", "emoji": "🍽️", "text": "Dinner's ready!"},
    {"id": "downstairs", "emoji": "👋", "text": "Can everyone come downstairs, please?"},
    {"id": "leaving", "emoji": "👟", "text": "Shoes on, we're leaving in five minutes."},
    {"id": "bedtime", "emoji": "🌙", "text": "Time to get ready for bed."},
    {"id": "teeth", "emoji": "🪥", "text": "Teeth brushed and pajamas on, please."},
    {"id": "tidy", "emoji": "🧺", "text": "Ten minute tidy, starting now!"},
]


def _first(name: str | None) -> str:
    return str(name or "").strip().split(" ")[0]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Household:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._task: asyncio.Task | None = None
        self._last_home: dict[str, bool | None] = {}

    # -- presence ----------------------------------------------------------
    def presence(self, p: dict[str, Any]) -> dict[str, Any]:
        """{home: True/False/None, since, source}. None means the hub can't tell."""
        states = self.hub.home.states
        eid = p.get("presence_entity")
        if not eid:
            guess = f"person.{_first(p['name']).lower()}"
            eid = guess if guess in states else None
        if eid and eid in states:
            st = states[eid]
            s = st.get("state")
            if s not in ("unknown", "unavailable"):
                where = None if s in HOME_STATES or s == "not_home" else str(s).replace("_", " ").capitalize()
                return {"home": s in HOME_STATES, "since": st.get("last_changed"), "source": "home_assistant",
                        "where": where}
        rep = self.hub.db.kv_get(f"presence.{p['id']}")
        if rep and time.time() - rep.get("at", 0) < APP_PRESENCE_TTL_S:
            return {"home": bool(rep["home"]), "since": iso(rep["at"]), "source": "phone", "where": None}
        return {"home": None, "since": None, "source": None, "where": None}

    def family(self, s: "Subject") -> list[dict[str, Any]]:
        """Everyone (but tablets and guests) with whether they're home, for the who's-home row."""
        out = []
        for p in self.hub.db.people():
            if p["role"] in ("tablet", "pending", "guest"):
                continue
            pr = self.presence(p)
            out.append({"id": p["id"], "name": p["name"], "first": _first(p["name"]), "role": p["role"],
                        "avatar": p.get("avatar"), "ui_style": p.get("ui_style"), "home": pr["home"],
                        "since": pr["since"], "where": pr["where"] if s.role in ("admin", "adult") else None,
                        "me": p["id"] == s.person_id})
        order = {"admin": 0, "adult": 1, "child": 2}
        out.sort(key=lambda r: (order.get(r["role"], 3), r["name"]))
        return out

    def anyone_else_home(self, person_id: str | None) -> list[str]:
        names = []
        for p in self.hub.db.people():
            if p["id"] == person_id or p["role"] in ("tablet", "pending", "guest"):
                continue
            if self.presence(p)["home"]:
                names.append(_first(p["name"]))
        return names

    # -- leaving: what's still open ----------------------------------------
    def open_things(self) -> list[dict[str, Any]]:
        """Doors, garages, ovens and lights still on, each with the action that fixes it."""
        out: list[dict[str, Any]] = []
        lights = 0
        for eid, st in sorted(self.hub.home.states.items()):
            k = kind_for(st)
            s = st.get("state")
            name = (st.get("attributes") or {}).get("friendly_name") or eid
            if k == "lock" and s in ("unlocked", "open", "jammed"):
                out.append({"id": eid, "kind": "lock", "title": f"{name} is unlocked", "fix": "Lock",
                            "action": "lock.set", "target": eid, "params": {"locked": True}})
            elif k == "garage" and s in ("open", "opening"):
                out.append({"id": eid, "kind": "garage", "title": f"{name} is open", "fix": "Close",
                            "action": "garage.set", "target": eid, "params": {"open": False}, "secure": True})
            elif k == "oven" and oven_state(st)["on"]:
                out.append({"id": eid, "kind": "oven", "title": f"{name} is on", "fix": "Turn off",
                            "action": "oven.set", "target": eid, "params": {"on": False}})
            elif k == "light" and s == "on" and eid in self.hub.home.entity_area:
                lights += 1
        if lights:
            out.append({"id": "lights", "kind": "lights", "title": f"{lights} light{'s' if lights != 1 else ''} on",
                        "fix": "All off", "action": "steps.run", "target": None,
                        "params": {"label": "Lights off", "steps": self._lights_off_steps()}})
        return out

    def _lights_off_steps(self) -> list[dict[str, Any]]:
        return [{"action": "light.set", "target": eid, "params": {"on": False}}
                for eid, st in self.hub.home.states.items()
                if kind_for(st) == "light" and st.get("state") == "on" and eid in self.hub.home.entity_area]

    def departure(self, s: "Subject") -> dict[str, Any]:
        others = self.anyone_else_home(s.person_id)
        items = self.open_things() if s.role in ("admin", "adult") else []
        return {"last_one_out": not others, "others_home": others, "open": items,
                "all_clear": not items, "line": self._departure_line(others, items)}

    @staticmethod
    def _departure_line(others: list[str], items: list[dict[str, Any]]) -> str:
        if others:
            return f"{', '.join(others)} {'is' if len(others) == 1 else 'are'} still home."
        if not items:
            return "Everything's locked up. Have a good trip."
        return "You're the last one out. " + "; ".join(i["title"] for i in items[:3]) + "."

    # -- a phone's own arrive / leave --------------------------------------
    async def report(self, s: "Subject", event: str) -> dict[str, Any]:
        if not s.person_id:
            return {"ok": False}
        home = event == "arrive"
        self.hub.db.kv_set(f"presence.{s.person_id}", {"home": home, "at": time.time(), "device": s.device_id})
        self.hub.bus.publish({"type": "presence", "person_id": s.person_id, "home": home})
        if home:
            return {"ok": True, **(await self.arrived(s))}
        return {"ok": True, **(await self.left(s.person_id, s))}

    async def left(self, person_id: str, s: "Subject | None" = None) -> dict[str, Any]:
        p = self.hub.db.person(person_id)
        if p is None or p["role"] not in ("admin", "adult"):
            return {}
        others = self.anyone_else_home(person_id)
        items = self.open_things()
        if others or not items:
            return {"departure": {"last_one_out": not others, "others_home": others, "open": [], "all_clear": True}}
        key = f"lastout.{person_id}"
        if time.time() - (self.hub.db.kv_get(key) or 0) < 600:  # one nudge per departure
            return {"departure": {"last_one_out": True, "others_home": [], "open": items, "all_clear": False}}
        self.hub.db.kv_set(key, time.time())
        body = "; ".join(i["title"] for i in items[:3])
        await self.hub.notify.send(person_ids=[person_id], level="act_now", cat="house",
                                   title="You're the last one out", body=body, ref="thomascreek://leaving",
                                   push_category="LAST_OUT", interruption="time-sensitive")
        return {"departure": {"last_one_out": True, "others_home": [], "open": items, "all_clear": False}}

    async def arrived(self, s: "Subject") -> dict[str, Any]:
        """Run the person's welcome mode (everyday steps only: never unlocks a door)."""
        mode_id = self.welcome_mode(s.person_id)
        if not mode_id:
            return {"welcome": None}
        mode = next((m for m in self.hub.config.modes if m.id == mode_id), None)
        if mode is None:
            return {"welcome": None}
        from .actions import ActionError

        steps = [st for st in mode.steps if st.get("action") in ("light.set", "switch.set", "fan.set", "scene.run",
                                                                   "climate.set", "media.command")]
        if not steps:
            return {"welcome": None}
        try:
            plan = self.hub.actions.plan(s, "steps.run", None, {"label": f"Welcome home ({mode.name})", "steps": steps})
            if not plan.decision.allowed or plan.decision.tier not in ("look", "everyday"):
                return {"welcome": None}
            res = await self.hub.actions.run(s, plan, source="arrival")
            return {"welcome": {"mode": mode.id, "name": mode.name, "message": res["message"]}}
        except ActionError as exc:
            log.info("welcome mode failed: %s", exc)
            return {"welcome": None}

    def welcome_mode(self, person_id: str | None) -> str | None:
        return (self.hub.db.kv_get(f"welcome.{person_id}") or {}).get("mode") if person_id else None

    def set_welcome_mode(self, person_id: str, mode: str | None) -> None:
        self.hub.db.kv_set(f"welcome.{person_id}", {"mode": mode})

    # Home Assistant's own presence: notice the last one out even without the app's geofence.
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("presence tick failed: %s", exc)
            await asyncio.sleep(30)

    async def tick(self) -> list[str]:
        """Who just left according to Home Assistant; nudges the last of them if things are open."""
        left = []
        for p in self.hub.db.people_with_role("admin", "adult"):
            pr = self.presence(p)
            if pr["source"] != "home_assistant":
                continue
            was = self._last_home.get(p["id"])
            self._last_home[p["id"]] = pr["home"]
            if was is True and pr["home"] is False:
                left.append(p["id"])
        for pid in left:
            await self.left(pid)
        return left

    # -- kids' bedtime ------------------------------------------------------
    def bedtime(self, p: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
        if p["role"] != "child":
            return None
        cfg = self.hub.config.smart
        own = self.hub.db.kv_get(f"bedtime.{p['id']}") or {}
        if own.get("enabled") is False:
            return {"enabled": False, "active": False}
        bt = hm(own.get("time"), -1) if own.get("time") else _bedtime(self.hub.smart, p)
        wake = hm(own.get("wake") or cfg.wake_time, 420)
        now = now or datetime.now(self.hub.policy.tz)
        m = now.hour * 60 + now.minute
        extended = (self.hub.db.kv_get(f"bedtime_extend.{p['id']}") or {}).get("until", 0)
        later = extended > now.timestamp()
        active = (m >= bt or m < wake) and not later
        return {"enabled": True, "time": f"{bt // 60:02d}:{bt % 60:02d}", "wake": f"{wake // 60:02d}:{wake % 60:02d}",
                "label": clock(bt), "wake_label": clock(wake), "active": active,
                "minutes_left": (bt - m) if 0 <= bt - m <= 120 else None,
                "extended_until": iso(extended) if later else None}

    def set_bedtime(self, person_id: str, *, time_: str | None, wake: str | None, enabled: bool | None) -> None:
        cur = self.hub.db.kv_get(f"bedtime.{person_id}") or {}
        if time_ is not None:
            cur["time"] = time_
        if wake is not None:
            cur["wake"] = wake
        if enabled is not None:
            cur["enabled"] = enabled
        self.hub.db.kv_set(f"bedtime.{person_id}", cur)
        self.hub.bus.publish({"type": "bedtime", "person_id": person_id}, person_id=person_id)

    def extend_bedtime(self, person_id: str, minutes: int) -> float:
        until = time.time() + max(5, min(minutes, 180)) * 60
        self.hub.db.kv_set(f"bedtime_extend.{person_id}", {"until": until})
        self.hub.bus.publish({"type": "bedtime", "person_id": person_id}, person_id=person_id)
        return until

    # -- Today ---------------------------------------------------------------
    def today(self, s: "Subject", chores: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(self.hub.policy.tz)
        h = self.hub
        me = h.db.person(s.person_id) if s.person_id else None
        weather = h.smart.weather.data or h.smart.weather.from_ha()
        w = None
        if weather:
            w = {k: weather.get(k) for k in ("temp", "condition", "symbol", "high", "low", "rain_chance", "rain_at",
                                            "sunset", "sunrise", "tonight_low")}
            w = {k: v for k, v in w.items() if v is not None}
            w["line"] = self._weather_line(weather)
        events = []
        if h.policy.has(s, "family.view"):
            first = _first(me["name"]).lower() if me else ""
            for e in h.home.calendar or []:
                who = str(e.get("who") or "").lower()
                if s.role == "child" and who and first not in who and "everyone" not in who and "family" not in who:
                    continue
                events.append(e)
        dinner = h.home.dinner
        dinner_out = None
        if dinner:
            dinner_out = {**dinner, "time": clock(hm(h.config.smart.dinner_time, 1095))}
        bedtime = self.bedtime(me, now) if me else None
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening" if now.hour < 21 else "night"
        out: dict[str, Any] = {
            "date": now.date().isoformat(),
            "part_of_day": part,
            "headline": self._headline(part, w, events, dinner_out),
            "weather": w,
            "events": events[:8],
            "dinner": dinner_out,
            "family": self.family(s) if s.role != "guest" else [],
            "chores": chores,
            "bedtime": bedtime,
        }
        if s.role in ("admin", "adult"):
            out["kids_bedtimes"] = [{"id": p["id"], "name": _first(p["name"]), **(self.bedtime(p, now) or {})}
                                    for p in h.db.people_with_role("child")]
        return out

    @staticmethod
    def _weather_line(w: dict[str, Any]) -> str:
        bits = []
        if w.get("temp") is not None:
            bits.append(f"{round(w['temp'])}° and {str(w.get('condition') or '').lower()}".strip())
        if w.get("high") is not None and w.get("low") is not None:
            bits.append(f"high {round(w['high'])}°, low {round(w['low'])}°")
        if w.get("rain_at"):
            hh = int(str(w["rain_at"])[11:13] or 0)
            bits.append(f"rain likely around {clock(hh * 60)}")
        elif (w.get("rain_chance") or 0) >= 50:
            bits.append(f"{w['rain_chance']}% chance of rain")
        return ", ".join(bits).capitalize() if bits else ""

    @staticmethod
    def _headline(part: str, w: dict[str, Any] | None, events: list[dict[str, Any]], dinner: dict[str, Any] | None) -> str:
        n = len([e for e in events if not e.get("all_day")])
        if part == "morning":
            s = "A quiet day" if not n else f"{n} thing{'s' if n != 1 else ''} on today"
        elif part in ("afternoon", "evening"):
            s = f"Dinner is {dinner['title']}" if dinner else ("A quiet evening" if part == "evening" else "Afternoon")
        else:
            s = "Time to wind down"
        return s

    # -- guest passes -------------------------------------------------------
    def guest_pass_public(self, row: dict[str, Any]) -> dict[str, Any]:
        g = self.hub.config.guest
        pw = self.hub.vault.get(g.wifi_password_secret) if row.get("show_wifi") else None
        code = self.hub.vault.get(f"guest_pass.{row['id']}.door_code")
        return {
            "name": row["name"], "house": self.hub.config.hub.name, "host": g.host_name,
            "starts_at": iso(row["starts_at"]), "expires_at": iso(row["expires_at"]),
            "wifi": {"ssid": g.wifi_ssid, "password": str(pw) if pw else None} if row.get("show_wifi") and g.wifi_ssid else None,
            "door_code": str(code) if code else None, "door_name": row.get("door_name"),
            "note": row.get("note"), "guidance": list(g.guidance),
            "address": getattr(g, "address", None), "host_phone": getattr(g, "host_phone", None),
        }

    def guest_pass_row(self, row: dict[str, Any], base: str | None) -> dict[str, Any]:
        now = time.time()
        state = ("revoked" if row.get("revoked_at") else "ended" if row["expires_at"] <= now
                 else "upcoming" if row["starts_at"] > now else "active")
        return {"id": row["id"], "name": row["name"], "starts_at": iso(row["starts_at"]),
                "expires_at": iso(row["expires_at"]), "state": state, "views": row.get("views") or 0,
                "show_wifi": bool(row.get("show_wifi")), "has_door_code": self.hub.vault.has(f"guest_pass.{row['id']}.door_code"),
                "door_name": row.get("door_name"), "lock_set": bool(row.get("lock_slot")), "note": row.get("note"),
                "created_by": row.get("created_by"),
                "url": f"{base.rstrip('/')}/g/{row['token']}" if base and row.get("token") and state != "revoked" else None}

    async def create_guest_pass(self, s: "Subject", *, name: str, starts_at: float, expires_at: float, show_wifi: bool,
                                door_code: str | None, note: str | None) -> dict[str, Any]:
        pid = new_id("gp")
        token = secrets.token_urlsafe(18)
        lock = self.hub.config.guest.lock_code
        row = {"id": pid, "token": token, "token_hash": token_hash(token), "name": name[:60], "created_by": s.person_id,
               "created_at": time.time(), "starts_at": starts_at, "expires_at": expires_at,
               "show_wifi": 1 if show_wifi else 0, "door_name": lock.door_name if lock else None,
               "note": (note or "")[:500] or None, "views": 0, "lock_slot": None}
        if door_code:
            self.hub.vault.set(f"guest_pass.{pid}.door_code", door_code)
        if door_code and lock and lock.entity:
            slot = self._free_slot()
            if slot is not None:
                ok = await self._set_code(lock, slot, door_code, name)
                if ok:
                    row["lock_slot"] = slot
        self.hub.db.insert("guest_passes", row)
        self.hub.timers.add(expires_at, "guest_pass.end", {"id": pid})
        self.hub.db.log("guest_pass.create", "ok", person_id=s.person_id, device_id=s.device_id, target=pid,
                        params={"name": name, "until": iso(expires_at), "door_code": bool(door_code)})
        return row

    def _free_slot(self) -> int | None:
        lock = self.hub.config.guest.lock_code
        if not lock:
            return None
        used = {r["lock_slot"] for r in self.hub.db.all(
            "SELECT lock_slot FROM guest_passes WHERE lock_slot IS NOT NULL AND revoked_at IS NULL AND expires_at > ?",
            [time.time()])}
        return next((sl for sl in lock.slots if sl not in used), None)

    async def _set_code(self, lock: Any, slot: int, code: str, name: str) -> bool:
        if not self.hub.actions.is_live("guest_pass.lock_code"):
            return False
        try:
            if lock.service == "keymaster":
                await self.hub.ha.call_service("keymaster", "set_lock_code",
                                               {"entity_id": lock.entity, "code_slot": slot, "usercode": code,
                                                "code_slot_name": f"Guest {name}"[:30]})
            else:
                await self.hub.ha.call_service("zwave_js", "set_lock_usercode",
                                               {"entity_id": lock.entity, "code_slot": slot, "usercode": code})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set the guest door code: %s", exc)
            return False

    async def end_guest_pass(self, pass_id: str, *, revoked_by: str | None = None) -> None:
        row = self.hub.db.one("SELECT * FROM guest_passes WHERE id=?", [pass_id])
        if row is None:
            return
        lock = self.hub.config.guest.lock_code
        if row.get("lock_slot") and lock and lock.entity and self.hub.actions.is_live("guest_pass.lock_code"):
            try:
                if lock.service == "keymaster":
                    await self.hub.ha.call_service("keymaster", "clear_code",
                                                   {"entity_id": lock.entity, "code_slot": row["lock_slot"]})
                else:
                    await self.hub.ha.call_service("zwave_js", "clear_lock_usercode",
                                                   {"entity_id": lock.entity, "code_slot": row["lock_slot"]})
            except Exception as exc:  # noqa: BLE001
                log.warning("could not clear the guest door code: %s", exc)
        self.hub.vault.delete(f"guest_pass.{pass_id}.door_code")
        vals: dict[str, Any] = {"lock_slot": None}
        if revoked_by:
            vals["revoked_at"] = time.time()
            vals["expires_at"] = min(row["expires_at"], time.time())
        self.hub.db.update("guest_passes", "id", pass_id, vals)
        self.hub.db.log("guest_pass.end", "ok", person_id=revoked_by, target=pass_id, source="app" if revoked_by else "schedule")

    def find_pass(self, token: str) -> dict[str, Any] | None:
        return self.hub.db.one("SELECT * FROM guest_passes WHERE token_hash=?", [token_hash(token)])

    # -- doorbell Live Activity ----------------------------------------------
    def set_live_token(self, device_id: str, token: str | None, env: str) -> None:
        self.hub.db.kv_set(f"la_start.{device_id}", {"token": token, "env": env} if token else None)

    async def doorbell_live(self, person_ids: list[str], camera: str | None, camera_name: str) -> int:
        """Start the doorbell Live Activity on each phone that registered a push-to-start token."""
        apns = self.hub.apns
        if not apns.enabled or not person_ids:
            return 0
        now = int(time.time())
        payload = {"aps": {
            "timestamp": now, "event": "start",
            "content-state": {"status": "ringing", "atUnix": now, "answeredBy": None},
            "attributes-type": "DoorbellAttributes",
            "attributes": {"camera": camera or "", "cameraName": camera_name, "door": "Front door"},
            "alert": {"title": "Someone's at the front door", "body": camera_name},
            "stale-date": now + 180, "dismissal-date": now + 600,
        }}
        sent = 0
        for pid in person_ids:
            for d in self.hub.db.devices(pid):
                if d["status"] != "active":
                    continue
                tok = self.hub.db.kv_get(f"la_start.{d['id']}")
                if not tok or not tok.get("token"):
                    continue
                res = await apns.send(tok["token"], payload, env=tok.get("env") or "production", push_type="liveactivity",
                                      topic_suffix=".push-type.liveactivity", priority=10)
                if res.ok:
                    sent += 1
                elif res.token_dead:
                    self.set_live_token(d["id"], None, "production")
        return sent

    # -- Tell Dylan --------------------------------------------------------
    async def add_feedback(self, s: "Subject", text: str, screen: str | None, build: str | None,
                           shot: bytes | None) -> str:
        fid = new_id("fb")
        if shot:
            d = self.feedback_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{fid}.jpg").write_bytes(shot)
        self.hub.db.insert("feedback", {"id": fid, "ts": time.time(), "person_id": s.person_id, "device_id": s.device_id,
                                        "text": text[:4000], "screen": (screen or "")[:200] or None,
                                        "app_build": (build or "")[:20] or None, "has_shot": 1 if shot else 0})
        p = self.hub.db.person(s.person_id) if s.person_id else None
        who = _first(p["name"]) if p else "Someone"
        await self.hub.notify.send(roles=("admin",), level="decision", cat="feedback", title=f"{who}: Tell Dylan",
                                   body=text[:180], ref=f"thomascreek://feedback/{fid}", exclude_person=None,
                                   image=f"/v1/feedback/{fid}/shot.jpg" if shot else None)
        return fid

    def feedback_dir(self):
        from pathlib import Path

        base = Path(self.hub.db._path).parent if self.hub.db._path != ":memory:" else Path("/tmp")
        return base / "feedback"


def default_pass_window(days: float | None, until: float | None, starts: float | None) -> tuple[float, float]:
    start = starts or time.time()
    end = until or (start + (days or 2) * 86400)
    return start, min(end, start + 60 * 86400)


__all__ = ["Household", "INTERCOM_PHRASES", "default_pass_window", "token_hash", "timedelta"]
