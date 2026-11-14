"""House-brain jobs: what each one watches, how far it may go, and what it may spend.

A job is plain Python that *detects* (free: states, the activity log, presence) and returns a Finding, or
None when there's nothing to say. Only then does the model get involved, and only to word and rank what
was found. A job never sits between a person and a tap.

Triggers: a schedule (every N seconds or at a local "HH:MM"), Home Assistant state changes on the shared
websocket stream (`on_state`), and hub events (`last_left`, `arrival`, `first_open`, `ha_down`).

Autonomy: observe (log only) | suggest (a Right-now card or an inbox item) | act. "act" is reserved: a job
may only take an action listed in brain.auto_actions, never one in NEVER_AUTO, and never right after a
person changed that thing by hand. With auto_actions empty (the default) act behaves as suggest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from . import Brain

AUTONOMY = ("observe", "suggest", "act")
NEVER_AUTO = frozenset({"lock.set", "garage.set", "alarm.set", "network.pause", "network.grant", "vpn.revoke"})
HUB_EVENTS = ("last_left", "arrival", "first_open", "ha_down")


@dataclass
class Trigger:
    kind: str                 # schedule | ha | hub | manual
    name: str                 # "every 600s", an entity id, an event name
    data: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class Finding:
    """What a job noticed. `items` go to the model to word; each becomes one card (or one notice)."""

    items: list[dict[str, Any]]
    summary: str
    audience: dict[str, Any] = field(default_factory=lambda: {"roles": ["admin", "adult"]})
    context: dict[str, Any] = field(default_factory=dict)  # extra facts for the model, not hashed per item
    scope: str = ""            # keeps findings for different people apart (catch-up is per person)


@dataclass
class Job:
    id: str
    title: str
    about: str
    detect: Callable[["Brain", Trigger], Awaitable[Finding | None]]
    deliver: Callable[["Brain", Trigger, Finding, dict[str, Any] | None, str], Awaitable[dict[str, Any]]]
    system: str
    schema: dict[str, Any]
    every_s: float | None = None
    at: str | Callable[["Brain"], str] | None = None
    on_state: Callable[["Brain", str, dict[str, Any] | None, dict[str, Any] | None], bool] | None = None
    hub_events: tuple[str, ...] = ()
    autonomy: str = "suggest"
    daily_budget_usd: float | None = None  # None = unlimited
    cooldown_s: float = 0.0                # between model calls for this job (a trigger inside it waits)
    human_window_s: float = 900.0          # leave alone what a person changed this recently
    dedupe_s: float = 6 * 3600             # the same finding isn't re-worded within this
    tier: str = "cheap"
    debounce_s: float = 20.0               # Home Assistant bursts collapse into one run

    def at_time(self, brain: "Brain") -> str | None:
        return self.at(brain) if callable(self.at) else self.at


@dataclass
class JobSettings:
    enabled: bool
    autonomy: str
    daily_budget_usd: float | None
    cooldown_s: float
    muted: str | None = None   # why it muted itself, if it did


def settings_for(brain: "Brain", job: Job) -> JobSettings:
    """Defaults from the job, then config.yaml (brain.jobs.<id>), then the app's switches (hub_kv)."""
    cfg = brain.cfg.jobs.get(job.id)
    over = (brain.hub.db.kv_get("brain.jobs") or {}).get(job.id) or {}
    enabled = cfg.enabled if cfg else True
    autonomy = (cfg.autonomy if cfg and cfg.autonomy else None) or job.autonomy
    budget = cfg.daily_budget_usd if cfg and cfg.daily_budget_usd is not None else job.daily_budget_usd
    cooldown = cfg.cooldown_min * 60 if cfg and cfg.cooldown_min is not None else job.cooldown_s
    if "enabled" in over:
        enabled = bool(over["enabled"]) and enabled
    if over.get("autonomy") in AUTONOMY:
        autonomy = over["autonomy"]
    if autonomy not in AUTONOMY:
        autonomy = "suggest"
    return JobSettings(enabled=enabled, autonomy=autonomy, daily_budget_usd=budget or None,
                       cooldown_s=float(cooldown or 0), muted=over.get("muted"))


def may_act(brain: "Brain", action: str) -> bool:
    """Only listed, never-dangerous actions. Empty auto_actions (the default) means never."""
    return action in set(brain.cfg.auto_actions) and action not in NEVER_AUTO


# Shared JSON shapes the model fills in (structured outputs: every field required, nothing extra).
CARDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "title": {"type": "string"}, "detail": {"type": "string"},
                           "priority": {"type": "integer"}},
            "required": ["key", "title", "detail", "priority"], "additionalProperties": False}},
        "unsure": {"type": "boolean"},
    },
    "required": ["items", "unsure"], "additionalProperties": False,
}
NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "detail": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "properties": {"title": {"type": "string"}, "value": {"type": "string"}},
            "required": ["title", "value"], "additionalProperties": False}},
        "unsure": {"type": "boolean"},
    },
    "required": ["title", "detail", "items", "unsure"], "additionalProperties": False,
}
