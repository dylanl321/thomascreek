"""Sorting a notice into a category, so it only reaches the people it is for.

Most notices already know what they are: a doorbell is `cameras`, a NAS app that
stopped is `systems`. Those keep their category and go out at once. Only notices
whose words come from somewhere else (Home Assistant automations, the house
brain, anything sent as `auto`) are sorted here:

1. a correction an admin made on the admin page for that kind of notice,
2. what the AI said last time for that kind of notice (cached),
3. otherwise the AI (Haiku, a few seconds at most), and
4. if the AI is off, slow or unsure, plain keyword rules.

Instant notices never go through the AI at all, not even in the background: an
urgent or time-sensitive one, a doorbell or someone at the door, or an automation
that carries a camera snapshot. They use an admin's correction if there is one,
otherwise the keyword rules, and go out at once.

The sorter only picks a category. Who gets it is still the sender's list,
narrowed by each person's settings for that category, so the AI can take
people off a notice but never add anyone.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from .notify import CATEGORIES, CATEGORY

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.notify")

SORTED = {"house", "auto"}  # categories whose words come from elsewhere
AI_TIMEOUT_S = 6.0
CACHE_MAX = 400
LOG_MAX = 60

_TECH = re.compile(
    r"\b(truenas|nas|pool|zfs|scrub|smart test|disk|drive|dataset|snapshot|replication|backup|container|docker|"
    r"app (stopped|crashed|updated)|service|server|hub|home assistant|integration|zigbee|z-wave|zwave|firmware|"
    r"unavailable|not responding|offline|went quiet|battery (low|is low)|update(s)? available|build|testflight|"
    r"certificate|api key|token|cpu|memory|temperature of|uptime|reboot|restart(ed)?|vpn|wireguard|firewalla|"
    r"internet|wan|dns|error|failed|failing)\b", re.I)
_DOOR = re.compile(r"\b(doorbell|door ?bell|someone at the door|person at|visitor|knock)\b", re.I)
_SIGN_NUM = re.compile(r"\d+")
_SIGN_WS = re.compile(r"\s+")

SYSTEM = """You sort one home notification into a category, which decides who in a family of five gets it.
Categories (id: who it is for, what belongs there):
{cats}
Rules:
- Anything about keeping the systems running is technical (`systems`, or `updates` for new builds): the NAS or
  TrueNAS, apps or containers stopping, Home Assistant integrations or devices unavailable or not responding,
  batteries in sensors, backups, network and internet, certificates, errors. The family does not need these.
- Things happening at the house that the household should know go to a household category: someone at the door
  (`cameras`), doors and garage (`doors`), leaks, smoke, freezer, things left on (`house`), weather (`weather`).
- Something about one person's own request goes to that personal category (`media`, `printer`, `requests`).
- When in doubt between household and technical, pick technical. Set unsure true when you are guessing.
Answer only with the JSON."""

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": [c["id"] for c in CATEGORIES]},
        "reason": {"type": "string"},
        "unsure": {"type": "boolean"},
    },
    "required": ["category", "reason", "unsure"],
    "additionalProperties": False,
}


def signature(cat: str, title: str) -> str:
    """One kind of notice: the source category and the title with numbers taken out."""
    t = _SIGN_WS.sub(" ", _SIGN_NUM.sub("#", (title or "").lower())).strip()[:120]
    return hashlib.sha1(f"{cat}|{t}".encode()).hexdigest()[:16]


def by_rules(cat: str, title: str, body: str | None) -> str:
    text = f"{title}\n{body or ''}"
    if _DOOR.search(text):
        return "cameras"
    if _TECH.search(text):
        return "systems"
    return "house" if cat in SORTED else cat


def instant(title: str, body: str | None, urgent: bool) -> bool:
    """Notices that must go out with no delay and never reach the AI: urgent ones and doorbells."""
    return urgent or bool(_DOOR.search(f"{title}\n{body or ''}"))


class Sorter:
    def __init__(self, hub: "Hub"):
        self.hub = hub

    # -- storage ----------------------------------------------------------
    def rules(self) -> dict[str, str]:
        return dict(self.hub.db.kv_get("notify.rules") or {})

    def _cache(self) -> dict[str, dict[str, Any]]:
        return dict(self.hub.db.kv_get("notify.sort_cache") or {})

    def recent(self) -> list[dict[str, Any]]:
        return list(self.hub.db.kv_get("notify.sorted") or [])

    def correct(self, sig: str, cat: str | None) -> None:
        """An admin's correction for one kind of notice. None removes it."""
        rules = self.rules()
        if cat is None:
            rules.pop(sig, None)
        elif cat in CATEGORY:
            rules[sig] = cat
        self.hub.db.kv_set("notify.rules", rules)
        log_ = self.recent()
        for row in log_:
            if row.get("sig") == sig:
                row["fixed"] = cat
        self.hub.db.kv_set("notify.sorted", log_)

    def _remember(self, sig: str, cat: str, reason: str) -> None:
        cache = self._cache()
        cache[sig] = {"cat": cat, "reason": reason[:200], "t": time.time()}
        if len(cache) > CACHE_MAX:
            for k, _ in sorted(cache.items(), key=lambda kv: kv[1].get("t", 0))[: len(cache) - CACHE_MAX]:
                cache.pop(k, None)
        self.hub.db.kv_set("notify.sort_cache", cache)

    def _log(self, *, sig: str, src: str, title: str, cat: str, how: str, reason: str = "") -> None:
        rows = self.recent()
        rows = [r for r in rows if r.get("sig") != sig]  # one line per kind, newest first
        rows.insert(0, {"t": time.time(), "sig": sig, "src": src, "title": title[:120], "cat": cat, "how": how,
                        "reason": reason[:200], "fixed": self.rules().get(sig)})
        self.hub.db.kv_set("notify.sorted", rows[:LOG_MAX])

    # -- the AI -------------------------------------------------------------
    def _ai_ready(self) -> bool:
        cfg = self.hub.config.brain
        return bool(cfg.enabled and self.hub.vault.get(cfg.key_secret))

    async def _ask(self, cat: str, title: str, body: str | None) -> tuple[str, str] | None:
        cats = "\n".join(f"- {c['id']} ({c['group']}): {c['title']}, {c['detail']}" for c in CATEGORIES)
        thought = await self.hub.brain.router.think(
            system=SYSTEM.format(cats=cats),
            payload={"source": cat, "title": title[:200], "body": (body or "")[:600]},
            schema=SCHEMA, tier="cheap", escalate=True, max_tokens=200)
        d = thought.data or {}
        got = d.get("category")
        if got not in CATEGORY:
            return None
        return got, str(d.get("reason") or "")

    # -- sorting ------------------------------------------------------------
    async def sort(self, *, cat: str, title: str, body: str | None, urgent: bool) -> str:
        if cat not in SORTED:
            return cat
        sig = signature(cat, title)
        fixed = self.rules().get(sig)
        if fixed:
            self._log(sig=sig, src=cat, title=title, cat=fixed, how="admin")
            return fixed
        rule = by_rules(cat, title, body)
        if instant(title, body, urgent):
            # Doorbells and urgent notices skip the classifier entirely: keyword rules, sent now.
            self._log(sig=sig, src=cat, title=title, cat=rule, how="instant")
            return rule
        hit = self._cache().get(sig)
        if hit and hit.get("cat") in CATEGORY:
            self._log(sig=sig, src=cat, title=title, cat=hit["cat"], how="ai", reason=hit.get("reason", ""))
            return hit["cat"]
        if not self._ai_ready():
            self._log(sig=sig, src=cat, title=title, cat=rule, how="rules")
            return rule
        try:
            got = await asyncio.wait_for(self._ask(cat, title, body), AI_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001  (timeout, network, no key)
            log.info("notify sort fell back to rules: %s", type(exc).__name__)
            got = None
        if not got:
            self._log(sig=sig, src=cat, title=title, cat=rule, how="rules")
            return rule
        self._remember(sig, *got)
        self._log(sig=sig, src=cat, title=title, cat=got[0], how="ai", reason=got[1])
        return got[0]
