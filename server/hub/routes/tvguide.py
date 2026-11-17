"""Phone-facing TV guide, series, movies and the list of TVs a person can play on.

Reads use the same bearer as the rest of the app. A child, a guest or a kids' TV
only receives titles in the kids' categories. Nothing here includes the provider
login or a stream address.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..actions import ActionError
from ..adapters.base import AdapterError
from ..policy import Target
from ..state import Hub
from ..tvapp import TvAppError
from .. import tvcontrol, tvexplore, tvhandoff, tvnames, tvpicks, tvsport
from ..tvapp import _household as household
from ..upnext import shelf, warm_plex, who_key
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


@router.post("/remote")
async def remote(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """One button on the phone, sent straight to that TV's event stream."""
    _media(h, c)
    try:
        result = tvcontrol.send(h, c.subject, body)
    except tvcontrol.RemoteError as exc:
        if exc.status == 200:
            return {"ok": False, "message": exc.message}
        raise error(exc.status, exc.code, exc.message) from None
    return result


@router.post("/handoff")
async def handoff(body: HandoffIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Start what's playing on another TV from the same second, then stop the first one."""
    _media(h, c)
    try:
        return await tvhandoff.handoff(h, c.subject, body.from_device, body.to_device)
    except ActionError as exc:
        raise error(exc.status, exc.error, exc.message, **exc.extra) from None


@router.get("/explore")
async def explore(profile: str | None = None, person: str | None = None, c: Caller = Depends(caller),
                  h: Hub = Depends(hub)) -> dict[str, Any]:
    """New, popular and acclaimed titles this house can play or ask for, plus rows and a hero for whoever is
    watching (tvpicks): the same person rule as /upnext. A named child, a child's phone or a kids' TV gets the
    kids' feed; naming a grown-up never widens a kids' TV."""
    kids = _media(h, c)
    if profile == "kids" or tvexplore._profile(h, c.subject) == "kids":
        kids = True
    subject = c.subject
    if getattr(subject, "tv", False):
        found = next((p for p in household(h.db) if p["id"] == person), None) if person else None
        if found is not None and found.get("role") == "child":
            kids = True
        who = who_key(person=(found or {}).get("id") or subject.person_id, tv=subject.tv_id)
    else:
        who = who_key(person=subject.person_id)
    return tvpicks.personal_feed(h, "kids" if kids else "adult", who)


@router.post("/explore/request")
async def explore_request(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Ask for one title. The answer is a sentence."""
    _media(h, c)
    try:
        prepared = tvexplore.ask(h, c.subject, body)
    except tvexplore.ExploreError as exc:
        raise error(exc.status, "bad_request", exc.message) from None
    if prepared.get("pending_submit"):
        return await tvexplore.submit(h, prepared)
    return prepared


@router.get("/explore/art/{kind}/{media}/{tmdb_id}")
async def explore_art(kind: str, media: str, tmdb_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """A cached poster, backdrop, logo or title card. A missing file is a quiet 404."""
    _media(h, c)
    found = await tvexplore.cached_art(h, kind, media, tmdb_id)
    if found is None:
        raise error(404, "not_found", "No artwork")
    body, mime = found
    return Response(body, media_type=mime, headers={"Cache-Control": "private, max-age=604800"})


@router.get("/upnext")
async def up_next(profile: str | None = None, person: str | None = None, c: Caller = Depends(caller),
                  h: Hub = Depends(hub)) -> dict[str, Any]:
    """What to play next, ranked from what this person watches.

    A phone gets its own person's rows. A TV names who is watching with `person`; with nobody named it gets
    its own history. Either falls back to the profile's rows until there is history of its own."""
    kids = _media(h, c)
    if profile == "kids":
        kids = True
    elif profile == "adult" and kids:
        kids = True
    subject = c.subject
    if getattr(subject, "tv", False):
        named = person if person and any(p["id"] == person for p in household(h.db)) else None
        who = who_key(person=named or subject.person_id, tv=subject.tv_id)
    else:
        who = who_key(person=subject.person_id)
    await warm_plex(h, who, "kids" if kids else "adult")
    return shelf(h, "kids" if kids else "adult", who=who)


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
                channels: str | None = None,
                c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Now, next and the programmes in a window. `start` and `end` are unix seconds. `channels` is a comma-separated
    list of up to 100 channel keys, answered in one call."""
    kids = _media(h, c)
    await _ready(h)
    now = time.time()
    begin = now - 3600 if start is None else start
    finish = now + 6 * 3600 if end is None else end
    if finish <= begin or finish - begin > 48 * 3600:
        raise error(400, "bad_request", "Ask for a window of up to 48 hours")
    keys = [key.strip() for key in (channels or "").split(",") if key.strip()]
    if len(keys) > 100:
        raise error(400, "bad_request", "Ask for up to 100 channels at a time")
    body = h.tv_catalog.guide(start=begin, end=finish, channel=channel or None, kids_only=kids, now=now,
                              channels=keys or None)
    h.source_health.annotate(body.get("channels") or [])
    return body


@router.get("/sport")
async def sport(day: str | None = None, league: str | None = None, c: Caller = Depends(caller),
                h: Hub = Depends(hub)) -> dict[str, Any]:
    """Today's games (US leagues first, racing included), each with the house channels that show it:
    the broadcaster on the basic lineup (local CBS/NBC/FOX/ABC, ESPN, FS1...), then guide programmes naming
    the game, then event channels named after both teams. `watch` is the channel to play.

    `?day=` (`2026-11-18`, or an offset such as `-1`, `+2`) lists that local day's games instead, and
    `?league=NFL` keeps one league: alone it is the league page (ESPN's current board: football's week, a race
    weekend). Those answers add `day`, `league` and `failed` (leagues whose scores didn't load); without
    either parameter the body is unchanged."""
    kids = _media(h, c)
    await _ready(h)
    now = time.time()
    feed = getattr(h, "_sport", None)
    if feed is None:
        feed = h._sport = tvsport.Sport(h)
    names = [name for name, *_rest in tvsport.LEAGUES]
    if league is not None and league not in names:
        raise error(404, "not_found", "The hub doesn't follow that league")
    ymd = None
    if day is not None:
        ymd = feed.day_of(day, now)
        if ymd is None:
            raise error(400, "bad_request", "Pick a day within two weeks of today")
    extra: dict[str, Any] = {}
    if ymd is None and league is None:
        games = await feed.games(now)
    else:
        try:
            got = await (feed.day(ymd, now, league) if ymd else feed.league_page(league, now))
        except tvsport.SportError as exc:
            raise error(503, "unavailable", str(exc)) from None
        games = got["games"]
        extra = {"day": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}" if ymd else None, "league": league, "failed": got["failed"]}
    try:
        home = await h.smart.weather._location()
    except Exception:  # noqa: BLE001
        home = None
    basic = h.tv_catalog.basic(home)
    body = tvsport.match(games, h.tv_catalog, basic, kids_only=kids, now=now)
    return {"games": body, "leagues": names, "at": now, **extra}


@router.get("/names")
async def names(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """Clean film, series and category names keyed by the provider's ids, so screens never clean names.

    Answers 304 when the TV already holds this `version` (sent back as If-None-Match).
    """
    kids = _media(h, c)
    await _ready(h)
    doc = h.tv_catalog.load()
    cache = h.__dict__.setdefault("_tv_names", {})
    memo = cache.get(kids)
    if memo is None or memo[0] is not doc:
        body = tvnames.names(doc, kids_only=kids, is_kids=h.tv_catalog._is_kids)
        memo = cache[kids] = (doc, body["version"], json.dumps(body, separators=(",", ":")).encode())
    tag = f'"{memo[1]}"'
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return Response(memo[2], media_type="application/json", headers={"ETag": tag})


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
    # Any provider's film or series: the main list, or the light list of the provider the id names.
    row = h.tv_catalog.known_row(kind, item_id)
    if row is None or (kids_only and not h.tv_catalog._is_kids(row.get("category"))):
        raise error(404, "not_found", "That isn't in the library")


# -- Plex in the TV library (hub/tvplex.py) -------------------------------------------------------------------------
PLEX_FILE_AUD = "tv-plex-file"
PLEX_FILE_TTL_S = 8 * 3600


def _plex_ok(h: Hub, c: Caller, rating: object) -> bool:
    return c.subject.role in ("admin", "adult", "tablet") or rating in h.config.child_policy.ratings


@router.get("/plex/library")
async def tv_plex_library(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Every Plex film and show with TMDB / IMDb ids, so the TV folds them into its own tiles."""
    _watch_cap(h, c)
    if not h.plex.enabled:
        return {"version": "", "movies": [], "shows": []}
    try:
        doc = await h.plex_library.load()
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if c.subject.role in ("admin", "adult", "tablet"):
        return doc
    return {"version": doc["version"] + "k",
            "movies": [m for m in doc["movies"] if _plex_ok(h, c, m.get("rating"))],
            "shows": [s for s in doc["shows"] if _plex_ok(h, c, s.get("rating"))]}


def _plex_id(item_id: str) -> str:
    if not item_id.isdigit() or len(item_id) > 12:
        raise error(400, "bad_params", "Unknown title")
    return item_id


@router.get("/plex/{item_id}/episodes")
async def tv_plex_episodes(item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _watch_cap(h, c)
    _plex_id(item_id)
    try:
        show = await h.plex.metadata(item_id)
        if show is None or not _plex_ok(h, c, show.get("rating")):
            raise error(404, "not_found", "That isn't in the library")
        rows = await h.plex.episodes(item_id)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    episodes = [{
        "id": row["id"], "season": row.get("season"), "episode": row.get("episode"),
        "title": str(row.get("title") or "").split(": ", 1)[-1], "duration_s": row.get("duration_s"),
        "offset_s": row.get("view_offset_s"),
    } for row in rows if row.get("kind") == "episode" and row.get("season") is not None and row.get("episode") is not None]
    return {"episodes": episodes}


@router.post("/plex/{item_id}/play")
async def tv_plex_play(item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A pass for the file itself (direct play). The TV's player handles MKV, HEVC and the rest."""
    _watch_cap(h, c)
    _plex_id(item_id)
    try:
        item = await h.plex.metadata(item_id)
        media = await h.plex.media_info(item_id) if item else None
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if item is None or media is None or not _plex_ok(h, c, item.get("rating")) or item.get("kind") not in ("movie", "episode"):
        raise error(404, "not_found", "That isn't in the library")
    token, _exp = h.tokens.issue_scoped({"aud": PLEX_FILE_AUD, "part": media["part_key"], "dev": c.device["id"]}, PLEX_FILE_TTL_S)
    h.db.log("tv.plex.play", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=item_id,
             params={"title": item.get("title")})
    return {"url": f"/v1/tv/plex/file/{token}", "offset_s": item.get("view_offset_s"),
            "resolution": media.get("resolution"), "container": media.get("container")}


@router.get("/plex/file/{pass_token}")
async def tv_plex_file(pass_token: str, request: Request, h: Hub = Depends(hub)) -> Response:
    from ..crypto import CryptoError

    try:
        claims = h.tokens.verify(pass_token, audience=PLEX_FILE_AUD)
    except CryptoError:
        raise error(401, "expired", "Playback pass expired") from None
    part = str(claims.get("part") or "")
    if not part.startswith("/library/parts/") or ".." in part:
        raise error(404, "not_found", "Not part of this title")
    headers = h.plex.auth_headers()
    headers.pop("Accept", None)
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        client = h.plex.client()
        upstream = await client.send(client.build_request("GET", part, headers=headers), stream=True)
    except Exception:  # noqa: BLE001
        raise error(502, "unavailable", "The movie server didn't answer") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise error(502, "unavailable", "The movie server didn't answer")
    keep = {k: upstream.headers[k] for k in ("content-length", "content-range", "last-modified", "etag", "content-type")
            if k in upstream.headers}
    keep["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=keep,
                             media_type=keep.pop("content-type", "application/octet-stream"))


class PlexProgress(BaseModel):
    offset_s: float = Field(ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    watched: bool = False


@router.post("/plex/{item_id}/progress")
async def tv_plex_progress(item_id: str, body: PlexProgress, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Where the TV stopped, so Plex (and every other Plex app) resumes there."""
    _watch_cap(h, c)
    _plex_id(item_id)
    try:
        await h.plex.report_progress(item_id, int(body.offset_s * 1000),
                                     int(body.duration_s * 1000) if body.duration_s else None, body.watched)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    return {"ok": True}
