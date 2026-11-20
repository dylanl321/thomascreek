"""Watch a TV-library title on the phone itself.

The provider login stays on the hub, except for a grown-up's own phone: `/logins` (Dylan, Nov 19 2026) and a
`direct` play. Otherwise the phone gets a short pass (a signed token that names the
title, not the address) and the hub fetches the stream for it:

- films and episodes: `/v1/tv/phone/file/{pass}`, a byte-range proxy of the provider file;
- channels: `/v1/tv/phone/live/{pass}/index.m3u8`, the provider's HLS with every line pointed back at
  the hub. The provider's own segment addresses stay in this process (`_Live`), never in the playlist.

Kids, guests and kids' devices only get kids' titles, the same rule as the guide.

The phone is part of Continue watching like a TV: `/play` answers with `resume_s` (where this person stopped,
on any screen) and takes `start_s` (start from the second a TV was at), and the player reports with
`/progress` every minute and on close. Plays are kept under the caller's person (hub/upnext.py).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
import shutil
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..crypto import CryptoError
from ..state import Hub
from ..tvapp import TvAppError
from .deps import Caller, caller, error, hub
from ..upnext import remember, resume_at, who_key
from .tvguide import _media, _ready, watcher
from ..faults import note

log = logging.getLogger("hub.tvphone")

router = APIRouter(prefix="/v1/tv/phone", tags=["tv"])

AUD = "tv-phone"
TTL_S = 6 * 3600
# AVPlayer opens these. MKV, AVI and the rest only play on a TV.
PHONE_TYPES = {"mp4", "m4v", "mov"}
# An Android phone's player (Media3, `"player": "exo"` on /play) also opens these, like the TVs do.
EXO_TYPES = PHONE_TYPES | {"mkv", "avi", "webm", "ts", "m2ts", "flv", "wmv", "mpg", "mpeg"}
# Players that open every file type: Media3 on the Android phone, and the FFmpeg player (`"ks"`) in the
# iPhone and iPad media app, the one the Apple TV app plays these files with.
FULL_PLAYERS = {"exo", "ks"}
SOURCE_PREFIX = {"movie": "vod:", "episode": "ep:", "live": "xc:"}


def _stream_id(key: str) -> str:
    part = key.rsplit(":", 1)[-1]
    if not part or "/" in part or ".." in part:
        raise error(404, "not_found", "That isn't in the library")
    return part


def _grown_up_phone(c: Caller) -> bool:
    """Whose device may hold a provider address or login: an admin's or adult's own phone. Never a TV, a child,
    a guest or a kids' device (the rule `/logins` keeps). Watching as a child on that phone doesn't change it:
    the address still only reaches the grown-up's phone."""
    s = c.subject
    return not getattr(s, "tv", False) and not s.is_child_like and s.role in ("admin", "adult")


@router.post("/play")
async def phone_play(body: dict[str, Any], response: Response, c: Caller = Depends(caller),
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    """{movie_id} | {episode_id} | {channel_key} -> a hub path the phone's player opens.

    `player: "exo"` (optional) says the phone plays with Media3, as an Android phone does, so MKV and the other
    files only a TV opened before are allowed too. `player: "ks"` says the same for the iPhone and iPad media
    app. Without either, the MP4 rule of Apple's own player holds.

    `direct: true` (optional, a grown-up's Android phone, iPhone or iPad) answers the provider's own address
    instead of a hub pass: a free login is held for this phone like a TV's, kept by `/hold` and freed by
    `/hold {"stop":true}`. A child, a guest or a kids' device that asks gets the relayed answer instead.

    `person` (optional) is who is watching on a grown-up's phone: one of the children, so only kids' titles play
    and the place is kept for them (see tvguide.watcher).

    `Server-Timing` says how long each step took (ready, row, lease, retry when nothing was free, source,
    resume, next), so a slow start is found; answers over a second keep theirs on /v1/ping/timings."""
    lap = _Laps(response)
    if getattr(c.subject, "tv", False):
        kids_only, person = _media(h, c), c.subject.person_id
    else:
        person, kids_only = watcher(h, c, _person(body))
    await _ready(h)
    lap("ready")
    cat = h.tv_catalog
    kind = next((k for k in ("movie_id", "episode_id", "channel_key") if body.get(k)), None)
    if kind is None:
        raise error(400, "bad_params", "Pick a film, an episode or a channel")
    key = str(body[kind])
    if kind == "movie_id":
        row, mode = cat.movie(key), "movie"
    elif kind == "episode_id":
        row, mode = cat.episode(key), "episode"
        sid = str(body.get("series_id") or "")
        if row is None and not kids_only and sid.startswith("series:") and cat.known_row("series", sid) is not None:
            # A grown-up's phone read this series straight from the provider (`/logins`), so the hub may not have
            # its episodes yet: read the series once, then the episode is known like any other.
            try:
                await cat.fetch_series(sid)
            except TvAppError as exc:
                note("tv.phone.play: reading the series for an episode", exc)
            row = cat.episode(key)
    else:
        row, mode = cat.channel(key), "live"
    lap("row")
    if row is None or (kids_only and not row.get("kids")):
        raise error(404, "not_found", "That isn't in the library")
    if kids_only and not _level_allows(h, c, person, row):
        raise error(404, "not_found", "That isn't in the library")
    ext = "m3u8" if mode == "live" else str(row.get("ext") or "mp4").lower()
    allowed = EXO_TYPES if body.get("player") in FULL_PLAYERS else PHONE_TYPES
    if mode != "live" and ext not in allowed:
        raise error(409, "tv_only", "This one only plays on a TV")
    if body.get("direct") is True and _grown_up_phone(c):
        # Straight from the provider (the Android travel phone, Dylan's choice Nov 2026; a grown-up's iPhone and
        # iPad too, Nov 20 2026): the address carries a login, so it goes only to this signed-in grown-up's
        # phone and is never logged. A child, a guest or a kids' device asking for it is relayed instead, below.
        from ..iptvplay import busy_reason, direct_all, holders, read_twins, refresh_usage, warm_twins

        holder = f"phone:{c.device['id']}"
        # The pool takes its own lock (hub/iptvplay.py _POOL), so the lease runs off the loop.
        urls = await asyncio.to_thread(direct_all, h, key, holder)
        lap("lease")
        if not urls:
            # Only when no login looked free: ask the provider for its live counts (an old count may be stale),
            # then the other providers' copies of an episode.
            await refresh_usage(h, key)
            urls = await asyncio.to_thread(direct_all, h, key, holder)
            if not urls and mode == "episode" and await read_twins(h, key):
                urls = await asyncio.to_thread(direct_all, h, key, holder)  # the same episode, another provider
            lap("retry")
        warm_twins(h, key)
        url = urls[0] if urls else None
        if not url:
            raise error(503, "busy", busy_reason(h, key), holders=holders(h, key, titles=not kids_only))
        h.db.log("tv.phone.play", "ok", person_id=person, device_id=c.device["id"], target=key,
                 params={"title": row.get("name") or row.get("title"), "direct": True})
        out_direct: dict[str, Any] = {"url": url, "kind": mode, "title": row.get("name") or row.get("title"),
                                      "direct": True}
        source = await asyncio.to_thread(_source, h, holder, out_direct["title"], row, "direct", len(urls))
        if source:
            out_direct["source"] = source
        lap("source")
        alternates = urls[1:]
        relay = _relay_pass(h, c, mode, key, ext)
        if relay is not None:
            # The same stream relayed by the hub, on the same leased login (one stream at a time either way): the
            # route that still works where the provider turns away the phone's own address (abroad, a hotel
            # network). `relay_url` is the hub path; the absolute address goes last in `alternates`, so phones
            # that only know `alternates` try it too after the provider's own addresses.
            out_direct["relay_url"] = relay
            base = (h.config.hub.remote_url or "").rstrip("/")
            if base:
                alternates.append(base + relay)
        if alternates:
            # The same stream on the provider's backup addresses (same login), to try in order when `url` won't play.
            out_direct["alternates"] = alternates
        _resume(h, person, body, mode, key, row, out_direct)
        lap("resume")
        await _episode(h, mode, key, row, out_direct, kids_only)
        lap("next", done=True)
        return out_direct
    # The hub relays it, on a login leased to this phone like a TV's (one stream per login).
    from ..iptvplay import busy_reason, holders, lease, read_twins, refresh_usage, warm_twins

    holder = f"phone:{c.device['id']}"
    held = await asyncio.to_thread(lease, h, key, holder)
    lap("lease")
    if held is None:
        await refresh_usage(h, key)  # only when no login looked free (see the direct path)
        held = await asyncio.to_thread(lease, h, key, holder)
        if held is None and mode == "episode" and await read_twins(h, key):
            held = await asyncio.to_thread(lease, h, key, holder)  # the same episode from another provider
        lap("retry")
    warm_twins(h, key)
    if held is None:
        raise error(503, "busy", busy_reason(h, key), holders=holders(h, key, titles=not kids_only))
    token, exp = h.tokens.issue_scoped({"aud": AUD, "k": mode, "s": _stream_id(key), "x": ext,
                                        "sid": secrets.token_urlsafe(9), "dev": c.device["id"],
                                        "pl": f"{held['provider']}|{held['login']}"}, TTL_S)
    title = row.get("name") or row.get("title")
    h.db.log("tv.phone.play", "ok", person_id=person, device_id=c.device["id"], target=key,
             params={"title": title})
    path = f"/v1/tv/phone/live/{token}/index.m3u8" if mode == "live" else f"/v1/tv/phone/file/{token}"
    out: dict[str, Any] = {"url": path, "kind": mode, "title": title, "expires_at": exp}
    _cast_fields(out, mode, ext, token, cast=body.get("cast") is True)
    source = await asyncio.to_thread(_source, h, holder, title, row, "relay", None)
    if source:
        out["source"] = source
    lap("source")
    _resume(h, person, body, mode, key, row, out)
    lap("resume")
    await _episode(h, mode, key, row, out, kids_only)
    lap("next", done=True)
    return out


def _source(h: Hub, holder: str, title: Any, row: dict[str, Any], route: str,
            addresses: int | None) -> dict[str, Any] | None:
    """The `source` of a play answer (see iptvplay.source_view), and what this holder plays for a busy answer."""
    from ..iptvplay import note_title, source_view

    note_title(h, holder, str(title) if title else None, bool(row.get("kids")))
    return source_view(h, holder, route=route, addresses=addresses)


class _Laps:
    """Milliseconds per step of a play, sent as `Server-Timing` and logged when the whole call is over a second."""

    def __init__(self, response: Response) -> None:
        self.response, self.start = response, time.perf_counter()
        self.last, self.parts = self.start, []  # type: ignore[var-annotated]

    def __call__(self, name: str, *, done: bool = False) -> None:
        now = time.perf_counter()
        self.parts.append(f"{name};dur={(now - self.last) * 1000:.0f}")
        self.last = now
        if done:
            self.response.headers["Server-Timing"] = ", ".join(self.parts)
            if now - self.start > 1.0:
                log.info("tv.phone.play slow: %s", self.response.headers["Server-Timing"])


def _relay_pass(h: Hub, c: Caller, mode: str, key: str, ext: str) -> str | None:
    """A hub pass for the stream on the login this phone leased for direct play, or None without a lease."""
    from ..iptvplay import held_by

    held = held_by(h, f"phone:{c.device['id']}")
    if held is None:
        return None
    token, _exp = h.tokens.issue_scoped({"aud": AUD, "k": mode, "s": _stream_id(key), "x": ext,
                                         "sid": secrets.token_urlsafe(9), "dev": c.device["id"],
                                         "pl": f"{held['provider']}|{held['login']}"}, TTL_S)
    return f"/v1/tv/phone/live/{token}/index.m3u8" if mode == "live" else f"/v1/tv/phone/file/{token}"


# -- casting ------------------------------------------------------------------------------------------------------
# What the phone tells a Cast receiver the stream is. Google's receivers open MP4, WebM, MPEG-TS and HLS; MKV, AVI
# and the rest are not supported, and many Chromecasts have no HEVC or AC-3/E-AC-3 decoding. Those get the hub's
# converted copy (`/cast/{pass}`): ffmpeg remuxes to fragmented MP4, keeps H.264 video and makes stereo AAC.
CONTENT_TYPES = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/mp4", "webm": "video/webm",
                 "ts": "video/mp2t", "m3u8": "application/x-mpegURL"}
CAST_NATIVE = {"mp4", "m4v", "mov", "webm"}


def _cast_fields(out: dict[str, Any], mode: str, ext: str, token: str, *, cast: bool) -> None:
    """Relayed plays carry what a Cast TV needs: the receiver's content type and a second way to play.

    The converted copy (`/cast/{pass}`) is the fallback for a TV that can't decode a stream it should open
    (HEVC, AC-3). A file no receiver opens (MKV and the rest) gets it as `cast_url`; with `cast: true` on /play
    (the phone is casting) it is the `url` itself, so the TV never sits on a black screen, the original stays
    as `fallback_url` for the TVs that can, and `cast_note` says why."""
    converted = f"/v1/tv/phone/cast/{token}"
    out["container"] = ext
    if mode == "live" or ext in CAST_NATIVE:
        out["content_type"] = CONTENT_TYPES.get(ext, "video/mp4")
        out["fallback_url"] = converted
        out["fallback_content_type"] = "video/mp4"
        return
    original = {"mkv": "video/x-matroska", "ts": "video/mp2t", "webm": "video/webm"}.get(ext, "video/mp4")
    out["cast_url"] = converted
    out["cast_note"] = f"Cast TVs can't open {ext.upper()} files, so the TV gets a converted copy."
    if cast:
        out["fallback_url"], out["fallback_content_type"] = out["url"], original
        out["url"], out["content_type"], out["converted"] = converted, "video/mp4", True
    else:
        out["content_type"] = original


def _person(body: dict[str, Any]) -> str | None:
    value = body.get("person")
    return str(value)[:64] if isinstance(value, str) and value else None


def _resume(h: Hub, person: str | None, body: dict[str, Any], mode: str, key: str, row: dict[str, Any],
            out: dict[str, Any]) -> None:
    if mode == "live":
        return
    start = _seconds(body.get("start_s"))
    if start is None:
        ids = {"movie_id": key} if mode == "movie" else {"episode_id": key}
        start = resume_at(h, who_key(person=person), **ids)["position_s"]
    out["resume_s"] = start
    out["duration_s"] = row.get("duration_s") if isinstance(row.get("duration_s"), (int, float)) else None
    if mode == "episode":
        out["series_id"] = row.get("series_id")


async def _episode(h: Hub, mode: str, key: str, row: dict[str, Any], out: dict[str, Any], kids_only: bool) -> None:
    """Which episode this is and the one after it (optional `season`, `number`, `next`), for the player's title
    line and its "Next episode" card. The screens draw `next`; they do not work out the order."""
    if mode != "episode":
        return
    from ..playernext import next_episode

    for field in ("season", "number"):
        if isinstance(row.get(field), int):
            out[field] = row[field]
    if row.get("series_name"):
        out["series_name"] = row["series_name"]
    try:
        nxt = await asyncio.to_thread(next_episode, h.tv_catalog, key, kids_only=kids_only)
    except Exception as exc:  # noqa: BLE001 - the episode still plays without its "next"
        note("tv.phone.play: finding the next episode", exc)
        return
    if nxt:
        out["next"] = nxt


def _seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(value)
    return seconds if 0 < seconds <= 24 * 3600 else None


@router.get("/logins")
async def phone_logins(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The provider logins, so a grown-up's phone reads film and series lists and pages straight from the provider
    (Dylan, Nov 19 2026: "fine for our highly controlled list of devices connected to the home").

    Only for a signed-in admin's or adult's own phone: never a TV, a child, a guest or a kids' device; those keep
    reading through the hub, which filters for kids. One login per provider (the one the hub reads its catalog
    with), its addresses best first. `id` is the provider part of the hub's ids (`vod:<id>:<stream>`,
    `series:<id>:<series>`, `ep:<id>:<episode>`), so the phone keeps using the hub's ids for play, Continue
    watching and My list; streams still lease a login through `/play`. The answer carries passwords: it is never
    logged or cached by the hub, and the phone keeps it in memory only."""
    if not _grown_up_phone(c):
        raise error(403, "forbidden", "Only a grown-up's phone reads the provider directly. The hub's lists work.")
    cat = h.tv_catalog
    found: list[dict[str, Any]] = []
    from ..iptvplay import logins, provider_names

    names = provider_names(h)
    for pid in dict.fromkeys(str(lg["provider"]) for lg in logins(h)):
        try:
            got, servers, user, password = cat._login_for(f"vod:{pid}:0")
        except TvAppError:
            continue
        if not servers or not user or not password:
            continue
        if any(p["id"] == got for p in found):
            continue
        found.append({"id": got, "name": names.get(got) or got,
                      "server": servers[0], "servers": servers, "username": user, "password": password})
    h.db.log("tv.phone.providers", "ok", person_id=c.subject.person_id, device_id=c.device["id"],
             params={"providers": len(found)})
    if not found:
        raise error(503, "no_provider", "The hub has no TV provider login yet. The hub's lists still work.")
    return {"providers": found}


@router.get("/update")
async def phone_update(version_code: int | None = None, c: Caller = Depends(caller),
                       h: Hub = Depends(hub)) -> dict[str, Any]:
    """The Android phone app's own update: the newest `phone-*` build the hub keeps, for a signed-in phone.

    Answers `{"up_to_date": true}` when `version_code` (the installed one) is already that build. Otherwise the
    build's version, sha256 and signer, and `apk_path` to download with the same bearer. The phone checks the
    file and the signer itself, then installs it (silently on Android 12 and newer)."""
    build = await h.phone_app.ensure()
    if not build:
        why = h.phone_app.last_error or "No phone app build yet"
        raise error(503, "no_build", f"{why}. The phone will look again later.")
    code = build.get("version_code")
    if not isinstance(code, int):
        raise error(503, "no_version", "The newest phone build has no version number, so it can't be offered yet")
    if version_code is not None and version_code >= code:
        return {"up_to_date": True, "version_code": code}
    return {"up_to_date": False, "version_code": code, "version_name": build.get("version_name"),
            "notes": build.get("notes") or "", "size": build.get("size"), "sha256": build.get("sha256"),
            "signer_sha256": build.get("signer_sha256"), "tag": build.get("tag"),
            "apk_path": "/v1/tv/phone/update/apk"}


@router.get("/update/apk")
async def phone_update_apk(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """The kept phone build, for a signed-in phone updating itself."""
    build = await h.phone_app.ensure()
    if not build:
        raise error(503, "no_build", "No phone app build yet. The phone will look again later.")
    h.db.log("phoneapp.update", "ok", device_id=c.device["id"], params={"tag": build.get("tag")})
    return FileResponse(h.phone_app.path, media_type="application/vnd.android.package-archive",
                        filename="ThomasCreekPhone.apk")


@router.post("/hold")
async def phone_hold(body: dict[str, Any] | None = None, c: Caller = Depends(caller),
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    """A phone playing straight from the provider keeps its login slot (every minute), or frees it on close."""
    from ..iptvplay import release_tv, touch

    _media(h, c)
    holder = f"phone:{c.device['id']}"
    if (body or {}).get("stop") is True:
        release_tv(h, holder)
        return {"ok": True, "held": False}
    touch(h, holder)
    return {"ok": True, "held": True}


@router.post("/progress")
async def phone_progress(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """{movie_id|episode_id, position_s, duration_s?} from the phone's player, every minute and on close.

    The same record a TV's heartbeat makes, kept for the caller's person, so Continue watching follows them to
    every TV. A title the caller may not see is refused like /play. `person` is who is watching (a child picked
    on a grown-up's phone), and `shuffle` ({order, seasons}) keeps a shuffled show's order so any screen
    continues it, the same as a TV's heartbeat."""
    if getattr(c.subject, "tv", False):
        kids_only, person = _media(h, c), c.subject.person_id
    else:
        person, kids_only = watcher(h, c, _person(body))
    await _ready(h)
    cat = h.tv_catalog
    if body.get("movie_id"):
        key = str(body["movie_id"])
        row = cat.movie(key)
        playing: dict[str, Any] = {"kind": "movie", "movie_id": key}
    elif body.get("episode_id"):
        key = str(body["episode_id"])
        row = cat.episode(key)
        playing = {"kind": "episode", "episode_id": key}
        if row is not None and row.get("series_id"):
            playing["series_id"] = row["series_id"]
    else:
        raise error(400, "bad_params", "Say which film or episode: movie_id or episode_id")
    if row is None or (kids_only and not row.get("kids")):
        raise error(404, "not_found", "That isn't in the library")
    position = _seconds(body.get("position_s"))
    if position is None:
        raise error(400, "bad_params", "position_s is the second the player is at")
    playing["position_s"] = position
    duration = _seconds(body.get("duration_s"))
    if duration is not None:
        playing["duration_s"] = duration
    playing["title"] = row.get("name") or row.get("title")
    if playing["kind"] == "episode":
        from ..tvapp_common import _play_shuffle

        order = _play_shuffle(body.get("shuffle"))
        if order:
            playing["shuffle"] = order
    remember(h.db, "kids" if kids_only else "adult", playing, person=person)
    return {"ok": True, "position_s": position}


def _subtitle_title(h: Hub, c: Caller, episode_id: str | None, movie_id: str | None,
                    person: str | None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The title a subtitle call is about (its id, its row and, for an episode, its series row), after the same
    kids' rule as `/play`."""
    if getattr(c.subject, "tv", False):
        kids_only, who = _media(h, c), c.subject.person_id
    else:
        who, kids_only = watcher(h, c, person[:64] if person else None)
    cat = h.tv_catalog
    if episode_id:
        title_id, row = episode_id, cat.episode(episode_id)
        series = cat.known_row("series", str((row or {}).get("series_id") or "")) or {}
    elif movie_id:
        title_id, row, series = movie_id, cat.movie(movie_id), {}
    else:
        raise error(400, "bad_params", "Pick a film or an episode")
    if row is None or (kids_only and (not row.get("kids") or not _level_allows(h, c, who, row))):
        raise error(404, "not_found", "That isn't in the library")
    return title_id, row, series


@router.get("/subtitles")
async def phone_subtitles(episode_id: str | None = None, movie_id: str | None = None, lang: str = "en",
                          person: str | None = None, c: Caller = Depends(caller),
                          h: Hub = Depends(hub)) -> dict[str, Any]:
    """Subtitles the hub can fetch for a film or episode that has none of its own in `lang`:
    `{"subtitles":[{"id","lang","label","hearing_impaired","downloads","url"}], "reason"?}`, best first. Asked
    only when the person opens Subtitles; `url` is the hub path of the WebVTT file. An empty list comes with a
    short `reason` to show."""
    from .. import playersubs, tvnames

    await _ready(h)
    title_id, row, series = _subtitle_title(h, c, episode_id, movie_id, person)
    known = series if episode_id else row
    name = tvnames.title(known.get("name") or row.get("series_name") or "").get("name")
    async with _client(h, 10) as client:
        out = await playersubs.search(
            h, title_id, tmdb=str(known.get("tmdb") or "") or None, name=name,
            year=str(known.get("year") or "") or None,
            season=row.get("season") if episode_id else None, number=row.get("number") if episode_id else None,
            lang=lang.lower()[:6], client=client)
    pick = f"episode_id={quote(title_id)}" if episode_id else f"movie_id={quote(title_id)}"
    who = f"&person={quote(person[:64])}" if person else ""
    out["subtitles"] = [{**s, "url": f"/v1/tv/phone/subtitles/{s['id']}.vtt?{pick}{who}"} for s in out["subtitles"]]
    return out


@router.get("/subtitles/{file_id}.vtt")
async def phone_subtitle_file(file_id: str, episode_id: str | None = None, movie_id: str | None = None,
                              person: str | None = None, c: Caller = Depends(caller),
                              h: Hub = Depends(hub)) -> Response:
    """One subtitle the hub offered for this title, as WebVTT. The hub downloads it once and keeps it."""
    from .. import playersubs

    await _ready(h)
    title_id, _row, _series = _subtitle_title(h, c, episode_id, movie_id, person)
    if not await asyncio.to_thread(playersubs.offered, h, title_id, file_id):
        raise error(404, "not_found", "That subtitle isn't on offer for this title")
    try:
        async with _client(h, 15) as client:
            text = await playersubs.fetch(h, h.tv_app.dir / "subtitles", file_id, client)
    except playersubs.SubtitleError as exc:
        raise error(exc.status, exc.error, exc.message) from None
    return Response(text, media_type="text/vtt; charset=utf-8", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/next")
async def phone_next(episode_id: str, person: str | None = None, c: Caller = Depends(caller),
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    """The episode after `episode_id`, for a player that did not start through `/play` (the TVs):
    `{"next": {episode_id, series_id, title, season?, number?, still_url?}}`, or `{}` at the end of the series.
    The same pick and the same kids' rule as `next` on a play answer."""
    from ..playernext import next_episode

    await _ready(h)
    title_id, _row, _series = _subtitle_title(h, c, episode_id, None, person)
    if getattr(c.subject, "tv", False):
        kids_only = _media(h, c)
    else:
        kids_only = watcher(h, c, person[:64] if person else None)[1]
    nxt = await asyncio.to_thread(next_episode, h.tv_catalog, title_id, kids_only=kids_only)
    return {"next": nxt} if nxt else {}


@router.get("/thumbs")
async def phone_thumbs(episode_id: str | None = None, movie_id: str | None = None, person: str | None = None,
                       c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The scrub bar's small pictures for a film or episode: `{"interval_s","width","height","columns","rows",
    "sheets":[address]}`. Picture `second // interval_s` sits on sheet `n // (columns*rows)`, cell
    `n % (columns*rows)` along the rows. `sheets` is empty when the hub has not made them (it does at night, for
    what people are watching), and the player then shows the time alone."""
    from .. import playerthumbs

    await _ready(h)
    title_id, _row, _series = _subtitle_title(h, c, episode_id, movie_id, person)
    query = urlencode({"episode_id" if episode_id else "movie_id": title_id})
    return await asyncio.to_thread(playerthumbs.view, h, title_id, base="/v1/tv/phone/thumbs", query=query)


@router.get("/thumbs/{name}/{number}.jpg")
async def phone_thumb_sheet(name: str, number: int, episode_id: str | None = None, movie_id: str | None = None,
                            person: str | None = None, c: Caller = Depends(caller),
                            h: Hub = Depends(hub)) -> FileResponse:
    """One sheet of pictures. The player sends the title again, so the kids' rule is checked on the file too."""
    from .. import playerthumbs

    await _ready(h)
    title_id, _row, _series = _subtitle_title(h, c, episode_id, movie_id, person)
    path = await asyncio.to_thread(playerthumbs.sheet, h, h.tv_app.dir / "thumbs", title_id, name, number)
    if path is None:
        raise error(404, "not_found", "That picture isn't made yet")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


class QueueChange(BaseModel):
    add: str | None = Field(default=None, max_length=120)
    first: bool = False
    remove: str | None = Field(default=None, max_length=120)
    order: list[str] | None = Field(default=None, max_length=50)
    clear: bool = False
    person: str | None = Field(default=None, max_length=64)


def _queue_of(h: Hub, c: Caller, person: str | None) -> tuple[str, Any]:
    """Whose queue this is, and the rule for what they may see in it."""
    if getattr(c.subject, "tv", False):
        # A TV: the queue of whoever is watching it, or the TV's own when nobody is picked.
        kids_only, who = _media(h, c), c.subject.person_id
        owner = who_key(person=who, tv=str(c.subject.tv_id)) or "tv"
    else:
        who, kids_only = watcher(h, c, person)
        owner = who_key(person=who) or f"phone:{c.device['id']}"

    def allowed(row: dict[str, Any]) -> bool:
        return not kids_only or (bool(row.get("kids")) and _level_allows(h, c, who, row))
    return owner, allowed


@router.get("/queue")
async def phone_queue(person: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The person's "Play next" queue: `{"items":[{"id","kind":"movie"|"episode","title","subtitle"?,"season"?,
    "number"?,"series_id"?,"image_url"?,"duration_s"?}]}`. It follows the person to every screen; a grown-up's
    phone watching as a child (`person`) reads the child's queue, with kids' titles only."""
    from .. import playerqueue

    owner, allowed = _queue_of(h, c, person[:64] if person else None)
    await _ready(h)
    items = await asyncio.to_thread(lambda: playerqueue.present(h.tv_catalog, playerqueue.ids(h, owner), allowed))
    return {"items": items}


@router.post("/queue")
async def phone_queue_change(body: QueueChange, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """One change: `{"add": id, "first"?: true}` (play next, or at the end), `{"remove": id}`, `{"order":[ids]}`
    or `{"clear": true}`. Answers the new queue like the GET. A title the caller may not see can't be added."""
    from .. import playerqueue

    owner, allowed = _queue_of(h, c, body.person)
    await _ready(h)
    cat = h.tv_catalog
    if body.add:
        row = cat.episode(body.add) if body.add.startswith("ep:") else cat.movie(body.add)
        if row is None or not allowed(row):
            raise error(404, "not_found", "That isn't in the library")

    def work() -> list[dict[str, Any]]:
        kept = playerqueue.change(h, owner, add=body.add, first=body.first, remove=body.remove, order=body.order,
                                  clear=body.clear)
        return playerqueue.present(cat, kept, allowed)
    return {"items": await asyncio.to_thread(work)}


@router.get("/segments")
async def phone_segments(episode_id: str | None = None, movie_id: str | None = None, duration_s: int | None = None,
                         person: str | None = None, c: Caller = Depends(caller),
                         h: Hub = Depends(hub)) -> dict[str, Any]:
    """Where the recap, intro and credits of an episode (or the credits of a film) are, for the player's Skip
    button: `{"segments":[{"kind":"recap"|"intro"|"credits","start_s","end_s"}]}`, in play order and empty when
    nothing is known. The player asks once after the stream starts, with the length it measured (`duration_s`),
    so a slow answer never delays the picture. The hub asks the outside service and keeps the answer."""
    from ..playersegments import segments

    if getattr(c.subject, "tv", False):
        kids_only, who = _media(h, c), c.subject.person_id
    else:
        who, kids_only = watcher(h, c, person[:64] if person else None)
    await _ready(h)
    cat = h.tv_catalog
    if episode_id:
        row = cat.episode(episode_id)
        series = cat.known_row("series", str((row or {}).get("series_id") or "")) or {}
        tmdb, season, number = series.get("tmdb"), (row or {}).get("season"), (row or {}).get("number")
    elif movie_id:
        row = cat.movie(movie_id)
        tmdb, season, number = (row or {}).get("tmdb"), None, None
    else:
        raise error(400, "bad_params", "Pick a film or an episode")
    if row is None or (kids_only and (not row.get("kids") or not _level_allows(h, c, who, row))):
        raise error(404, "not_found", "That isn't in the library")
    if episode_id and not (isinstance(season, int) and isinstance(number, int)):
        return {"segments": []}
    length = duration_s if duration_s and 0 < duration_s < 86400 else row.get("duration_s")
    async with _client(h, 6) as client:
        found = await segments(h, tmdb=str(tmdb) if tmdb else None, season=season, number=number,
                               duration_s=int(length) if isinstance(length, (int, float)) and length > 0 else None,
                               client=client)
    return {"segments": found}


def _level_allows(h: Hub, c: Caller, person: str | None, row: dict[str, Any]) -> bool:
    """A child's own level (`kidratings`) on top of the kids' library: a TV-PG title a nine-year-old's level
    leaves out never plays, even from a link or an old list."""
    from .. import kidcerts, kidratings
    from .tvguide import child_ratings

    allowed = child_ratings(h, c, person) or kidratings.for_person(h, None)
    return kidratings.allows_item(allowed, row, kidcerts.store(h))


@router.get("/available")
async def phone_available(movie_id: str | None = None, episode_id: str | None = None,
                          channel_key: str | None = None, person: str | None = None, series_id: str | None = None,
                          c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Whether Play would start now and from how many copies, for a details page, without taking a login.
    {"state": "ready" | "busy", "copies", "providers", "reason" when busy}. A child's title only for a child.

    An episode a grown-up's phone read straight from the provider is not known here yet; with its `series_id`
    the answer is the series' (its copies on every provider), without reading the series."""
    if getattr(c.subject, "tv", False):
        kids_only = _media(h, c)
    else:
        kids_only = watcher(h, c, person)[1]
    await _ready(h)
    cat = h.tv_catalog
    key = movie_id or episode_id or channel_key
    if not key:
        raise error(400, "bad_params", "Say which film, episode or channel")
    row = cat.movie(key) if movie_id else cat.episode(key) if episode_id else cat.channel(key)
    if row is None and movie_id and hasattr(cat, "extra_movie"):
        row = cat.extra_movie(key)
    sid = str(series_id or "")
    if row is None and episode_id and not kids_only and sid.startswith("series:"):
        row = cat.known_row("series", sid)
        key = sid if row is not None else key
    if row is None or (kids_only and not row.get("kids")):
        raise error(404, "not_found", "That isn't in the library")
    if kids_only and not _level_allows(h, c, person, row):
        raise error(404, "not_found", "That isn't in the library")
    from ..playnow import availability

    holder = str(c.subject.tv_id) if getattr(c.subject, "tv", False) else f"phone:{c.device['id']}"
    return availability(h, key, holder)


@router.get("/resume")
async def phone_resume(movie_id: str | None = None, episode_id: str | None = None, person: str | None = None,
                       c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Where to start a film or episode. A phone gets its own person's place; a TV names who is watching with
    `person` (the same rule as /v1/tv/upnext) and otherwise gets its own."""
    _media(h, c)
    if not movie_id and not episode_id:
        raise error(400, "bad_params", "Say which film or episode: movie_id or episode_id")
    s = c.subject
    if getattr(s, "tv", False):
        from ..tvapp import _household

        named = person if person and any(p["id"] == person for p in _household(h.db)) else None
        who = who_key(person=named or s.person_id, tv=s.tv_id)
    else:
        who = who_key(person=watcher(h, c, person)[0])
    return resume_at(h, who, movie_id=movie_id, episode_id=episode_id)


def _claims(h: Hub, token: str, mode: set[str]) -> dict[str, Any]:
    try:
        claims = h.tokens.verify(token, audience=AUD)
    except CryptoError:
        raise error(401, "expired", "Playback pass expired") from None
    if claims.get("k") not in mode:
        raise error(404, "not_found", "Not part of this stream")
    return claims


def _keep(h: Hub, claims: dict[str, Any]) -> None:
    """A relayed live channel keeps its leased login while the player keeps asking for it."""
    if claims.get("pl"):
        from ..iptvplay import touch

        touch(h, f"phone:{claims.get('dev')}")


def _provider(h: Hub, claims: dict[str, Any], ext: str) -> str:
    """The stream address on the provider's best address now (a backup when the main one is marked down)."""
    return _providers(h, claims, ext)[0][2]


def _providers(h: Hub, claims: dict[str, Any], ext: str) -> list[tuple[str, str, str]]:
    """The provider address on the login leased for this pass, as (provider, address, stream address) on each of
    the provider's addresses, best first (hub/iptvaddr.py). Never logged or returned.

    Each request keeps the phone's lease alive, so the login is freed shortly after the bytes stop."""
    from ..iptvplay import credentials, logins, touch

    found = None
    pid = "thomas-creek"
    servers: list[str] = []
    if isinstance(claims.get("pl"), str) and "|" in claims["pl"]:
        provider, login = claims["pl"].split("|", 1)
        found = credentials(h, provider, login)
        row = next((lg for lg in logins(h) if lg["provider"] == provider and lg["login"] == login), None)
        pid, servers = provider, list((row or {}).get("servers") or [])
        touch(h, f"phone:{claims.get('dev')}")
    if found is not None:
        server, user, password = found
    else:
        try:
            servers, user, password = h.tv_catalog._main_login()
            server = servers[0]
        except TvAppError as exc:
            raise error(exc.status, exc.code, exc.message) from None
    folder = {"movie": "movie", "episode": "series"}.get(claims["k"], "live")
    tail = f"/{folder}/{quote(user)}/{quote(password)}/{quote(str(claims['s']))}.{ext}"
    return [(pid, s, s + tail) for s in (servers or [server])]


class _Unreachable(Exception):
    """The provider address didn't connect: try the provider's next address."""


async def _on_each_address(h: Hub, claims: dict[str, Any], ext: str, attempt: Any) -> Any:
    """Run `attempt(url, may_fall_back)` on the best address, then the backups when one can't be reached."""
    from ..iptvaddr import note as note_address

    every = _providers(h, claims, ext)
    for i, (pid, server, url) in enumerate(every):
        try:
            return await attempt(url, i < len(every) - 1)
        except _Unreachable:
            note_address(h.db, pid, server, False, "couldn't be reached")
    raise error(502, "unavailable", "The TV source didn't answer")


def _client(h: Hub, timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=60), follow_redirects=True,
                             transport=getattr(h.tv_catalog, "_transport", None),
                             headers={"User-Agent": "ThomasCreekHub"})


async def _relay(client: httpx.AsyncClient, url: str, headers: dict[str, str], media: str | None,
                 fall_back: bool = False) -> Response:
    try:
        upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        if fall_back:
            raise _Unreachable() from None
        raise error(502, "unavailable", "The TV source didn't answer") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise error(502, "unavailable", "The TV source didn't answer")
    keep = {k: upstream.headers[k] for k in ("content-length", "content-range", "last-modified", "etag")
            if k in upstream.headers}
    keep["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    kind = media or upstream.headers.get("content-type") or "application/octet-stream"
    return StreamingResponse(body(), status_code=upstream.status_code, headers=keep, media_type=kind)


@router.get("/file/{token}")
async def phone_file(token: str, request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"movie", "episode"})
    ext = str(claims.get("x") or "mp4")
    headers = {"Range": request.headers["range"]} if request.headers.get("range") else {}
    media = {"mp4": "video/mp4", "m4v": "video/x-m4v", "mov": "video/quicktime"}.get(ext)
    return await _on_each_address(h, claims, ext,
                                  lambda url, more: _relay(_client(h), url, headers, media, fall_back=more))


# -- live channels (HLS) -------------------------------------------------------------------------------------------
class _Live:
    """Per pass: the provider's playlist address after its redirect, and numbered provider lines."""

    MAX_SESSIONS = 40
    MAX_REFS = 400

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def get(self, sid: str) -> dict[str, Any]:
        row = self.sessions.get(sid)
        if row is None:
            row = {"media": None, "refs": OrderedDict(), "by_url": {}, "next": 0}
            self.sessions[sid] = row
            while len(self.sessions) > self.MAX_SESSIONS:
                self.sessions.popitem(last=False)
        self.sessions.move_to_end(sid)
        row["at"] = time.time()
        return row

    def number(self, row: dict[str, Any], url: str) -> int:
        n = row["by_url"].get(url)
        if n is None:
            n = row["next"]
            row["next"] += 1
            row["refs"][n] = url
            row["by_url"][url] = n
            while len(row["refs"]) > self.MAX_REFS:
                _old, gone = row["refs"].popitem(last=False)
                row["by_url"].pop(gone, None)
        return n


LIVE = _Live()


def _outside(url: str) -> bool:
    """A provider line may only send the hub to the internet, never to the house network."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def rewrite(text: str, base_url: str, row: dict[str, Any], token: str) -> str:
    """Every URI in a provider playlist becomes a hub path. Playlists (variants) end in .m3u8, media in .ts."""
    root = f"/v1/tv/phone/live/{token}"

    def hub_path(ref: str) -> str | None:
        absolute = urljoin(base_url, ref.strip())
        if not _outside(absolute):
            return None
        n = LIVE.number(row, absolute)
        is_list = absolute.split("?", 1)[0].lower().endswith((".m3u8", ".m3u"))
        return f"{root}/p/{n}.m3u8" if is_list else f"{root}/s/{n}"

    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                head, _, rest = stripped.partition('URI="')
                ref, _, tail = rest.partition('"')
                target = hub_path(ref)
                if target is None:
                    continue
                line = f'{head}URI="{target}"{tail}'
            out.append(line)
        elif stripped:
            target = hub_path(stripped)
            if target is not None:
                out.append(target)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


async def _playlist(h: Hub, url: str, fall_back: bool = False) -> tuple[str, str]:
    async with _client(h, 15) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            if fall_back:
                raise _Unreachable() from None
            raise error(502, "unavailable", "The channel didn't answer") from None
    if resp.status_code >= 400 or "#EXTM3U" not in resp.text[:200]:
        raise error(502, "unavailable", "The channel didn't answer")
    return resp.text, str(resp.url)


def _m3u8(text: str) -> Response:
    return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


@router.get("/live/{token}/index.m3u8")
async def phone_live(token: str, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"live"})
    _keep(h, claims)
    row = LIVE.get(str(claims.get("sid") or token[-16:]))
    text = None
    if row["media"]:
        # After the first answer, reload the provider's own (redirected) playlist, not the login address
        try:
            text, final = await _playlist(h, row["media"])
        except Exception as exc:  # noqa: BLE001  falls back to the login address below
            note("phone stream: provider playlist", exc)
            text = None
    if text is None:
        text, final = await _on_each_address(h, claims, "m3u8", lambda url, more: _playlist(h, url, fall_back=more))
    row["media"] = final
    return _m3u8(rewrite(text, final, row, token))


@router.get("/live/{token}/p/{n}.m3u8")
async def phone_live_list(token: str, n: int, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"live"})
    _keep(h, claims)
    row = LIVE.get(str(claims.get("sid") or token[-16:]))
    url = row["refs"].get(n)
    if url is None:
        raise error(404, "not_found", "Not part of this stream")
    text, final = await _playlist(h, url)
    return _m3u8(rewrite(text, final, row, token))


@router.get("/live/{token}/s/{n}")
async def phone_live_segment(token: str, n: int, request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"live"})
    _keep(h, claims)
    row = LIVE.get(str(claims.get("sid") or token[-16:]))
    url = row["refs"].get(n)
    if url is None:
        raise error(404, "not_found", "Not part of this stream")
    headers = {"Range": request.headers["range"]} if request.headers.get("range") else {}
    return await _relay(_client(h), url, headers, None)


# -- the converted copy for Cast TVs -------------------------------------------------------------------------------
class _Converts:
    """One ffmpeg per device: a new cast from the same phone stops the old one. A few at most for the house."""

    MAX = 3

    def __init__(self) -> None:
        self.running: OrderedDict[str, Any] = OrderedDict()

    async def start(self, dev: str, args: list[str]) -> Any:
        old = self.running.pop(dev, None)
        if old is not None:
            await _stop(old)
        while len(self.running) >= self.MAX:
            _dev, oldest = self.running.popitem(last=False)
            await _stop(oldest)
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        self.running[dev] = proc
        return proc

    def done(self, dev: str, proc: Any) -> None:
        if self.running.get(dev) is proc:
            self.running.pop(dev, None)


CONVERTS = _Converts()


async def _stop(proc: Any) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def video_codec(url: str) -> str | None:
    """The first video stream's codec, or None when the probe fails (then the video is re-encoded). Tests replace
    this."""
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    proc = await asyncio.create_subprocess_exec(
        probe, "-v", "error", "-rw_timeout", "10000000", "-select_streams", "v:0", "-show_entries",
        "stream=codec_name", "-of", "default=nw=1:nk=1", url,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=12)
    except asyncio.TimeoutError:
        await _stop(proc)
        return None
    name = out.decode("ascii", "ignore").strip().splitlines()
    return name[0] if name else None


def convert_args(ffmpeg: str, url: str, codec: str | None, start_s: int, live: bool) -> list[str]:
    """Fragmented MP4 a Cast receiver plays as it arrives: H.264 kept (or made), stereo AAC, no subtitles."""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-rw_timeout", "15000000"]
    if start_s > 0 and not live:
        args += ["-ss", str(start_s)]
    args += ["-i", url, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if codec == "h264":
        args += ["-c:v", "copy"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                 "-vf", "scale=-2:'min(1080,ih)'"]
    args += ["-c:a", "aac", "-ac", "2", "-b:a", "192k",
             "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return args


@router.api_route("/cast/{token}", methods=["GET", "HEAD"])
async def phone_cast(token: str, request: Request, t: int = 0, h: Hub = Depends(hub)) -> Response:
    """The converted copy (see `_cast_fields`). `t` starts a film at that second; seeking means asking again."""
    claims = _claims(h, token, {"movie", "episode", "live"})
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "none"}
    if request.method == "HEAD":
        return Response(status_code=200, media_type="video/mp4", headers=headers)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise error(503, "unavailable", "The hub can't convert videos right now")
    live = claims["k"] == "live"
    _keep(h, claims)
    url = _provider(h, claims, "m3u8" if live else str(claims.get("x") or "mp4"))
    codec = await video_codec(url)
    dev = str(claims.get("dev") or "")
    proc = await CONVERTS.start(dev, convert_args(ffmpeg, url, codec, max(0, min(int(t), 12 * 3600)), live))
    assert proc.stdout is not None
    first = await proc.stdout.read(1 << 16)
    if not first:
        await _stop(proc)
        CONVERTS.done(dev, proc)
        h.db.log("tv.phone.cast", "error", device_id=dev or None, params={"step": "convert", "codec": codec})
        raise error(502, "unavailable", "The TV source didn't answer")
    h.db.log("tv.phone.cast", "ok", device_id=dev or None,
             params={"step": "convert", "codec": codec, "video": "copy" if codec == "h264" else "x264"})

    async def body():
        last = time.time()
        try:
            yield first
            while True:
                chunk = await proc.stdout.read(1 << 16)
                if not chunk:
                    break
                if time.time() - last > 30:
                    _keep(h, claims)
                    last = time.time()
                yield chunk
        finally:
            await _stop(proc)
            CONVERTS.done(dev, proc)

    return StreamingResponse(body(), media_type="video/mp4", headers=headers)


class CastReport(BaseModel):
    """What the Cast TV did with a stream, from the phone: kept so a black screen can be traced afterwards."""

    kind: str = Field(default="", max_length=16)
    id: str = Field(default="", max_length=200)
    state: str = Field(max_length=24)
    code: int | None = None
    reason: str = Field(default="", max_length=200)
    attempt: str = Field(default="", max_length=24)
    receiver: str = Field(default="", max_length=80)


@router.post("/cast/report")
async def phone_cast_report(body: CastReport, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """`state` is playing, failed or timeout. Failed and timeout are logged as errors with the receiver's code."""
    _media(h, c)
    outcome = "ok" if body.state == "playing" else "error"
    h.db.log("tv.phone.cast", outcome, person_id=c.subject.person_id, device_id=c.device["id"],
             target=body.id or None,
             params={"kind": body.kind, "state": body.state, "code": body.code, "reason": body.reason[:200],
                     "attempt": body.attempt, "receiver": body.receiver})
    return {"ok": True}


# -- the player's "Stats" panel -------------------------------------------------------------------------------

_STAT_KEY = re.compile(r"^[a-z0-9_.]{1,40}$")
MAX_STATS = 40


class StreamReport(BaseModel):
    kind: str = Field(default="", max_length=12)
    id: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=200)
    stats: dict[str, Any] = Field(default_factory=dict)


def clean_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """The numbers and short words of a report, and nothing that could be an address: a provider address carries
    its login, so a value with `://`, an `@` or a query string is dropped, not kept in the log."""
    out: dict[str, Any] = {}
    for key, value in stats.items():
        if len(out) >= MAX_STATS or not isinstance(key, str) or not _STAT_KEY.match(key):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            out[key] = value
        elif isinstance(value, float):
            out[key] = round(value, 3)
        elif isinstance(value, str) and len(value) <= 120 and not any(mark in value for mark in ("://", "@", "?", "&")):
            out[key] = value
    return out


@router.post("/report")
async def phone_report(body: StreamReport, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """"Send to hub" on the player's Stats panel: what a grown-up's player measured (route, codec, bitrate, buffer,
    dropped frames, start times), kept in the activity log under their phone so a bad stream can be found later.
    Grown-ups only; a child's player has no Stats panel."""
    _media(h, c)
    if c.subject.role not in ("admin", "adult"):
        raise error(403, "forbidden", "Only a grown-up can send player stats")
    stats = clean_stats(body.stats)
    h.db.log("tv.phone.report", "ok", person_id=c.subject.person_id, device_id=c.device["id"],
             target=body.id or None, params={"kind": body.kind, "note": body.note, "stats": stats})
    return {"ok": True, "kept": len(stats)}
