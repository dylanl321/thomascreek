"""Turning an app action into a call on the right system.

Every action passes through four gates, in order:
  1. the target exists and is the right kind (light.set cannot aim at a lock),
  2. the policy allows this person to do it to that target,
  3. the tier's extra step was satisfied (checked by the route: Face ID key),
  4. the action is live; otherwise it is a dry run that is logged, not sent.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .home import kind_for
from .policy import TIERS, Decision, Subject, Target

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.actions")

# Which entity kinds each action may aim at.
ACTION_KINDS: dict[str, tuple[str, ...]] = {
    "light.set": ("light",), "fan.set": ("fan",), "switch.set": ("switch",), "climate.set": ("climate",),
    "lock.set": ("lock",), "garage.set": ("garage",), "media.command": ("media", "speaker"),
    "media.volume": ("media", "speaker"), "media.play": ("media", "speaker"), "speaker.group": ("speaker",),
    "vacuum.start": ("vacuum",), "valve.run": ("valve", "switch"), "shower.preheat": ("shower",),
    "camera.view": ("camera",), "oven.set": ("oven",), "oven.timer": ("oven",),
}

MEDIA_COMMANDS = {
    "power": ("media_player", "toggle", {}), "play": ("media_player", "media_play", {}),
    "pause": ("media_player", "media_pause", {}), "next": ("media_player", "media_next_track", {}),
    "prev": ("media_player", "media_previous_track", {}), "vol_up": ("media_player", "volume_up", {}),
    "vol_down": ("media_player", "volume_down", {}),
}
REMOTE_COMMANDS = {"up": "up", "down": "down", "left": "left", "right": "right", "select": "select",
                   "back": "back", "home": "home"}


class ActionError(Exception):
    def __init__(self, status: int, error: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.error, self.message, self.extra = status, error, message, extra


@dataclass
class Plan:
    action: str
    target: Target
    params: dict[str, Any]
    decision: Decision
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)  # HA service calls
    custom: Any = None  # non-HA work (network, system)
    label: str = ""


def hex_to_rgb(v: str) -> list[int]:
    v = v.lstrip("#")
    if len(v) != 6:
        raise ActionError(400, "bad_params", "color must be #rrggbb")
    return [int(v[i:i + 2], 16) for i in (0, 2, 4)]


VALVE_DEFAULT_MIN = 10
SELF_APP = "thomascreek-hub"  # restarting it would cut off the request asking for it


class ActionRunner:
    def _valve_timers(self, eid: str) -> list[dict[str, Any]]:
        return [t for t in self.hub.timers.pending("ha_call")
                if ((t.get("payload") or {}).get("data") or {}).get("entity_id") == eid]

    def _clear_valve_timer(self, eid: str) -> None:
        for t in self._valve_timers(eid):
            self.hub.db.execute("DELETE FROM timers WHERE id=?", [t["id"]])

    def valve_remaining(self, eid: str) -> int | None:
        """Seconds until a running zone's automatic stop, or None when nothing is scheduled."""
        ts = self._valve_timers(eid)
        return max(0, int(ts[0]["due_at"] - time.time())) if ts else None

    def __init__(self, hub: "Hub"):
        self.hub = hub

    def is_live(self, action: str) -> bool:
        cfg = self.hub.config.hub
        return (not cfg.dry_run) or action in cfg.live_actions

    # -- planning ---------------------------------------------------------
    def plan(self, s: Subject, action: str, target_id: str | None, params: dict[str, Any]) -> Plan:
        home = self.hub.home
        if action == "mode.run":
            return self._plan_mode(s, target_id, params)
        if action == "steps.run":
            return self._plan_steps(s, params)
        if action == "scene.run":
            if not target_id or not target_id.startswith("scene.") or target_id not in home.states:
                raise ActionError(404, "not_found", "That scene isn't in the house")
            target = Target(entity_id=target_id, area=home.entity_area.get(target_id), kind="scene")
            d = self.hub.policy.decide(s, action, target, params)
            return Plan(action, target, params, d, [("scene", "turn_on", {"entity_id": target_id})], label=target_id)
        if action in ("network.pause", "network.grant"):
            return self._plan_network(s, action, target_id, params)
        if action == "announce":
            return self._plan_announce(s, params)
        if action.startswith("printer."):
            return self._plan_printer(s, action, target_id, params)
        if action == "system.app_restart":
            app = str(target_id or params.get("app") or "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", app):
                raise ActionError(400, "bad_target", "Name the NAS app to restart")
            if app == SELF_APP:
                raise ActionError(400, "not_allowed", "The hub can't restart itself")
            d = self.hub.policy.decide(s, action, Target(entity_id=app, kind="system"), params)
            return Plan(action, Target(entity_id=app, kind="system"), params, d, custom={"app_restart": app},
                        label=f"Restart {app}")

        kinds = ACTION_KINDS.get(action)
        if kinds is None:
            raise ActionError(400, "unknown_action", f"Unknown action {action}")
        st = home.states.get(target_id or "")
        if st is None:
            raise ActionError(404, "not_found", "That device isn't in the house")
        kind = kind_for(st)
        if kind not in kinds:
            raise ActionError(400, "wrong_target", f"{action} can't be used on that device")
        target = Target(entity_id=target_id, area=home.entity_area.get(target_id), kind=kind)
        d = self.hub.policy.decide(s, action, target, params)
        p = {**params, **d.clamp}
        plan = Plan(action, target, p, d, label=st.get("attributes", {}).get("friendly_name", target_id))
        if d.allowed:
            plan.calls = self._calls(action, target_id, kind, p)  # type: ignore[arg-type]
        return plan

    def _calls(self, action: str, eid: str, kind: str, p: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        t = {"entity_id": eid}
        if action == "light.set":
            if p.get("on") is False:
                return [("light", "turn_off", t)]
            data = dict(t)
            if "brightness" in p:
                data["brightness_pct"] = max(0, min(100, int(p["brightness"])))
            if p.get("color"):
                data["rgb_color"] = hex_to_rgb(p["color"])
            return [("light", "turn_on", data)]
        if action == "fan.set":
            if p.get("on") is False:
                return [("fan", "turn_off", t)]
            if "speed" in p:
                return [("fan", "set_percentage", {**t, "percentage": max(0, min(100, int(p["speed"])))})]
            return [("fan", "turn_on", t)]
        if action == "switch.set":
            return [("switch", "turn_on" if p.get("on", True) else "turn_off", t)]
        if action == "climate.set":
            calls = []
            if p.get("hvac"):
                if p["hvac"] not in ("heat", "cool", "off", "auto", "heat_cool"):
                    raise ActionError(400, "bad_params", "hvac must be heat, cool, auto or off")
                calls.append(("climate", "set_hvac_mode", {**t, "hvac_mode": p["hvac"]}))
            if "target_temp" in p:
                calls.append(("climate", "set_temperature", {**t, "temperature": float(p["target_temp"])}))
            return calls
        if action == "lock.set":
            return [("lock", "lock" if p.get("locked", True) else "unlock", t)]
        if action == "garage.set":
            return [("cover", "open_cover" if p.get("open") else "close_cover", t)]
        if action == "media.command":
            from .home import DPAD, PLAYER_KEY_SERVICES, REMOTE_KEYS

            cmd = p.get("command")
            if cmd in MEDIA_COMMANDS:
                dom, svc, extra = MEDIA_COMMANDS[cmd]
                return [(dom, svc, {**t, **extra})]
            if cmd == "stop":
                return [("media_player", "media_stop", t)]
            if cmd == "mute":
                muted = bool(self.hub.home.states.get(eid, {}).get("attributes", {}).get("is_volume_muted"))
                return [("media_player", "volume_mute", {**t, "is_volume_muted": not muted})]
            if cmd == "volume":
                return [("media_player", "volume_set", {**t, "volume_level": max(0, min(100, int(p["volume"]))) / 100})]
            if cmd == "source":
                src = p.get("source")
                options = self.hub.home.states.get(eid, {}).get("attributes", {}).get("source_list") or []
                if src not in options:
                    raise ActionError(400, "bad_params", "source must be one of the TV's inputs")
                return [("media_player", "select_source", {**t, "source": src})]
            if cmd in DPAD:
                link = self.hub.home.media_links.get(eid) or {}
                plat = link.get("platform")
                remote = link.get("remote") or self.hub.config.home_assistant.extra.get("remotes", {}).get(eid)
                key = REMOTE_KEYS.get(plat or "", {}).get(cmd)
                if plat in PLAYER_KEY_SERVICES and key:
                    dom, svc, field = PLAYER_KEY_SERVICES[plat]
                    return [(dom, svc, {**t, field: key})]
                if remote:
                    return [("remote", "send_command", {"entity_id": remote, "command": key or REMOTE_COMMANDS[cmd]})]
                raise ActionError(400, "bad_params", "this TV has no remote control in Home Assistant")
            raise ActionError(400, "bad_params", "unknown media command")
        if action == "media.volume":
            return [("media_player", "volume_set", {**t, "volume_level": max(0, min(100, int(p["volume"]))) / 100})]
        if action == "media.play":
            ctype, cid = p["_content_type"], p["_content_id"]
            return [("media_player", "play_media", {**t, "media_content_type": ctype, "media_content_id": cid})]
        if action == "speaker.group":
            members = [m for m in p.get("members", []) if m in self.hub.home.states]
            if not members:
                return [("media_player", "unjoin", t)]
            return [("media_player", "join", {**t, "group_members": members})]
        if action == "vacuum.start":
            return [("vacuum", "start", t)]
        if action == "valve.run":
            dom = eid.split(".", 1)[0]
            if p.get("on") is False:
                off = ("valve", "close_valve") if dom == "valve" else ("switch", "turn_off")
                return [(off[0], off[1], t)]
            on = ("valve", "open_valve") if dom == "valve" else ("switch", "turn_on")
            return [(on[0], on[1], t)]
        if action == "oven.set":
            return self.hub.kitchen.calls(eid, p)
        if action == "oven.timer":
            return []  # hub-side timer, see run()
        if action == "shower.preheat":
            target = self.hub.config.home_assistant.extra.get("shower_preheat_script")
            if target:
                return [("script", "turn_on", {"entity_id": target})]
            return [("water_heater", "turn_on", t)]
        raise ActionError(400, "unsupported", "That action isn't supported yet")

    def _plan_mode(self, s: Subject, mode_id: str | None, params: dict[str, Any]) -> Plan:
        mode = next((m for m in self.hub.config.modes if m.id == mode_id), None)
        if mode is None:
            raise ActionError(404, "not_found", "Unknown mode")
        if s.role not in mode.roles:
            return Plan("mode.run", Target(), params, Decision(False, reason="mode not available"), label=mode.name)
        tier = "everyday"
        calls: list[tuple[str, str, dict[str, Any]]] = []
        for step in mode.steps:
            sub = self.plan(s, step["action"], step.get("target"), step.get("params", {}))
            if not sub.decision.allowed:
                return Plan("mode.run", Target(), params, Decision(False, reason=f"step {step['action']} not allowed"), label=mode.name)
            if TIERS.index(sub.decision.tier) > TIERS.index(tier):
                tier = sub.decision.tier
            calls.extend(sub.calls)
        return Plan("mode.run", Target(entity_id=mode.id), params, Decision(True, tier, "mode"), calls, label=mode.name)

    def _plan_steps(self, s: Subject, params: dict[str, Any]) -> Plan:
        """Several taps in one (a smart card's "Goodnight"): each step is planned and allowed on its own,
        and the whole thing needs the strongest confirmation any step needs."""
        from .smart import STEP_ACTIONS

        steps = params.get("steps")
        label = str(params.get("label") or "Steps")[:60]
        if not isinstance(steps, list) or not steps or len(steps) > 40:
            raise ActionError(400, "bad_params", "steps must be a list of 1 to 40 actions")
        tier = "everyday"
        calls: list[tuple[str, str, dict[str, Any]]] = []
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in STEP_ACTIONS:
                raise ActionError(400, "bad_params", "That step can't be combined")
            sub = self.plan(s, step["action"], step.get("target"), step.get("params") or {})
            if not sub.decision.allowed:
                return Plan("steps.run", Target(), params, Decision(False, reason=f"step {step['action']} not allowed"),
                            label=label)
            if TIERS.index(sub.decision.tier) > TIERS.index(tier):
                tier = sub.decision.tier
            calls.extend(sub.calls)
        return Plan("steps.run", Target(), {"label": label, "steps": steps}, Decision(True, tier, "steps"), calls,
                    label=label)

    def _plan_network(self, s: Subject, action: str, person_id: str | None, params: dict[str, Any]) -> Plan:
        person = self.hub.db.person(person_id)
        if person is None or person["role"] != "child":
            raise ActionError(404, "not_found", "Network time is only managed for children")
        d = self.hub.policy.decide(s, action, Target(entity_id=person_id), params)
        minutes = int(params.get("minutes") or 0)
        return Plan(action, Target(entity_id=person_id), params, d, custom={"person": person, "minutes": minutes},
                    label=person["name"])

    def _plan_printer(self, s: Subject, action: str, printer_id: str | None, params: dict[str, Any]) -> Plan:
        from .bambu import HEATERS, SPEED_LEVELS, PrinterError

        try:
            p = self.hub.workshop.get(printer_id or "")
        except PrinterError as exc:
            raise ActionError(exc.status, exc.error, exc.message) from None
        if action not in ("printer.control", "printer.light", "printer.speed", "printer.temp", "printer.print"):
            raise ActionError(400, "unknown_action", f"Unknown action {action}")
        clean: dict[str, Any] = {}
        if action == "printer.control":
            if params.get("command") not in ("pause", "resume", "stop"):
                raise ActionError(400, "bad_params", "command must be pause, resume or stop")
            clean["command"] = params["command"]
        elif action == "printer.light":
            clean["on"] = bool(params.get("on", True))
        elif action == "printer.speed":
            if params.get("speed") not in SPEED_LEVELS:
                raise ActionError(400, "bad_params", "speed must be silent, standard, sport or ludicrous")
            clean["speed"] = params["speed"]
        elif action == "printer.temp":
            heater = params.get("heater")
            if heater not in HEATERS:
                raise ActionError(400, "bad_params", "heater must be nozzle, bed or chamber")
            try:
                temp = int(params.get("temp"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ActionError(400, "bad_params", "temp must be a number of °C") from None
            if not 0 <= temp <= HEATERS[heater][1]:
                raise ActionError(400, "bad_params", f"{heater} must be 0 to {HEATERS[heater][1]}°C")
            clean.update(heater=heater, temp=temp)
        else:
            if not params.get("model_id"):
                raise ActionError(400, "bad_params", "model_id is required")
            clean = {k: params[k] for k in ("model_id", "plate", "ams_mapping", "bed_leveling", "timelapse") if k in params}
        target = Target(entity_id=p.id, kind="printer")
        d = self.hub.policy.decide(s, action, target, clean)
        return Plan(action, target, clean, d, custom={"printer": p.id}, label=p.entry.name)

    async def _run_printer(self, s: Subject, plan: Plan) -> str:
        from .bambu import PrinterError

        ws = self.hub.workshop
        p = ws.get(plan.custom["printer"])
        q = plan.params
        name = p.entry.name
        try:
            if plan.action == "printer.control":
                await p.control(q["command"])
                return {"pause": f"{name} paused", "resume": f"{name} is printing again",
                        "stop": f"Stopped the print on the {name}"}[q["command"]]
            if plan.action == "printer.light":
                await p.set_light(q["on"])
                return f"{name} light {'on' if q['on'] else 'off'}"
            if plan.action == "printer.speed":
                await p.set_speed(q["speed"])
                return f"{name} speed: {q['speed']}"
            if plan.action == "printer.temp":
                await p.set_temp(q["heater"], q["temp"])
                return f"{q['heater'].capitalize()} set to {q['temp']}°C" if q["temp"] else f"{q['heater'].capitalize()} heater off"
            title = await ws.print_model(p.id, q["model_id"], q, s.person_id)
            return f"Printing {title} on the {name}"
        except PrinterError as exc:
            raise ActionError(exc.status, exc.error, exc.message) from None

    def _plan_announce(self, s: Subject, params: dict[str, Any]) -> Plan:
        text = str(params.get("text", "")).strip()[:300]
        if not text:
            raise ActionError(400, "bad_params", "Nothing to say")
        rooms = set(params.get("rooms") or [])
        speakers = [eid for eid, st in self.hub.home.states.items()
                    if kind_for(st) == "speaker" and (not rooms or self.hub.home.entity_area.get(eid) in rooms)]
        tts = self.hub.config.home_assistant.extra.get("tts_entity", "tts.home_assistant_cloud")
        d = self.hub.policy.decide(s, "announce", Target(), params)
        calls = [("tts", "speak", {"entity_id": tts, "media_player_entity_id": speakers, "message": text})] if speakers else []
        return Plan("announce", Target(), {"text": text, "rooms": sorted(rooms)}, d, calls, label="announcement")

    # -- running ----------------------------------------------------------
    async def run(self, s: Subject, plan: Plan, *, source: str = "app") -> dict[str, Any]:
        db = self.hub.db
        live = self.is_live(plan.action)
        detail = None
        try:
            if not live:
                msg = f"Practice mode: {plan.label or plan.action} was not changed"
                outcome = "dry_run"
                detail = "; ".join(f"{d}.{sv}" for d, sv, _ in plan.calls) or None
            elif plan.custom is not None:
                msg = await self._run_custom(s, plan)
                outcome = "ok"
            else:
                for dom, svc, data in plan.calls:
                    await self.hub.ha.call_service(dom, svc, data)
                msg = "Done"
                outcome = "ok"
                if plan.action == "oven.set":
                    await self.hub.kitchen.after_run(s, plan.target.entity_id, plan.params)  # type: ignore[arg-type]
                    msg = self._oven_message(plan)
                elif plan.action == "oven.timer":
                    minutes = int(plan.params.get("minutes") or 0)
                    if minutes <= 0:
                        raise ActionError(400, "bad_params", "minutes must be 1 to 360")
                    auto = plan.params.get("auto_off")
                    await self.hub.kitchen.start_timer(plan.target.entity_id, minutes,  # type: ignore[arg-type]
                                                       None if auto is None else bool(auto))
                    msg = f"Timer set for {minutes} min"
                if plan.action == "valve.run":
                    # Water never runs open-ended: every start sets its own stop (default 10 min).
                    eid = plan.target.entity_id
                    self._clear_valve_timer(eid)  # type: ignore[arg-type]
                    if plan.params.get("on") is not False:
                        minutes = max(1, min(180, int(plan.params.get("minutes") or VALVE_DEFAULT_MIN)))
                        off = ("valve", "close_valve") if eid.startswith("valve.") else ("switch", "turn_off")  # type: ignore[union-attr]
                        self.hub.timers.add(time.time() + 60 * minutes, "ha_call",
                                            {"domain": off[0], "service": off[1], "data": {"entity_id": eid}})
                        msg = f"Watering for {minutes} min"
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001
            db.log(plan.action, "error", person_id=s.person_id, device_id=s.device_id,
                   target=plan.target.entity_id, params=_safe(plan.params), detail=str(exc)[:300], source=source)
            raise ActionError(502, "failed", f"The house didn't respond: {exc}") from exc
        db.log(plan.action, outcome, person_id=s.person_id, device_id=s.device_id,
               target=plan.target.entity_id, params=_safe(plan.params), detail=detail, source=source)
        if live and plan.target.entity_id and not plan.action.startswith("printer."):
            self.hub.bus.publish({"type": "device", "entity_id": plan.target.entity_id})
        await self._notice(s, plan, outcome)
        return {"ok": True, "message": msg, "dry_run": not live}

    def _oven_message(self, plan: Plan) -> str:
        p = plan.params
        label = self.hub.kitchen.label(plan.target.entity_id or "")
        if p.get("on") is False:
            return f"{label} is off"
        temp = f" {p['temp']}°" if p.get("temp") else ""
        return f"{label}: {p.get('mode') or 'Bake'}{temp}. You'll get a notice when it's hot."

    async def _run_custom(self, s: Subject, plan: Plan) -> str:
        if plan.action.startswith("printer."):
            return await self._run_printer(s, plan)
        if "app_restart" in plan.custom:
            app = plan.custom["app_restart"]
            await self.hub.truenas.restart_app(app)
            return f"Restarting {app}"
        fw = self.hub.firewalla
        person = plan.custom["person"]
        rules = fw.rules_for(person["id"])
        if not rules:
            raise ActionError(409, "not_set_up", f"{person['name']}'s devices aren't linked to a network rule yet")
        minutes = plan.custom["minutes"]
        if plan.action == "network.pause":
            for r in rules:
                await fw.resume_rule(r)  # block rule on = internet off
            if minutes:
                self.hub.timers.add(time.time() + minutes * 60, "network.restore", {"person_id": person["id"], "to": "on"})
            return f"{person['name']}'s internet is paused"
        for r in rules:
            await fw.pause_rule(r)  # block rule paused = internet on
        if minutes:
            self.hub.timers.add(time.time() + minutes * 60, "network.restore", {"person_id": person["id"], "to": "off"})
        return f"{person['name']}'s internet is on" + (f" for {minutes} min" if minutes else "")

    async def _notice(self, s: Subject, plan: Plan, outcome: str) -> None:
        """Household notices like "Dylan's iPhone opened the garage"."""
        if outcome != "ok" or plan.action not in ("garage.set", "lock.set"):
            return
        who = s.name or "Someone"
        verb = {"garage.set": "opened" if plan.params.get("open") else "closed",
                "lock.set": "locked" if plan.params.get("locked", True) else "unlocked"}[plan.action]
        await self.hub.notify.send(
            roles=("admin", "adult"), level="fyi", cat="doors",
            title=f"{who} {verb} {plan.label}", body=None, ref="thomascreek://doors", exclude_person=s.person_id,
        )


def _safe(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if not k.startswith("_")}
