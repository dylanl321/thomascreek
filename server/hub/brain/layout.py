"""Each person's Home, arranged by the house brain.

The model (the brain's cheap tier, Haiku, by default; brain.layout_tier: smart for Sonnet) decides which sections a person's Home shows, in what order, which
"Right now" cards lead and how many show, from the situation: time of day, weekday or weekend, home or
away, the cards the rules found, what's playing, a trip coming up, which services are down. It bases its
choices on a set of example screens (layout_examples.py).

Plain Python decides what is *allowed* and checks every answer before it reaches a phone:
  - only sections this person may see (the same capability checks the app uses) and that have a service
    behind them; a section whose service is down becomes a one-line note for grown-ups and goes away for kids
  - sections the person hid in Edit Home stay hidden; anything the model left out is kept under
    "Show everything" so nothing is ever lost
  - safety cards (oven left on, unlocked while away, garage open, weather warnings) always lead
  - a child's request carries only that child's own cards; nothing about cameras, doors or the network
  - guests and TVs get no plan (their screens are fixed)

Plans are cached per person and only asked for again when the situation changes (and not more often than
`brain.layout_cooldown_min`). The request never waits for the model: it gets the last plan, checked against
the house as it is now, or the plain default; a fresh plan arrives a moment later as a `smart` event.
With the brain off, paused, capped or without a key, everyone gets the default (their own order).
Every model call is costed in `ai_runs` (job "layout") and counts toward brain.monthly_cap_usd.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .layout_examples import EXAMPLES

if TYPE_CHECKING:
    from ..policy import Subject
    from . import Brain

log = logging.getLogger("hub.brain.layout")

JOB = "layout"
PLANS_KV = "brain.layout"
SAFETY_RULES = {"oven_on", "away_unlocked", "garage_open", "wx_alert"}
SAFETY_PRIORITY = 90
MAX_AGE_S = 12 * 3600

# Grown-up Home sections, in the app's default order: id -> (any of these capabilities, services behind it).
ADULT_SECTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "whos_home": ((), ()),
    "rightnow": ((), ()),
    "weather": ((), ()),
    "status": ((), ("home_assistant",)),
    "favorites": ((), ("home_assistant",)),
    "modes": ((), ("home_assistant",)),
    "kids": (("network.manage",), ("firewalla",)),
    "playing": ((), ("home_assistant",)),
    "printer": (("appliances",), ("printer",)),
    "cameras": (("cameras",), ("frigate",)),
    "holiday": (("holiday.looks", "holiday.full"), ("show_player",)),
    "trip": (("trips.view",), ("trek",)),
    "today": ((), ("family_board",)),
    "systems": (("systems",), ()),
}
# A child's Home (KidHomeView). "board" is the points ring (stars for the little one), "actions" the tiles.
KID_SECTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "rightnow": ((), ()),
    "whos_home": ((), ()),
    "playing": ((), ("home_assistant",)),
    "printer": ((), ("printer",)),
    "trip": (("trips.view",), ("trek",)),
    "board": ((), ("family_board",)),
    "actions": ((), ()),
    "todo": ((), ("family_board",)),
    "my_room": ((), ("home_assistant",)),
    "dinner": ((), ("mealie",)),
    "holiday": ((), ("show_player",)),
}
LITTLE_SECTIONS = ("rightnow", "playing", "printer", "trip", "board", "actions", "dinner")
TITLES = {"whos_home": "Who's home", "rightnow": "Right now", "weather": "Weather", "status": "House status",
          "favorites": "Favorites", "modes": "Modes", "kids": "Kids online", "playing": "Now playing",
          "printer": "3D printer", "cameras": "Cameras", "holiday": "Tonight's show", "trip": "Next trip",
          "today": "Today", "systems": "Systems", "board": "Points", "actions": "Buttons", "todo": "Still to do",
          "my_room": "My room", "dinner": "Dinner"}

SYSTEM = """You arrange the Home screen of a family's home app for one person at one moment.

You get the situation (who: role and home type, time of day, weekday or weekend, home or away, the "Right now" cards the house found for them with their priority, what's going on, which services are down) and the sections their Home can show. Answer with:
- sections: the section ids to show, most useful first. Leave out what isn't useful right now; it stays one tap away under "Show everything". Show at least 3 and keep "rightnow" when there are cards.
- cards: the card keys in the order they should appear (most timely first).
- cards_shown: how many cards to show before "Show more" (1-5). Fewer is calmer; show more only when several matter now.
- why: a few plain words on what this screen is for right now, like "Dinner and wind-down" (under 50 characters, no names).
- unsure: true only if the situation is genuinely unclear.

Base your choices on the examples below (each is a situation and the screen it should get). Use only ids you were given. A person's own order (prefs.order) is a strong hint for sections that matter equally. Safety cards and outages are handled after you answer, so don't worry about them. Keep a child's screen simple and about them."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {"type": "array", "items": {"type": "string"}},
        "cards": {"type": "array", "items": {"type": "string"}},
        "cards_shown": {"type": "integer"},
        "why": {"type": "string"},
        "unsure": {"type": "boolean"},
    },
    "required": ["sections", "cards", "cards_shown", "why", "unsure"], "additionalProperties": False,
}


_SYSTEMS: dict[str, str] = {}


def system_for(home: str) -> str:
    """The instructions plus that home type's example screens. Fixed text, so the provider can cache it."""
    if home not in _SYSTEMS:
        ex = [{"name": e["name"], "situation": e["situation"], "screen": e["plan"]} for e in EXAMPLES
              if e["situation"].get("home") == home]
        _SYSTEMS[home] = SYSTEM + "\n\nExamples:\n" + json.dumps(ex, ensure_ascii=False, indent=1)
    return _SYSTEMS[home]


def part_of_day(now: datetime) -> str:
    h = now.hour
    if h < 5:
        return "night"
    return ("early" if h < 9 else "morning" if h < 12 else "afternoon" if h < 17 else "evening" if h < 21
            else "night")


def _hash(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Planner:
    def __init__(self, brain: "Brain"):
        self.brain = brain
        self.hub = brain.hub
        self.plans: dict[str, dict[str, Any]] = {}
        self._inflight: set[str] = set()
        self._last_call: dict[str, float] = {}
        try:
            self.plans = dict(self.hub.db.kv_get(PLANS_KV) or {})
        except Exception:  # noqa: BLE001
            self.plans = {}

    # -- switches -----------------------------------------------------------
    @property
    def cfg(self) -> Any:
        return self.brain.cfg

    def ai_ready(self) -> bool:
        b = self.brain
        return bool(self.cfg.layout_enabled) and b.on and b.has_key() and not b.capped()

    @staticmethod
    def home_type(s: "Subject") -> str | None:
        if getattr(s, "tv", False) or not s.person_id:
            return None
        if s.role in ("admin", "adult", "tablet"):
            return "adult"
        if s.role == "child":
            return "kid"
        return None  # guest, pending: fixed screens

    # -- what this person may get -------------------------------------------
    def _style(self, s: "Subject") -> str | None:
        try:
            p = self.hub.db.person(s.person_id)
            return (p or {}).get("ui_style")
        except Exception:  # noqa: BLE001
            return None

    def _services(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        try:
            for h in self.hub.health():
                sid = "printer" if str(h.get("id", "")).startswith("printer_") else h.get("id")
                if sid == "printer" and "printer" in out:
                    out["printer"]["ok"] = out["printer"]["ok"] or bool(h.get("ok"))  # any printer up is enough
                    continue
                out[sid] = {"enabled": bool(h.get("enabled")), "ok": bool(h.get("ok")), "title": h.get("title")}
        except Exception as exc:  # noqa: BLE001
            log.info("health for layout: %s", exc)
        return out

    def allowed(self, s: "Subject", home: str) -> tuple[list[str], dict[str, str]]:
        """Sections this person can have, in default order, and {section: note} for those whose service is down."""
        table = ADULT_SECTIONS if home == "adult" else KID_SECTIONS
        ids = list(table)
        if home == "kid" and self._style(s) == "little":
            ids = [i for i in ids if i in LITTLE_SECTIONS]
        caps = set(self.hub.policy.capabilities_for(s))
        svc = self._services()
        out, down = [], {}
        for sid in ids:
            need, services = table[sid]
            if need and not caps & set(need):
                continue
            gone = False
            for name in services:
                st = svc.get(name)
                if st is None or not st["enabled"]:
                    if name not in ("home_assistant",):  # a service that isn't set up: no section
                        gone = True
                    continue
                if not st["ok"]:
                    if name == "home_assistant" and sid != "status":
                        continue  # one note on House status is enough; the rest show what they last knew
                    down[sid] = f"{st.get('title') or TITLES.get(sid, sid)} isn't answering right now"
            if gone:
                continue
            out.append(sid)
        return out, down

    def _prefs(self, s: "Subject") -> dict[str, Any]:
        try:
            st = self.hub.settings.get(s)["settings"]
        except Exception:  # noqa: BLE001
            st = {}
        return {"order": list(st.get("home.order") or []), "hidden": list(st.get("home.hidden") or []),
                "ai": st.get("home.ai", True) is not False}

    def _content(self, s: "Subject", home: str) -> dict[str, Any]:
        h = self.hub
        out: dict[str, Any] = {}
        try:
            out["playing"] = sum(1 for e, st in h.home.states.items()
                                 if e.startswith("media_player.") and st.get("state") == "playing")
        except Exception:  # noqa: BLE001
            pass
        try:
            out["printing"] = sum(1 for p in h.workshop.summaries() if p.get("job"))
        except Exception:  # noqa: BLE001
            pass
        try:
            days = [t.get("days_until") for t in h.smart.trips if t.get("status") == "upcoming"]
            days = [d for d in days if isinstance(d, (int, float))]
            if days:
                out["trip_days"] = int(min(days))
        except Exception:  # noqa: BLE001
            pass
        out["holiday_season"] = h.policy.now().month in (11, 12, 1)
        if home == "adult":
            out["kids_online"] = bool(getattr(h.smart, "kids_online", False))
        return out

    @staticmethod
    def card_keys(cards: list[dict[str, Any]]) -> dict[str, str]:
        """card id -> a short key for the model: the rule, numbered by card id when a rule has several, so the
        same card keeps the same key whatever order the rules produce them in."""
        out: dict[str, str] = {}
        seen: dict[str, int] = {}
        for c in sorted(cards, key=lambda c: (str(c.get("rule") or "card"), str(c.get("id")))):
            rule = str(c.get("rule") or "card")
            n = seen.get(rule, 0)
            seen[rule] = n + 1
            out[str(c.get("id"))] = rule if n == 0 else f"{rule}#{n + 1}"
        return out

    def situation(self, s: "Subject", home: str, cards: list[dict[str, Any]], allowed: list[str],
                  down: dict[str, str], prefs: dict[str, Any]) -> dict[str, Any]:
        now = self.hub.policy.now()
        keys = self.card_keys(cards)
        card_list = []
        for c in cards:
            item = {"key": keys[str(c.get("id"))], "priority": int(c.get("priority") or 0)}
            if home == "adult":
                item["title"] = str(c.get("title") or "")[:80]
            else:
                item["rule"] = c.get("rule")  # a child's card titles can name others; the rule is enough
            card_list.append(item)
        sit: dict[str, Any] = {
            "home": home, "role": s.role, "part": part_of_day(now), "day": "weekend" if now.weekday() >= 5 else "weekday",
            "where": "home" if s.at_home else "away", "cards": card_list, "sections": allowed,
            "down": sorted({v.split(" isn't")[0] for v in down.values()}),
            "content": self._content(s, home), "prefs": {"order": [o for o in prefs["order"] if o in allowed]},
        }
        if home == "kid":
            sit["style"] = self._style(s) or "kid"
            sit.pop("where")  # where a child is isn't the model's business
        return sit

    @staticmethod
    def key_of(sit: dict[str, Any]) -> str:
        """What makes a new plan worth asking for: the coarse situation, not every minute or card wording."""
        c = sit.get("content") or {}
        return _hash({k: sit.get(k) for k in ("home", "role", "part", "day", "where", "sections", "down", "prefs", "style")}
                     | {"cards": sorted(x["key"] for x in sit["cards"]),
                        "content": {"playing": bool(c.get("playing")), "printing": bool(c.get("printing")),
                                    "trip_soon": (c.get("trip_days") or 99) <= 7}})

    # -- the plan -------------------------------------------------------------
    def plan_for(self, s: "Subject", cards: list[dict[str, Any]], ask: bool = False
                 ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """(plan, cards in plan order). Never waits for the model and never raises. Only `ask` (the app's Home
        screen opening, not a widget, Siri or the assistant) may start a model call."""
        home = self.home_type(s)
        if home is None:
            return None, cards
        try:
            return self._plan_for(s, home, cards, ask)
        except Exception as exc:  # noqa: BLE001
            log.warning("layout plan failed: %s", exc)
            return None, cards

    def _plan_for(self, s: "Subject", home: str, cards: list[dict[str, Any]], ask: bool):
        allowed, down = self.allowed(s, home)
        prefs = self._prefs(s)
        sit = self.situation(s, home, cards, allowed, down, prefs)
        key = self.key_of(sit)
        pid = s.person_id or ""
        cached = self.plans.get(pid)
        use_ai = prefs["ai"] and self.ai_ready()
        if ask and use_ai and (cached is None or cached.get("key") != key or time.time() - cached.get("at", 0) > MAX_AGE_S):
            self._maybe_ask(pid, s, sit, key)
        raw = cached if (use_ai and cached) else None
        return self.check(raw, s, home, sit, allowed, down, prefs, cards)

    def check(self, raw: dict[str, Any] | None, s: "Subject", home: str, sit: dict[str, Any], allowed: list[str],
              down: dict[str, str], prefs: dict[str, Any], cards: list[dict[str, Any]]):
        """The guard rails. Whatever the model said, this is what reaches the phone."""
        hidden = set(prefs["hidden"])
        default = [o for o in prefs["order"] if o in allowed] + [a for a in allowed if a not in prefs["order"]]
        if raw:
            shown = [x for x in dict.fromkeys(raw.get("sections") or []) if x in allowed]
            if len(shown) < 3:  # a too-empty answer: fall back to the default order behind what it picked
                shown += [x for x in default if x not in shown][: 3 - len(shown)]
        else:
            shown = list(default)
        tucked = [x for x in default if x not in shown]

        # Cards: the model's order, then the rest by priority; safety always first.
        keys = self.card_keys(cards)
        keyed = [(keys[str(c.get("id"))], c) for c in cards]
        order = {k: i for i, k in enumerate((raw or {}).get("cards") or [])}
        keyed.sort(key=lambda kc: (order.get(kc[0], 1000), -int(kc[1].get("priority") or 0)))
        safety = [kc for kc in keyed if kc[1].get("rule") in SAFETY_RULES or int(kc[1].get("priority") or 0) >= SAFETY_PRIORITY]
        rest = [kc for kc in keyed if kc not in safety]
        safety.sort(key=lambda kc: -int(kc[1].get("priority") or 0))
        out_cards = [c for _, c in safety + rest]
        n = int((raw or {}).get("cards_shown") or 3)
        n = max(1, min(5, n, len(out_cards) or 1), len(safety))

        if safety and "rightnow" in allowed and "rightnow" not in shown:
            shown.insert(0, "rightnow")
        if out_cards and "rightnow" in allowed:
            if safety:
                shown = ["rightnow"] + [x for x in shown if x != "rightnow"]
            elif "rightnow" not in shown:
                shown.insert(0, "rightnow")
            if "rightnow" in tucked:
                tucked.remove("rightnow")

        sections = []
        for sid in shown:
            pinned = sid == "rightnow" and bool(safety)
            if sid in hidden and not pinned:
                continue
            item: dict[str, Any] = {"id": sid, "show": True}
            if pinned:
                item["pinned"] = True  # a safety card shows even if they hid Right now
            if sid in down:
                if home != "adult":
                    continue  # a child just doesn't see it
                item["note"] = down[sid]
            sections.append(item)
        for sid in tucked:
            if sid in hidden or (sid in down and home != "adult"):
                continue
            sections.append({"id": sid, "show": False})
        plan = {
            "source": "ai" if raw else ("off" if not prefs["ai"] else "default"),
            "home": home,
            "sections": sections,
            "cards_shown": n,
            "why": (str(raw.get("why") or "")[:60] or None) if raw else None,
            "generated_at": (datetime.fromtimestamp(raw["at"], self.hub.policy.tz).isoformat()
                             if raw and raw.get("at") else None),
        }
        return plan, out_cards

    def _maybe_ask(self, pid: str, s: "Subject", sit: dict[str, Any], key: str) -> None:
        now = time.time()
        if pid in self._inflight or now - self._last_call.get(pid, 0) < float(self.cfg.layout_cooldown_min) * 60:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._inflight.add(pid)
        self._last_call[pid] = now
        loop.create_task(self._ask(pid, sit, key))

    async def _ask(self, pid: str, sit: dict[str, Any], key: str) -> dict[str, Any] | None:
        try:
            thought = await self.brain.router.think(system=system_for(sit["home"]), payload={"now": sit}, schema=SCHEMA,
                                                    tier="cheap" if self.cfg.layout_tier == "cheap" else "smart",
                                                    escalate=False, max_tokens=600)
            words = thought.data
            if not isinstance(words, dict) or not isinstance(words.get("sections"), list) or not words["sections"]:
                words = None
            outcome = "planned" if words else "fallback"
            self.brain.log_run(job=JOB, trigger=f"open:{pid}", input_summary=json.dumps(sit, default=str)[:600],
                               input_hash=key, model=thought.model or "template", tier=thought.tier,
                               tokens_in=thought.tokens_in, tokens_out=thought.tokens_out,
                               tokens_cached=thought.cached, cost_usd=thought.cost_usd,
                               latency_ms=thought.latency_ms,
                               decision={"plan": words, **({"fallback": thought.error} if thought.error else {})},
                               action="layout", outcome=outcome)
            if not words:
                # Try again in a minute rather than after the whole cooldown.
                cool = float(self.cfg.layout_cooldown_min) * 60
                self._last_call[pid] = time.time() - max(0.0, cool - 60)
                return None
            plan = {"key": key, "at": time.time(), "sections": [str(x) for x in words.get("sections") or []][:30],
                    "cards": [str(x) for x in words.get("cards") or []][:30],
                    "cards_shown": words.get("cards_shown"), "why": str(words.get("why") or "")[:60]}
            self.plans[pid] = plan
            self._save()
            self.hub.bus.publish({"type": "smart", "data": {"reason": "layout"}}, person_id=pid)
            return plan
        except Exception as exc:  # noqa: BLE001
            log.warning("layout model run failed: %s", exc)
            return None
        finally:
            self._inflight.discard(pid)

    def forget(self, pid: str) -> None:
        self.plans.pop(pid, None)
        self._last_call.pop(pid, None)
        self._save()

    def _save(self) -> None:
        try:
            now = time.time()
            self.hub.db.kv_set(PLANS_KV, {k: v for k, v in self.plans.items() if now - v.get("at", 0) < MAX_AGE_S})
        except Exception:  # noqa: BLE001
            pass
