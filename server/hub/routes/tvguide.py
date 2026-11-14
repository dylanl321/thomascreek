"""Phone-facing TV guide, series, movies and the list of TVs a person can play on.

Reads use the same bearer as the rest of the app. A child, a guest or a kids' TV
only receives titles in the kids' categories. Nothing here includes the provider
login or a stream address.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..actions import ActionError
from ..adapters.base import AdapterError
from ..policy import Target
from ..state import Hub
from ..tvapp import TvAppError
from .. import tvhandoff
from ..upnext import shelf
from .deps import Caller, caller, error, hub, need
from .media import _filter, _watch_cap

router = APIRouter(prefix="/v1/tv", tags=["tv"])


def _media(h: Hub, c: Caller) -> bool:
    """True when this caller only sees kids' titles."""
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)
    return c.subject.is_child_like


async def _ready(h: Hub) -> None:
    try:
        await h.tv_catalog.ensure()
    except TvAppError as exc:
        raise error(exc.status, exc.code, exc.message) from None


@router.get("/players")
async def players(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """TVs this person may send a title to, with what's on now."""
    _media(h, c)
    now = time.time()
    names = {area["id"]: area.get("name") for area in h.home.areas.values()}
    rows = []
    for install_id, tv in sorted(h.tv_app.tvs().items(), key=lambda kv: str(kv[1].get("name") or "")):
        if tv.get("status") != "approved":
            continue
        target = Target(entity_id=tv.get("media_entity") or install_id, area=tv.get("room"), kind="tv")
        if not h.policy.decide(c.subject, "tv.stop", target, {}).allowed:
            continue
        seen = float(tv.get("state_at") or tv.get("last_seen_at") or 0)
        rows.append({
            "id": install_id,
            "name": tv.get("name"),
            "room": tv.get("room"),
            "room_name": names.get(tv.get("room") or ""),
            "profile": tv.get("profile"),
            "media_entity": tv.get("media_entity"),
            "online": bool(seen and now - seen < 120),
            "playing": tv.get("playing"),
            "last_command": tv.get("last_command"),
        })
    return {"tvs": rows}


@router.get("/devices")
async def devices(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """TVs this person can move a show to. The room they last used is first."""
    _media(h, c)
    return {"devices": tvhandoff.devices(h, c.subject)}


class HandoffIn(BaseModel):
    from_device: str = Field(min_length=1, max_length=200)
    to_device: str = Field(min_length=1, max_length=200)


@router.post("/handoff")
async def handoff(body: HandoffIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Start what's playing on another TV from the same second, then stop the first one."""
    _media(h, c)
    try:
        return await tvhandoff.handoff(h, c.subject, body.from_device, body.to_device)
    except ActionError as exc:
        raise error(exc.status, exc.error, exc.message, **exc.extra) from None


@router.get("/upnext")
async def up_next(profile: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """What to play next for this profile, ranked from what that profile watches."""
    kids = _media(h, c)
    if profile == "kids":
        kids = True
    elif profile == "adult" and kids:
        kids = True
    return shelf(h, "kids" if kids else "adult")


@router.post("/playback_events")
async def playback_events(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """How a play actually went. The hub scores sources from these. It does not call the provider."""
    _media(h, c)
    events = body.get("events")
    if not isinstance(events, list):
        raise error(400, "bad_request", "events is a list")
    return {"ok": True, "accepted": h.source_health.accept(events)}


@router.get("/health")
async def channel_health(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Sources we have played, worst first. A source with no plays is not listed."""
    _media(h, c)
    return h.source_health.worst()


@router.get("/bedtime")
async def bedtime(profile: str = "kids", c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The rules and whether that profile is in the warning or locked right now."""
    _media(h, c)
    return h.tv_bedtime.status(_profile(c, profile))


@router.put("/bedtime")
async def put_bedtime(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only Dylan can change this")
    return h.tv_bedtime.update(_profile(c, str(body.get("profile") or "kids")), body)


class BedtimeExtend(BaseModel):
    profile: str = "kids"
    minutes: int = Field(ge=5, le=120)


@router.post("/bedtime/extend")
async def extend_bedtime(body: BedtimeExtend, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A parent adds a few minutes. A child is asked to wait."""
    _media(h, c)
    if c.subject.role not in ("admin", "adult"):
        raise error(403, "forbidden", "Ask a parent", ask_parent=c.subject.is_child_like)
    return h.tv_bedtime.extend(_profile(c, body.profile), body.minutes)


def _profile(c: Caller, requested: str) -> str:
    if c.subject.tv and c.subject.is_child_like:
        return "kids"
    if c.subject.is_child_like:
        return "kids"
    return "kids" if requested == "kids" else "adult"


@router.get("/guide")
async def guide(start: float | None = None, end: float | None = None, channel: str | None = None,
                c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Now, next and the programmes in a window. `start` and `end` are unix seconds."""
    kids = _media(h, c)
    await _ready(h)
    now = time.time()
    begin = now - 3600 if start is None else start
    finish = now + 6 * 3600 if end is None else end
    if finish <= begin or finish - begin > 48 * 3600:
        raise error(400, "bad_request", "Ask for a window of up to 48 hours")
    body = h.tv_catalog.guide(start=begin, end=finish, channel=channel or None, kids_only=kids, now=now)
    h.source_health.annotate(body.get("channels") or [])
    return body


@router.get("/series")
async def series(q: str | None = None, category: str | None = None, limit: int = 60,
                 c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Series in the provider library."""
    kids = _media(h, c)
    await _ready(h)
    return h.tv_catalog.series(q=q, category=category, kids_only=kids, limit=_limit(limit))


@router.get("/series/{series_id:path}")
async def one_series(series_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Seasons and episodes for one series. The id is `series:<source>:<id>`."""
    kids = _media(h, c)
    await _ready(h)
    _known(h, "series", series_id, kids)
    try:
        return await h.tv_catalog.fetch_series(series_id)
    except TvAppError as exc:
        raise error(exc.status, exc.code, exc.message) from None


@router.get("/movies")
async def movies(q: str | None = None, category: str | None = None, limit: int = 60,
                 c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Movies in the provider library."""
    kids = _media(h, c)
    await _ready(h)
    return h.tv_catalog.movies(q=q, category=category, kids_only=kids, limit=_limit(limit))


@router.get("/movies/{movie_id:path}")
async def one_movie(movie_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """One movie, including artwork and the plot. The id is `vod:<source>:<id>`."""
    kids = _media(h, c)
    await _ready(h)
    _known(h, "movies", movie_id, kids)
    try:
        return await h.tv_catalog.fetch_movie(movie_id)
    except TvAppError as exc:
        raise error(exc.status, exc.code, exc.message) from None


@router.get("/library/search")
async def library_search(q: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Channels, programmes in the cached guide, series and movies matching these words."""
    kids = _media(h, c)
    if len(q.strip()) < 2:
        raise error(400, "bad_request", "Type at least two letters")
    await _ready(h)
    return h.tv_catalog.search(q, kids_only=kids)


def _plex_item(item: dict[str, Any], machine: str | None) -> dict[str, Any]:
    link = f"plex://preplay?metadataKey=/library/metadata/{item['id']}"
    if machine:
        link += f"&server={machine}"
    return {
        "id": item["id"], "title": item.get("title"), "year": item.get("year"), "kind": item.get("kind"),
        "poster_url": item.get("poster_url"), "summary": item.get("summary"), "show": item.get("show"),
        "season": item.get("season"), "episode": item.get("episode"), "rating": item.get("rating"),
        "deep_link": link, "package": "com.plexapp.android",
    }


@router.get("/plex")
async def tv_plex(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Continue watching, on deck and recently added, for the TV app's Plex row. Art is a hub path."""
    _watch_cap(h, c)
    if not h.plex.enabled:
        raise error(503, "unavailable", "Plex isn't set up")
    try:
        try:
            machine = await h.plex.identity()
        except AdapterError:
            machine = None
        deck = await h.plex.continue_watching()
        recent = await h.plex.recently_added()
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    continuing = [row for row in deck if row.get("view_offset_s")]
    on_deck = [row for row in deck if not row.get("view_offset_s")]

    def rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_plex_item(row, machine) for row in _filter(h, c, items)]

    return {"continue": rows(continuing), "on_deck": rows(on_deck), "recent": rows(recent)}


def _limit(limit: int) -> int:
    return max(1, min(limit, 100))


def _known(h: Hub, kind: str, item_id: str, kids_only: bool) -> None:
    rows = h.tv_catalog.load().get(kind) or []
    row = next((item for item in rows if item.get("id") == item_id), None)
    if row is None or (kids_only and not h.tv_catalog._is_kids(row.get("category"))):
        raise error(404, "not_found", "That isn't in the library")
