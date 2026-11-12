"""Ovens: quick start, set from a recipe, preheat and timer alerts.

The ovens come from Home Assistant's GE Home integration, which shows each
oven cavity as a `water_heater` whose operation mode is the cook mode ("Bake",
"Convection Roast"...) and whose target temperature is the set point in °F.
GE only accepts remote commands after Remote Enable is pressed at the oven;
until then the entity reports no supported features and Home Assistant
refuses writes, so the hub says so plainly instead of failing vaguely.

This module also reads a recipe and finds the oven step ("Preheat the oven
to 425°F ... roast 25 minutes"), and watches running ovens to push "it's
preheated", "time's up" and "still on" notices.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .policy import Subject
    from .state import Hub

log = logging.getLogger("hub.kitchen")

# WaterHeaterEntityFeature.TARGET_TEMPERATURE | OPERATION_MODE
FEATURE_TEMP, FEATURE_MODE = 1, 2
OFF_STATES = ("off", "unavailable", "unknown", "")
# GE Home's names for the cook modes, in the order the app shows them.
KNOWN_MODES = ["Bake", "Convection Bake", "Convection Roast", "Conv. Multi-Bake", "Broil High", "Broil Low",
               "Air Fry", "Warm", "Proof", "Frozen Pizza", "Frozen Snacks", "Baked Goods", "Vent Bake"]
NO_TEMP_MODES = ("Broil High", "Broil Low", "Proof")
MIN_F, MAX_F = 170, 550
STILL_ON_S = 3 * 3600

PRESETS = [
    {"id": "bake350", "title": "Preheat 350°", "detail": "Bake", "mode": "Bake", "temp": 350},
    {"id": "conv425", "title": "425° convection", "detail": "Roasting", "mode": "Convection Roast", "temp": 425},
    {"id": "broil", "title": "Broil", "detail": "High", "mode": "Broil High", "temp": None},
    {"id": "warm", "title": "Keep warm", "detail": "170°", "mode": "Warm", "temp": 170},
    {"id": "airfry", "title": "Air fry", "detail": "400°", "mode": "Air Fry", "temp": 400},
    {"id": "pizza", "title": "Frozen pizza", "detail": "400°", "mode": "Frozen Pizza", "temp": 400},
]


def is_oven(state: dict[str, Any]) -> bool:
    eid: str = state.get("entity_id", "")
    if not eid.startswith("water_heater."):
        return False
    attrs = state.get("attributes", {})
    name = str(attrs.get("friendly_name") or eid).lower()
    if "oven" in name or "oven" in eid or attrs.get("icon") == "mdi:stove":
        return True
    ops = attrs.get("operation_list") or []
    return "Bake" in ops


def oven_label(eid: str, name: str, all_ovens: list[str]) -> str:
    low = f"{eid} {name}".lower()
    if "lower" in low:
        return "Lower oven"
    if "upper" in low:
        return "Upper oven"
    # GE names the upper cavity of a double oven just "Oven" when a lower one exists.
    if any("lower" in o for o in all_ovens if o != eid):
        return "Upper oven"
    return "Oven"


def oven_state(st: dict[str, Any]) -> dict[str, Any]:
    a = st.get("attributes", {})
    s = st.get("state", "")
    feats = int(a.get("supported_features") or 0)
    available = s not in ("unavailable", "unknown")
    mode = a.get("operation_mode") or (s if s not in OFF_STATES else None)
    on = available and s not in OFF_STATES and mode not in (None, "off")
    target = a.get("temperature")
    return {
        "on": on, "available": available, "mode": mode if on else None,
        "modes": [m for m in (a.get("operation_list") or []) if m != "off"] or KNOWN_MODES,
        "current_temp": _num(a.get("current_temperature")),
        "target_temp": _num(target) if on and target else None,
        "min_temp": _num(a.get("min_temp")) or MIN_F, "max_temp": _num(a.get("max_temp")) or MAX_F,
        "remote_ready": available and bool(feats & FEATURE_MODE),
        "display_state": a.get("display_state"),
        "cook_time_remaining": a.get("cook_time_remaining"),
        "probe_temp": _num(a.get("probe_temperature")),
    }


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# -- reading a recipe ------------------------------------------------------
_TEMP = re.compile(r"(\d{3})\s*(?:°|º|degrees?|deg\.?)?\s*(F|C|fahrenheit|celsius)?\b", re.I)
_MIN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*)?(minutes?|mins?|hours?|hrs?)\b", re.I)
_OVENISH = re.compile(r"\b(oven|preheat|bake|roast|broil|air[- ]?fr|convection)", re.I)


def _mode_for(text: str) -> str:
    t = text.lower()
    if "broil" in t:
        return "Broil Low" if "low" in t else "Broil High"
    if re.search(r"air[- ]?fr", t):
        return "Air Fry"
    if "convection" in t or "fan oven" in t or "fan-forced" in t:
        return "Convection Roast" if "roast" in t else "Convection Bake"
    if "keep warm" in t or "warm oven" in t:
        return "Warm"
    if "proof" in t and "oven" in t:
        return "Proof"
    return "Bake"


_HM = re.compile(r"(\d+)\s*(?:hours?|hrs?|h)\s*(?:and\s*)?(\d+)\s*(?:minutes?|mins?|m)\b", re.I)


def _minutes(text: str) -> int | None:
    hm = _HM.search(text)
    if hm:
        return int(hm.group(1)) * 60 + int(hm.group(2))
    best = None
    for m in _MIN.finditer(text):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        unit = m.group(3).lower()
        val = hi * 60 if unit.startswith("h") else hi  # the upper end: "20-25 minutes" -> 25
        if 1 <= val <= 360:
            best = val if best is None else max(best, val)
    return int(round(best)) if best else None


def _iso_minutes(v: Any) -> int | None:
    """Mealie times are free text ("25 minutes", "1 hour 10 minutes", "PT25M")."""
    if not v:
        return None
    s = str(v)
    m = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?", s.strip(), re.I)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    h = re.search(r"(\d+)\s*h", s, re.I)
    mm = re.search(r"(\d+)\s*m", s, re.I)
    total = (int(h.group(1)) * 60 if h else 0) + (int(mm.group(1)) if mm else 0)
    return total or None


def oven_plan(steps: list[str], cook_time: Any = None, title: str | None = None) -> dict[str, Any] | None:
    """Find the oven setting in a recipe: mode, °F, minutes and the line it came from."""
    temp = None
    temp_line = None
    mode_text = ""
    for i, raw in enumerate(steps):
        line = raw or ""
        if not _OVENISH.search(line):
            continue
        for m in _TEMP.finditer(line):
            v = int(m.group(1))
            unit = (m.group(2) or "").lower()
            if unit.startswith("c"):
                v = int(round((v * 9 / 5 + 32) / 5) * 5)
            elif not unit and not re.search(r"°|degree|oven|preheat", line, re.I):
                continue
            if MIN_F <= v <= MAX_F or (unit.startswith("c") and 75 <= int(m.group(1)) <= 290):
                temp = max(MIN_F, min(MAX_F, v))
                temp_line = i
                break
        mode_text += " " + line
        if temp is not None:
            break
    broil = "broil" in mode_text.lower()
    if temp is None and not broil:
        return None
    # The cooking time usually follows the preheat line: "roast for 25 minutes".
    minutes = None
    start = temp_line if temp_line is not None else 0
    for line in steps[start:start + 4]:
        if _OVENISH.search(line or "") or re.search(r"\b(cook|until)\b", line or "", re.I):
            minutes = _minutes(line or "")
            if minutes:
                mode_text += " " + line
                break
    if minutes is None:
        minutes = _iso_minutes(cook_time)
    mode = _mode_for(mode_text)
    quote = (steps[temp_line] if temp_line is not None else "").strip()
    if len(quote) > 200:
        quote = quote[:197].rstrip() + "…"
    return {"mode": mode, "temp": None if mode in NO_TEMP_MODES else temp, "minutes": minutes,
            "quote": quote or None, "title": title}


def saved_oven(extras: dict[str, Any], title: str | None = None) -> dict[str, Any] | None:
    """An oven setting someone corrected by hand, kept in the recipe's Mealie extras."""
    mode = extras.get("tc_oven_mode")
    if not mode:
        return None
    def _int(v: Any) -> int | None:
        try:
            return int(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {"mode": str(mode), "temp": _int(extras.get("tc_oven_temp")), "minutes": _int(extras.get("tc_oven_minutes")),
            "quote": None, "title": title, "saved": True}


_SECTION = re.compile(r"^\s*(ingredients?|what you.ll need|you will need|for the [\w ]+)\s*:?\s*$", re.I)
_STEPS = re.compile(r"^\s*(instructions?|directions?|method|steps|preparation|how to make it)\s*:?\s*$", re.I)
_QTY = re.compile(r"^\s*([•\-\*·▢□]|\d|[½¼¾⅓⅔⅛]|one |two |a pinch|pinch|salt|pepper)", re.I)
_NUMSTEP = re.compile(r"^\s*(?:step\s*)?(\d{1,2})[\.\):]\s+", re.I)


def parse_recipe_text(text: str) -> dict[str, Any]:
    """Turn pasted or scanned text into title, ingredients and steps for the editor to check."""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    title = None
    ingredients: list[str] = []
    steps: list[str] = []
    section = None
    for ln in lines:
        if _SECTION.match(ln):
            section = "ing"
            continue
        if _STEPS.match(ln):
            section = "steps"
            continue
        if title is None and section is None and len(ln) <= 80 and not _QTY.match(ln):
            title = ln
            continue
        clean = re.sub(r"^\s*[•\-\*·▢□]\s*", "", ln)
        if section == "steps" or (section is None and _NUMSTEP.match(ln)):
            m = _NUMSTEP.match(clean)
            if m:
                steps.append(clean[m.end():].strip())
            elif steps and not re.search(r"[.!?]$", steps[-1]):
                steps[-1] += " " + clean  # a step wrapped onto the next line
            else:
                steps.append(clean)
        elif section == "ing" or _QTY.match(ln):
            ingredients.append(clean)
        elif steps:
            steps[-1] += " " + clean
        # Anything else before the ingredients (serves, prep time, a blurb) is left out.
    return {"title": title or "New recipe", "ingredients": ingredients, "steps": steps,
            "oven": oven_plan(steps, None, title)}


def plan_text(p: dict[str, Any]) -> str:
    parts = [f"{int(p['temp'])}°"] if p.get("temp") else []
    parts.append(p["mode"].lower() if parts else p["mode"])
    s = " ".join(parts)
    if p.get("minutes"):
        s += f", {p['minutes']} min"
    return s


# -- running ovens -----------------------------------------------------------
@dataclass
class Session:
    eid: str
    person_id: str | None
    target: float | None
    mode: str
    started: float = field(default_factory=time.time)
    title: str | None = None
    timer_min: int | None = None
    timer_from: str = "now"  # now | preheat | manual
    auto_off: bool = False
    preheated: bool = False
    timer_ends: float | None = None


class Kitchen:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.sessions: dict[str, Session] = {}
        self._still_on_sent: dict[str, float] = {}
        self._on_since: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # -- discovery ----------------------------------------------------------
    def ovens(self) -> list[str]:
        extra = self.hub.config.home_assistant.extra.get("ovens")
        states = self.hub.home.states
        if extra:
            return [e for e in extra if e in states]
        return sorted(eid for eid, st in states.items() if is_oven(st))

    def label(self, eid: str) -> str:
        st = self.hub.home.states.get(eid, {})
        return oven_label(eid, st.get("attributes", {}).get("friendly_name", eid), self.ovens())

    def related(self, eid: str, suffix: str, domains: tuple[str, ...] = ("number", "sensor")) -> str | None:
        """The GE entity of the same cavity, e.g. its kitchen timer."""
        obj = eid.split(".", 1)[1]
        serial = obj.split("_", 1)[0]
        lower = "lower" in obj
        for dom in domains:
            for other in self.hub.home.states:
                if not other.startswith(dom + ".") or suffix not in other:
                    continue
                o = other.split(".", 1)[1]
                if not o.startswith(serial):
                    continue
                if ("lower" in o) == lower:
                    return other
        return None

    def oven_out(self, eid: str) -> dict[str, Any]:
        st = self.hub.home.states.get(eid, {"entity_id": eid, "state": "unavailable", "attributes": {}})
        out = {"id": eid, "name": self.label(eid), **oven_state(st)}
        sess = self.sessions.get(eid)
        if sess and out["on"]:
            out["session"] = {"title": sess.title, "timer_min": sess.timer_min, "timer_from": sess.timer_from,
                              "auto_off": sess.auto_off, "preheated": sess.preheated,
                              "timer_ends_at": _iso(sess.timer_ends),
                              "started_at": _iso(sess.started)}
        return out

    async def summary(self, s: "Subject") -> dict[str, Any]:
        """Everything the Kitchen screen and the Home dinner card need."""
        dinner = None
        d = self.hub.home.dinner
        if d and d.get("slug") and self.hub.mealie.enabled:
            try:
                r = await self.hub.mealie.recipe(d["slug"])
                dinner = {"title": r["title"], "slug": r["slug"], "image_url": r.get("image_url"), "oven": r.get("oven")}
            except Exception as exc:  # noqa: BLE001
                log.info("dinner recipe read failed: %s", exc)
                dinner = {"title": d["title"], "slug": d["slug"], "image_url": d.get("image_url"), "oven": None}
        elif d:
            dinner = {"title": d["title"], "slug": d.get("slug"), "image_url": d.get("image_url"), "oven": None}
        return {"ovens": [self.oven_out(e) for e in self.ovens()], "presets": PRESETS, "dinner": dinner}

    # -- action planning (called from ActionRunner) --------------------------
    def calls(self, eid: str, p: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        from .actions import ActionError

        st = self.hub.home.states.get(eid, {})
        o = oven_state(st)
        t = {"entity_id": eid}
        if not o["available"]:
            raise ActionError(409, "oven_offline",
                              "The oven isn't connected to Home Assistant right now. Check the GE Home integration.")
        on = p.get("on", True)
        if not o["remote_ready"]:
            if on:
                raise ActionError(409, "remote_enable",
                                  "Press Remote Enable on the oven, then try again. GE needs it before an app can start the oven.")
            raise ActionError(409, "remote_enable", "The oven won't take remote commands right now. Turn it off at the oven.")
        if not on:
            return [("water_heater", "set_operation_mode", {**t, "operation_mode": "off"})]
        mode = p.get("mode") or o["mode"] or "Bake"
        if mode not in o["modes"]:
            raise ActionError(400, "bad_params", f"This oven has no {mode} mode")
        calls = [("water_heater", "set_operation_mode", {**t, "operation_mode": mode})]
        if mode not in NO_TEMP_MODES:
            temp = p.get("temp")
            if temp is None:
                temp = o["target_temp"] or 350
            lo, hi = o["min_temp"] or MIN_F, o["max_temp"] or MAX_F
            # Warm and Proof run below the normal minimum, so only clamp the top.
            temp = max(100 if mode in ("Warm", "Proof") else lo, min(hi, float(temp)))
            p["temp"] = int(temp)
            calls.append(("water_heater", "set_temperature", {**t, "temperature": int(temp)}))
        return calls

    async def after_run(self, s: "Subject", eid: str, p: dict[str, Any]) -> None:
        """Remember what was asked so the watcher can announce preheat and the timer."""
        if p.get("on") is False:
            self.sessions.pop(eid, None)
            self._cancel_timer(eid)
            return
        try:
            timer = max(1, min(360, int(p["timer_min"]))) if p.get("timer_min") else None
        except (TypeError, ValueError):
            timer = None
        timer_from = p.get("timer_from") if p.get("timer_from") in ("now", "preheat", "manual") else None
        sess = Session(eid=eid, person_id=s.person_id, target=p.get("temp"), mode=p.get("mode") or "Bake",
                       title=(str(p["recipe"])[:80] if p.get("recipe") else None), timer_min=timer,
                       timer_from=timer_from or ("now" if timer else "manual"),
                       auto_off=bool(p.get("auto_off")))
        self.sessions[eid] = sess
        self._still_on_sent.pop(eid, None)
        self._on_since[eid] = sess.started
        if timer and sess.timer_from == "now":
            await self.start_timer(eid, timer)

    async def start_timer(self, eid: str, minutes: int, auto_off: bool | None = None) -> None:
        minutes = max(1, min(360, int(minutes)))
        sess = self.sessions.get(eid)
        if sess is None:
            st = self.hub.home.states.get(eid, {})
            o = oven_state(st)
            sess = Session(eid=eid, person_id=None, target=o["target_temp"], mode=o["mode"] or "Bake", preheated=True)
            self.sessions[eid] = sess
        sess.timer_min = minutes
        if auto_off is not None:
            sess.auto_off = auto_off
        sess.timer_ends = time.time() + minutes * 60
        self._cancel_timer(eid)
        self.hub.timers.add(sess.timer_ends, "oven.timer", {"entity_id": eid})
        # Also set the oven's own kitchen timer so it beeps in the kitchen. Best effort.
        if self.hub.actions.is_live("oven.set"):
            try:
                num = self.related(eid, "kitchen_timer", ("number",))
                if num:
                    await self.hub.ha.call_service("number", "set_value", {"entity_id": num, "value": minutes})
                else:
                    sen = self.related(eid, "kitchen_timer", ("sensor",))
                    if sen:
                        await self.hub.ha.call_service("ge_home", "set_timer", {"entity_id": sen, "duration": minutes})
            except Exception as exc:  # noqa: BLE001
                log.info("oven kitchen timer not set: %s", exc)

    def _cancel_timer(self, eid: str) -> None:
        self.hub.db.execute("DELETE FROM timers WHERE kind='oven.timer' AND json_extract(payload,'$.entity_id')=?", [eid])

    async def timer_done(self, eid: str) -> None:
        sess = self.sessions.get(eid)
        label = self.label(eid)
        what = sess.title if sess and sess.title else label
        turned_off = False
        if sess and sess.auto_off and self.hub.actions.is_live("oven.set"):
            try:
                await self.hub.ha.call_service("water_heater", "set_operation_mode",
                                               {"entity_id": eid, "operation_mode": "off"})
                turned_off = True
                self.hub.db.log("oven.set", "ok", target=eid, params={"on": False}, detail="timer ended", source="schedule")
            except Exception as exc:  # noqa: BLE001
                log.warning("oven auto off failed: %s", exc)
        if sess:
            sess.timer_ends = None
        body = f"{label} turned off, as you asked." if turned_off else f"{label} is still on."
        actions = [] if turned_off else [{"id": "off", "title": "Turn off", "destructive": True}]
        await self._notify(sess, f"Time's up: {what}", body, eid, actions, urgent=True)
        if turned_off:
            self.sessions.pop(eid, None)

    async def _notify(self, sess: Session | None, title: str, body: str, eid: str,
                      actions: list[dict[str, Any]], *, urgent: bool = False, everyone: bool = False) -> None:
        kw: dict[str, Any] = {"person_ids": [sess.person_id]} if (sess and sess.person_id and not everyone) else {"roles": ("admin", "adult")}
        await self.hub.notify.send(title=title, body=body, cat="kitchen", level="act_now" if urgent else "fyi",
                                   actions=actions, ref=f"thomascreek://kitchen/{eid}",
                                   group_key=f"oven:{eid}:{int(time.time() // 60)}",
                                   interruption="time-sensitive" if urgent else "active",
                                   push_category=("OVEN" if any(a["id"] == "timer" for a in actions)
                                                  else "OVEN_ON" if actions else None), **kw)

    # -- watcher ---------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("kitchen tick failed: %s", exc)
            await asyncio.sleep(10)

    async def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        states = self.hub.home.states
        for eid in self.ovens():
            st = states.get(eid)
            if not st:
                continue
            o = oven_state(st)
            sess = self.sessions.get(eid)
            if not o["on"]:
                if sess and now - sess.started > 120:
                    # Turned off at the oven (or by GE when the cycle ended).
                    self.sessions.pop(eid, None)
                    self._cancel_timer(eid)
                self._still_on_sent.pop(eid, None)
                self._on_since.pop(eid, None)
                continue
            # Preheat reached.
            if sess and not sess.preheated and o["target_temp"] and o["current_temp"] is not None \
                    and o["current_temp"] >= o["target_temp"] - 5:
                sess.preheated = True
                label = self.label(eid)
                if sess.timer_min and sess.timer_from == "preheat":
                    await self.start_timer(eid, sess.timer_min)
                    body = f"Timer started: {sess.timer_min} min."
                    acts = [{"id": "off", "title": "Turn off", "destructive": True}]
                elif sess.timer_min and not sess.timer_ends:
                    body = f"Put {sess.title or 'it'} in and start the {sess.timer_min} min timer."
                    acts = [{"id": "timer", "title": f"Start {sess.timer_min} min timer"},
                            {"id": "off", "title": "Turn off", "destructive": True}]
                else:
                    body = f"Ready for {sess.title}." if sess.title else "Ready when you are."
                    acts = [{"id": "off", "title": "Turn off", "destructive": True}]
                await self._notify(sess, f"{label} is at {int(o['target_temp'])}°", body, eid, acts, urgent=True)
            # Still on a long time.
            if eid not in self._on_since:
                self._on_since[eid] = sess.started if sess else min(now, _ts(st.get("last_changed")) or now)
            on_since = self._on_since[eid]
            if now - on_since > STILL_ON_S and eid not in self._still_on_sent and not (sess and sess.timer_ends):
                self._still_on_sent[eid] = now
                hours = int((now - on_since) // 3600)
                away = self._everyone_away()
                body = f"{o['mode'] or 'On'} for {hours} hours." + (" Everyone has left home." if away else "")
                await self._notify(sess, f"{self.label(eid)} is still on", body, eid,
                                   [{"id": "off", "title": "Turn off", "destructive": True}],
                                   urgent=away, everyone=True)

    def _everyone_away(self) -> bool:
        ents = [p.get("presence_entity") for p in self.hub.db.people() if p["role"] in ("admin", "adult")]
        ents = [e for e in ents if e]
        if not ents:
            return False
        states = self.hub.home.states
        return all(states.get(e, {}).get("state") not in ("home", "on") for e in ents)


def _iso(ts: float | None) -> str | None:
    from .home import iso

    return iso(ts) if ts else None


def _ts(v: Any) -> float | None:
    if not v:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
