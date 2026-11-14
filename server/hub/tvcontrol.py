"""The phone as a remote for one TV.

A tap is sent on that TV's event stream and the phone is answered at once.
The hub does not wait, and it does not ask the TV to confirm. Moving a show
to another room stays the handoff path.
"""

from __future__ import annotations

import time
from typing import Any

from .artwork import programme_key
from .db import new_id
from .home import iso
from .policy import Subject, Target
from .state import Hub
from .tvapp import TvAppError, _position
from .tvlineup import valid_key

COMMANDS = frozenset({"play", "pause", "seek", "channel_up", "channel_down", "tune", "text", "back", "home"})
WINDOW_S = 5.0
MAX_IN_WINDOW = 12


class RemoteError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def with_artwork(h: Hub, playing: object) -> dict[str, Any] | None:
    """The now-playing card, with a poster or logo when one can be named."""
    if not isinstance(playing, dict):
        return None
    shown = dict(playing)
    poster = _artwork(h, shown)
    if poster and not shown.get("poster_url"):
        shown["poster_url"] = poster
    return shown


def send(h: Hub, subject: Subject, body: dict[str, Any]) -> dict[str, Any]:
    """Relay one remote button. The answer does not wait for the TV."""
    if not isinstance(body, dict):
        raise RemoteError(400, "bad_request", "That button isn't on this remote.")
    command = body.get("command")
    if not isinstance(command, str) or command not in COMMANDS:
        raise RemoteError(400, "bad_request", "That button isn't on this remote.")
    install_id, tv = _tv(h, subject, body.get("device"))
    if not _room(h, subject, tv, install_id):
        raise RemoteError(403, "forbidden", "That room isn't one you can use")
    data = _fields(h, subject, tv, command, body)
    if not _pace(h, subject.person_id, install_id):
        return {"ok": False, "message": "That's as fast as it goes."}
    event_id = new_id("tvr")
    payload: dict[str, Any] = {
        "id": event_id,
        "install_id": install_id,
        "command": command,
        "at": iso(time.time()),
    }
    payload.update(data)
    h.bus.publish({"type": "tv_remote", "data": payload}, device_id=f"tv:{install_id}")
    return {"ok": True, "id": event_id}


def _artwork(h: Hub, playing: dict[str, Any]) -> str | None:
    art = getattr(h, "artwork", None)
    if art is None:
        return None
    movie = playing.get("movie_id")
    series = playing.get("series_id")
    title = playing.get("title")
    channel = playing.get("channel_key")
    if isinstance(movie, str) and movie:
        return art.public_url("poster", movie)
    if isinstance(series, str) and series:
        return art.public_url("poster", series)
    if isinstance(title, str) and title.strip():
        return art.public_url("poster", programme_key(title))
    if isinstance(channel, str) and channel:
        return art.public_url("logo", channel)
    return None


def _tv(h: Hub, subject: Subject, device: object) -> tuple[str, dict[str, Any]]:
    chosen = device if isinstance(device, str) and device.strip() else _guess(h, subject)
    if not chosen:
        raise RemoteError(200, "which_room", "Pick a room.")
    try:
        install_id, tv = h.tv_app.resolve(chosen.strip())
    except TvAppError as exc:
        raise RemoteError(exc.status, exc.code, exc.message) from None
    if tv.get("status") != "approved":
        raise RemoteError(403, "pending", "That TV isn't ready yet")
    return install_id, tv


def _guess(h: Hub, subject: Subject) -> str | None:
    # The room list lives in tvhandoff, and that list asks this module for artwork.
    from .tvhandoff import devices

    rows = devices(h, subject)
    playing = [row for row in rows if row.get("playing")]
    if len(playing) == 1:
        return str(playing[0]["id"])
    online = [row for row in rows if row.get("online")]
    if len(online) == 1:
        return str(online[0]["id"])
    usual = next((row for row in rows if row.get("usual")), None)
    if usual is not None:
        return str(usual["id"])
    if len(rows) == 1:
        return str(rows[0]["id"])
    return None


def _room(h: Hub, subject: Subject, tv: dict[str, Any], install_id: str) -> bool:
    target = Target(entity_id=tv.get("media_entity") or install_id, area=tv.get("room"), kind="tv")
    return bool(h.policy.decide(subject, "tv.stop", target, {}).allowed)


def _fields(h: Hub, subject: Subject, tv: dict[str, Any], command: str, body: dict[str, Any]) -> dict[str, Any]:
    if command == "seek":
        if body.get("position_s") is None:
            raise RemoteError(400, "bad_request", "Say where to skip to.")
        try:
            return {"position_s": _position(body.get("position_s"))}
        except TvAppError as exc:
            raise RemoteError(exc.status, exc.code, exc.message) from None
    if command == "tune":
        key = body.get("channel_key")
        if not isinstance(key, str) or not valid_key(key):
            raise RemoteError(400, "bad_request", "Say which channel.")
        channel = _channel(h, key)
        if (tv.get("profile") == "kids" or subject.is_child_like) and not channel.get("kids"):
            raise RemoteError(403, "forbidden", "That channel isn't for this TV")
        return {"channel_key": channel["key"]}
    if command == "text":
        text = body.get("text")
        if not isinstance(text, str):
            raise RemoteError(400, "bad_request", "Say what to type.")
        cleaned = " ".join(text.split())
        if not cleaned or len(cleaned) > 80 or "://" in cleaned:
            raise RemoteError(400, "bad_request", "Say what to type.")
        return {"text": cleaned}
    return {}


def _channel(h: Hub, key: str) -> dict[str, Any]:
    try:
        return h.tv_app.require_channel(key)
    except TvAppError as exc:
        if exc.code != "not_found":
            raise RemoteError(exc.status, exc.code, exc.message) from None
    found = h.tv_catalog.channel(key)
    if found is None:
        raise RemoteError(404, "not_found", "That channel isn't in the lineup")
    return found


def _pace(h: Hub, person_id: str | None, install_id: str) -> bool:
    now = time.time()
    store: dict[tuple[str, str], list[float]] = getattr(h, "_remote_times", None)
    if store is None:
        store = {}
        h._remote_times = store  # type: ignore[attr-defined]
    key = (person_id or "", install_id)
    recent = [stamp for stamp in store.get(key, []) if now - stamp < WINDOW_S]
    if len(recent) >= MAX_IN_WINDOW:
        store[key] = recent
        return False
    recent.append(now)
    store[key] = recent
    return True
