"""Opt-in house alerts: Frigate detections, the doorbell, Home Assistant sensors,
and alerts sent by Home Assistant automations.

Each person chooses their own alerts (``alert_prefs`` table). Dylan chooses which
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
        person = self.hub.db.person(person_id)
        if person and person["role"] == "child":
            out["quiet"] = {"on": True, "from": "21:00", "to": "07:00", "ring_urgent": False}
            out["hazards"] = False
        for k, v in (saved or {}).items():
            if k == "quiet" and isinstance(v, dict):
                out["quiet"].update(v)
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

    async def _deliver(self, people: list[tuple[dict[str, Any], dict[str, Any]]], *, title: str, body: str | None,
                       cat: str, urgent: bool = False, level: str = "fyi", ref: str | None = None,
                       image: str | None = None, push_category: str | None = None, group: str | None = None) -> int:
        n = 0
        for person, prefs in people:
            silent = self._quiet_now(prefs["quiet"]) and not (urgent and prefs["quiet"].get("ring_urgent", True))
            await self.hub.notify.send(
                title=title, body=body, cat=cat, level=("act_now" if urgent and not silent else level),
                person_ids=[person["id"]], ref=ref, group_key=group, push_category=push_category,
                silent=silent, image=image, interruption=None if urgent else "active",
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
        sub = e.get("sub_label")
        if isinstance(sub, list):
            sub = sub[0] if sub else None
        from .routes.cameras import camera_name

        cam_title = camera_name(self.hub, cam)
        image = f"/v1/cameras/events/{e['id']}/snapshot.jpg" if e.get("has_snapshot", True) else None
        ref = f"thomascreek://camera/{cam}"
        if label in PACKAGE_LABELS:
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["package"]
                   and self._cooled(p["id"], cam, "package", 1800, now)]
            return await self._deliver(who, title=f"Package at the {cam_title.lower()}", body=f"{cam_title} camera",
                                       cat="cameras", ref=ref, image=image, group=f"cam:{cam}")
        group = next((g for g, labels in LABEL_GROUPS.items() if label in labels), None)
        if not group:
            return 0
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
        what = LABEL_TITLES[group] if group != "car" or label == "car" else label.capitalize()
        body = f"{sub} · {cam_title}" if sub else f"{cam_title} camera"
        return await self._deliver(who, title=f"{what} at the {cam_title.lower()}", body=body, cat="cameras",
                                   ref=ref, image=image, group=f"cam:{cam}")

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
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["doorbell"]
                   and self._cooled(p["id"], eid, "doorbell", 20, now)]
            sent += await self._deliver(who, title="Someone's at the front door", body="Doorbell pressed",
                                        cat="cameras", urgent=True, ref=f"thomascreek://camera/{cam}" if cam else None,
                                        image=image, push_category="DOORBELL", group="doorbell")
        # Leak, smoke, gas, CO
        if eid in self.hazards() and n == "on":
            dc = (new.get("attributes") or {}).get("device_class")
            what = {"moisture": "Water leak", "smoke": "Smoke", "gas": "Gas", "carbon_monoxide": "Carbon monoxide"}[dc]
            who = [(p, pr) for p in self._people(("admin", "adult", "child")) if (pr := self.prefs(p["id"]))["on"]
                   and pr["hazards"] and self._cooled(p["id"], eid, "hazard", 300, now)]
            sent += await self._deliver(who, title=f"{what}: {name}", body="Detected just now", cat="house",
                                        urgent=True, group="hazard")
        # Outside doors while nobody is home
        if eid in src["outside_doors"] and n in OPEN_STATES and self.nobody_home():
            who = [(p, pr) for p in self._people() if (pr := self.prefs(p["id"]))["on"] and pr["away_doors"]
                   and self._cooled(p["id"], eid, "away_door", 120, now)]
            sent += await self._deliver(who, title=f"{name} opened", body="Nobody is home", cat="doors",
                                        level="decision", ref="thomascreek://doors", push_category="SECURE",
                                        group="doors")
        # Extra entities Dylan offered
        if any(x["entity_id"] == eid for x in src["extra"]):
            label = next((x.get("name") for x in src["extra"] if x["entity_id"] == eid), None) or name
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
        return await self._deliver(who, title=title, body=message, cat="house", urgent=urgent,
                                   ref=f"thomascreek://camera/{camera}" if camera else None, image=image, group="automation")

    # -- background --------------------------------------------------------
    def start(self) -> None:
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
        while True:
            await asyncio.sleep(30)
            try:
                await self.check_garages()
            except Exception as exc:  # noqa: BLE001
                log.warning("garage check: %s", exc)

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
