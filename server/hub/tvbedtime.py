"""Bedtime for a TV profile.

Kids start with a limit and a night window taken from the household bedtime, so
nothing has to be set. A few minutes before either one, the TV and the parents
hear a warning. At the limit the picture stops and, if the house has a bedtime
scene, the lights dim. A parent can add time from the phone.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("hub.tvbedtime")

KV = "tv.bedtime"
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SCENE = re.compile(r"^scene\.[a-z0-9_]+$")
_SCENE_WORDS = re.compile(r"bedtime|good\s*night|goodnight|dim", re.I)


class TvBedtime:
    def __init__(self, hub: Any):
        self.hub = hub
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def rules(self, profile: str) -> dict[str, Any]:
        saved = self._saved().get(profile) if profile in ("kids", "adult") else None
        saved = saved if isinstance(saved, dict) else {}
        if profile == "adult" and "daily_minutes" not in saved and "from" not in saved:
            return {"profile": "adult", "daily_minutes": None, "from": None, "to": None,
                    "warn_minutes": int(saved.get("warn_minutes") or 10), "scene": saved.get("scene")}
        daily = saved.get("daily_minutes", 90 if profile == "kids" else None)
        start = saved.get("from", self.hub.config.child_policy.bedtime if profile == "kids" else None)
        end = saved.get("to", self.hub.config.smart.wake_time if profile == "kids" else None)
        warn = saved.get("warn_minutes", 10)
        scene = saved.get("scene", None)
        return {
            "profile": profile,
            "daily_minutes": None if daily is None else int(daily),
            "from": start if _CLOCK.match(str(start or "")) else None,
            "to": end if _CLOCK.match(str(end or "")) else None,
            "warn_minutes": max(1, min(int(warn or 10), 60)),
            "scene": scene if scene is None or scene == "" or _SCENE.match(str(scene)) else None,
        }

    def update(self, profile: str, body: dict[str, Any]) -> dict[str, Any]:
        if profile not in ("kids", "adult"):
            profile = "kids"
        all_rules = self._saved()
        cur = dict(all_rules.get(profile) or {})
        if "daily_minutes" in body:
            cur["daily_minutes"] = _minutes(body.get("daily_minutes"), 600)
        if "from" in body:
            cur["from"] = _clock(body.get("from"))
        if "to" in body:
            cur["to"] = _clock(body.get("to"))
        if "warn_minutes" in body:
            warn = _minutes(body.get("warn_minutes"), 60)
            cur["warn_minutes"] = 10 if warn is None else max(1, warn)
        if "scene" in body:
            scene = body.get("scene")
            cur["scene"] = "" if scene == "" else (str(scene) if isinstance(scene, str) and _SCENE.match(scene) else None)
        all_rules[profile] = cur
        self.hub.db.kv_set(KV, all_rules)
        return self.status(profile)

    def status(self, profile: str, now: datetime | None = None) -> dict[str, Any]:
        rules = self.rules(profile)
        moment = self._now(now)
        used = self._used(profile, moment)
        extend = self._extension(profile, moment)
        warn = rules["warn_minutes"]
        cap = rules["daily_minutes"]
        until_bed = _until(rules.get("from"), moment) if not extend else None
        in_bed = _in_window(rules.get("from"), rules.get("to"), moment) and not extend
        minutes_left = None
        state = "unlocked"
        if in_bed:
            state = "locked"
        elif until_bed is not None and until_bed <= warn:
            state = "warning"
            minutes_left = until_bed
        if cap is not None:
            left = max(0, int(cap) + (extend or 0) - used)
            if left <= 0:
                state = "locked"
                minutes_left = 0
            elif left <= warn and state != "locked":
                state = "warning"
                minutes_left = left if minutes_left is None else min(minutes_left, left)
            elif state == "unlocked":
                minutes_left = left
        out = {**rules, "state": state, "scene": self._scene_id(rules)}
        if minutes_left is not None and state != "locked":
            out["minutes_left"] = minutes_left
        elif state == "locked" and cap is not None and max(0, int(cap) + (extend or 0) - used) <= 0:
            out["minutes_left"] = 0
        return out

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        moment = self._now(now)
        sent = []
        for profile in ("kids", "adult"):
            if self._playing(profile, moment):
                self._add(profile, 1, moment)
            current = self.status(profile, moment)
            if self._announced(profile) == current["state"]:
                continue
            previous = self._announced(profile)
            self._announce(profile, current["state"])
            event = _event(current)
            self.hub.bus.publish({"type": "bedtime", "data": event})
            sent.append(event)
            if current["state"] == "locked" and previous != "locked":
                await self._dim(current.get("scene"))
            if current["state"] == "warning" and previous != "warning":
                await self._ping(current)
        return sent

    def extend(self, profile: str, minutes: int, now: datetime | None = None) -> dict[str, Any]:
        moment = self._now(now)
        extra = max(5, min(int(minutes), 120))
        usage = self._usage()
        row = dict(usage.get(profile) or {})
        row["day"] = moment.date().isoformat()
        row["extend"] = int(row.get("extend") or 0) + extra
        row["extend_until"] = moment.timestamp() + extra * 60
        usage[profile] = row
        self.hub.db.kv_set(KV + ".usage", usage)
        current = self.status(profile, moment)
        self._announce(profile, current["state"])
        self.hub.bus.publish({"type": "bedtime", "data": _event(current)})
        return current

    async def _loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("bedtime: %s", exc)
            await asyncio.sleep(60)

    def _playing(self, profile: str, now: datetime) -> bool:
        moment = now.timestamp()
        for tv in self.hub.tv_app.tvs().values():
            if tv.get("status") != "approved":
                continue
            if ("kids" if tv.get("profile") == "kids" else "adult") != profile:
                continue
            seen = float(tv.get("state_at") or 0)
            if tv.get("playing") and tv.get("foreground", True) and moment - seen < 90:
                return True
        return False

    def _add(self, profile: str, minutes: int, now: datetime) -> None:
        usage = self._usage()
        row = dict(usage.get(profile) or {})
        day = now.date().isoformat()
        if row.get("day") != day:
            row = {"day": day, "minutes": 0}
        row["minutes"] = int(row.get("minutes") or 0) + minutes
        usage[profile] = row
        self.hub.db.kv_set(KV + ".usage", usage)

    def _used(self, profile: str, now: datetime) -> int:
        row = self._usage().get(profile) or {}
        if row.get("day") != now.date().isoformat():
            return 0
        return int(row.get("minutes") or 0)

    def _extension(self, profile: str, now: datetime) -> int | None:
        row = self._usage().get(profile) or {}
        if row.get("day") != now.date().isoformat():
            return None
        until = float(row.get("extend_until") or 0)
        if until <= now.timestamp():
            return None
        return int(row.get("extend") or 0)

    def _scene_id(self, rules: dict[str, Any]) -> str | None:
        chosen = rules.get("scene")
        if chosen == "":
            return None
        if isinstance(chosen, str) and chosen in self.hub.home.states:
            return chosen
        if chosen:
            return None
        for eid, state in self.hub.home.states.items():
            if not eid.startswith("scene."):
                continue
            name = str((state.get("attributes") or {}).get("friendly_name") or eid)
            if _SCENE_WORDS.search(f"{eid} {name}"):
                return eid
        return None

    async def _dim(self, scene: str | None) -> None:
        if not scene or not self.hub.actions.is_live("scene.run"):
            return
        try:
            await self.hub.ha.call_service("scene", "turn_on", {"entity_id": scene})
        except Exception as exc:  # noqa: BLE001
            log.info("bedtime lights stayed as they were: %s", exc)

    async def _ping(self, current: dict[str, Any]) -> None:
        left = current.get("minutes_left")
        if left is None:
            return
        try:
            await self.hub.notify.send(
                title=f"{left} minutes left on the kids' TV" if current["profile"] == "kids" else f"{left} minutes left on the TV",
                body="You can add a little more from the phone.",
                cat="media", level="fyi", roles=("admin", "adult"),
                ref="thomascreek://bedtimes", group_key=f"tv-bedtime:{current['profile']}",
            )
        except Exception as exc:  # noqa: BLE001
            log.info("bedtime notice stayed on the TV: %s", exc)

    def _saved(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV) or {}
        return raw if isinstance(raw, dict) else {}

    def _usage(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV + ".usage") or {}
        return raw if isinstance(raw, dict) else {}

    def _announced(self, profile: str) -> str | None:
        return (self._usage().get(profile) or {}).get("announced")

    def _announce(self, profile: str, state: str) -> None:
        usage = self._usage()
        row = dict(usage.get(profile) or {})
        row["announced"] = state
        usage[profile] = row
        self.hub.db.kv_set(KV + ".usage", usage)

    def _now(self, now: datetime | None) -> datetime:
        tz = ZoneInfo(self.hub.config.hub.timezone)
        if now is None:
            return datetime.now(tz)
        return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)


def _event(current: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {"state": current["state"], "profile": current["profile"]}
    if "minutes_left" in current:
        data["minutes_left"] = current["minutes_left"]
    return data


def _minutes(value: object, cap: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0, min(number, cap))


def _clock(value: object) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    return text if _CLOCK.match(text) else None


def _minutes_of(value: str | None) -> int | None:
    if not value or not _CLOCK.match(value):
        return None
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _until(start: str | None, now: datetime) -> int | None:
    begin = _minutes_of(start)
    if begin is None:
        return None
    current = now.hour * 60 + now.minute
    if current <= begin:
        return begin - current
    return None


def _in_window(start: str | None, end: str | None, now: datetime) -> bool:
    begin, finish = _minutes_of(start), _minutes_of(end)
    if begin is None or finish is None or begin == finish:
        return False
    current = now.hour * 60 + now.minute
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish
