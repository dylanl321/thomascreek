"""The house brain: cheap background AI that notices things and says them plainly.

Plain Python spots what matters (a washer that stopped, a light on in an empty room, a device gone
unavailable); the cheap model (Claude Haiku) only words and ranks it, and hands over to the smart model
(Sonnet) when it says it's unsure. Results land as Right-now cards (hub/smart.py asks `cards_for`) or as
maintenance inbox items. Nothing here acts on the house by itself unless brain.auto_actions allows it,
which it doesn't by default.

Every run is written to `ai_runs`: what set it off, a summary and hash of what it saw, which model, tokens,
cost, time taken, what it decided and what people did with it ("used", "not_now", "ignored"). Ask Thomas
Creek's turns are logged there too (job "assistant"), so the month's spend is in one place.

Kill switch: brain.enabled (config) or the app's pause switch. Optional monthly cap (brain.monthly_cap_usd,
unset by default): once reached, jobs only observe until the month turns over.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..db import new_id
from .jobs import AUTONOMY, Finding, Job, JobSettings, Trigger, settings_for
from .models import Router, cost

if TYPE_CHECKING:
    from ..policy import Subject
    from ..state import Hub

log = logging.getLogger("hub.brain")

TICK_S = 30.0
PRIVATE_KEYS = ("buttons", "covers", "why", "priority", "ref")  # delivery details: not hashed, not sent to the model
CARDS_KV = "brain.cards"
ASSISTANT_JOB = "assistant"


class Brain:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.router = Router(hub)
        from .house import build_jobs

        self.jobs: dict[str, Job] = {j.id: j for j in build_jobs()}
        self.memory: dict[str, Any] = {}          # what jobs remember between runs (appliance finishes, ...)
        self.cards: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, tuple[float, Trigger]] = {}
        self._last_run: dict[str, float] = {}      # job -> last scheduled run
        self._last_model: dict[str, float] = {}    # job -> last model call
        self._last_hash: dict[str, tuple[str, float]] = {}
        self._presence: dict[str, tuple[bool, float]] = {}
        self._ha_down_since: float | None = None
        self._ha_down_told = False
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()
        self._load_cards()
        from .layout import Planner

        self.layout = Planner(self)

    # -- switches -----------------------------------------------------------
    @property
    def cfg(self) -> Any:
        return self.hub.config.brain

    def has_key(self) -> bool:
        return self.hub.vault.has(self.cfg.key_secret)

    @property
    def paused(self) -> bool:
        return bool(self.hub.db.kv_get("brain.paused"))

    def set_paused(self, paused: bool) -> None:
        self.hub.db.kv_set("brain.paused", bool(paused))

    @property
    def on(self) -> bool:
        return bool(self.cfg.enabled) and not self.paused

    def settings(self, job: Job) -> JobSettings:
        return settings_for(self, job)

    def set_job(self, job_id: str, enabled: bool | None = None, autonomy: str | None = None) -> None:
        all_ = self.hub.db.kv_get("brain.jobs") or {}
        cur = dict(all_.get(job_id) or {})
        if enabled is not None:
            cur["enabled"] = bool(enabled)
        if autonomy is not None:
            if autonomy not in AUTONOMY:
                raise ValueError(autonomy)
            cur["autonomy"] = autonomy
            cur.pop("muted", None)  # picking a level by hand clears a self-mute
        all_[job_id] = cur
        self.hub.db.kv_set("brain.jobs", all_)

    # -- lifecycle ----------------------------------------------------------
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
        await asyncio.sleep(60)  # let the home model, weather and presence fill in first
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("brain tick failed: %s", exc)
            await asyncio.sleep(TICK_S)

    def now(self) -> datetime:
        return self.hub.policy.now()

    async def tick(self, now: float | None = None) -> list[str]:
        """One pass: presence and link changes become events, schedules come due, pending runs go."""
        now = now or time.time()
        self._expire_cards(now)
        if not self.on:
            return []
        self._watch_presence(now)
        self._watch_ha(now)
        local = datetime.fromtimestamp(now, self.hub.policy.tz)
        for job in self.jobs.values():
            if job.every_s and now - self._last_run.get(job.id, 0) >= job.every_s:
                self._last_run[job.id] = now
                self._queue(job, Trigger("schedule", f"every {int(job.every_s)}s"), now)
            at = job.at_time(self)
            if at:
                key = f"at:{job.id}"
                try:
                    h, m = (int(x) for x in at.split(":"))
                except ValueError:
                    continue
                day = local.date().isoformat()
                if (local.hour, local.minute) >= (h, m) and self.memory.get(key) != day:
                    self.memory[key] = day
                    if (local.hour * 60 + local.minute) - (h * 60 + m) <= 90:  # a restart at noon doesn't brief
                        self._queue(job, Trigger("schedule", f"at {at}"), now)
        ran = []
        for jid, (due, trig) in list(self._pending.items()):
            if due <= now:
                self._pending.pop(jid, None)
                out = await self.run(jid, trig, now=now)
                ran.append(f"{jid}:{out}")
        return ran

    def _queue(self, job: Job, trig: Trigger, now: float, delay: float = 0.0) -> None:
        s = self.settings(job)
        if not s.enabled:
            return
        due = now + delay
        wait = self._last_model.get(job.id, 0) + s.cooldown_s
        if wait > due and trig.kind != "hub":
            due = wait
        have = self._pending.get(job.id)
        # A person-specific event (arrival) never gets swallowed by a later schedule tick.
        if have and have[1].kind == "hub" and trig.kind != "hub":
            return
        self._pending[job.id] = (min(due, have[0]) if have and have[1].kind == trig.kind else due, trig)

    # -- inputs -------------------------------------------------------------
    def on_state(self, eid: str, old: dict[str, Any] | None, new: dict[str, Any] | None) -> None:
        """Every Home Assistant state change from the shared websocket. Must stay cheap."""
        if not self.on:
            return
        now = time.time()
        for job in self.jobs.values():
            if job.on_state is None:
                continue
            try:
                hit = job.on_state(self, eid, old, new)
            except Exception as exc:  # noqa: BLE001
                log.debug("brain %s on_state: %s", job.id, exc)
                continue
            if hit:
                self._queue(job, Trigger("ha", eid), now, delay=job.debounce_s)

    def fire(self, event: str, **data: Any) -> None:
        """A hub event: last_left, arrival (person_id, away_since), first_open (person_id), ha_down."""
        if not self.on:
            return
        now = time.time()
        for job in self.jobs.values():
            if event in job.hub_events:
                if job.id == "catch_up" and event == "arrival":
                    # One pending arrival per person; run it as its own job key.
                    self._pending[f"catch_up#{data.get('person_id')}"] = (now + 30, Trigger("hub", event, data))
                    continue
                self._queue(job, Trigger("hub", event, data), now, delay=5)

    def note_open(self, s: "Subject") -> None:
        """Someone looked at Home (called while building Right-now cards)."""
        if not s.person_id or s.role not in ("admin", "adult") or not self.cfg.briefing_on_first_open:
            return
        local = self.now()
        if not 5 <= local.hour < 11:
            return
        day = local.date().isoformat()
        if self.hub.db.kv_get("brain.briefing_day") == day or self.memory.get("first_open") == day:
            return
        self.memory["first_open"] = day
        self.fire("first_open", person_id=s.person_id)

    def _watch_presence(self, now: float) -> None:
        people = [p for p in self.hub.db.people_with_role("admin", "adult")]
        changed: list[tuple[dict[str, Any], bool, float]] = []
        for p in people:
            home = self.hub.household.presence(p).get("home")
            if home is None:
                continue
            was = self._presence.get(p["id"])
            if was is None:
                self._presence[p["id"]] = (bool(home), now)
                continue
            if was[0] != bool(home):
                self._presence[p["id"]] = (bool(home), now)
                changed.append((p, bool(home), was[1]))
        for p, home, since in changed:
            if home:
                self.fire("arrival", person_id=p["id"], away_since=since)
            elif not any(v[0] for v in self._presence.values()):
                self.fire("last_left", person_id=p["id"])

    def _watch_ha(self, now: float) -> None:
        ws = self.hub.ha_ws
        if not ws.enabled or ws.connected:
            self._ha_down_since, self._ha_down_told = None, False
            return
        if self._ha_down_since is None:
            self._ha_down_since = now
        elif now - self._ha_down_since >= 120 and not self._ha_down_told:
            self._ha_down_told = True
            self.fire("ha_down", since=self._ha_down_since)

    def ha_down_for(self, now: float | None = None) -> float:
        return ((now or time.time()) - self._ha_down_since) if self._ha_down_since else 0.0

    def human_touched(self, eid: str, window_s: float, now: float | None = None) -> bool:
        """A person changed this recently (the app, a switch on the wall, Home Assistant itself)."""
        now = now or time.time()
        from ..smart import _ts

        st = self.hub.home.states.get(eid) or {}
        changed = _ts(st.get("last_changed"))
        if changed and now - changed < window_s:
            return True
        row = self.hub.db.one("SELECT id FROM activity WHERE target=? AND ts>? AND source!='brain' LIMIT 1",
                              [eid, now - window_s])
        return row is not None

    # -- running a job ------------------------------------------------------
    async def run(self, job_id: str, trig: Trigger | None = None, now: float | None = None) -> str:
        """Detect, then (if there's something) word it and deliver it. Returns what happened."""
        jid = job_id.split("#", 1)[0]
        job = self.jobs.get(jid)
        if job is None:
            return "unknown"
        trig = trig or Trigger("manual", "run")
        now = now or time.time()
        s = self.settings(job)
        if not self.on or not s.enabled:
            return "off"
        if job_id in self._running:
            return "busy"
        self._running.add(job_id)
        try:
            return await self._run(job, s, trig, now)
        finally:
            self._running.discard(job_id)

    async def _run(self, job: Job, s: JobSettings, trig: Trigger, now: float) -> str:
        try:
            finding = await job.detect(self, trig)
        except Exception as exc:  # noqa: BLE001
            log.warning("brain %s detect failed: %s", job.id, exc)
            return "error"
        if finding is None or not finding.items:
            return "nothing"
        facts = [{k: v for k, v in i.items() if k not in PRIVATE_KEYS} for i in finding.items]
        digest = _hash({"items": facts, "scope": finding.scope})
        key = f"{job.id}:{finding.scope}"
        last = self._last_hash.get(key)
        if last and last[0] == digest and now - last[1] < job.dedupe_s:
            return "unchanged"
        self._last_hash[key] = (digest, now)

        autonomy = s.autonomy
        capped = self.capped()
        if capped:
            autonomy = "observe"
        base = {"job": job.id, "trigger": trig.label(), "input_summary": finding.summary[:600],
                "input_hash": digest}
        if autonomy == "observe":
            self.log_run(**base, model=None, tier=None, decision={"items": facts},
                         action="observe", outcome="capped" if capped else "observed")
            return "observed"

        thought = None
        words: dict[str, Any] | None = None
        reason = None
        if not self.has_key():
            reason = "no_key"
        elif s.daily_budget_usd and self.spend_today(job.id) >= s.daily_budget_usd:
            reason = "daily_budget"
        if reason is None:
            payload = {"now": self.now().strftime("%A %H:%M"), "house": self.hub.config.assistant.house_name,
                       "items": facts, **({"context": finding.context} if finding.context else {})}
            thought = await self.router.think(system=job.system, payload=payload, schema=job.schema, tier=job.tier)
            self._last_model[job.id] = now
            words = thought.data
            if words is None:
                reason = thought.error or "no_answer"
        run_id = new_id("air")
        try:
            out = await job.deliver(self, trig, finding, words, run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("brain %s deliver failed: %s", job.id, exc)
            out = {"outcome": "error", "error": str(exc)[:200]}
        decision = {"items": facts, "words": words, **({"fallback": reason} if reason else {}),
                    **({"escalated": True} if thought and thought.escalated else {}),
                    **({"calls": thought.calls} if thought and thought.calls else {}),
                    **{k: v for k, v in out.items() if k in ("cards", "notice", "error")}}
        self.log_run(**base, id=run_id, model=(thought.model if thought and thought.model else "template"),
                     tier=thought.tier if thought else None,
                     tokens_in=thought.tokens_in if thought else 0, tokens_out=thought.tokens_out if thought else 0,
                     tokens_cached=thought.cached if thought else 0, cost_usd=thought.cost_usd if thought else 0.0,
                     latency_ms=thought.latency_ms if thought else None, decision=decision,
                     action=out.get("action", "suggest"), card_id=(out.get("cards") or [None])[0],
                     outcome=out.get("outcome", "suggested"))
        if self.cfg.monthly_cap_usd and not capped and self.capped():
            await self._tell_capped()
        return out.get("outcome", "suggested")

    # -- the run log --------------------------------------------------------
    def log_run(self, *, job: str, trigger: str | None, input_summary: str | None = None, input_hash: str | None = None,
                model: str | None = None, tier: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
                tokens_cached: int = 0, cost_usd: float = 0.0, latency_ms: int | None = None,
                decision: Any = None, action: str | None = None, activity_id: int | None = None,
                card_id: str | None = None, outcome: str | None = None, id: str | None = None) -> str:
        rid = id or new_id("air")
        self.hub.db.insert("ai_runs", {
            "id": rid, "ts": time.time(), "job": job, "trigger": trigger, "input_summary": input_summary,
            "input_hash": input_hash, "model": model, "tier": tier, "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out), "tokens_cached": int(tokens_cached), "cost_usd": float(cost_usd),
            "latency_ms": latency_ms, "decision": json.dumps(decision, default=str) if decision is not None else None,
            "action": action, "activity_id": activity_id, "card_id": card_id, "outcome": outcome,
        })
        return rid

    def record_assistant(self, *, model: str, usage: dict[str, int], latency_ms: int, person_id: str | None,
                         steps: int, activity_id: int | None = None) -> str | None:
        """Ask Thomas Creek's turn, costed like a brain run."""
        try:
            dollars = cost(model, usage.get("in", 0), usage.get("out", 0), usage.get("cached", 0),
                           usage.get("cache_write", 0))
            return self.log_run(job=ASSISTANT_JOB, trigger=f"ask:{person_id or '-'}", model=model, tier="assistant",
                                tokens_in=usage.get("in", 0) + usage.get("cache_write", 0),
                                tokens_out=usage.get("out", 0), tokens_cached=usage.get("cached", 0),
                                cost_usd=dollars, latency_ms=latency_ms, decision={"steps": steps},
                                action="answer", activity_id=activity_id, outcome="answered")
        except Exception as exc:  # noqa: BLE001
            log.info("assistant cost log failed: %s", exc)
            return None

    def month_start(self, now: datetime | None = None) -> float:
        n = now or self.now()
        return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    def spend(self, since: float, job: str | None = None, brain_only: bool = True) -> float:
        sql, args = "SELECT COALESCE(SUM(cost_usd),0) AS c FROM ai_runs WHERE ts>=?", [since]
        if job:
            sql += " AND job=?"
            args.append(job)
        elif brain_only:
            sql += " AND job!=?"
            args.append(ASSISTANT_JOB)
        return float((self.hub.db.one(sql, args) or {}).get("c") or 0.0)

    def spend_today(self, job: str) -> float:
        n = self.now()
        return self.spend(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), job=job)

    def capped(self) -> bool:
        cap = self.cfg.monthly_cap_usd
        return bool(cap) and self.spend(self.month_start()) >= float(cap)

    async def _tell_capped(self) -> None:
        month = self.now().strftime("%Y-%m")
        if self.hub.db.kv_get("brain.capped_told") == month:
            return
        self.hub.db.kv_set("brain.capped_told", month)
        await self.hub.notify.send(title="House brain paused for the month",
                                   body=f"It reached its ${self.cfg.monthly_cap_usd:.2f} cap, so it only watches "
                                        "until the 1st. Raise brain.monthly_cap_usd to resume.",
                                   cat="systems", level="fyi", roles=["admin"], ref="thomascreek://brain",
                                   group_key="brain.cap")

    # -- cards on Home ------------------------------------------------------
    def add_card(self, *, job: str, run_id: str, card: dict[str, Any], audience: dict[str, Any],
                 ttl_s: float, buttons: list[dict[str, Any]] | None = None) -> str:
        cid = card["id"]
        self.cards[cid] = {"job": job, "run_id": run_id, "card": card, "buttons": buttons or [],
                           "audience": audience, "expires_at": time.time() + ttl_s, "created_at": time.time()}
        self._save_cards()
        self.hub.bus.publish({"type": "smart", "data": {}})
        return cid

    def drop_cards(self, job: str, keep: set[str] | None = None, why: str = "resolved") -> None:
        """Cards whose situation went away (the light got turned off): not counted against the job."""
        gone = [k for k, v in self.cards.items() if v["job"] == job and k not in (keep or set())]
        for k in gone:
            self._close(k, why)
            self.cards.pop(k, None)
        if gone:
            self._save_cards()
            self.hub.bus.publish({"type": "smart", "data": {}})

    def cards_for(self, sm: Any, c: Any) -> list[dict[str, Any]]:
        """The brain's live cards for this person, with buttons planned through their own policy."""
        if not self.cfg.enabled:
            return []
        try:
            self.note_open(c.s)
        except Exception:  # noqa: BLE001
            pass
        now = time.time()
        hidden = sm.hidden(c.s.person_id)
        off = set(self.hub.config.smart.off_rules) | set(hidden["muted"])
        out = []
        for cid, v in self.cards.items():
            if v["expires_at"] <= now or not _for(v["audience"], c.s):
                continue
            rule = f"brain_{v['job']}"
            if rule in off:
                continue
            card = json.loads(json.dumps(v["card"]))
            card["rule"] = rule
            btns = []
            for b in v["buttons"]:
                if b.get("kind") == "action":
                    btns.append(sm.button(c, b["title"], b["action"], b.get("target"), b.get("params"),
                                          primary=b.get("primary", True), done=b.get("done")))
                elif b.get("kind") == "open":
                    btns.append(sm.link(b["title"], b["url"], primary=b.get("primary", False)))
                else:
                    btns.append(sm.ok(b.get("title", "Got it"), primary=b.get("primary", True), done=b.get("done")))
            card["buttons"] = btns
            card["ai"] = {"run_id": v["run_id"], "job": v["job"]}
            out.append(card)
        return out

    def feedback(self, card_id: str, handled: bool, person_id: str | None = None) -> None:
        """A card's button was used, or "Not now". Feeds the run log and the self-mute."""
        v = self.cards.get(card_id)
        if v is None:
            return
        what = "used" if handled else "not_now"
        self.hub.db.execute("UPDATE ai_runs SET feedback=?, feedback_at=? WHERE id=? AND "
                            "(feedback IS NULL OR feedback='not_now')", [what, time.time(), v["run_id"]])
        if handled:
            v.setdefault("used_by", []).append(person_id)
        self._check_mute(v["job"])

    def _close(self, card_id: str, what: str) -> None:
        v = self.cards.get(card_id)
        if v:
            self.hub.db.execute("UPDATE ai_runs SET feedback=?, feedback_at=? WHERE id=? AND feedback IS NULL",
                                [what, time.time(), v["run_id"]])

    def _expire_cards(self, now: float) -> None:
        gone = [k for k, v in self.cards.items() if v["expires_at"] <= now]
        jobs = set()
        for k in gone:
            self._close(k, "ignored")
            jobs.add(self.cards[k]["job"])
            self.cards.pop(k, None)
        if gone:
            self._save_cards()
            for j in jobs:
                self._check_mute(j)

    def _check_mute(self, job_id: str) -> None:
        """A job whose last N cards were all ignored or put off turns itself down to observe."""
        n = int(self.cfg.mute_after_ignored or 0)
        job = self.jobs.get(job_id)
        if n <= 0 or job is None or self.settings(job).autonomy == "observe":
            return
        rows = self.hub.db.all("SELECT feedback FROM ai_runs WHERE job=? AND card_id IS NOT NULL "
                               "ORDER BY ts DESC LIMIT ?", [job_id, n])
        if len(rows) < n or any(r["feedback"] not in ("ignored", "not_now") for r in rows):
            return
        all_ = self.hub.db.kv_get("brain.jobs") or {}
        cur = dict(all_.get(job_id) or {})
        cur.update({"autonomy": "observe", "muted": f"Its last {n} cards were ignored or put off"})
        all_[job_id] = cur
        self.hub.db.kv_set("brain.jobs", all_)
        self.drop_cards(job_id, why="muted")
        log.info("brain: %s muted itself", job_id)

        async def tell() -> None:
            await self.hub.notify.send(title=f"House brain: {job.title} went quiet",
                                       body=f"Its last {n} cards were ignored or put off, so it only watches now. "
                                            "Turn it back on in Systems → House brain.",
                                       cat="systems", level="fyi", roles=["admin"], ref="thomascreek://brain",
                                       group_key=f"brain.mute.{job_id}")
        try:
            asyncio.get_running_loop().create_task(tell())
        except RuntimeError:
            pass

    def _load_cards(self) -> None:
        try:
            raw = self.hub.db.kv_get(CARDS_KV) or {}
            now = time.time()
            self.cards = {k: v for k, v in raw.items() if v.get("expires_at", 0) > now}
        except Exception:  # noqa: BLE001
            self.cards = {}

    def _save_cards(self) -> None:
        self.hub.db.kv_set(CARDS_KV, self.cards)

    # -- what the app shows -------------------------------------------------
    def summary(self) -> dict[str, Any]:
        now = self.now()
        since = self.month_start(now)
        db = self.hub.db
        rows = db.all("SELECT job, COUNT(*) AS runs, COALESCE(SUM(cost_usd),0) AS cost, MAX(ts) AS last, "
                      "SUM(CASE WHEN card_id IS NOT NULL THEN 1 ELSE 0 END) AS cards, "
                      "SUM(CASE WHEN feedback='used' THEN 1 ELSE 0 END) AS used, "
                      "SUM(CASE WHEN feedback='not_now' THEN 1 ELSE 0 END) AS not_now, "
                      "SUM(CASE WHEN feedback='ignored' THEN 1 ELSE 0 END) AS ignored, "
                      "SUM(CASE WHEN feedback='resolved' THEN 1 ELSE 0 END) AS resolved, "
                      "SUM(CASE WHEN action='act' THEN 1 ELSE 0 END) AS acts, "
                      "SUM(CASE WHEN feedback='undone' THEN 1 ELSE 0 END) AS undone "
                      "FROM ai_runs WHERE ts>=? GROUP BY job", [since])
        by = {r["job"]: r for r in rows}
        brain_rows = [r for j, r in by.items() if j != ASSISTANT_JOB]
        cards = sum(r["cards"] or 0 for r in brain_rows)
        not_now = sum(r["not_now"] or 0 for r in brain_rows)
        ignored = sum(r["ignored"] or 0 for r in brain_rows)
        acts = sum(r["acts"] or 0 for r in brain_rows)
        undone = sum(r["undone"] or 0 for r in brain_rows)
        spend = round(sum(r["cost"] or 0 for r in brain_rows), 4)
        asst = by.get(ASSISTANT_JOB) or {}
        cap = self.cfg.monthly_cap_usd or None
        jobs = []
        for j in self.jobs.values():
            s = self.settings(j)
            r = by.get(j.id) or {}
            jobs.append({"id": j.id, "title": j.title, "about": j.about, "enabled": s.enabled, "autonomy": s.autonomy,
                         "muted": s.muted, "runs": r.get("runs") or 0, "cost_usd": round(r.get("cost") or 0, 4),
                         "cards": r.get("cards") or 0, "used": r.get("used") or 0,
                         "last_run": r.get("last"), "daily_budget_usd": s.daily_budget_usd})
        return {
            "enabled": bool(self.cfg.enabled), "paused": self.paused, "ready": self.has_key(),
            "status": ("off" if not self.cfg.enabled else "paused" if self.paused else "needs_key" if not self.has_key()
                       else "capped" if self.capped() else "on"),
            "models": {"cheap": self.cfg.cheap_model, "smart": self.cfg.smart_model},
            "month": now.strftime("%Y-%m"), "spend_usd": spend,
            "assistant_usd": round(asst.get("cost") or 0, 4), "assistant_runs": asst.get("runs") or 0,
            "total_usd": round(spend + (asst.get("cost") or 0), 4), "cap_usd": cap,
            "runs": sum(r["runs"] or 0 for r in brain_rows), "cards": cards,
            "used": sum(r["used"] or 0 for r in brain_rows), "not_now": not_now, "ignored": ignored,
            "resolved": sum(r["resolved"] or 0 for r in brain_rows),
            "dismiss_rate": round((not_now + ignored) / cards, 3) if cards else None,
            "actions": acts, "undo_rate": round(undone / acts, 3) if acts else None,
            "auto_actions": list(self.cfg.auto_actions), "live_cards": len(self.cards), "jobs": jobs,
            "layout": {"enabled": bool(self.cfg.layout_enabled), "runs": (by.get("layout") or {}).get("runs") or 0,
                       "cost_usd": round((by.get("layout") or {}).get("cost") or 0, 4),
                       "people": len(self.layout.plans)},
        }

    def runs(self, job: str | None = None, limit: int = 50, before: float | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM ai_runs WHERE 1=1", []
        if job:
            sql += " AND job=?"
            args.append(job)
        if before:
            sql += " AND ts<?"
            args.append(before)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        return [self._shape(r) for r in self.hub.db.all(sql, args)]

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        r = self.hub.db.one("SELECT * FROM ai_runs WHERE id=?", [run_id])
        return self._shape(r, full=True) if r else None

    def _shape(self, r: dict[str, Any], full: bool = False) -> dict[str, Any]:
        job = self.jobs.get(r["job"])
        dec = r.get("decision")
        out = {k: r.get(k) for k in ("id", "ts", "job", "trigger", "model", "tier", "tokens_in", "tokens_out",
                                     "tokens_cached", "cost_usd", "latency_ms", "action", "card_id", "outcome",
                                     "feedback", "input_summary")}
        out["job_title"] = job.title if job else ("Ask Thomas Creek" if r["job"] == ASSISTANT_JOB else r["job"])
        out["headline"] = _headline(dec)
        if full:
            out["decision"] = dec
            out["input_hash"] = r.get("input_hash")
            out["feedback_at"] = r.get("feedback_at")
        return out


def _for(audience: dict[str, Any], s: Any) -> bool:
    if audience.get("person_ids"):
        return s.person_id in audience["person_ids"]
    return s.role in (audience.get("roles") or ["admin", "adult"])


def _hash(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _headline(dec: Any) -> str | None:
    """One line for the runs list: what it said (or saw)."""
    if not isinstance(dec, dict):
        return None
    w = dec.get("words") or {}
    if isinstance(w, dict):
        if w.get("title"):
            return str(w["title"])[:140]
        items = w.get("items") or []
        if items and isinstance(items[0], dict) and items[0].get("title"):
            return "; ".join(str(i.get("title")) for i in items[:3])[:140]
    if dec.get("notice"):
        return str(dec["notice"])[:140]
    items = dec.get("items") or []
    if items and isinstance(items[0], dict):
        return "; ".join(str(i.get("say") or i.get("kind") or i.get("key")) for i in items[:3])[:140]
    if dec.get("steps") is not None:
        return f"{dec['steps']} step(s)"
    return None
