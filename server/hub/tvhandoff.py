"""Move what's playing from one TV to another.

The phone asks once. The hub reads where the show is, tells the other TV to
start there, then tells the first TV to stop. Rooms and TVs are the ones
already enrolled. Nothing here needs a setting.
"""

from __future__ import annotations

import time
from typing import Any

from .actions import ActionError
from .policy import Subject, Target
from .state import Hub
from .tvapp import TvAppError

KV_RANK = "tv.handoff.rank"
ONLINE_S = 120
_IDENTITY = ("channel_key", "movie_id", "episode_id")


def devices(h: Hub, subject: Subject, now: float | None = None) -> list[dict[str, Any]]:
    """TVs this person can stop, in the order they're likely to want next."""
    now = time.time() if now is None else now
    rows = []
    for install_id, tv in h.tv_app.tvs().items():
        if tv.get("status") != "approved":
            continue
        target = Target(entity_id=tv.get("media_entity") or install_id, area=tv.get("room"), kind="tv")
        if not h.policy.decide(subject, "tv.stop", target, {}).allowed:
            continue
        seen = float(tv.get("state_at") or tv.get("last_seen_at") or 0)
        row: dict[str, Any] = {
            "id": install_id,
            "name": tv.get("name"),
            "room": tv.get("room"),
            "online": bool(seen and now - seen < ONLINE_S),
            "playing": tv.get("playing"),
        }
        if tv.get("media_entity"):
            row["media_entity"] = tv["media_entity"]
        rows.append(row)
    return _order(h, subject.person_id, rows)


def source_playing(h: Hub, install_id: str) -> dict[str, Any] | None:
    """What's on, with the channel or title filled in from the last play when the TV only sent a name."""
    tv = h.tv_app.tvs().get(install_id) or {}
    playing = tv.get("playing")
    if not isinstance(playing, dict):
        return None
    merged = dict(playing)
    if not any(merged.get(key) for key in _IDENTITY):
        last = tv.get("last_play") or {}
        for key, value in last.items():
            if value is not None and merged.get(key) in (None, ""):
                merged[key] = value
    if not any(merged.get(key) for key in _IDENTITY):
        return None
    return merged


def play_params(playing: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key in ("kind", "channel_key", "movie_id", "series_id", "episode_id", "title", "start"):
        if playing.get(key) not in (None, ""):
            params[key] = playing[key]
    position = playing.get("position_s")
    if isinstance(position, (int, float)) and not isinstance(position, bool):
        params["resume"] = int(position)
    return params


async def handoff(h: Hub, subject: Subject, from_device: str, to_device: str) -> dict[str, Any]:
    """Play `from_device`'s show on `to_device` from the same second, then stop the first TV."""
    try:
        src_id, src = h.tv_app.resolve(str(from_device))
        dst_id, dst = h.tv_app.resolve(str(to_device))
    except TvAppError as exc:
        raise ActionError(exc.status, exc.code, exc.message) from None
    if src.get("status") != "approved" or dst.get("status") != "approved":
        raise ActionError(403, "pending", "That TV isn't ready yet")
    src_target = Target(entity_id=src.get("media_entity") or src_id, area=src.get("room"), kind="tv")
    dst_target = Target(entity_id=dst.get("media_entity") or dst_id, area=dst.get("room"), kind="tv")
    if not h.policy.decide(subject, "tv.stop", src_target, {}).allowed or not h.policy.decide(subject, "tv.play", dst_target, {}).allowed:
        raise ActionError(403, "forbidden", "That room isn't one you can use", ask_parent=subject.is_child_like)
    if src_id == dst_id:
        return {"ok": False, "moved": False, "message": "It's already playing there", "from": src_id, "to": dst_id}

    playing = source_playing(h, src_id)
    if playing is None:
        name = src.get("name") or "That TV"
        return {"ok": False, "moved": False, "message": f"Nothing is playing on {name}", "from": src_id, "to": dst_id}

    params = play_params(playing)
    try:
        play = h.actions.plan(subject, "tv.play", dst_id, params)
        stop = h.actions.plan(subject, "tv.stop", src_id, {})
    except ActionError as exc:
        message = exc.message
        if dst.get("profile") == "kids" and "isn't for this TV" in message:
            message = f"{dst.get('name') or 'That room'} only plays kids' shows"
        raise ActionError(exc.status, exc.error, message, **exc.extra) from None
    if not play.decision.allowed or not stop.decision.allowed:
        raise ActionError(403, "forbidden", "That room isn't one you can use", ask_parent=subject.is_child_like)

    played = await h.actions.run(subject, play, source="app")
    stopped: dict[str, Any] | None = None
    try:
        stopped = await h.actions.run(subject, stop, source="app")
    except ActionError:
        stopped = None
    _remember(h, subject.person_id, dst_id)
    title = params.get("title") or playing.get("name") or "It"
    where = dst.get("name") or "the other TV"
    seen = float(dst.get("state_at") or dst.get("last_seen_at") or 0)
    awake = bool(seen and time.time() - seen < ONLINE_S)
    if played.get("dry_run"):
        message = f"Would move {title} to {where}"
    elif stopped is None:
        message = f"Playing {title} on {where}. {src.get('name') or 'The first TV'} may take a moment to stop."
    elif not awake:
        message = f"Moved {title} to {where}. It'll come on when that TV wakes up."
    else:
        message = f"Moved {title} to {where}"
    out: dict[str, Any] = {
        "ok": True, "moved": True, "message": message, "from": src_id, "to": dst_id,
        "dry_run": bool(played.get("dry_run")),
    }
    if played.get("command_id"):
        out["command_id"] = played["command_id"]
    if stopped and stopped.get("command_id"):
        out["stop_command_id"] = stopped["command_id"]
    return out


def _remember(h: Hub, person_id: str | None, to_id: str) -> None:
    if not person_id:
        return
    raw = h.db.kv_get(KV_RANK) or {}
    if not isinstance(raw, dict):
        raw = {}
    person = dict(raw.get(person_id) or {})
    counts = dict(person.get("counts") or {})
    counts[to_id] = int(counts.get(to_id) or 0) + 1
    person["counts"] = counts
    person["last"] = to_id
    raw[person_id] = person
    h.db.kv_set(KV_RANK, raw)


def _order(h: Hub, person_id: str | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = h.db.kv_get(KV_RANK) or {}
    person = raw.get(person_id) if isinstance(raw, dict) and person_id else {}
    if not isinstance(person, dict):
        person = {}
    counts = person.get("counts") if isinstance(person.get("counts"), dict) else {}
    last = person.get("last")

    def key(row: dict[str, Any]) -> tuple:
        return (row["id"] != last, 0 if row["online"] else 1, -int(counts.get(row["id"]) or 0),
                str(row.get("name") or "").casefold())

    ordered = sorted(rows, key=key)
    if last and any(row["id"] == last for row in ordered):
        for row in ordered:
            if row["id"] == last:
                row["usual"] = True
                break
    return ordered
