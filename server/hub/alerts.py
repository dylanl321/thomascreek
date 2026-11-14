"""Opt-in house alerts: Frigate detections, the doorbell, Home Assistant sensors,
and alerts sent by Home Assistant automations.

Each person chooses their own alerts (``alert_prefs`` table). Alerts are off for
a person until they switch them on; the choices underneath start at sensible
defaults. Dylan chooses which
extra Home Assistant things are on offer and, if auto-detection misses it, which
entity is the doorbell (``hub_kv`` key ``alerts.sources``).

Everything goes out through ``Notifier.send``, so it also lands in the person's
inbox. Quiet hours make an alert silent instead of dropping it; the doorbell,
leaks and smoke still ring unless the person turned that off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import ssl
import time
from datetime import datetime, time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .state import Hub

from .db import new_id

log = logging.getLogger("hub.alerts")

WEBHOOK_SECRET = "alerts.webhook_token"
ALERT_ROLES = ("admin", "adult")  # cameras, doors and garage are grown-up alerts

# Frigate labels grouped into what a person picks per camera.
LABEL_GROUPS = {
    "person": {"person"},
    "car": {"car", "motorcycle", "truck", "bus"},
    "animal": {"dog", "cat", "deer", "bear", "horse", "fox", "raccoon"},
}
LABEL_TITLES = {"person": "Person", "car": "Car", "animal": "Animal"}
PACKAGE_LABELS = {"package"}
HAZARD_CLASSES = {"moisture", "smoke", "gas", "carbon_monoxide"}
DOORBELL_WORDS = re.compile(r"doorbell|door bell|visitor|\bding\b|\bring\b", re.I)
DEAD_STATES = {"unavailable", "unknown", "", None}
OPEN_STATES = {"on", "open", "opening", "unlocked", "detected"}

DEFAULT_PREFS: dict[str, Any] = {
    "on": True,
    "doorbell": True,
    "package": True,
    "cameras": {},          # camera id -> ["person", "car", "animal"]; filled from DEFAULT_CAMERAS on first read
    "skip_known": True,     # skip faces/plates Frigate recognised (sub_label)
    "only_away": False,     # camera alerts only when nobody is home
    "gap_s": 300,           # one alert per camera and kind in this window
    "garage": True,         # a garage door left open
    "hazards": True,        # leak, smoke, gas, CO
    "away_doors": False,    # an outside door opens while nobody is home
    "entities": {},         # extra Home Assistant entities -> bool
    "automations": True,    # alerts sent by Home Assistant automations
    # Firewalla alarms about the house network (Dylan only). No push unless switched on; History keeps them.
    "network": {"new_device": False, "security": False, "internet": False, "vpn": False, "offline": False},
    # Firewalla alarms about the kids' devices (parents). Off by default too.
    "kids": {"adult": False, "bedtime": False, "gaming": False, "video": False, "bandwidth": False},
    # Official weather alerts (NWS at home, DWD on a trip in Germany). Grown-ups only; on by default.
    "weather": {"warnings": True, "watches": True, "advisories": False, "trips": True},
    "quiet": {"on": False, "from": "22:00", "to": "06:30", "ring_urgent": True},
}
DEFAULT_CAMERA_WORDS = ("frontdoor", "front_door", "sidedoor", "side_door")
GAPS = (60, 300, 900, 3600)


def _is_time(v: Any) -> bool:
    return isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) is not None


class Alerts:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._last: dict[tuple[str, str, str], float] = {}   # (person, camera|entity, kind) -> sent at
        self._garage_sent: dict[str, str] = {}               # entity -> last_changed already alerted
        self._ws_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self.ws_connected = False
        self.ws_error: str | None = None
        self.sent = 0
        self._hist_last: dict[str, float] = {}
        self._pruned = 0.0

    # -- settings ----------------------------------------------------------
    def sources(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get("alerts.sources") or {}
        return {"doorbell_entity": raw.get("doorbell_entity"), "doorbell_camera": raw.get("doorbell_camera"),
                "outside_doors": list(raw.get("outside_doors") or []), "extra": list(raw.get("extra") or []),
                "garage_after_s": int(raw.get("garage_after_s") or 600)}

    def set_sources(self, values: dict[str, Any]) -> dict[str, Any]:
        cur = self.sources()
        cur.update({k: v for k, v in values.items() if k in cur})
        self.hub.db.kv_set("alerts.sources", cur)
        return cur

    def default_cameras(self) -> dict[str, list[str]]:
        names = list(self.hub.frigate._info) or list(self.hub.frigate.cfg.cameras)
        return {c: ["person"] for c in names if any(w in c.lower() for w in DEFAULT_CAMERA_WORDS)}

    def prefs(self, person_id: str) -> dict[str, Any]:
        row = self.hub.db.one("SELECT prefs FROM alert_prefs WHERE person_id=?", [person_id])
        saved = row["prefs"] if row else {}
        if isinstance(saved, str):
            saved = json.loads(saved)
        out = json.loads(json.dumps(DEFAULT_PREFS))
        out["cameras"] = self.default_cameras()
        # Opt-in: nobody gets house alerts until they switch them on in the app.
        out["on"] = False if row is None else out["on"]
        person = self.hub.db.person(person_id)
        if person and person["role"] == "child":
            out["quiet"] = {"on": True, "from": "21:00", "to": "07:00", "ring_urgent": False}
            out["hazards"] = False
        for k, v in (saved or {}).items():
            if k in ("quiet", "network", "kids", "weather") and isinstance(v, dict):
                out[k].update(v)
            elif k in out:
                out[k] = v
        return out

    def set_prefs(self, person_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Merge and validate. Unknown keys and bad values are dropped, not stored."""
        cur = self.prefs(person_id)
        for k, v in changes.items():
            if k in ("on", "doorbell", "package", "skip_known", "only_away", "garage", "hazards", "away_doors",
                     "automations") and isinstance(v, bool):
                cur[k] = v
            elif k == "gap_s" and isinstance(v, int) and v in GAPS:
                cur[k] = v
            elif k == "cameras" and isinstance(v, dict):
                cams: dict[str, list[str]] = {}
                for cam, labels in v.items():
                    if isinstance(cam, str) and isinstance(labels, list):
                        ok = [lb for lb in labels if lb in LABEL_GROUPS]
                        if ok:
                            cams[cam[:64]] = sorted(set(ok))
                cur[k] = cams
            elif k == "entities" and isinstance(v, dict):
                offered = {e["entity_id"] for e in self.sources()["extra"]}
                ents = dict(cur.get("entities") or {})
                ents.update({e: bool(on) for e, on in v.items() if e in offered})
                cur[k] = {e: on for e, on in ents.items() if e in offered}
            elif k in ("network", "kids", "weather") and isinstance(v, dict):
                cur[k] = {**cur[k], **{kk: vv for kk, vv in v.items() if kk in cur[k] and isinstance(vv, bool)}}
            elif k == "quiet" and isinstance(v, dict):
                q = dict(cur["quiet"])
                for qk in ("on", "ring_urgent"):
                    if isinstance(v.get(qk), bool):
                        q[qk] = v[qk]
                for qk in ("from", "to"):
                    if _is_time(v.get(qk)):
                        q[qk] = v[qk]
                cur[k] = q
        self.hub.db.execute("INSERT INTO alert_prefs (person_id, prefs, updated_at) VALUES (?,?,?) "
                            "ON CONFLICT(person_id) DO UPDATE SET prefs=excluded.prefs, updated_at=excluded.updated_at",
                            [person_id, json.dumps(cur), time.time()])
        return cur

    def webhook_token(self) -> str:
        tok = self.hub.vault.get(WEBHOOK_SECRET)
        if not tok:
            tok = secrets.token_urlsafe(32)
            self.hub.vault.set(WEBHOOK_SECRET, tok)
        return str(tok)

    # -- what Home Assistant has ---------------------------------------------
    def _states(self) -> dict[str, dict[str, Any]]:
        return self.hub.home.states

    @staticmethod
    def _name(st: dict[str, Any] | None, eid: str) -> str:
        return ((st or {}).get("attributes") or {}).get("friendly_name") or eid.split(".", 1)[-1].replace("_", " ")

    def doorbells(self) -> list[str]:
        src = self.sources()
        if src["doorbell_entity"]:
            return [src["doorbell_entity"]]
        out = []
        for eid, st in self._states().items():
            attrs = st.get("attributes") or {}
            dom = eid.split(".", 1)[0]
            if dom == "event" and attrs.get("device_class") == "doorbell":
                out.append(eid)
            elif dom in ("binary_sensor", "event") and DOORBELL_WORDS.search(f"{eid} {attrs.get('friendly_name', '')}"):
                if attrs.get("device_class") not in ("motion", "occupancy", "door", "opening"):
                    out.append(eid)
        return sorted(out)

    def garages(self) -> list[str]:
        return sorted(e for e, st in self._states().items()
                      if e.startswith("cover.") and (st.get("attributes") or {}).get("device_class") == "garage")

    def hazards(self) -> list[str]:
        return sorted(e for e, st in self._states().items()
                      if e.startswith("binary_sensor.") and (st.get("attributes") or {}).get("device_class") in HAZARD_CLASSES)

    def nobody_home(self) -> bool:
        st = self._states()
        zone = st.get("zone.home")
        if zone and zone.get("state") not in DEAD_STATES:
            try:
                return int(float(zone["state"])) == 0
            except ValueError:
                pass
        persons = [s for e, s in st.items() if e.startswith("person.")]
        return bool(persons) and all(p.get("state") != "home" for p in persons)

    def summary(self) -> dict[str, Any]:
        """What the hub found, for the settings screen."""
        st = self._states()
        src = self.sources()
        return {
            "doorbells": [{"entity_id": e, "name": self._name(st.get(e), e)} for e in self.doorbells()],
            "garages": [{"entity_id": e, "name": self._name(st.get(e), e)} for e in self.garages()],
            "hazards": [{"entity_id": e, "name": self._name(st.get(e), e)} for e in self.hazards()],
            "outside_doors": [{"entity_id": e, "name": self._name(st.get(e), e)} for e in src["outside_doors"]],
            "extra": [{"entity_id": x["entity_id"], "name": x.get("name") or self._name(st.get(x["entity_id"]), x["entity_id"])}
                      for x in src["extra"]],
            "garage_after_s": src["garage_after_s"],
            "listening": self.ws_connected,
        }

    def candidates(self, q: str = "") -> list[dict[str, Any]]:
        """Home Assistant entities Dylan can offer as alerts."""
        doms = ("binary_sensor", "cover", "lock", "event", "sensor", "input_boolean", "switch", "alarm_control_panel",
                "person", "device_tracker", "valve", "siren")
        ql = q.lower().strip()
        out = []
        for eid, s in sorted(self._states().items()):
            if not eid.startswith(doms):
                continue
            name = self._name(s, eid)
            if ql and ql not in eid.lower() and ql not in name.lower():
                continue
            out.append({"entity_id": eid, "name": name, "state": s.get("state"),
                        "device_class": (s.get("attributes") or {}).get("device_class")})
        return out[:200]

    # -- who gets it -------------------------------------------------------
    def _quiet_now(self, quiet: dict[str, Any], now: float | None = None) -> bool:
        if not quiet.get("on"):
            return False
        try:
            tz = ZoneInfo(self.hub.config.hub.timezone)
        except Exception:  # noqa: BLE001
            tz = None
        t = datetime.fromtimestamp(now or time.time(), tz).time()
        a = dtime.fromisoformat(quiet.get("from") or "22:00")
        b = dtime.fromisoformat(quiet.get("to") or "06:30")
        return (a <= t < b) if a <= b else (t >= a or t < b)

    def _people(self, roles: tuple[str, ...] = ALERT_ROLES) -> list[dict[str, Any]]:
        return [p for p in self.hub.db.people_with_role(*roles)
                if not (p.get("expires_at") and p["expires_at"] < time.time())]

    def _cooled(self, pid: str, key: str, kind: str, gap: float, now: float) -> bool:
        k = (pid, key, kind)
        if now - self._last.get(k, 0) < gap:
            return False
        self._last[k] = now
        return True

    # -- history: every alert, pushed or not ----------------------------------
    HISTORY_KINDS = ("camera", "package", "doorbell", "house", "doors", "kids", "network", "media", "weather")
    KEEP_DAYS = 30

    def record(self, kind: str, title: str, body: str | None, people: list[dict[str, Any]], *,
               ref: str | None = None, image: str | None = None, key: str | None = None, gap: float = 0,
               payload: dict[str, Any] | None = None, now: float | None = None) -> str | None:
        """One line in the alert history for everyone who could get this alert, whether or not
        they turned its push on. `key`+`gap` keep a busy source (a driveway camera) to one line per gap."""
        now = now or time.time()
        if not people:
            return None
        if key and gap and now - self._hist_last.get(key, 0) < gap:
            return None
        if key:
            self._hist_last[key] = now
        hid = new_id("alh")
        self.hub.db.insert("alert_log", {
            "id": hid, "ts": now, "kind": kind, "title": title, "body": body, "ref": ref, "image": image,
            "people": "," + ",".join(p["id"] for p in people) + ",", "payload": payload or {}})
        if now - self._pruned > 3600:
            self._pruned = now
            self.hub.db.execute("DELETE FROM alert_log WHERE ts < ?", [now - self.KEEP_DAYS * 86400])
        return hid

    def history(self, person_id: str, kind: str | None = None, before: float | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alert_log WHERE people LIKE ?"
        args: list[Any] = [f"%,{person_id},%"]
        if kind:
            sql += " AND kind IN (" + ",".join("?" * len(kind.split(","))) + ")"
            args += kind.split(",")
        if before:
            sql += " AND ts < ?"
            args.append(before)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        return [{k: v for k, v in r.items() if k != "people"} for r in self.hub.db.all(sql, args)]

    async def _deliver(self, people: list[tuple[dict[str, Any], dict[str, Any]]], *, title: str, body: str | None,
                       cat: str, urgent: bool = False, level: str = "fyi", ref: str | None = None,
                       image: str | None = None, push_category: str | None = None, group: str | None = None,
                       extra: dict[str, Any] | None = None) -> int:
        n = 0
        for person, prefs in people:
            silent = self._quiet_now(prefs["quiet"]) and not (urgent and prefs["quiet"].get("ring_urgent", True))
            await self.hub.notify.send(
                title=title, body=body, cat=cat, level=("act_now" if urgent and not silent else level),
                person_ids=[person["id"]], ref=ref, group_key=group, push_category=push_category,
                silent=silent, image=image, interruption=None if urgent else "active", extra=extra,
            )
            n += 1
        self.sent += n
        return n

    # -- Frigate -----------------------------------------------------------
    async def on_frigate(self, e: dict[str, Any], now: float | None = None) -> int:
        """A new Frigate event. Returns how many people were alerted."""
        now = now or time.time()
        if e.get("false_positive"):
            return 0
        cam, label = e.get("camera") or "", e.get("label") or ""
        if self.suppressed(cam, label, (e.get("data") or {}).get("box")):
            return 0
        sub = e.get("sub_label")
        if isinstance(sub, list):
            sub = sub[0] if sub else None
        from .routes.cameras import camera_name

        cam_title = camera_name(self.hub, cam)
        image = f"/v1/cameras/events/{e['id']}/snapshot.jpg" if e.get("has_snapshot", True) else None
        ref = f"thomascreek://camera/{cam}"
        if label in PACKAGE_LABELS:
            self.record("package", f"Package at the {cam_title.lower()}", f"{cam_title} camera", self._people(),
                        ref=ref, image=image, key=f"pkg:{cam}", gap=1800, now=now,
                        payload={"event_id": e["id"], "label": label, "camera": cam})
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["package"]
                   and self._cooled(p["id"], cam, "package", 1800, now)]
            return await self._deliver(who, title=f"Package at the {cam_title.lower()}", body=f"{cam_title} camera",
                                       cat="cameras", ref=ref, image=image, group=f"cam:{cam}",
                                       push_category="CAMERA_PACKAGE", extra={"event_id": e["id"], "label": label})
        group = next((g for g, labels in LABEL_GROUPS.items() if label in labels), None)
        if not group:
            return 0
        what = LABEL_TITLES[group] if group != "car" or label == "car" else label.capitalize()
        body = f"{sub} · {cam_title}" if sub else f"{cam_title} camera"
        self.record("camera", f"{what} at the {cam_title.lower()}", body, self._people(), ref=ref, image=image,
                    key=f"cam:{cam}:{group}", gap=60, now=now, payload={"event_id": e["id"], "label": label, "camera": cam})
        away = None
        who = []
        for p in self._people():
            pr = self.prefs(p["id"])
            if not pr["on"] or group not in (pr["cameras"].get(cam) or []):
                continue
            if sub and pr["skip_known"]:
                continue
            if pr["only_away"]:
                away = self.nobody_home() if away is None else away
                if not away:
                    continue
            if self._cooled(p["id"], cam, group, float(pr["gap_s"]), now):
                who.append((p, pr))
        if not who:
            return 0
        return await self._deliver(who, title=f"{what} at the {cam_title.lower()}", body=body, cat="cameras",
                                   ref=ref, image=image, group=f"cam:{cam}", push_category=f"CAMERA_{group.upper()}",
                                   extra={"event_id": e["id"], "label": label})

    # -- Home Assistant ------------------------------------------------------
    async def on_state(self, eid: str, old: dict[str, Any] | None, new: dict[str, Any] | None,
                       now: float | None = None) -> int:
        now = now or time.time()
        if not new:
            return 0
        o, n = (old or {}).get("state"), new.get("state")
        if n in DEAD_STATES or o == n:
            return 0
        if o in DEAD_STATES and not eid.startswith("event."):
            return 0
        name = self._name(new, eid)
        src = self.sources()
        sent = 0
        # Doorbell
        if eid in self.doorbells() and (eid.startswith("event.") or n in OPEN_STATES):
            cam = src["doorbell_camera"] or next(iter(self.default_cameras()), None)
            image = f"/v1/cameras/{cam}/snapshot.jpg" if cam else None
            self.record("doorbell", "Someone's at the front door", "Doorbell pressed", self._people(),
                        ref=f"thomascreek://camera/{cam}" if cam else None, image=image, key=f"bell:{eid}", gap=20, now=now)
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["doorbell"]
                   and self._cooled(p["id"], eid, "doorbell", 20, now)]
            sent += await self._deliver(who, title="Someone's at the front door", body="Doorbell pressed",
                                        cat="cameras", urgent=True, ref=f"thomascreek://camera/{cam}" if cam else None,
                                        image=image, push_category="DOORBELL", group="doorbell")
            try:
                await self.hub.household.doorbell_live([p["id"] for p, _ in who], cam,
                                                       self._name(self.hub.home.states.get(f"camera.{cam}"), cam or "Door") if cam else "Front door")
            except Exception as exc:  # noqa: BLE001
                log.info("doorbell live activity failed: %s", exc)
            try:
                self.hub.home_alerts.note_doorbell(eid, cam, now)
            except Exception as exc:  # noqa: BLE001
                log.info("door alert failed: %s", exc)
        # Leak, smoke, gas, CO
        if eid in self.hazards() and n == "on":
            dc = (new.get("attributes") or {}).get("device_class")
            what = {"moisture": "Water leak", "smoke": "Smoke", "gas": "Gas", "carbon_monoxide": "Carbon monoxide"}[dc]
            self.record("house", f"{what}: {name}", "Detected just now", self._people(("admin", "adult", "child")),
                        key=f"hz:{eid}", gap=300, now=now)
            who = [(p, pr) for p in self._people(("admin", "adult", "child")) if (pr := self.prefs(p["id"]))["on"]
                   and pr["hazards"] and self._cooled(p["id"], eid, "hazard", 300, now)]
            sent += await self._deliver(who, title=f"{what}: {name}", body="Detected just now", cat="house",
                                        urgent=True, group="hazard")
        # Outside doors while nobody is home
        if eid in src["outside_doors"] and n in OPEN_STATES and self.nobody_home():
            self.record("doors", f"{name} opened", "Nobody is home", self._people(), ref="thomascreek://doors",
                        key=f"door:{eid}", gap=120, now=now)
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["away_doors"]
                   and self._cooled(p["id"], eid, "away_door", 120, now)]
            sent += await self._deliver(who, title=f"{name} opened", body="Nobody is home", cat="doors",
                                        level="decision", ref="thomascreek://doors", push_category="SECURE",
                                        group="doors")
        # Extra entities Dylan offered
        if any(x["entity_id"] == eid for x in src["extra"]):
            label = next((x.get("name") for x in src["extra"] if x["entity_id"] == eid), None) or name
            self.record("house", label, f"Now {self._state_words(new)}", self._people(("admin", "adult", "child")),
                        key=f"ha:{eid}", gap=60, now=now)
            who = [(p, pr) for p in self._people(("admin", "adult", "child")) if (pr := self.prefs(p["id"]))["on"]
                   and (pr["entities"] or {}).get(eid) and self._cooled(p["id"], eid, "entity", 60, now)]
            sent += await self._deliver(who, title=label, body=f"Now {self._state_words(new)}", cat="house",
                                        group=f"ha:{eid}")
        return sent

    @staticmethod
    def _state_words(st: dict[str, Any]) -> str:
        s = str(st.get("state"))
        unit = (st.get("attributes") or {}).get("unit_of_measurement")
        dc = (st.get("attributes") or {}).get("device_class")
        if s in ("on", "off") and dc in ("door", "window", "opening", "garage_door"):
            return "open" if s == "on" else "closed"
        if s in ("on", "off") and dc in ("motion", "occupancy", "presence"):
            return "detecting" if s == "on" else "clear"
        return f"{s} {unit}" if unit else s.replace("_", " ")

    async def check_garages(self, now: float | None = None) -> int:
        """A garage door open longer than the limit gets one alert per opening."""
        now = now or time.time()
        limit = self.sources()["garage_after_s"]
        sent = 0
        for eid in self.garages():
            st = self._states().get(eid) or {}
            if st.get("state") != "open":
                self._garage_sent.pop(eid, None)
                continue
            changed = st.get("last_changed") or ""
            try:
                since = datetime.fromisoformat(changed.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if now - since < limit or self._garage_sent.get(eid) == changed:
                continue
            self._garage_sent[eid] = changed
            mins = int((now - since) // 60)
            self.record("doors", f"{self._name(st, eid)} is open", f"Open for {mins} minutes", self._people(),
                        ref="thomascreek://garage", now=now)
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["garage"]]
            sent += await self._deliver(who, title=f"{self._name(st, eid)} is open", body=f"Open for {mins} minutes",
                                        cat="doors", level="decision", ref="thomascreek://garage",
                                        push_category="SECURE", group="garage")
        return sent

    # -- Home Assistant automations ---------------------------------------
    async def from_automation(self, title: str, message: str | None, to: list[str] | str | None,
                              urgent: bool = False, camera: str | None = None) -> int:
        people = self._people(("admin", "adult", "child"))
        targets = [to] if isinstance(to, str) else (to or ["adults"])
        chosen: dict[str, dict[str, Any]] = {}
        for t in targets:
            tl = str(t).lower()
            for p in people:
                if (tl in ("everyone", "all") or (tl == "adults" and p["role"] in ALERT_ROLES)
                        or (tl in ("admin", "admins") and p["role"] == "admin")
                        or (tl in ("kids", "children") and p["role"] == "child") or p["name"].lower() == tl):
                    chosen[p["id"]] = p
        who = [(p, pr) for p in chosen.values() if (pr := self.prefs(p["id"]))["on"] and pr["automations"]]
        image = f"/v1/cameras/{camera}/snapshot.jpg" if camera else None
        self.record("house", title, message, list(chosen.values()),
                    ref=f"thomascreek://camera/{camera}" if camera else None, image=image)
        return await self._deliver(who, title=title, body=message, cat="house", urgent=urgent,
                                   ref=f"thomascreek://camera/{camera}" if camera else None, image=image, group="automation")

    # -- "That's not a person" ----------------------------------------------
    def suppressions(self) -> list[dict[str, Any]]:
        return list(self.hub.db.kv_get("alerts.suppress") or [])

    def suppressed(self, cam: str, label: str, box: list[float] | None) -> bool:
        if not box or len(box) != 4:
            return False
        for s in self.suppressions():
            if s["camera"] == cam and s["label"] == label and _iou(s["box"], box) >= 0.35:
                return True
        return False

    async def mark_wrong(self, event_id: str, person_id: str | None) -> dict[str, Any]:
        """Someone says this detection was wrong. Frigate+ learns from it (when Frigate+ is set up), the
        hub stops alerting for that object in that spot, and its alerts leave everyone's inbox."""
        e = await self.hub.frigate.event(event_id)
        cam, label = e.get("camera") or "", e.get("label") or ""
        box = (e.get("data") or {}).get("box")
        taught = False
        try:
            await self.hub.frigate.mark_false_positive(event_id)
            taught = True
        except Exception as exc:  # noqa: BLE001  (no Frigate+, no snapshot, already sent)
            log.info("Frigate+ false positive for %s: %s", event_id, exc)
        rule = None
        if box and len(box) == 4:
            rules = [s for s in self.suppressions() if not (s["camera"] == cam and s["label"] == label and _iou(s["box"], box) >= 0.8)]
            rule = {"id": secrets.token_hex(6), "camera": cam, "label": label, "box": [round(float(x), 4) for x in box],
                    "event_id": event_id, "by": person_id, "at": time.time()}
            rules.append(rule)
            self.hub.db.kv_set("alerts.suppress", rules[-200:])
        self.hub.db.execute("UPDATE inbox SET acked_at=? WHERE acked_at IS NULL AND ref LIKE ? AND title LIKE ?",
                            [time.time(), f"thomascreek://camera/{cam}%", f"{LABEL_TITLES.get(label, label.capitalize())}%"])
        self.hub.db.execute("DELETE FROM alert_log WHERE payload LIKE ?", [f'%"event_id": {json.dumps(event_id)}%'])
        self.hub.db.log("cameras.wrong", "ok", person_id=person_id, target=event_id,
                        params={"camera": cam, "label": label, "frigate_plus": taught}, source="app")
        return {"camera": cam, "label": label, "frigate_plus": taught, "ignored_spot": rule}

    def forget_suppression(self, rule_id: str) -> bool:
        rules = self.suppressions()
        left = [s for s in rules if s["id"] != rule_id]
        self.hub.db.kv_set("alerts.suppress", left)
        return len(left) != len(rules)

    # -- Firewalla alarms -----------------------------------------------------
    NETWORK_TYPES = {5: "new_device", 1: "security", 2: "security", 14: "security", 16: "security",
                     15: "internet", 11: "vpn", 13: "vpn", 7: "offline"}
    KID_TYPES = {10: "adult", 9: "gaming", 8: "video", 3: "bandwidth"}

    def kid_for_device(self, dev: dict[str, Any]) -> dict[str, Any] | None:
        """The child a Firewalla device belongs to: linked by the hub, or in a Firewalla group named after them."""
        mac = str(dev.get("mac") or dev.get("id") or "").upper()
        kids = self.hub.db.people_with_role("child")
        if mac:
            row = self.hub.db.one("SELECT person_id FROM network_rules WHERE upper(mac)=?", [mac])
            if row:
                return next((k for k in kids if k["id"] == row["person_id"]), None)
        group = ((dev.get("group") or {}).get("name") if isinstance(dev.get("group"), dict) else None) or ""
        dname = str(dev.get("name") or "")
        for k in kids:
            first = k["name"].split()[0].lower()
            if group.lower() == first or re.match(rf"^{re.escape(first)}('s|s)?\b", dname.lower()):
                return k
        return None

    def _bedtime(self, now: float) -> bool:
        bed = self.hub.config.child_policy.bedtime or "20:30"
        return self._quiet_now({"on": True, "from": bed, "to": "06:30"}, now)

    async def on_firewalla(self, a: dict[str, Any], now: float | None = None) -> int:
        now = now or time.time()
        t = int(a.get("type") or 0)
        dev = a.get("device") or {}
        dname = dev.get("name") or dev.get("mac") or "A device"
        msg = a.get("message") or ""
        kid = self.kid_for_device(dev) if dev else None
        sent = 0
        if kid:
            kind = self.KID_TYPES.get(t)
            if t in (6, 8, 9) and self._bedtime(now):
                kind = "bedtime"
            if kind:
                titles = {"adult": f"Adult site on {kid['name']}'s {dname}",
                          "gaming": f"{kid['name']} is gaming", "video": f"{kid['name']} is watching video",
                          "bandwidth": f"{kid['name']}'s {dname} is using a lot of data",
                          "bedtime": f"{kid['name']}'s {dname} is up past bedtime"}
                self.record("kids", titles[kind], msg or None, self._people(), ref="thomascreek://kids",
                            key=f"kid:{dev.get('mac') or dname}:{kind}", gap=1800, now=now,
                            payload={"kid": kid["name"], "kind": kind})
                who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["kids"].get(kind)
                       and self._cooled(p["id"], f"fw:{dev.get('mac') or dname}", kind, 1800, now)]
                sent += await self._deliver(who, title=titles[kind], body=msg or None, cat="house",
                                            urgent=False, level="decision" if kind == "adult" else "fyi",
                                            ref="thomascreek://kids", group=f"kid:{kid['id']}")
                return sent
        kind = self.NETWORK_TYPES.get(t)
        if kind:
            title = {"new_device": f"New device on the network: {dname}", "security": "Network security alert",
                     "internet": "Internet connection changed", "vpn": "VPN alert", "offline": f"{dname} went offline"}[kind]
            self.record("network", title, msg or None, self._people(("admin",)), ref="thomascreek://systems",
                        now=now, payload={"kind": kind})
            who = [(p, pr) for p in self._people(("admin",)) if (pr := self.prefs(p["id"]))["on"] and pr["network"].get(kind)
                   and self._cooled(p["id"], f"fw:{a.get('aid')}", kind, 60, now)]
            sent += await self._deliver(who, title=title, body=msg or None, cat="systems",
                                        level="decision" if kind == "security" else "fyi", ref="thomascreek://systems",
                                        group=f"fw:{kind}")
        return sent

    async def poll_firewalla(self) -> int:
        fw = self.hub.firewalla
        if not fw.enabled:
            return 0
        rows = await fw.alarms(limit=50)
        last = float(self.hub.db.kv_get("alerts.firewalla_ts") or 0)
        newest = max([last] + [float(a.get("ts") or 0) for a in rows])
        sent = 0
        if last:  # the first look only sets where to start
            for a in sorted(rows, key=lambda r: float(r.get("ts") or 0)):
                if float(a.get("ts") or 0) > last:
                    try:
                        sent += await self.on_firewalla(a)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Firewalla alarm %s: %s", a.get("aid"), exc)
        if newest != last:
            self.hub.db.kv_set("alerts.firewalla_ts", newest)
        return sent

    # -- media someone asked for is ready ---------------------------------------
    async def poll_media(self) -> None:
        if not self.hub.jellyseerr.enabled:
            return
        from .routes.media_requests import push_ready

        await push_ready(self.hub)

    # -- background --------------------------------------------------------
    def firewalla_push_off_once(self) -> int:
        """Firewalla pushes used to default on, and saved prefs carry those old defaults. Turn them off
        once for everyone (Dylan, 2026-11-13); anyone can switch them back on afterwards."""
        if self.hub.db.kv_get("alerts.firewalla_off_v1"):
            return 0
        n = 0
        for row in self.hub.db.all("SELECT person_id, prefs FROM alert_prefs"):
            prefs = row["prefs"] if isinstance(row["prefs"], dict) else json.loads(row["prefs"] or "{}")
            changed = False
            for k in ("network", "kids"):
                if isinstance(prefs.get(k), dict) and any(prefs[k].values()):
                    prefs[k] = {kk: False for kk in prefs[k]}
                    changed = True
            if changed:
                self.hub.db.execute("UPDATE alert_prefs SET prefs=?, updated_at=? WHERE person_id=?",
                                    [json.dumps(prefs), time.time(), row["person_id"]])
                n += 1
        self.hub.db.kv_set("alerts.firewalla_off_v1", True)
        return n

    def start(self) -> None:
        try:
            self.firewalla_push_off_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("Firewalla default-off migration: %s", exc)
        if self.hub.ha.enabled and self._ws_task is None:
            self._ws_task = asyncio.create_task(self._listen())
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._ticker())

    async def stop(self) -> None:
        for t in (self._ws_task, self._tick_task):
            if t:
                t.cancel()
        self._ws_task = self._tick_task = None

    async def _ticker(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(30)
            n += 1
            for name, job, every in (("garage check", self.check_garages, 1), ("Firewalla alarms", self.poll_firewalla, 2),
                                     ("media ready", self.poll_media, 4)):
                if n % every:
                    continue
                try:
                    await job()
                except Exception as exc:  # noqa: BLE001
                    log.info("%s: %s", name, exc)

    def _ws_url(self) -> str:
        url = (self.hub.config.home_assistant.url or "").rstrip("/")
        return re.sub(r"^http", "ws", url) + "/api/websocket"

    async def _listen(self) -> None:
        """Home Assistant's state changes as they happen (the 15 s poll is too slow for a doorbell)."""
        import websockets

        delay = 5.0
        while True:
            try:
                ctx = None
                if self._ws_url().startswith("wss://"):
                    ctx = ssl.create_default_context()
                    if not self.hub.config.home_assistant.verify_tls:
                        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                token = self.hub.ha.secret(self.hub.config.home_assistant.token_secret)
                if not token:
                    raise RuntimeError("no Home Assistant token")
                async with websockets.connect(self._ws_url(), ssl=ctx, max_size=16 * 2**20, open_timeout=10) as ws:
                    await ws.recv()  # auth_required
                    await ws.send(json.dumps({"type": "auth", "access_token": token}))
                    if json.loads(await ws.recv()).get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant refused the token")
                    await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
                    self.ws_connected, self.ws_error, delay = True, None, 5.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "event":
                            continue
                        d = (msg.get("event") or {}).get("data") or {}
                        eid = d.get("entity_id") or ""
                        if eid in self.hub.home.states and d.get("new_state"):
                            self.hub.home.states[eid] = {**self.hub.home.states[eid], "state": d["new_state"].get("state"),
                                                         "attributes": d["new_state"].get("attributes"),
                                                         "last_changed": d["new_state"].get("last_changed")}
                        try:
                            await self.on_state(eid, d.get("old_state"), d.get("new_state"))
                        except Exception as exc:  # noqa: BLE001
                            log.warning("alert for %s failed: %s", eid, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.ws_error = f"{type(exc).__name__}: {exc}"[:200]
                log.info("Home Assistant events: %s", self.ws_error)
            self.ws_connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


def _iou(a: list[float], b: list[float]) -> float:
    """Overlap of two Frigate boxes [x, y, w, h] (0..1)."""
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0.0, min(ax2, bx2) - max(a[0], b[0])), max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0
