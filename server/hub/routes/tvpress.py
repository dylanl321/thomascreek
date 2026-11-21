"""One remote for every TV, for the phone media apps (see hub/tvpress.py).

GET   /v1/tv/phone/tvs               the TVs this person may use: name, room, online, now_playing, watching and
                                     `controls`, the buttons that work on that TV right now (plus apps, inputs, volume)
POST  /v1/tv/phone/tvs/{id}/press    {key, position_s?, channel_key?, text?, volume?, app?, source?,
                                      movie_id?, episode_id?, series_id?, title?}
                                     -> {ok, via: app|ha|adb, message}

Keys: up down left right select back home menu, play_pause play pause rewind fast_forward seek stop,
channel_up channel_down tune send, volume_up volume_down mute volume_set, power_on power_off input open_app,
text app source.
A phone draws only the keys in a TV's `controls`. A failed press answers with a sentence to show.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from .. import tvpress
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/tv/phone/tvs", tags=["tvpress"])


def _phone(h: Hub, c: Caller) -> None:
    if c.subject.tv or not c.subject.person_id:
        raise error(403, "forbidden", "Sign in on your phone to use the remote")
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)


@router.get("")
async def list_tvs(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _phone(h, c)
    return tvpress.listing(h, c.subject)


@router.post("/{install_id}/press")
async def press(install_id: str, body: dict[str, Any] = Body(...), c: Caller = Depends(caller),
                h: Hub = Depends(hub)) -> dict[str, Any]:
    _phone(h, c)
    key = str((body or {}).get("key") or "")[:40]
    try:
        out = await tvpress.press(h, c.subject, install_id, body)
    except tvpress.PressError as exc:
        h.db.log("tvs.press", "error", person_id=c.subject.person_id, target=install_id,
                 params={"key": key}, detail=exc.message, source="app")
        raise error(exc.status, exc.code, exc.message) from None
    return out
