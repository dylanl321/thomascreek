"""Phone-facing TV guide, series, movies and the list of TVs a person can play on.

Reads use the same bearer as the rest of the app. A child, a guest or a kids' TV
only receives titles in the kids' categories. Nothing here includes the provider
login or a stream address.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
from .. import tvask, tvcontrol, tvexplore, tvhandoff, tvnames, tvpicks, tvsport
from ..tvapp import _household as household
from ..upnext import shelf, warm_plex, who_key
from .deps import Caller, caller, error, hub, need
from .media import _filter, _watch_cap
from ..faults import note

log = logging.getLogger("hub.tvguide")
router = APIRouter(prefix="/v1/tv", tags=["tv"])


def _media(h: Hub, c: Caller) -> bool:
    """True when this caller only sees kids' titles."""
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)
    return c.subject.is_child_like


def watcher(h: Hub, c: Caller, person: str | None) -> tuple[str | None, bool]:
    """Who a phone is watching as, and whether that is kids-only: the phone's own person, or one of the house's
    children picked by a grown-up's phone ("who's watching"). Picking only ever narrows: a child's phone can't
    pick anyone else, and nobody picks a grown-up. A TV keeps its own rule (`person` names who sits there)."""
    kids = _media(h, c)
    own = c.subject.person_id
    if not person or person == own:
        return own, kids
    found = next((p for p in household(h.db) if p["id"] == person), None)
    if found is None:
        raise error(404, "not_found", "Nobody in the house has that id")
    if found.get("role") != "child" or kids or c.subject.role not in ("admin", "adult"):
        raise error(403, "forbidden", "A phone can watch as its own person or, from a grown-up's phone, a child")
    return str(found["id"]), True


def child_ratings(h: Hub, c: Caller, person: str | None = None) -> set[str] | None:
    """The ratings this caller may see when it is a child or watches as one (`kidratings`), or None for a
    grown-up. A kids' TV or a child's screen with nobody named gets the strictest child's level."""
    from .. import kidratings

    subject = c.subject
    child = None
    if person:
        found = next((p for p in household(h.db) if p["id"] == person), None)
        if found is not None and found.get("role") == "child":
            child = str(found["id"])
    if child is None and subject.role == "child":
        child = subject.person_id
    kids_tv = getattr(subject, "tv", False) and tvexplore._profile(h, subject) == "kids"
    if child is None and not (getattr(subject, "is_child_like", False) or kids_tv):
        return None
    return kidratings.for_person(h, child)


def _narrow(h: Hub, c: Caller, body: Any, person: str | None = None, *, kids: bool = True) -> Any:
    """Takes out what this child's level doesn't allow (a grown-up's answer is left as it is)."""
    from .. import kidratings

    allowed = child_ratings(h, c, person) if kids else None
    if allowed is None and kids:
        allowed = kidratings.for_person(h, None)
    return kidratings.narrow(body, allowed) if allowed is not None else body


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


_CARD_KINDS = {"movie", "series", "show", "tv", "episode"}


def _polished(body: Any) -> Any:
    """Every title card in an answer reads as people say it ("EN - Dune (2021) 4K" -> "Dune", year 2021). Plex
    and TMDB titles are already clean and pass through unchanged."""
    if isinstance(body, dict):
        if body.get("kind") in _CARD_KINDS and isinstance(body.get("title"), str):
            name, year = tvnames.polish(body["title"])
            body["title"] = name
            if year and not body.get("year"):
                body["year"] = year
        for value in body.values():
            if isinstance(value, (dict, list)):
                _polished(value)
    elif isinstance(body, list):
        for value in body:
            _polished(value)
    return body


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
        watching, narrowed = watcher(h, c, person)
        kids = kids or narrowed
        who = who_key(person=watching)
        person = watching
    body = tvpicks.personal_feed(h, "kids" if kids else "adult", who)
    return _polished(_narrow(h, c, body, person, kids=True) if kids else body)


@router.get("/watchers")
async def watchers(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Who a phone may watch as: its own person first, then (on a grown-up's phone) the house's children.
    What the phone's "Who's watching" menu lists; each id goes back as `person`."""
    kids = _media(h, c)
    own = c.subject.person_id
    out: list[dict[str, Any]] = []
    for p in household(h.db):
        if p["id"] == own:
            out.insert(0, {"id": p["id"], "name": p["name"], "role": p.get("role"), "self": True})
        elif p.get("role") == "child" and not kids and c.subject.role in ("admin", "adult"):
            out.append({"id": p["id"], "name": p["name"], "role": "child", "self": False})
    return {"watchers": out}


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    person: str | None = Field(default=None, max_length=64)


@router.post("/ask")
async def ask_to_watch(body: AskBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Something to watch, asked in plain words ("something funny under 90 minutes"). {reply, items, by}: the
    items are cards like Explore's, from the caller's own library, so a child only ever gets kids' titles."""
    if getattr(c.subject, "tv", False):
        kids = _media(h, c) or tvexplore._profile(h, c.subject) == "kids"
        who = who_key(person=c.subject.person_id, tv=c.subject.tv_id)
    else:
        watching, kids = watcher(h, c, body.person)
        who = who_key(person=watching)
    await _ready(h)
    answer = await tvask.ask(h, body.text, kids=kids, who=who)
    return _polished(_narrow(h, c, answer, body.person or c.subject.person_id) if kids else answer)


@router.get("/pick")
async def pick_for_me(person: str | None = None, skip: str | None = None, c: Caller = Depends(caller),
                      h: Hub = Depends(hub)) -> dict[str, Any]:
    """"Pick for me": one title for right now with its reason, from this person's own hero and picks. `skip`
    (comma-separated keys from earlier answers) steps on to the next one."""
    if getattr(c.subject, "tv", False):
        kids = _media(h, c) or tvexplore._profile(h, c.subject) == "kids"
        who = who_key(person=c.subject.person_id, tv=c.subject.tv_id)
    else:
        watching, kids = watcher(h, c, person)
        who = who_key(person=watching)
    skipped = [k for k in (skip or "").split(",") if k][:50]
    picked = tvask.pick(h, kids=kids, who=who, skip=skipped)
    return _polished(_narrow(h, c, picked, person or c.subject.person_id) if kids else picked)


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
        watching, narrowed = watcher(h, c, person)
        kids = kids or narrowed
        who = who_key(person=watching)
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
    except Exception as exc:  # noqa: BLE001  sport channels fall back to no home market
        note("sport: house location", exc)
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


SORTS = ("name", "year", "added")


def _sort(sort: str) -> str:
    if sort not in SORTS:
        raise error(400, "bad_request", "Sort by name, year or added")
    return sort


@router.get("/series")
async def series(request: Request, q: str | None = None, category: str | None = None, limit: int = 60,
                 offset: int = 0, sort: str = "name", c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Any:
    """Series in the provider library, a page at a time (`offset`, `limit` up to 100, `total`), sorted by `name`,
    `year` or `added`. Sends an `ETag`; the same page asked with `If-None-Match` while the library is unchanged
    answers 304 with no body."""
    return await _title_page(request, h, c, "series", q=q, category=category, limit=limit, offset=offset, sort=sort)


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
async def movies(request: Request, q: str | None = None, category: str | None = None, limit: int = 60,
                 offset: int = 0, sort: str = "name", c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Any:
    """Movies in the provider library, a page at a time (`offset`, `limit` up to 100, `total`), sorted by `name`,
    `year` or `added`. Sends an `ETag`, like /series."""
    return await _title_page(request, h, c, "movies", q=q, category=category, limit=limit, offset=offset, sort=sort)


async def _title_page(request: Request, h: Hub, c: Caller, kind: str, *, q: str | None, category: str | None,
                      limit: int, offset: int, sort: str) -> Any:
    kids = _media(h, c)
    await _ready(h)
    args: dict[str, Any] = {"q": q, "category": category, "kids_only": kids, "limit": _limit(limit),
            "offset": max(0, offset),
            "sort": _sort(sort)}
    cat = h.tv_catalog
    stamp = cat.snapshot_stamp()
    tag = None
    if stamp is not None:
        seed = json.dumps([kind, stamp, sorted(cat._kid_names()) if kids else [], args], sort_keys=True)
        tag = 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:20] + '"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
    # SQLite does the scan (phase 3) without holding the loop; the snapshot fallback runs here too.
    fn = cat.series if kind == "series" else cat.movies
    body = await asyncio.to_thread(fn, **args)
    headers = {"ETag": tag} if tag else {}
    return Response(json.dumps(body), media_type="application/json", headers=headers)


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
async def library_search(q: str, response: Response, c: Caller = Depends(caller),
                         h: Hub = Depends(hub)) -> dict[str, Any]:
    """Channels, programmes in the cached guide, series and movies matching these words.

    The work runs off the event loop (it reads the whole library), so a search never holds up other calls, and
    `Server-Timing` says how long each part took."""
    kids = _media(h, c)
    if len(q.strip()) < 2:
        raise error(400, "bad_request", "Type at least two letters")
    timing: dict[str, float] = {}
    asked = time.perf_counter()
    await _ready(h)
    timing["ready"] = round((time.perf_counter() - asked) * 1000, 1)
    queued = time.perf_counter()

    def run() -> dict[str, Any]:
        # How long the search waited for a worker thread: the part of `total` no other lap covered (Nov 19 2026).
        timing["wait"] = round((time.perf_counter() - queued) * 1000, 1)
        return h.tv_catalog.search(q, kids_only=kids, timing=timing)

    found = await asyncio.to_thread(run)
    for card in found.get("channels") or []:
        card.setdefault("display_name", tvnames.channel_name(card.get("name")))  # optional; "name" stays the provider's
    response.headers["Server-Timing"] = ", ".join(f"{name};dur={ms}" for name, ms in timing.items())
    if sum(timing.values()) > 3000:
        log.info("tv.library.search slow: %s", response.headers["Server-Timing"])
    return _narrow(h, c, found) if kids else found


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
    from .. import kidratings

    row = h.tv_catalog.known_row(kind, item_id)
    if row is None or (kids_only and not h.tv_catalog._is_kids(row.get("category"))):
        raise error(404, "not_found", "That isn't in the library")
    if kids_only and not kidratings.allows(kidratings.for_person(h, None), row.get("rating")):
        raise error(404, "not_found", "That isn't in the library")


# -- Plex in the TV library (hub/tvplex.py) -------------------------------------------------------------------------
PLEX_FILE_AUD = "tv-plex-file"
PLEX_FILE_TTL_S = 8 * 3600


def _plex_ok(h: Hub, c: Caller, rating: object, person: str | None = None) -> bool:
    if c.subject.role in ("admin", "adult", "tablet") and not person:
        return True
    from .. import kidratings

    return str(rating or "") in (child_ratings(h, c, person) or kidratings.for_person(h, None))


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
async def tv_plex_play(item_id: str, person: str | None = None, c: Caller = Depends(caller),
                       h: Hub = Depends(hub)) -> dict[str, Any]:
    """A pass for the file itself (direct play). The TV's player handles MKV, HEVC and the rest. A grown-up's
    phone watching as a child (`person`, as on /explore) only gets that child's ratings."""
    _watch_cap(h, c)
    _plex_id(item_id)
    narrowed, _watching = False, None
    if person and not getattr(c.subject, "tv", False):
        _watching, narrowed = watcher(h, c, person)
    try:
        item = await h.plex.metadata(item_id)
        media = await h.plex.media_info(item_id) if item else None
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    allowed = _plex_ok(h, c, item.get("rating")) if item else False
    if narrowed and item is not None:
        allowed = _plex_ok(h, c, item.get("rating"), _watching)
    if item is None or media is None or not allowed or item.get("kind") not in ("movie", "episode"):
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
    except Exception as exc:  # noqa: BLE001  the TV is told the movie server didn't answer
        note("plex stream", exc)
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
