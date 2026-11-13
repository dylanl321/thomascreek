"""Roles, capabilities and the action policy.

Everything is denied unless a rule allows it. A role is a starting bundle of
capabilities; each person can then be widened (grants) or narrowed (denies).
The app hides what a person cannot do using `capabilities_for`, and the hub
refuses it anyway in `decide`.

Tiers (from the plan):
  look      status and reading                         no extra step
  everyday  lights, scenes, music, TV, thermostat      no extra step
  secure    garage, unlock, cameras away, ovens,
            a child's device time                       Face ID (biometric key signature)
  admin     restart apps, people and permissions       Face ID (biometric key signature)
  never     delete storage, firewall rules, wipe        not exposed at all
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROLES = ("admin", "adult", "child", "guest", "tablet", "pending")

ALL_CAPABILITIES = (
    "lights.all", "lights.own_room", "climate.all", "climate.own_room", "doors", "cameras",
    "media.all", "media.own_tv", "media.request", "media.approve", "pipeline",
    "holiday.full", "holiday.looks", "holiday.pick", "family.edit", "family.view",
    "shopping.add", "chores.manage", "chores.do", "inventory.edit", "inventory.search",
    "appliances", "network.manage", "network.ask", "systems", "people", "activity",
    "trips.view", "trips.plan", "guests",
)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "admin": frozenset(ALL_CAPABILITIES),
    "adult": frozenset({
        "lights.all", "climate.all", "doors", "cameras", "media.all", "media.request",
        "media.approve", "holiday.looks", "family.edit", "family.view", "shopping.add",
        "chores.manage", "inventory.edit", "inventory.search", "appliances", "network.manage",
        "trips.view", "trips.plan", "guests",
    }),
    "child": frozenset({
        "lights.own_room", "climate.own_room", "media.own_tv", "media.request", "holiday.pick",
        "family.view", "shopping.add", "chores.do", "inventory.search", "network.ask",
        "trips.view",
    }),
    # A guest gets "own room" style control of their area (e.g. the apartment).
    "guest": frozenset({"lights.own_room", "climate.own_room", "media.own_tv"}),
    # Shared wall screen: everyday adult controls, nothing private.
    "tablet": frozenset({
        "lights.all", "climate.all", "media.all", "holiday.looks", "family.view",
        "shopping.add", "inventory.search", "chores.do", "trips.view",
    }),
    # A phone nobody has been assigned to yet: the child's limits, no room.
    "pending": frozenset({"family.view", "holiday.pick"}),
}

TIERS = ("look", "everyday", "secure", "admin")


@dataclass
class Subject:
    """Who is asking."""

    person_id: str | None
    role: str
    areas: list[str] = field(default_factory=list)         # own room(s) / guest area
    shared_areas: list[str] = field(default_factory=list)  # parent-picked shared spaces
    grants: list[dict[str, Any]] = field(default_factory=list)
    denies: list[dict[str, Any]] = field(default_factory=list)
    tv_device: str | None = None
    at_home: bool = False
    device_id: str | None = None
    name: str | None = None

    @property
    def is_child_like(self) -> bool:
        return self.role in ("child", "pending", "guest")


@dataclass
class Target:
    entity_id: str | None = None
    area: str | None = None
    kind: str | None = None


@dataclass
class Decision:
    allowed: bool
    tier: str = "everyday"
    reason: str = ""
    ask_parent: bool = False
    # Parameter adjustments the policy insists on (e.g. clamp a child's thermostat).
    clamp: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionSpec:
    name: str
    tier: str
    # Capabilities that allow it anywhere / only in own areas.
    any_caps: tuple[str, ...] = ()
    own_caps: tuple[str, ...] = ()
    # Optional per-action tier override, e.g. locking is everyday but unlocking is secure.
    tier_fn: Callable[[dict[str, Any], Subject], str] | None = None
    # Child can ask for it instead (becomes a request to a parent).
    askable: bool = False


DOOR_OPENER = re.compile(r"garage_?door|gate|door_?opener", re.I)


def is_door_opener(entity_id: str | None) -> bool:
    return bool(entity_id) and entity_id.startswith("switch.") and bool(DOOR_OPENER.search(entity_id))


def _lock_tier(params: dict[str, Any], _s: Subject) -> str:
    return "everyday" if params.get("locked", True) else "secure"


def _oven_tier(params: dict[str, Any], _s: Subject) -> str:
    # Starting heat needs Face ID; turning an oven off never should.
    return "everyday" if params.get("on") is False else "secure"


def _printer_tier(params: dict[str, Any], _s: Subject) -> str:
    # Stopping throws the print away, so it needs Face ID like starting one.
    return "secure" if params.get("command") == "stop" else "everyday"


def _garage_tier(params: dict[str, Any], _s: Subject) -> str:
    return "secure" if params.get("open") else "everyday"


ACTIONS: dict[str, ActionSpec] = {
    a.name: a
    for a in [
        ActionSpec("light.set", "everyday", ("lights.all",), ("lights.own_room",), askable=True),
        ActionSpec("fan.set", "everyday", ("lights.all",), ("lights.own_room",), askable=True),
        ActionSpec("switch.set", "everyday", ("lights.all",), ("lights.own_room",), askable=True),
        ActionSpec("scene.run", "everyday", ("lights.all",), ("lights.own_room",), askable=True),
        ActionSpec("mode.run", "everyday", ("lights.all",)),
        ActionSpec("climate.set", "everyday", ("climate.all",), ("climate.own_room",), askable=True),
        ActionSpec("lock.set", "secure", ("doors",), tier_fn=_lock_tier),
        ActionSpec("garage.set", "secure", ("doors",), tier_fn=_garage_tier),
        ActionSpec("media.command", "everyday", ("media.all",), ("media.own_tv",)),
        ActionSpec("media.volume", "everyday", ("media.all",), ("media.own_tv",)),
        ActionSpec("media.play", "everyday", ("media.all",), ("media.own_tv",)),
        ActionSpec("speaker.group", "everyday", ("media.all",)),
        ActionSpec("announce", "everyday", ("family.edit",)),
        ActionSpec("valve.run", "everyday", ("appliances",)),
        ActionSpec("vacuum.start", "everyday", ("appliances",)),
        ActionSpec("oven.set", "secure", ("appliances",), tier_fn=_oven_tier),
        ActionSpec("oven.timer", "everyday", ("appliances",)),
        ActionSpec("shower.preheat", "secure", ("appliances",)),
        ActionSpec("printer.control", "everyday", ("appliances",), tier_fn=_printer_tier),
        ActionSpec("printer.light", "everyday", ("appliances",)),
        ActionSpec("printer.speed", "everyday", ("appliances",)),
        ActionSpec("printer.temp", "everyday", ("appliances",)),
        ActionSpec("printer.print", "secure", ("appliances",)),
        ActionSpec("camera.view", "look", ("cameras",)),
        ActionSpec("network.pause", "secure", ("network.manage",)),
        ActionSpec("network.grant", "secure", ("network.manage",)),
        ActionSpec("media.approve", "everyday", ("media.approve",)),
        ActionSpec("request.decide", "everyday", ("media.approve", "chores.manage", "network.manage")),
        ActionSpec("system.app_restart", "admin", ("systems",)),
        ActionSpec("nas.app", "admin", ("systems",)),
        ActionSpec("nas.alert", "admin", ("systems",)),
        ActionSpec("nas.scrub", "admin", ("systems",)),
        ActionSpec("nas.snapshot", "admin", ("systems",)),
        ActionSpec("nas.service", "admin", ("systems",)),
        ActionSpec("people.edit", "admin", ("people",)),
        ActionSpec("device.assign", "admin", ("people",)),
        ActionSpec("device.revoke", "admin", ("people",)),
    ]
}


def _rule_matches(rule: dict[str, Any], action: str, target: Target, now: datetime) -> bool:
    if not fnmatch.fnmatch(action, rule.get("action", "")):
        return False
    areas = rule.get("areas")
    if areas and (target.area is None or target.area not in areas):
        return False
    return _in_window(rule.get("from"), rule.get("until"), now)


def _parse_hhmm(v: str) -> dtime:
    h, m = v.split(":")
    return dtime(int(h), int(m))


def _in_window(start: str | None, end: str | None, now: datetime) -> bool:
    if not start and not end:
        return True
    t = now.time()
    s = _parse_hhmm(start) if start else dtime(0, 0)
    e = _parse_hhmm(end) if end else dtime(23, 59, 59)
    if s <= e:
        return s <= t <= e
    return t >= s or t <= e  # window wraps midnight


class Policy:
    def __init__(self, *, timezone: str, bedtime: str, climate_min: float, climate_max: float):
        self.tz = ZoneInfo(timezone)
        self.bedtime = _parse_hhmm(bedtime)
        self.climate_min = climate_min
        self.climate_max = climate_max

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def capabilities_for(self, s: Subject) -> list[str]:
        caps = set(ROLE_CAPABILITIES.get(s.role, frozenset()))
        for g in s.grants:
            if "capability" in g:
                caps.add(g["capability"])
        for d in s.denies:
            if "capability" in d:
                caps.discard(d["capability"])
        return sorted(caps)

    def has(self, s: Subject, cap: str) -> bool:
        return cap in self.capabilities_for(s)

    def decide(
        self,
        s: Subject,
        action: str,
        target: Target,
        params: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Decision:
        params = params or {}
        now = now or self.now()
        spec = ACTIONS.get(action)
        if spec is None:
            return Decision(False, reason="unknown action")
        tier = spec.tier_fn(params, s) if spec.tier_fn else spec.tier
        if action == "camera.view" and not s.at_home:
            tier = "secure"
        # A plain switch wired to a garage door or gate (e.g. "Small Garage Door Toggle Only") moves the
        # door, so it gets the same rules as the garage itself: Face ID, and only people with doors.
        if action == "switch.set" and is_door_opener(target.entity_id):
            if "doors" not in self.capabilities_for(s):
                return Decision(False, "secure", "doors are not allowed for this person")
            tier = "secure"

        # Explicit denies win over everything.
        for rule in s.denies:
            if "action" in rule and _rule_matches(rule, action, target, now):
                return Decision(False, tier, "denied for this person", ask_parent=s.is_child_like and spec.askable)

        caps = set(self.capabilities_for(s))
        allowed = bool(caps & set(spec.any_caps))
        why = "role"
        if not allowed and caps & set(spec.own_caps):
            allowed = self._in_own_scope(s, action, target, now)
            why = "own room"
        if not allowed:
            for rule in s.grants:
                if "action" in rule and _rule_matches(rule, action, target, now):
                    allowed, why = True, "personal grant"
                    break
        if not allowed:
            return Decision(False, tier, "not allowed", ask_parent=s.is_child_like and spec.askable)

        # Children never reach secure/admin tiers, even through a grant.
        if s.is_child_like and tier in ("secure", "admin"):
            return Decision(False, tier, "needs a grown-up", ask_parent=True)

        clamp: dict[str, Any] = {}
        if action == "climate.set" and "climate.all" not in caps and "target_temp" in params:
            t = float(params["target_temp"])
            clamp["target_temp"] = min(max(t, self.climate_min), self.climate_max)
        return Decision(True, tier, why, clamp=clamp)

    def _in_own_scope(self, s: Subject, action: str, target: Target, now: datetime) -> bool:
        if action.startswith("media.") and s.tv_device and target.entity_id == s.tv_device:
            return True
        if target.area is None:
            return False
        if target.area in s.areas:
            return True
        if target.area in s.shared_areas:
            # Shared spaces go quiet at bedtime for children.
            if s.role == "child" and now.time() >= self.bedtime:
                return False
            return True
        return False
