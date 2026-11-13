"""Turning an app action into a call on the right system.

Every action passes through four gates, in order:
  1. the target exists and is the right kind (light.set cannot aim at a lock),
  2. the policy allows this person to do it to that target,
  3. the tier's extra step was satisfied (checked by the route: Face ID key),
  4. the action is live; otherwise it is a dry run that is logged, not sent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .adapters.base import AdapterError
from .home import iso, kind_for
from .policy import TIERS, Decision, Subject, Target
from .tvapp import TvAppError

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
NAS_JOB_POLL_S = 5.0
NAS_APP_OPS = {"start": "Start", "stop": "Stop", "restart": "Restart", "update": "Update"}


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
        self._nas_batch: asyncio.Task | None = None

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
        if action in ("tv.play", "tv.stop"):
            return self._plan_tv(s, action, target_id, params)
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
        if action.startswith("nas."):
            return self._plan_nas(s, action, target_id, params)

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
            if action in ("media.command", "media.play", "media.volume"):
                plan.calls = self._with_tv(action, target_id, p, plan.calls)  # type: ignore[arg-type]
        return plan

    def _plan_tv(self, s: Subject, action: str, target_id: str | None, params: dict[str, Any]) -> Plan:
        """Hand a title to the Thomas Creek TV app. The TV already has the provider login from config."""
        allowed = {"kind", "channel_key", "start", "movie_id", "series_id", "episode_id", "resume", "title"}
        if action == "tv.stop":
            allowed = set()
        if set(params) - allowed:
            raise ActionError(400, "bad_params", "Unknown parameter")
        if not target_id:
            raise ActionError(400, "bad_target", "Name the TV")
        try:
            install_id, tv = self.hub.tv_app.resolve(str(target_id))
        except TvAppError as exc:
            raise ActionError(exc.status, exc.code, exc.message) from None
        if tv.get("status") == "revoked":
            raise ActionError(403, "revoked", "That TV was removed")
        if tv.get("status") != "approved":
            raise ActionError(403, "pending", "That TV isn't approved yet")
        command: dict[str, Any] = {"action": "stop" if action == "tv.stop" else "play"}
        if action == "tv.play":
            command.update(self._play_command(s, tv, params))
        target = Target(entity_id=tv.get("media_entity") or install_id, area=tv.get("room"), kind="tv")
        decision = self.hub.policy.decide(s, action, target, params)
        return Plan(action, target, params, decision,
                    custom={"tv_command": True, "install_id": install_id, "command": command},
                    label=str(tv.get("name") or "the TV"))

    def _play_command(self, s: Subject, tv: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind") or "").strip() or _play_kind(params)
        if kind not in ("live", "catchup", "movie", "episode"):
            raise ActionError(400, "bad_params", "kind is live, catchup, movie or episode")
        title = params.get("title")
        if title is not None and not isinstance(title, str):
            raise ActionError(400, "bad_params", "title must be text")
        resume = _resume(params.get("resume"))
        kids_lock = tv.get("profile") == "kids" or s.is_child_like
        if kind in ("live", "catchup"):
            try:
                channel = _channel_for(self.hub, str(params.get("channel_key") or ""))
            except TvAppError as exc:
                raise ActionError(exc.status, exc.code, exc.message) from None
            if kids_lock and not channel.get("kids"):
                raise ActionError(403, "forbidden", "That channel isn't for this TV", ask_parent=s.is_child_like)
            command: dict[str, Any] = {
                "kind": kind, "channel_key": channel["key"], "name": channel.get("name"),
                "number": channel.get("number"), "title": (title or "").strip()[:120] or None,
            }
            if kind == "catchup":
                start = _parse_start(params.get("start"))
                days = int(channel.get("catchup_days") or 0)
                now = time.time()
                if days <= 0 or start > now or start < now - days * 86400:
                    raise ActionError(400, "bad_params", "That showing isn't available to start over")
                command["start"] = iso(start)
            if resume is not None:
                command["resume"] = resume
            return command
        if kind == "movie":
            item = self.hub.tv_catalog.movie(str(params.get("movie_id") or ""))
            if item is None:
                raise ActionError(404, "not_found", "That movie isn't in the library")
            if kids_lock and not item.get("kids"):
                raise ActionError(403, "forbidden", "That movie isn't for this TV", ask_parent=s.is_child_like)
            command = {"kind": "movie", "movie_id": item["id"], "name": item.get("name"),
                       "title": (title or item.get("name") or "").strip()[:120] or None}
        else:
            episode = self.hub.tv_catalog.episode(str(params.get("episode_id") or ""))
            if episode is None:
                raise ActionError(404, "not_found", "That episode isn't in the library")
            if kids_lock and not episode.get("kids"):
                raise ActionError(403, "forbidden", "That episode isn't for this TV", ask_parent=s.is_child_like)
            series_id = str(params.get("series_id") or episode.get("series_id") or "")
            if params.get("series_id") and params.get("series_id") != episode.get("series_id"):
                raise ActionError(400, "bad_params", "That episode isn't part of that series")
            command = {"kind": "episode", "episode_id": episode["id"], "series_id": series_id or None,
                       "name": episode.get("series_name") or episode.get("title"),
                       "title": (title or episode.get("title") or "").strip()[:120] or None}
        if resume is not None:
            command["resume"] = resume
        return command

    pause = staticmethod(asyncio.sleep)  # tests make TV recovery waits instant

    async def _run_tv_calls(self, plan: Plan) -> None:
        """Send a TV's calls, fixing the usual problems on the way so nobody has to.

        - A TV that dropped its connection to Home Assistant (its remote shows "unavailable") is reconnected
          by reloading its integration, then the press is sent again.
        - A call that fails once is retried after a short pause (Wi-Fi TVs miss the odd packet).
        - A TV told to turn on that stays off gets the wake-up key from its remote too.
        """
        home = self.hub.home

        def remote_of(x: str) -> str | None:
            if x.startswith("remote."):
                return x
            return (home.media_links.get(x) or {}).get("remote") or \
                self.hub.config.home_assistant.extra.get("remotes", {}).get(x)

        checked: set[str] = set()
        for dom, svc, data in plan.calls:
            x = str(data.get("entity_id") or plan.target.entity_id or "")
            remote = remote_of(x)
            if remote and remote not in checked:
                checked.add(remote)
                if home.states.get(remote, {}).get("state") == "unavailable":
                    await self._reconnect_tv(remote)
            try:
                await self.hub.ha.call_service(dom, svc, data)
            except Exception as exc:  # noqa: BLE001
                log.info("TV call %s.%s failed (%s); reconnecting and retrying", dom, svc, exc)
                if remote:
                    await self._reconnect_tv(remote)
                else:
                    await self.pause(1.0)
                await self.hub.ha.call_service(dom, svc, data)
            if (dom, svc) == ("media_player", "turn_on"):
                if remote:
                    await self.pause(1.5)
                    if await self._fresh_state(x) in ("off", "standby"):
                        try:
                            await self.hub.ha.call_service("remote", "turn_on", {"entity_id": remote})
                        except Exception as exc:  # noqa: BLE001
                            log.info("wake-up through the remote failed: %s", exc)
                elif x != plan.target.entity_id:
                    await self.pause(2.0)  # a TV that just woke ignores an input switch for a moment

    async def _fresh_state(self, eid: str) -> str | None:
        try:
            st = await self.hub.ha.get_json(f"/api/states/{eid}")
            return (st or {}).get("state")
        except Exception:  # noqa: BLE001
            return self.hub.home.states.get(eid, {}).get("state")

    async def _reconnect_tv(self, remote: str) -> None:
        try:
            await self.hub.ha.call_service("homeassistant", "reload_config_entry", {"entity_id": remote})
        except Exception as exc:  # noqa: BLE001
            log.info("reconnecting %s failed: %s", remote, exc)
        await self.pause(3.0)

    def _with_tv(self, action: str, box: str, p: dict[str, Any],
                 calls: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[str, str, dict[str, Any]]]:
        """A streaming box plugged into a TV: wake the TV and switch it to the box's input, send volume to the
        TV, and turn both on or off together (see hookups.py)."""
        from .hookups import ASLEEP, VOLUME

        hk = self.hub.hookups
        link = hk.get(box)
        if not link:
            return calls
        tv = link["display"]
        cmd = p.get("command") if action == "media.command" else None
        if (action == "media.volume" or cmd in VOLUME) and tv in self.hub.home.states:
            return self._calls(action, tv, "media", p)
        if cmd == "power":
            # What the family sees is the TV, so its state decides: TV off means "turn it all on".
            b = {"entity_id": box}
            if self.hub.home.states.get(tv, {}).get("state") in ASLEEP:
                wake = [] if self.hub.home.states.get(box, {}).get("state") not in ASLEEP else \
                    [("media_player", "turn_on", b)]
                return wake + hk.prepare(box)
            return [("media_player", "turn_off", b), ("media_player", "turn_off", {"entity_id": tv})]
        return hk.prepare(box) + calls

    def _app_call(self, eid: str, plat: str | None, app: str) -> list[tuple[str, str, dict[str, Any]]]:
        """Open an app the same way on any TV: "youtube" becomes a package launch on a Google TV and the
        TV's own "YouTube" source on an Apple TV, Roku, LG or Samsung."""
        from .tvremote import device_apps, find_app

        t = {"entity_id": eid}
        sources = [str(x) for x in self.hub.home.states.get(eid, {}).get("attributes", {}).get("source_list") or []]
        hit = find_app(device_apps(plat, sources), app)
        if hit is None and plat == "androidtv_remote" and re.fullmatch(r"[a-z][\w]*(\.[\w]+){2,}", app):
            hit = {"target": app}  # any installed package, by name
        if hit is None:
            raise ActionError(400, "bad_params", "that app isn't on this TV")
        st = self.hub.home.states.get(eid, {})
        wake = [("media_player", "turn_on", t)] if st.get("state") in ("off", "standby") else []
        if plat == "androidtv_remote":
            return wake + [("media_player", "play_media", {**t, "media_content_type": "app",
                                                           "media_content_id": hit["target"]})]
        return wake + [("media_player", "select_source", {**t, "source": hit["target"]})]

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
            if "target_low" in p or "target_high" in p:
                # Auto (heat_cool) holds a range: Home Assistant wants both ends in one call.
                try:
                    low, high = float(p["target_low"]), float(p["target_high"])
                except (KeyError, TypeError, ValueError):
                    raise ActionError(400, "bad_params", "target_low and target_high go together") from None
                if low >= high:
                    raise ActionError(400, "bad_params", "Heat to must be below cool to")
                calls.append(("climate", "set_temperature", {**t, "target_temp_low": low, "target_temp_high": high}))
            elif "target_temp" in p:
                calls.append(("climate", "set_temperature", {**t, "temperature": float(p["target_temp"])}))
            return calls
        if action == "lock.set":
            return [("lock", "lock" if p.get("locked", True) else "unlock", t)]
        if action == "garage.set":
            return [("cover", "open_cover" if p.get("open") else "close_cover", t)]
        if action == "media.command":
            from .tvremote import ALL_KEYS, DPAD, PLAYER_KEY_SERVICES, REMOTE_KEYS, text_keys

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
            link = self.hub.home.media_links.get(eid) or {}
            plat = link.get("platform")
            remote = link.get("remote") or self.hub.config.home_assistant.extra.get("remotes", {}).get(eid)
            if cmd == "app":
                return self._app_call(eid, plat, str(p.get("app") or ""))
            if cmd == "text":
                keys = text_keys(str(p.get("text") or "")[:120])
                if plat != "androidtv_remote" or not remote:
                    raise ActionError(400, "bad_params", "this TV can't take typing from the remote")
                if not keys:
                    raise ActionError(400, "bad_params", "nothing to type")
                return [("remote", "send_command", {"entity_id": remote, "command": keys, "delay_secs": 0.05})]
            if cmd in DPAD or cmd in ALL_KEYS:
                key = REMOTE_KEYS.get(plat or "", {}).get(cmd)
                asleep = self.hub.home.states.get(eid, {}).get("state") in ("off", "standby")
                wake = [("media_player", "turn_on", t)] if asleep and cmd in ("select", "home") else []
                if key is None and cmd not in DPAD:
                    raise ActionError(400, "bad_params", "this TV's remote doesn't have that button")
                if plat in PLAYER_KEY_SERVICES and key:
                    dom, svc, field = PLAYER_KEY_SERVICES[plat]
                    return wake + [(dom, svc, {**t, field: key})]
                if remote:
                    return wake + [("remote", "send_command",
                                    {"entity_id": remote, "command": key or REMOTE_COMMANDS[cmd]})]
                raise ActionError(400, "bad_params", "This TV hasn't been added for the remote yet. A grown-up can "
                                  "add it from Media → Add a TV or box.")
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

    def _plan_nas(self, s: Subject, action: str, target_id: str | None, params: dict[str, Any]) -> Plan:
        """NAS changes: app start/stop/restart/update, alert dismiss, scrub, snapshot, sharing services."""
        nas = self.hub.truenas
        tgt = str(target_id or "")
        name_ok = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", tgt) is not None
        d = self.hub.policy.decide(s, action, Target(entity_id=tgt or None, kind="system"), params)
        if not d.allowed:
            return Plan(action, Target(entity_id=tgt or None, kind="system"), {}, d, label=action)
        work: dict[str, Any] = {"nas": action}
        if action == "nas.app":
            op = str(params.get("op") or "")
            if op not in NAS_APP_OPS or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", tgt):
                raise ActionError(400, "bad_params", "Name the app and one of start, stop, restart, update")
            guard = nas.protection(tgt)
            if guard == "hub" and op == "stop":
                raise ActionError(400, "not_allowed", "The hub can't stop itself. Restart it instead.")
            if guard == "remote" and op in ("stop", "restart", "update") and not s.at_home and not params.get("away_ok"):
                raise ActionError(409, "away_warning",
                                  f"You're away from home. {tgt} carries your connection, so this may cut you off.",
                                  confirm_param="away_ok")
            work.update(app=tgt, op=op, self=guard == "hub")
            label = f"{NAS_APP_OPS[op]} {tgt}"
        elif action == "nas.update_all":
            label = "Update all NAS apps"
        elif action == "nas.alert":
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", tgt):
                raise ActionError(400, "bad_target", "Which alert?")
            work["alert"] = tgt
            label = "Dismiss a NAS alert"
        elif action == "nas.scrub":
            op = str(params.get("op") or "")
            if op not in ("start", "pause", "stop") or not name_ok:
                raise ActionError(400, "bad_params", "Name the pool and start, pause or stop")
            work.update(pool=tgt, op=op)
            label = f"{op.capitalize()} the {tgt} scrub"
        elif action == "nas.snapshot":
            if not name_ok or ".." in tgt:
                raise ActionError(400, "bad_target", "Name the dataset to snapshot")
            work["dataset"] = tgt
            label = f"Snapshot {tgt}"
        elif action == "nas.service":
            from .adapters.truenas import SERVICES
            if tgt not in SERVICES or not isinstance(params.get("on"), bool):
                raise ActionError(400, "bad_params", "Name a sharing service and on true/false")
            work.update(service=tgt, on=params["on"])
            label = f"Turn {tgt} {'on' if params['on'] else 'off'}"
        else:
            raise ActionError(400, "unknown_action", f"Unknown action {action}")
        clean = {k: v for k, v in params.items() if k in ("op", "on", "away_ok")}
        return Plan(action, Target(entity_id=tgt or None, kind="system"), clean, d, custom=work, label=label)

    async def _run_nas(self, plan: Plan) -> str:
        nas = self.hub.truenas
        w = plan.custom
        a = w["nas"]
        if a == "nas.app":
            app, op = w["app"], w["op"]
            if op == "update":
                info = await nas.app(app)
                if info is None:
                    raise ActionError(404, "not_found", f"{app} isn't on the NAS")
                kind = "restart" if w["self"] else ("upgrade" if info["update"] == "upgrade" else "pull")
                if not info["update"] and not w["self"]:
                    raise ActionError(409, "up_to_date", f"{app} is already up to date")
            else:
                kind = op
            if w["self"]:
                # Answer first: the hub goes down with its own container. TrueNAS runs the job on its own.
                async def later() -> None:
                    await asyncio.sleep(1.5)
                    try:
                        await nas.app_op(app, kind)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("hub self-%s failed: %s", kind, exc)
                asyncio.get_running_loop().create_task(later())
                return "The hub is restarting. Thomas Creek reconnects in about a minute."
            job = await nas.app_op(app, kind)
            w["job"] = job if isinstance(job, int) else None
            self.hub.home.mark_nas_dirty()
            return {"start": f"Starting {app}", "stop": f"Stopping {app}", "restart": f"Restarting {app}",
                    "upgrade": f"Updating {app}", "pull": f"Pulling new images for {app}"}[kind]
        if a == "nas.update_all":
            apps = [x for x in await nas.apps() if x["update"] and not x["protected"]]
            if not apps:
                raise ActionError(409, "up_to_date", "Every app is up to date")
            if self._nas_batch and not self._nas_batch.done():
                raise ActionError(409, "busy", "Apps are already updating")
            self._nas_batch = asyncio.get_running_loop().create_task(self._update_all(apps))
            return f"Updating {len(apps)} apps one at a time. You'll get a notification when they're done."
        if a == "nas.alert":
            await nas.dismiss_alert(w["alert"])
            return "Alert dismissed"
        if a == "nas.scrub":
            await nas.scrub(w["pool"], {"start": "START", "pause": "PAUSE", "stop": "STOP"}[w["op"]])
            return {"start": f"Scrubbing {w['pool']}", "pause": f"{w['pool']} scrub paused",
                    "stop": f"{w['pool']} scrub stopped"}[w["op"]]
        if a == "nas.snapshot":
            name = "thomascreek-" + time.strftime("%Y-%m-%d-%H%M", time.localtime())
            await nas.snapshot(w["dataset"], name)
            return f"Snapshot {w['dataset']}@{name} taken"
        job = await nas.service(w["service"], w["on"])
        w["job"] = job if isinstance(job, int) else None
        return f"{w['service'].upper()} {'starting' if w['on'] else 'stopping'}"

    async def _wait_job(self, job: Any, limit_s: float = 900) -> str | None:
        """Wait for a TrueNAS job; None when it succeeded, else a short reason."""
        if not isinstance(job, int):
            return None
        end = time.time() + limit_s
        while time.time() < end:
            await asyncio.sleep(NAS_JOB_POLL_S)
            j = await self.hub.truenas.job(job)
            if j is None or j["state"] == "SUCCESS":
                return None
            if j["state"] in ("FAILED", "ABORTED"):
                return (j.get("error") or j["state"].lower()).splitlines()[0][:160]
        return "took longer than 15 minutes"

    async def _update_all(self, apps: list[dict[str, Any]]) -> None:
        nas = self.hub.truenas
        failed: list[str] = []
        for x in apps:
            try:
                why = await self._wait_job(await nas.app_op(x["name"], "upgrade" if x["update"] == "upgrade" else "pull"))
            except Exception as exc:  # noqa: BLE001
                why = str(exc)[:160]
            self.hub.db.log("nas.app", "ok" if why is None else "error", target=x["name"], params={"op": "update"},
                            detail=why, source="batch")
            if why:
                failed.append(x["name"])
        self.hub.home.mark_nas_dirty()
        ok = len(apps) - len(failed)
        await self.hub.notify.send(
            title=f"Updated {ok} of {len(apps)} NAS apps" if failed else f"Updated {ok} NAS apps",
            body=("Didn't update: " + ", ".join(failed)) if failed else None,
            cat="systems", level="maintenance" if failed else "fyi", roles=["admin"], ref="thomascreek://nas/updates",
        )

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
                if plan.action in ("media.command", "media.play", "media.volume"):
                    await self._run_tv_calls(plan)
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
            if plan.action.startswith("media."):
                raise ActionError(502, "failed", f"{plan.label or 'The TV'} didn't answer, even after the hub tried to "
                                  "reconnect. Check it's plugged in and on Wi-Fi, then try again.") from exc
            raise ActionError(502, "failed", f"The house didn't respond: {exc}") from exc
        db.log(plan.action, outcome, person_id=s.person_id, device_id=s.device_id,
               target=plan.target.entity_id, params=_safe(plan.params), detail=detail, source=source)
        if live and plan.target.entity_id and not plan.action.startswith("printer."):
            self.hub.bus.publish({"type": "device", "entity_id": plan.target.entity_id})
        await self._notice(s, plan, outcome)
        out: dict[str, Any] = {"ok": True, "message": msg, "dry_run": not live}
        if isinstance(plan.custom, dict) and plan.custom.get("job"):
            out["job"] = plan.custom["job"]
        if isinstance(plan.custom, dict) and plan.custom.get("command_id"):
            out["command_id"] = plan.custom["command_id"]
        return out

    def _oven_message(self, plan: Plan) -> str:
        p = plan.params
        label = self.hub.kitchen.label(plan.target.entity_id or "")
        if p.get("on") is False:
            return f"{label} is off"
        temp = f" {p['temp']}°" if p.get("temp") else ""
        return f"{label}: {p.get('mode') or 'Bake'}{temp}. You'll get a notice when it's hot."

    def _deliver_tv(self, plan: Plan) -> str:
        info = plan.custom
        command = self.hub.tv_app.enqueue_command(info["install_id"], info["command"])
        info["command_id"] = command["id"]
        self.hub.bus.publish({"type": "tv_command", "data": command}, device_id=f"tv:{info['install_id']}")
        if plan.action == "tv.stop":
            return f"Told {plan.label} to stop"
        return f"Told {plan.label} to play {command.get('name') or command.get('title') or 'that'}"

    async def _run_custom(self, s: Subject, plan: Plan) -> str:
        if isinstance(plan.custom, dict) and plan.custom.get("tv_command"):
            return self._deliver_tv(plan)
        if plan.action.startswith("printer."):
            return await self._run_printer(s, plan)
        if isinstance(plan.custom, dict) and "nas" in plan.custom:
            try:
                return await self._run_nas(plan)
            except AdapterError as exc:
                raise ActionError(502, "nas_error", str(exc)) from None
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


def _play_kind(params: dict[str, Any]) -> str:
    if params.get("episode_id"):
        return "episode"
    if params.get("movie_id"):
        return "movie"
    if params.get("channel_key") and params.get("start") is not None:
        return "catchup"
    if params.get("channel_key"):
        return "live"
    raise ActionError(400, "bad_params", "Say what to play: a channel, a catch-up start, a movie or an episode")


def _resume(value: object) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ActionError(400, "bad_params", "resume is a number of seconds") from None
    if not 0 <= seconds <= 24 * 3600:
        raise ActionError(400, "bad_params", "resume is too far into the title")
    return seconds


def _parse_start(value: object) -> float:
    if isinstance(value, bool) or value is None:
        raise ActionError(400, "bad_params", "start is when the programme began")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ActionError(400, "bad_params", "start is when the programme began") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _channel_for(hub: "Hub", key: str) -> dict[str, Any]:
    try:
        row = hub.tv_app.require_channel(key)
    except TvAppError as exc:
        if exc.code != "not_found":
            raise
        row = None
    else:
        # The stored lineup wins for name and kids, but catch-up days come from the provider.
        extra = hub.tv_catalog.channel(key) or {}
        if extra.get("catchup_days") and not row.get("catchup_days"):
            row = {**row, "catchup_days": extra["catchup_days"]}
        return row
    found = hub.tv_catalog.channel(key)
    if found is None:
        raise TvAppError(404, "not_found", "That channel isn't in the lineup")
    return found
