"""Smart "Right now" cards for Home.

The hub decides which few cards each person sees, from the time of day, the
weather, today's plans, what the house is doing and who is asking. The app only
draws them, so every phone, iPad and wall tablet agrees, and rules can change
without a new app build.

A card is a plain dict (see `card()`); buttons are planned through the same
policy as a tap, so a card never offers something the person can't do. "Not now"
hides a card until its window comes round again; three "Not now" in a row on the
same rule turns that rule off for that person (they can turn it back on).

Slow inputs (weather, chores, trips, tonight's oven setting, kids' internet state,
learned habits) are refreshed in the background; `cards()` itself is synchronous
and cheap so it can run inside every /v1/home.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .home import kind_for
from .kitchen import oven_state
from .weather import Weather

if TYPE_CHECKING:
    from .policy import Subject
    from .state import Hub

log = logging.getLogger("hub.smart")

MAX_SHOWN = 3
SAFETY_RULES = {"oven_on", "away_unlocked", "garage_open"}
HABIT_ACTIONS = ("light.set", "switch.set", "fan.set", "scene.run", "mode.run", "climate.set")
STEP_ACTIONS = ("light.set", "switch.set", "fan.set", "lock.set", "garage.set", "media.command")

RULE_TITLES = {
    "oven_on": "Oven left on", "away_unlocked": "Door unlocked while away", "away_lights": "Lights on while away",
    "garage_open": "Garage left open", "package": "Package at the door", "weather_am": "Morning weather",
    "today": "Today's plans", "rain": "Rain coming", "freeze": "Freezing tonight", "heat": "Hot day",
    "kids_chores": "Kids' chores left", "dinner": "Dinner and the oven", "habit": "Usually about now",
    "show": "Holiday show", "bedtime": "Kids' bedtime", "trash": "Trash night", "lockup": "Lock up for the night",
    "media_ready": "Your request is ready", "wx_alert": "Weather warnings", "trip": "Trip coming up", "my_chores": "My chores",
    "my_bedtime": "My bedtime", "my_dinner": "What's for dinner", "my_weather": "Morning weather",
}


def hm(v: str | None, default: int) -> int:
    """'18:15' -> minutes after midnight."""
    try:
        h, m = (v or "").split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return default


def clock(minutes: int) -> str:
    h, m = divmod(minutes % 1440, 60)
    ap = "am" if h < 12 else "pm"
    return f"{h % 12 or 12}:{m:02d} {ap}" if m else f"{h % 12 or 12} {ap}"


def _iso_local(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


class Ctx:
    """Everything a rule needs for one person at one moment."""

    def __init__(self, smart: "Smart", s: "Subject", now: datetime):
        self.smart, self.hub, self.s, self.now = smart, smart.hub, s, now
        self.m = now.hour * 60 + now.minute
        self.today = now.date().isoformat()
        self.states = self.hub.home.states
        self.cfg = self.hub.config.smart
        self.wx = smart.weather.data or {}
        self._away: bool | None = None

    def has(self, cap: str) -> bool:
        return self.hub.policy.has(self.s, cap)

    @property
    def adult(self) -> bool:
        return self.s.role in ("admin", "adult")

    @property
    def away(self) -> bool:
        """Everyone is out. Conservative: phone presence must say so, and nobody may be showing
        signs of being home (this person on the home network, a child's device online)."""
        if self._away is None:
            if self.s.at_home or self.smart.kids_online:
                self._away = False
            else:
                self._away = self.hub.alerts.nobody_home() or self.hub.kitchen._everyone_away()
        return self._away

    def unlocked(self) -> list[tuple[str, list[str]]]:
        """Unlocked doors, one row per door: Home Assistant can list one lock under two entities."""
        by_name: dict[str, list[str]] = {}
        for e in self.of_kind("lock"):
            if self.states[e].get("state") == "unlocked":
                by_name.setdefault(self.name(e).strip().lower(), []).append(e)
        return [(self.name(eids[0]), eids) for eids in by_name.values()]

    def name(self, eid: str) -> str:
        return (self.states.get(eid, {}).get("attributes") or {}).get("friendly_name", eid)

    def of_kind(self, kind: str) -> list[str]:
        return sorted(e for e, st in self.states.items() if kind_for(st) == kind)

    def between(self, start: int, end: int) -> bool:
        """Inside [start, end) minutes, wrapping past midnight."""
        return start <= self.m < end if start <= end else (self.m >= start or self.m < end)


class Smart:
    REFRESH_S = 120

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.weather = Weather(hub)
        self.chores: dict[str, dict[str, Any]] = {}     # person id -> normalized today
        self.trips: list[dict[str, Any]] = []
        self.dinner_oven: dict[str, Any] = {}             # {"slug": ..., "oven": plan|None}
        self.dinner_ideas: dict[str, Any] = {}            # {"day": iso, "items": [{title, slug}]}
        self.internet: dict[str, dict[str, Any]] = {}     # child person id -> {managed, paused}
        self.kids_online = False                          # a child's device is on the home network now
        self.habits: dict[str, list[dict[str, Any]]] = {}  # person id -> habits
        self._task: asyncio.Task | None = None
        self._habits_at = 0.0

    # -- background --------------------------------------------------------
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
        await asyncio.sleep(5)
        while True:
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("smart refresh failed: %s", exc)
            await asyncio.sleep(self.REFRESH_S)

    async def refresh(self) -> None:
        h = self.hub
        jobs: list[tuple[str, Callable[[], Any]]] = [("weather", self.weather.refresh)]
        if h.config.smart.weather_alerts:
            jobs.append(("weather alerts", h.wx_watch.check))
        if h.family_board.enabled:
            jobs.append(("chores", self._refresh_chores))
        if h.trek.enabled:
            jobs.append(("trips", self._refresh_trips))
        if h.mealie.enabled:
            jobs.append(("dinner", self._refresh_dinner))
        if h.firewalla.enabled:
            jobs.append(("internet", self._refresh_internet))
        for name, job in jobs:
            try:
                await job()
            except Exception as exc:  # noqa: BLE001
                log.info("smart %s: %s", name, exc)
        if time.time() - self._habits_at > 600:
            self._habits_at = time.time()
            try:
                self.learn_habits()
            except Exception as exc:  # noqa: BLE001
                log.info("smart habits: %s", exc)
        self.hub.bus.publish({"type": "smart", "data": {}})

    async def _refresh_chores(self) -> None:
        from .adapters.family import FamilyBoard

        out = {}
        for p in self.hub.db.people():
            if p["role"] in ("child", "adult", "admin") and p.get("family_board_id"):
                try:
                    out[p["id"]] = FamilyBoard.normalize_today(await self.hub.family_board.today(p["family_board_id"]))
                except Exception as exc:  # noqa: BLE001
                    log.info("chores for %s: %s", p["name"], exc)
        self.chores = out

    async def _refresh_trips(self) -> None:
        from .adapters import trips as shape

        today = self.hub.policy.now().date()
        self.trips = [shape.summary(t, today) for t in await self.hub.trek.trips()]

    async def _refresh_dinner(self) -> None:
        d = self.hub.home.dinner
        if d and d.get("slug"):
            if self.dinner_oven.get("slug") != d["slug"]:
                r = await self.hub.mealie.recipe(d["slug"])
                self.dinner_oven = {"slug": d["slug"], "oven": r.get("oven"), "title": r.get("title")}
        else:
            self.dinner_oven = {}
        day = self.hub.policy.now().date().isoformat()
        if not d and self.dinner_ideas.get("day") != day:
            res = await self.hub.mealie.recipes(per_page=60)
            items = [{"title": r.get("title") or r.get("name"), "slug": r.get("slug")} for r in res.get("items", [])]
            items = [i for i in items if i["title"] and i["slug"]]
            random.Random(day).shuffle(items)
            self.dinner_ideas = {"day": day, "items": items[:3]}

    async def _refresh_internet(self) -> None:
        out = {}
        for p in self.hub.db.people_with_role("child"):
            try:
                out[p["id"]] = await self.hub.firewalla.internet_state(p["id"])
            except Exception as exc:  # noqa: BLE001
                log.info("internet state for %s: %s", p["name"], exc)
        self.internet = out
        # The kids' tablets and phones are Wi-Fi only, so one online on the Firewalla means someone is home.
        try:
            devs = await self.hub.firewalla.devices()
            self.kids_online = any(d.get("online") and self.hub.alerts.kid_for_device(d) for d in devs)
        except Exception as exc:  # noqa: BLE001
            log.info("kids' devices: %s", exc)

    # -- learned habits -------------------------------------------------------
    def learn_habits(self, now: float | None = None) -> None:
        """Things a person does on most weekdays (or weekend days) within the same half hour."""
        now = now or time.time()
        tz = self.hub.policy.tz
        since = now - 28 * 86400
        rows = self.hub.db.all(
            "SELECT ts, person_id, action, target, params FROM activity WHERE ts > ? AND outcome = 'ok' "
            "AND person_id IS NOT NULL AND action IN (" + ",".join("?" * len(HABIT_ACTIONS)) + ")",
            [since, *HABIT_ACTIONS])
        groups: dict[tuple[str, str, str, str, bool], list[datetime]] = {}
        for r in rows:
            dt = datetime.fromtimestamp(r["ts"], tz)
            params = r["params"] if isinstance(r["params"], str) else json.dumps(r["params"] or {}, sort_keys=True)
            try:
                params = json.dumps(json.loads(params or "{}"), sort_keys=True)
            except ValueError:
                continue
            weekend = dt.weekday() >= 5
            groups.setdefault((r["person_id"], r["action"], r["target"] or "", params, weekend), []).append(dt)
        out: dict[str, list[dict[str, Any]]] = {}
        today = datetime.fromtimestamp(now, tz).date()
        for (pid, action, target, params, weekend), dts in groups.items():
            # Cluster by time of day around the median of each 40-minute bucket.
            by_day: dict[Any, int] = {}
            for dt in dts:
                by_day.setdefault(dt.date(), dt.hour * 60 + dt.minute)
            mins = sorted(by_day.values())
            if len(mins) < 4:
                continue
            med = int(statistics.median(mins))
            days = [d for d, mm in by_day.items() if abs(mm - med) <= 25]
            eligible = sum(1 for i in range(1, 29) if ((today - timedelta(days=i)).weekday() >= 5) == weekend)
            if len(days) < 4 or len(days) < 0.4 * eligible:
                continue
            out.setdefault(pid, []).append({
                "action": action, "target": target or None, "params": json.loads(params), "minute": med,
                "days": len(days), "eligible": eligible, "weekend": weekend,
                "last": max(dts).isoformat(),
            })
        self.habits = out

    # -- per-person hide / mute ---------------------------------------------------
    def _hide_key(self, pid: str) -> str:
        return f"smart.hide.{pid}"

    def hidden(self, pid: str | None) -> dict[str, Any]:
        if not pid:
            return {"until": {}, "strikes": {}, "muted": []}
        v = self.hub.db.kv_get(self._hide_key(pid)) or {}
        return {"until": v.get("until", {}), "strikes": v.get("strikes", {}), "muted": v.get("muted", [])}

    def dismiss(self, s: "Subject", card_id: str, rule: str, handled: bool, now: datetime | None = None) -> dict[str, Any]:
        if not s.person_id:
            return {"ok": True}
        now = now or self.hub.policy.now()
        v = self.hidden(s.person_id)
        if rule in SAFETY_RULES and not handled:
            until = now + timedelta(minutes=30)
        else:
            until = (now - timedelta(hours=4)).replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=1)
        v["until"][card_id] = until.timestamp()
        v["until"] = {k: t for k, t in v["until"].items() if t > now.timestamp()}
        muted_now = False
        if handled:
            v["strikes"].pop(rule, None)
        elif rule not in SAFETY_RULES:
            v["strikes"][rule] = int(v["strikes"].get(rule, 0)) + 1
            if v["strikes"][rule] >= 3 and rule not in v["muted"]:
                v["muted"].append(rule)
                v["strikes"].pop(rule, None)
                muted_now = True
        self.hub.db.kv_set(self._hide_key(s.person_id), v)
        self.hub.bus.publish({"type": "smart", "data": {}})
        return {"ok": True, "muted": muted_now, "rule_title": RULE_TITLES.get(rule, rule)}

    def set_muted(self, s: "Subject", rule: str, muted: bool) -> list[str]:
        if not s.person_id:
            return []
        v = self.hidden(s.person_id)
        if muted and rule not in v["muted"]:
            v["muted"].append(rule)
        if not muted:
            v["muted"] = [r for r in v["muted"] if r != rule]
            v["strikes"].pop(rule, None)
        self.hub.db.kv_set(self._hide_key(s.person_id), v)
        self.hub.bus.publish({"type": "smart", "data": {}})
        return v["muted"]

    def settings(self, s: "Subject") -> dict[str, Any]:
        v = self.hidden(s.person_id)
        rules = KID_RULES if s.role == "child" else ADULT_RULES
        names = [r.__name__.removeprefix("r_") for r in rules]
        return {"muted": [{"rule": r, "title": RULE_TITLES.get(r, r)} for r in v["muted"]],
                "rules": [{"rule": n, "title": RULE_TITLES.get(n, n)} for n in names]}

    # -- building cards ---------------------------------------------------------------
    def button(self, c: Ctx, title: str, action: str, target: str | None = None, params: dict[str, Any] | None = None,
               primary: bool = True, done: str | None = None) -> dict[str, Any] | None:
        """An action button, or None when this person may not do it."""
        from .actions import ActionError

        try:
            plan = self.hub.actions.plan(c.s, action, target, params or {})
        except ActionError:
            return None
        if not plan.decision.allowed:
            return None
        b = {"title": title, "kind": "action", "action": action, "target": target, "params": params or {},
             "primary": primary, "secure": plan.decision.tier in ("secure", "admin")}
        if done:
            b["done"] = done
        return b

    @staticmethod
    def link(title: str, url: str, primary: bool = False) -> dict[str, Any]:
        return {"title": title, "kind": "open", "url": url, "primary": primary}

    @staticmethod
    def call(title: str, method: str, path: str, primary: bool = False, done: str | None = None) -> dict[str, Any]:
        return {"title": title, "kind": "call", "method": method, "path": path, "primary": primary, "done": done}

    @staticmethod
    def ok(title: str = "Done", primary: bool = True, done: str | None = None) -> dict[str, Any]:
        return {"title": title, "kind": "done", "primary": primary, "done": done}

    def cards(self, s: "Subject", now: datetime | None = None) -> list[dict[str, Any]]:
        if not self.hub.config.smart.enabled or s.role in ("pending",):
            return []
        c = Ctx(self, s, now or self.hub.policy.now())
        hidden = self.hidden(s.person_id)
        off = set(self.hub.config.smart.off_rules) | set(hidden["muted"])
        ts = c.now.timestamp()
        out: list[dict[str, Any]] = []
        little = s.role == "child" and self._ui_style(s) == "little"
        rules = KID_RULES if s.role == "child" else ADULT_RULES if s.role in ("admin", "adult", "tablet") else []
        for rule in rules:
            name = rule.__name__.removeprefix("r_")
            if name in off:
                continue
            try:
                got = rule(self, c)
            except Exception as exc:  # noqa: BLE001
                log.info("rule %s failed: %s", name, exc)
                continue
            for card in (got if isinstance(got, list) else [got]):
                if not card:
                    continue
                card.setdefault("rule", name)
                if hidden["until"].get(card["id"], 0) > ts:
                    continue
                card["buttons"] = [b for b in card.get("buttons", []) if b]
                if not card.get("ref"):
                    ref = default_ref(name, card)
                    if ref:
                        card["ref"] = ref
                out.append(card)
        out.sort(key=lambda x: -x["priority"])
        if little:
            for card in out:
                card["style"] = "picture"
        return out

    def _ui_style(self, s: "Subject") -> str | None:
        p = self.hub.db.person(s.person_id)
        return p.get("ui_style") if p else None


# What a card is about, so tapping the card itself opens it (the app's "web of links").
RULE_REFS = {
    "weather_am": "thomascreek://weather", "my_weather": "thomascreek://weather", "rain": "thomascreek://weather",
    "freeze": "thomascreek://weather", "heat": "thomascreek://weather", "today": "thomascreek://today",
    "kids_chores": "thomascreek://chores", "my_chores": "thomascreek://chores", "dinner": "thomascreek://dinners",
    "my_dinner": "thomascreek://dinners", "bedtime": "thomascreek://bedtime", "trash": "thomascreek://calendar",
    "lockup": "thomascreek://doors", "away_unlocked": "thomascreek://doors", "show": "thomascreek://show",
    "media_ready": "thomascreek://requests",
}


def default_ref(rule: str, c: dict[str, Any]) -> str | None:
    """The card's subject: the one device it names, else its first "open" button, else the rule's screen."""
    parts = str(c.get("id", "")).split(":")
    if rule in ("garage_open", "away_unlocked") and len(parts) > 2 and "." in parts[1]:
        return f"thomascreek://device/{parts[1]}"
    if rule == "habit" and len(parts) > 2 and "." in parts[2]:
        return f"thomascreek://device/{parts[2]}"
    for b in c.get("buttons") or []:
        if b and b.get("kind") == "open" and b.get("url"):
            return b["url"]
    return RULE_REFS.get(rule)


def card(id: str, priority: int, tint: str, symbol: str, eyebrow: str, title: str, *, detail: str | None = None,
         items: list[dict[str, Any]] | None = None, buttons: list[dict[str, Any] | None] | None = None,
         why: str = "", image: str | None = None, emoji: str | None = None, ref: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": id, "priority": priority, "tint": tint, "symbol": symbol, "eyebrow": eyebrow,
                           "title": title, "why": why, "buttons": buttons or []}
    for k, v in (("detail", detail), ("items", items), ("image", image), ("emoji", emoji), ("ref", ref)):
        if v:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Rules for grown-ups (and the wall tablet, which is filtered by its own policy)
# ---------------------------------------------------------------------------

def r_oven_on(sm: Smart, c: Ctx):
    if not c.has("appliances"):
        return None
    out = []
    now = time.time()
    lockup = hm(c.cfg.lockup_time, 1290)
    for eid in sm.hub.kitchen.ovens():
        o = oven_state(c.states.get(eid, {}))
        if not o["on"]:
            continue
        since = sm.hub.kitchen._on_since.get(eid, now)
        hours = (now - since) / 3600
        sess = sm.hub.kitchen.sessions.get(eid)
        timer = bool(sess and sess.timer_ends)
        late = c.between(lockup, 180)
        if timer or (hours < 2 and not late and not c.away):
            continue
        label = sm.hub.kitchen.label(eid)
        temp = f" at {int(o['target_temp'])}°" if o.get("target_temp") else ""
        why = "Nobody's home" if c.away else ("It's late" if late else f"On {int(hours)} h with no timer")
        detail = (f"{label}{temp}, on for {int(hours * 60)} min." if hours < 1
                  else f"{label}{temp}, on for {hours:.1f} h. No timer running.".replace(".0 h", " h"))
        out.append(card(f"oven_on:{eid}:{int(since)}", 100, "bad", "flame.fill", "Kitchen",
                        f"The {label.lower()} is still on", detail=detail,
                        buttons=[sm.button(c, "Turn off", "oven.set", eid, {"on": False}, done=f"{label} off"),
                                 sm.link("Kitchen", f"thomascreek://kitchen/{eid}")],
                        why=why))
    return out


def r_away_unlocked(sm: Smart, c: Ctx):
    if not c.has("doors") or not c.away:
        return None
    doors = c.unlocked()
    if not doors:
        return None
    steps = [{"action": "lock.set", "target": e, "params": {"locked": True}} for _, eids in doors for e in eids]
    if len(doors) == 1:
        name, eids = doors[0]
        btn = (sm.button(c, "Lock it", "lock.set", eids[0], {"locked": True}, done="Locked") if len(eids) == 1 else
               sm.button(c, "Lock it", "steps.run", None, {"steps": steps, "label": "Lock"}, done="Locked"))
        return card(f"away_unlocked:{eids[0]}:{c.today}", 98, "bad", "lock.open.fill", "Nobody's home",
                    f"{name} is unlocked", buttons=[btn],
                    why="Everyone is away and a lock reports unlocked")
    return card(f"away_unlocked:{c.today}", 98, "bad", "lock.open.fill", "Nobody's home",
                f"{len(doors)} doors are unlocked", items=[{"title": n, "value": "unlocked"} for n, _ in doors],
                buttons=[sm.button(c, "Lock all", "steps.run", None, {"steps": steps, "label": "Lock all"}, done="Locked")],
                why="Everyone is away and locks report unlocked")


def _lights_on(sm: Smart, c: Ctx, skip_kids: bool = False) -> list[str]:
    kid_areas: set[str] = set()
    if skip_kids:
        for p in sm.hub.db.people_with_role("child"):
            kid_areas |= set(json.loads(p["areas"]) if isinstance(p["areas"], str) else p["areas"] or [])
    out = []
    for e in c.of_kind("light"):
        area = sm.hub.home.entity_area.get(e)
        if c.states[e].get("state") == "on" and area and area not in kid_areas:
            out.append(e)
    return out


def r_away_lights(sm: Smart, c: Ctx):
    if not c.has("lights.all") or not c.away:
        return None
    lights = _lights_on(sm, c)
    if not lights:
        return None
    steps = [{"action": "light.set", "target": e, "params": {"on": False}} for e in lights]
    names = ", ".join(c.name(e) for e in lights[:4]) + ("…" if len(lights) > 4 else "")
    return card(f"away_lights:{c.today}:{len(lights)}", 70, "warn", "lightbulb.fill", "Nobody's home",
                f"{len(lights)} light{'s are' if len(lights) != 1 else ' is'} on", detail=names,
                buttons=[sm.button(c, "Turn them off", "steps.run", None, {"steps": steps, "label": "Lights off"},
                                   done="Lights off")],
                why="Everyone is away and lights were left on")


def r_garage_open(sm: Smart, c: Ctx):
    if not c.has("doors"):
        return None
    now = time.time()
    out = []
    dark = _after_sunset(c)
    for e in c.of_kind("garage"):
        st = c.states[e]
        if st.get("state") != "open":
            continue
        changed = _ts(st.get("last_changed")) or now
        mins = int((now - changed) / 60)
        if mins < 30 and not (dark and mins >= 10):
            continue
        out.append(card(f"garage_open:{e}:{int(changed)}", 95 if dark else 75, "bad" if dark else "warn",
                        "door.garage.open", "Garage", f"{c.name(e)} is open",
                        detail=f"Open {mins} min" + (" and it's dark." if dark else "."),
                        buttons=[sm.button(c, "Close it", "garage.set", e, {"open": False}, done="Closing")],
                        why="Garage open more than 30 minutes"))
    return out


def r_package(sm: Smart, c: Ctx):
    if not c.has("cameras") or not c.s.person_id:
        return None
    rows = sm.hub.alerts.history(c.s.person_id, kind="package", limit=3)
    out = []
    for r in rows:
        if time.time() - r["ts"] > 4 * 3600:
            continue
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        ev = payload.get("event_id")
        when = datetime.fromtimestamp(r["ts"], sm.hub.policy.tz)
        out.append(card(f"package:{ev or r['id']}", 85, "warn", "shippingbox.fill",
                        f"{r.get('body') or 'Camera'} · {clock(when.hour * 60 + when.minute)}", r["title"],
                        image=r.get("image"), ref=r.get("ref"),
                        buttons=[sm.ok("Got it", done="Marked as picked up"),
                                 sm.call("Not a package", "POST", f"/v1/cameras/events/{ev}/wrong",
                                         done="Thanks. The camera will learn from it.") if ev else None],
                        why="Frigate saw a package in the last 4 hours"))
        break
    return out


def r_wx_alert(sm: Smart, c: Ctx):
    """An official warning or watch for the house is the first thing a grown-up sees."""
    alerts = [a for a in sm.hub.forecasts.cached_home_alerts() if a["level"] in ("warning", "watch")]
    if not alerts:
        return None
    a = alerts[0]
    warn = a["level"] == "warning"
    ends = _iso_local((a.get("ends") or "")[:19])
    eyebrow = ("Until " + clock(ends.hour * 60 + ends.minute)) if ends else ("Warning" if warn else "Watch")
    detail = (a.get("headline") or "").replace("\n", " ").strip() or None
    if detail and len(detail) > 160:
        detail = detail[:157].rsplit(" ", 1)[0] + "…"
    more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
    return card(f"wx_alert:{a['id']}", 95 if warn else 70, "bad" if warn else "warn", "exclamationmark.triangle.fill",
                eyebrow, a["event"] + more, detail=detail, ref=f"thomascreek://weather/alert/{a['id']}",
                buttons=[sm.ok("Got it", done="OK")],
                why="An official weather " + ("warning" if warn else "watch") + " covers the house")


def r_weather_am(sm: Smart, c: Ctx):
    w = c.wx
    if not w or not c.between(300, 600) or w.get("temp") is None:
        return None
    hi, lo = w.get("high"), w.get("tonight_low") or w.get("low")
    temp = round(w["temp"])
    rain_at = _iso_local(w.get("rain_at"))
    tint, sym = "sun", w.get("symbol") or "sun.max.fill"
    if rain_at:
        title = f"Rain around {clock(rain_at.hour * 60)}"
        detail = f"{temp}° now, high {round(hi)}°. Grab an umbrella." if hi is not None else "Grab an umbrella."
        tint = "kid"
    elif hi is not None and hi >= 90:
        title = f"Hot one: {round(hi)}° this afternoon"
        detail = "Keep the blinds down on the sunny side."
        tint = "bad"
    elif lo is not None and lo <= 32:
        title = f"Cold morning, {temp}°"
        detail = f"Freezing tonight, low {round(lo)}°. Coats for the kids."
        tint = "kid"
    else:
        title = f"{w.get('condition') or 'Today'}, {temp}°"
        detail = f"High {round(hi)}°, low {round(w['low'])}°." if hi is not None and w.get("low") is not None else None
    return card(f"weather_am:{c.today}", 60, tint, sym, "This morning", title, detail=detail,
                why="Morning, with today's forecast")


def r_today(sm: Smart, c: Ctx):
    if not c.has("family.view") or not c.between(300, 600):
        return None
    events = sm.hub.home.calendar or []
    if not events:
        return None
    items = []
    for e in events[:4]:
        st = _iso_local(e.get("start"))
        when = "all day" if e.get("all_day") or not st else clock(st.hour * 60 + st.minute)
        items.append({"title": e.get("title") or "Event", "value": when})
    n = len(events)
    return card(f"today:{c.today}", 62, "accent", "calendar", "Today", f"{n} thing{'s' if n != 1 else ''} today",
                items=items, buttons=[sm.link("Calendar", "thomascreek://calendar")],
                why="Morning: today's family calendar")


def r_rain(sm: Smart, c: Ctx):
    w = c.wx
    rain_at = _iso_local(w.get("rain_at")) if w else None
    if not rain_at or not c.between(600, 1200):
        return None
    mins = (rain_at.hour * 60 + rain_at.minute) - c.m
    if mins < 0 or mins > 360:
        return None
    lead = "within the hour" if mins < 60 else f"in about {round(mins / 60)} h"
    open_g = [e for e in c.of_kind("garage") if c.states[e].get("state") == "open"] if c.has("doors") else []
    detail = "Bring in anything you left out." + (f" {c.name(open_g[0])} is open." if open_g else "")
    btns = [sm.button(c, "Close garage", "garage.set", open_g[0], {"open": False}, done="Closing")] if open_g else []
    return card(f"rain:{c.today}:{rain_at.hour}", 72, "kid", "cloud.rain.fill", f"Rain {lead}",
                f"Rain around {clock(rain_at.hour * 60)}", detail=detail, buttons=btns,
                why="Forecast shows rain likely in the next hours")


def r_freeze(sm: Smart, c: Ctx):
    w = c.wx
    lo = w.get("tonight_low") if w else None
    if lo is None or lo > 32 or not c.between(900, 1380):
        return None
    return card(f"freeze:{c.today}", 74, "kid", "snowflake", f"Low {round(lo)}° tonight", "Freezing tonight",
                detail="Cover tender plants, bring in hoses, and check that outdoor taps are off.",
                why="Forecast low at or below 32°")


def r_heat(sm: Smart, c: Ctx):
    w = c.wx
    if not w or w.get("temp") is None or w["temp"] < 88 or not c.between(720, 1110) or not c.has("climate.all"):
        return None
    climates = c.of_kind("climate")
    if c.away and climates:
        steps = [{"action": "climate.set", "target": e, "params": {"hvac": "cool", "target_temp": 72}} for e in climates]
        return card(f"heat:{c.today}", 65, "bad", "thermometer.sun.fill", f"It's {round(w['temp'])}° out",
                    "Cool the house before you're home?", detail="Sets the thermostats to cool to 72°.",
                    buttons=[sm.button(c, "Cool to 72°", "steps.run", None, {"steps": steps, "label": "Pre-cool"},
                                       done="Cooling to 72°")],
                    why="Hot afternoon and nobody's home")
    return None


def r_kids_chores(sm: Smart, c: Ctx):
    if not c.has("chores.manage") or not c.between(900, 1170):
        return None
    items = []
    for p in sm.hub.db.people_with_role("child"):
        t = sm.chores.get(p["id"])
        if not t:
            continue
        left = [i for i in t["items"] if not i["done"] and not i["waiting"]]
        waiting = [i for i in t["items"] if i["waiting"]]
        if left or waiting:
            v = f"{len(left)} left" + (f", {len(waiting)} to check" if waiting else "")
            items.append({"title": p["name"], "value": v})
    if not items:
        return None
    return card(f"kids_chores:{c.today}", 58, "kid", "checklist", "After school", "Kids' chores",
                items=items, buttons=[sm.link("Chores", "thomascreek://chores", primary=True)],
                why="Afternoon, with chores still open on the family board")


def r_dinner(sm: Smart, c: Ctx):
    dinner_m = hm(c.cfg.dinner_time, 1095)
    if not c.between(dinner_m - c.cfg.dinner_lead_min, dinner_m + 30) or not (c.has("appliances") or c.has("family.view")):
        return None
    d = sm.hub.home.dinner
    when = clock(dinner_m)
    if not d:
        if not c.has("family.edit"):
            return None
        ideas = (sm.dinner_ideas.get("items") or [])[:3]
        return card(f"dinner:{c.today}:none", 72, "warn", "fork.knife", f"Dinner · {when}", "Nothing planned for tonight",
                    detail="A few from your recipes:" if ideas else None,
                    items=[{"title": i["title"], "value": "", "url": f"thomascreek://recipe/{i['slug']}"} for i in ideas],
                    buttons=[sm.link("Plan dinner", "thomascreek://dinners", primary=True)],
                    why="Dinner time is coming and Mealie has no dinner today")
    title = d.get("title") or "Dinner"
    slug = d.get("slug")
    oven = sm.dinner_oven.get("oven") if sm.dinner_oven.get("slug") == slug else None
    ovens = sm.hub.kitchen.ovens()
    busy = [e for e in ovens if oven_state(c.states.get(e, {}))["on"]]
    recipe = sm.link("Recipe", f"thomascreek://recipe/{slug}") if slug else None
    if oven and oven.get("temp") and ovens and c.has("appliances") and not busy:
        mins = int(oven.get("minutes") or 30)
        start = dinner_m - mins - 15
        late = c.m >= start
        upper = next((e for e in ovens if sm.hub.kitchen.label(e) == "Upper oven"), ovens[0])
        params = {"mode": oven.get("mode") or "Bake", "temp": int(oven["temp"]), "timer_min": mins,
                  "timer_from": "preheat", "auto_off": False, "recipe": title}
        if late:
            ready = max(dinner_m, c.m + 15 + mins)
            detail = f"{oven.get('mode') or 'Bake'} {int(oven['temp'])}°F, {mins} min. Preheat now to eat by {clock(ready)}."
        else:
            detail = f"Needs {int(oven['temp'])}°F for {mins} min. Start the oven by {clock(start)} to eat at {when}."
        return card(f"dinner:{c.today}:{slug}", 88 if late else 80, "warn", "oven.fill",
                    "Preheat now" if late else f"Dinner · {when}", title, detail=detail,
                    image=d.get("image_url"),
                    buttons=[sm.button(c, f"Preheat to {int(oven['temp'])}°", "oven.set", upper, params,
                                       done=f"Preheating to {int(oven['temp'])}°"), recipe],
                    why="Tonight's Mealie dinner, its oven setting and your dinner time")
    if busy:
        return None  # the oven is already going; the Kitchen screen has the rest
    return card(f"dinner:{c.today}:{slug}", 60, "warn", "fork.knife", f"Dinner · {when}", title,
                image=d.get("image_url"), buttons=[recipe],
                why="Tonight's Mealie dinner")


def r_habit(sm: Smart, c: Ctx):
    if not c.s.person_id:
        return None
    weekend = c.now.weekday() >= 5
    out = []
    for hb in sm.habits.get(c.s.person_id, []):
        if hb["weekend"] != weekend or not (hb["minute"] - 15 <= c.m <= hb["minute"] + 20):
            continue
        if hb["last"][:10] == c.today:
            continue  # already done today
        target = hb["target"]
        st = c.states.get(target or "")
        p = hb["params"]
        if hb["action"] == "light.set" and st:
            want_on = p.get("on", True) is not False
            if (st.get("state") == "on") == want_on and "brightness" not in p:
                continue
        if hb["action"] in ("switch.set", "fan.set") and st and (st.get("state") == "on") == (p.get("on", True) is not False):
            continue
        label = _habit_label(sm, c, hb)
        out.append(card(f"habit:{hb['action']}:{target}:{c.today}", 40, "accent", "clock.arrow.circlepath",
                        "Usually about now", label,
                        detail=f"You've done this {hb['days']} {'weekend days' if weekend else 'weekdays'} "
                               f"of the last 4 weeks, around {clock(hb['minute'])}.",
                        buttons=[sm.button(c, "Do it", hb["action"], target, p, done="Done")],
                        why="Learned from what you tap"))
        if len(out) >= 2:
            break
    return out


def _habit_label(sm: Smart, c: Ctx, hb: dict[str, Any]) -> str:
    p, target, action = hb["params"], hb["target"], hb["action"]
    if action == "mode.run":
        mode = next((m for m in sm.hub.config.modes if m.id == target), None)
        return f"Run {mode.name if mode else target}"
    name = c.name(target) if target else "it"
    if action == "scene.run":
        return f"Set the {name} scene"
    if action == "climate.set" and p.get("target_temp"):
        return f"{name} to {p['target_temp']}°"
    if p.get("on") is False:
        return f"Turn off {name}"
    if p.get("brightness") is not None:
        return f"{name} to {p['brightness']}%"
    return f"Turn on {name}"


def r_show(sm: Smart, c: Ctx):
    if not sm.hub.show_player.enabled or c.now.month not in (11, 12, 1) or not (c.has("holiday.looks") or c.has("holiday.full")):
        return None
    sunset = _sunset_m(c)
    if sunset is None or not c.between(sunset - 20, sunset + 45):
        return None
    return card(f"show:{c.today}", 55, "night", "sparkles", f"Sunset {clock(sunset)}", "Holiday lights tonight",
                buttons=[sm.link("Open the show", "thomascreek://show", primary=True)],
                why="Holiday season, around sunset")


def _bedtime(sm: Smart, p: dict[str, Any]) -> int:
    bt = sm.hub.config.smart.bedtimes
    v = bt.get(p["name"]) or bt.get(p["name"].split()[0]) or bt.get(p["id"])
    if v:
        return hm(v, 1230)
    return 1170 if p.get("ui_style") == "little" else hm(sm.hub.config.child_policy.bedtime, 1230)


def r_bedtime(sm: Smart, c: Ctx):
    if not c.has("network.manage"):
        return None
    out = []
    wake = hm(c.cfg.wake_time, 420)
    for p in sm.hub.db.people_with_role("child"):
        bt = _bedtime(sm, p)
        if not c.between(bt - 30, bt + 60):
            continue
        net = sm.internet.get(p["id"]) or {}
        if not net.get("managed") or net.get("paused"):
            continue
        until = (wake - c.m) % 1440
        first = p["name"].split()[0]
        out.append(card(f"bedtime:{p['id']}:{c.today}", 78, "night", "moon.zzz.fill", f"{first} · bedtime {clock(bt)}",
                        f"Pause {first}'s internet for the night?" if c.m >= bt else f"{first}'s bedtime in {bt - c.m} min",
                        detail=f"Pauses {first}'s devices until {clock(wake)}.",
                        buttons=[sm.button(c, "Pause until morning", "network.pause", p["id"], {"minutes": until},
                                           done=f"{first}'s internet paused"),
                                 sm.link("Kids online", "thomascreek://kids")],
                        why="Bedtime is close and their devices are online"))
    return out


def r_trash(sm: Smart, c: Ctx):
    night = (c.cfg.trash_night or "").lower()
    if not night or c.now.strftime("%A").lower() != night or not c.between(1080, 1439):
        return None
    if not c.adult:
        return None
    return card(f"trash:{c.today}", 50, "accent", "trash.fill", "Tonight", "Trash and recycling go out",
                detail="Pickup is tomorrow morning.", buttons=[sm.ok("Done", done="Nice. See you next week.")],
                why="It's trash night")


def r_lockup(sm: Smart, c: Ctx):
    start = hm(c.cfg.lockup_time, 1290)
    if not c.between(start, 180) or not c.adult:
        return None
    items, steps = [], []
    if c.has("doors"):
        for name, eids in c.unlocked():
            items.append({"title": name, "value": "unlocked"})
            steps += [{"action": "lock.set", "target": e, "params": {"locked": True}} for e in eids]
        for e in c.of_kind("garage"):
            if c.states[e].get("state") == "open":
                items.append({"title": c.name(e), "value": "open"})
                steps.append({"action": "garage.set", "target": e, "params": {"open": False}})
    lights = _lights_on(sm, c, skip_kids=True) if c.has("lights.all") else []
    if lights:
        items.append({"title": "Lights", "value": f"{len(lights)} on"})
        steps += [{"action": "light.set", "target": e, "params": {"on": False}} for e in lights]
    if not steps:
        return None
    mode = c.cfg.goodnight_mode
    good = (sm.button(c, "Goodnight", "mode.run", mode, {}, done="Goodnight") if mode else
            sm.button(c, "Goodnight", "steps.run", None, {"steps": steps, "label": "Goodnight"}, done="All set. Goodnight."))
    lock_steps = [st for st in steps if st["action"] in ("lock.set", "garage.set")]
    just = (sm.button(c, "Just doors", "steps.run", None, {"steps": lock_steps, "label": "Doors"}, primary=False,
                      done="Doors done") if lock_steps and len(lock_steps) < len(steps) else None)
    title = "Lock up for the night" if lock_steps else "Lights still on"
    return card(f"lockup:{c.today}", 90, "night", "lock.fill", "Night", title, items=items, buttons=[good, just],
                why="It's late and doors or lights aren't set for the night")


def r_media_ready(sm: Smart, c: Ctx):
    if not c.s.person_id:
        return None
    rows = sm.hub.db.all("SELECT * FROM requests WHERE kind='media' AND status='ready' AND requester_id=? "
                         "ORDER BY created_at DESC LIMIT 5", [c.s.person_id])
    seen = sm.hub.db.kv_get("smart.media_ready") or {}
    now = time.time()
    changed = False
    out = []
    for r in rows:
        first = seen.get(r["id"])
        if first is None:
            seen[r["id"]] = first = now
            changed = True
        if now - first > 2 * 86400:
            continue
        kid = c.s.role == "child"
        out.append(card(f"media_ready:{r['id']}", 65 if kid else 52, "kid" if kid else "accent", "play.tv.fill",
                        "You asked for it" if kid else "Ready on Plex", r.get("title") or "Your request",
                        detail="It's ready to watch." if kid else "Your request finished downloading.",
                        image=r.get("poster_url"),
                        buttons=[sm.link("Watch", "thomascreek://requests", primary=True)],
                        why="Something you asked for is ready", emoji="🎬"))
        if len(out) >= 2:
            break
    if changed:
        sm.hub.db.kv_set("smart.media_ready", {k: v for k, v in seen.items() if now - v < 7 * 86400})
    return out


def r_trip(sm: Smart, c: Ctx):
    if not c.has("trips.view"):
        return None
    for t in sm.trips:
        if t.get("status") == "upcoming" and 1 <= (t.get("days_until") or 99) <= 3:
            n = t["days_until"]
            return card(f"trip:{t['id']}:{c.today}", 56, "accent", "airplane", "Tomorrow" if n == 1 else f"In {n} days",
                        t["title"], detail="Time to start packing." if n > 1 else "Pack tonight. Check doors before you go.",
                        buttons=[sm.link("Trip", f"thomascreek://trip/{t['id']}", primary=True)],
                        why="A trip starts soon")
    return None


def _after_sunset(c: Ctx) -> bool:
    s = _sunset_m(c)
    return s is not None and (c.m >= s or c.m < 360)


def _sunset_m(c: Ctx) -> int | None:
    ss = _iso_local((c.wx or {}).get("sunset"))
    if ss:
        return ss.hour * 60 + ss.minute
    sun = c.states.get("sun.sun")
    if sun:
        nxt = _ts((sun.get("attributes") or {}).get("next_setting"))
        if nxt:
            dt = datetime.fromtimestamp(nxt, c.now.tzinfo)
            return dt.hour * 60 + dt.minute
    return None


def _ts(v: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(v)).timestamp() if v else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Rules for children (Theo's cards; Adeline gets the same as picture cards)
# ---------------------------------------------------------------------------

def r_my_chores(sm: Smart, c: Ctx):
    t = sm.chores.get(c.s.person_id or "")
    if not t or not (c.between(360, 510) or c.between(900, 1170)):
        return None
    left = [i for i in t["items"] if not i["done"] and not i["waiting"]]
    if not left:
        return None
    morning = c.m < 600
    n = len(left)
    w = c.wx or {}
    wear = ""
    if morning and w.get("rain_at"):
        wear = " Rain later: bring your jacket!"
    elif morning and w.get("temp") is not None and w["temp"] < 45:
        wear = " It's cold: wear a coat!"
    return card(f"my_chores:{c.today}:{'am' if morning else 'pm'}", 80, "kid", "star.fill",
                "Before school" if morning else "Chores", f"{n} chore{'s' if n != 1 else ''} left",
                detail=(", ".join(i["title"] for i in left[:3]) + "." + wear),
                items=None, buttons=[sm.link("My chores", "thomascreek://chores", primary=True)],
                why="Chores still open on the family board", emoji="⭐️" if not morning else "🌞")


def r_my_dinner(sm: Smart, c: Ctx):
    dinner_m = hm(c.cfg.dinner_time, 1095)
    d = sm.hub.home.dinner
    if not d or not c.between(dinner_m - 60, dinner_m + 15):
        return None
    return card(f"my_dinner:{c.today}", 60, "warn", "fork.knife", f"Dinner at {clock(dinner_m)}", d.get("title") or "Dinner",
                detail="Wash your hands!", image=d.get("image_url"), why="Tonight's dinner", emoji="🍽️")


def r_my_bedtime(sm: Smart, c: Ctx):
    p = sm.hub.db.person(c.s.person_id)
    if not p:
        return None
    bt = _bedtime(sm, p)
    if not c.between(bt - 45, bt + 30):
        return None
    left = bt - c.m
    return card(f"my_bedtime:{c.today}", 90, "night", "moon.stars.fill", f"Bedtime {clock(bt)}",
                "It's bedtime" if left <= 0 else f"Bedtime in {left} min",
                detail="Brush your teeth and get your pajamas on.", why="Your bedtime is close", emoji="🌙")


def r_my_weather(sm: Smart, c: Ctx):
    w = c.wx
    if not w or w.get("temp") is None or not c.between(360, 540):
        return None
    t = round(w["temp"])
    if w.get("rain_at"):
        title, emoji = "Rainy day: rain boots!", "☔️"
    elif t < 40:
        title, emoji = f"Brrr, {t}°: hat and coat!", "🧣"
    elif t > 85:
        title, emoji = f"Hot, {t}°: drink water!", "🥤"
    else:
        title, emoji = f"{w.get('condition') or 'Nice'}, {t}°", "🌤️"
    return card(f"weather_am:{c.today}", 50, "sun", w.get("symbol") or "sun.max.fill", "Good morning!", title,
                why="Morning weather", emoji=emoji)


ADULT_RULES = [r_wx_alert, r_oven_on, r_away_unlocked, r_away_lights, r_garage_open, r_package, r_weather_am, r_today, r_rain,
               r_freeze, r_heat, r_kids_chores, r_dinner, r_habit, r_show, r_bedtime, r_trash, r_lockup, r_media_ready,
               r_trip]
KID_RULES = [r_my_weather, r_my_chores, r_my_dinner, r_my_bedtime, r_media_ready, r_trip]
