"""One title search across Plex, every IPTV source on the hub, and TMDB (via Jellyseerr).

A result is one card. The same film in Plex and on IPTV, or the same show in the
guide, is one card keyed by TMDB, then IMDb, then TVDB, then the cleaned title
and year. Offers say where it can be watched. Asking for a title uses the
Jellyseerr request the hub already sends to Sonarr and Radarr. A child does not
send that request: it waits for a parent, the same `requests` row and inbox
approval as the rest of the house.

Kids only see ratings on the household allow-list, and only kids' IPTV. A title
with no confirmed rating is left out.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .policy import Subject

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.titles")

MAX_CARDS = 12


class TitleError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def norm(text: str) -> str:
    raw = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", raw).strip()


def _year(value: object) -> int | None:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


def _identity(kind: str, *, tmdb: str | None, imdb: str | None, tvdb: str | None,
              title: str, year: int | None) -> str:
    if tmdb:
        return f"tmdb:{kind}:{tmdb}"
    if imdb:
        return f"imdb:{imdb}"
    if tvdb:
        return f"tvdb:{kind}:{tvdb}"
    return f"name:{kind}:{norm(title)}:{year or ''}"


def _plex_label(resolution: object) -> str:
    res = str(resolution or "").lower()
    if res in ("4k", "2160", "uhd"):
        return "In Plex (4K)"
    return "In Plex"


def _airs(start: str, channel: str, tz: ZoneInfo, *, catchup_until: str | None, now: float) -> str:
    try:
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return f"On {channel}"
    local = when.astimezone(tz)
    if catchup_until and when.timestamp() <= now:
        try:
            until = datetime.fromisoformat(catchup_until.replace("Z", "+00:00")).astimezone(tz)
            return f"Catch-up until {until.strftime('%a')}"
        except ValueError:
            return f"Catch-up on {channel}"
    if when.timestamp() <= now:
        return f"On now on {channel}"
    clock = local.strftime("%I%p").lstrip("0").lower()
    return f"Airs {local.strftime('%a')} {clock} on {channel}"


class _Card:
    def __init__(self, ident: str, title: str, kind: str, year: int | None):
        self.id = ident
        self.title = title
        self.kind = kind  # movie | show | channel
        self.year = year
        self.poster: str | None = None
        self.summary: str | None = None
        self.tmdb: str | None = None
        self.imdb: str | None = None
        self.tvdb: str | None = None
        self.rating: str | None = None
        self.offers: list[dict[str, Any]] = []
        self.names: set[str] = {norm(title)}
        self.score = 0.0

    def offer(self, source: str, label: str, **extra: Any) -> None:
        for existing in self.offers:
            if existing["source"] == source and existing["label"] == label:
                existing.update({k: v for k, v in extra.items() if v is not None})
                return
        self.offers.append({"source": source, "label": label, **extra})

    def out(self, request: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "kind": self.kind,
            "poster_url": self.poster,
            "summary": self.summary,
            "rating": self.rating,
            "ids": {"tmdb": self.tmdb, "imdb": self.imdb, "tvdb": self.tvdb},
            "offers": self.offers,
            "request": request,
        }


def _find(cards: list[_Card], *, tmdb: str | None, imdb: str | None, tvdb: str | None,
          kind: str, title: str, year: int | None) -> _Card | None:
    ident = _identity(kind, tmdb=tmdb, imdb=imdb, tvdb=tvdb, title=title, year=year)
    for card in cards:
        if card.id == ident or (tmdb and card.tmdb == tmdb and card.kind == kind):
            return card
        if imdb and card.imdb == imdb:
            return card
        if tvdb and card.tvdb == tvdb and card.kind == kind:
            return card
        if norm(title) in card.names and (year is None or card.year in (None, year)) and card.kind == kind:
            return card
    return None


def merge(h: "Hub", s: Subject, q: str, *, plex: list[dict[str, Any]], seerr: list[dict[str, Any]],
          iptv: dict[str, Any], waiting: dict[str, str], now: float | None = None) -> dict[str, Any]:
    """Fold the three sources into cards. `waiting` maps `movie:123` to a local request id."""
    now = time.time() if now is None else now
    tz = ZoneInfo(h.config.hub.timezone)
    kids = s.is_child_like
    cards: list[_Card] = []

    def take(kind: str, title: str, year: int | None, **ids: Any) -> _Card:
        card = _find(cards, tmdb=ids.get("tmdb"), imdb=ids.get("imdb"), tvdb=ids.get("tvdb"),
                     kind=kind, title=title, year=year)
        if card is None:
            card = _Card(_identity(kind, tmdb=ids.get("tmdb"), imdb=ids.get("imdb"), tvdb=ids.get("tvdb"),
                                   title=title, year=year), title, kind, year)
            cards.append(card)
        card.names.add(norm(title))
        for key in ("tmdb", "imdb", "tvdb"):
            if ids.get(key) and not getattr(card, key):
                setattr(card, key, str(ids[key]))
        if ids.get("tmdb") and card.id.startswith("name:"):
            card.id = f"tmdb:{kind}:{ids['tmdb']}"
        return card

    for row in seerr:
        kind = "movie" if row.get("mediaType") == "movie" else "show"
        title = row.get("title") or row.get("name") or ""
        if not title:
            continue
        card = take(kind, title, _year(row.get("releaseDate") or row.get("firstAirDate")), tmdb=str(row.get("id")))
        card.score += 2
        if row.get("posterPath") and not card.poster:
            card.poster = f"https://image.tmdb.org/t/p/w342{row['posterPath']}"
        card.summary = card.summary or row.get("overview")
        _status_from_seerr(card, row.get("mediaInfo") or {})

    for row in plex:
        kind = "show" if row.get("kind") == "show" else "movie" if row.get("kind") != "episode" else "show"
        title = row.get("show") or row["title"] if row.get("kind") == "episode" else row["title"]
        card = take(kind, title, _year(row.get("year")), tmdb=row.get("tmdb"), imdb=row.get("imdb"), tvdb=row.get("tvdb"))
        card.score += 3
        card.poster = card.poster or row.get("poster_url")
        card.summary = card.summary or row.get("summary")
        card.rating = card.rating or row.get("rating")
        plex_id = str(row["id"])
        offer: dict[str, Any] = {"plex_id": plex_id}
        if not s.tv:
            offer["play_on_phone"] = True
        offer["play_on_tv"] = True
        card.offer("plex", _plex_label(row.get("resolution")), **offer)
        card.request_state = "available"  # type: ignore[attr-defined]

    for film in iptv.get("movies") or []:
        card = take("movie", film.get("name") or "", _year(film.get("year")))
        card.score += 2
        card.poster = card.poster or film.get("cover")
        if film.get("id"):
            card.offer("iptv", "IPTV VOD", params={"movie_id": film["id"], "title": film.get("name")}, play_on_tv=True)

    for show in iptv.get("series") or []:
        card = take("show", show.get("name") or "", _year(show.get("year")))
        card.score += 2
        card.poster = card.poster or show.get("cover")
        if show.get("id"):
            card.offer("iptv", "IPTV series", series_id=show["id"])

    for programme in iptv.get("programmes") or []:
        title = programme.get("title") or ""
        if not title:
            continue
        card = _find(cards, tmdb=None, imdb=None, tvdb=None, kind="movie", title=title, year=None) \
            or _find(cards, tmdb=None, imdb=None, tvdb=None, kind="show", title=title, year=None)
        channel = programme.get("channel_name") or "TV"
        label = _airs(programme.get("start") or "", channel, tz, catchup_until=programme.get("catchup_until"), now=now)
        params: dict[str, Any] = {"channel_key": programme.get("channel_key"), "title": title}
        if programme.get("catchup"):
            params["kind"] = "catchup"
            params["start"] = programme.get("start")
        if card is None:
            card = take("show", title, None)
            card.score += 1
        card.offer("guide", label, params=params, play_on_tv=bool(params.get("channel_key")))

    for channel in iptv.get("channels") or []:
        card = _Card(f"iptv:{channel['key']}", channel.get("name") or "Channel", "channel", None)
        card.poster = channel.get("logo")
        card.score += 2
        card.offer("iptv", f"Live on {channel.get('name')}", params={"channel_key": channel["key"], "title": channel.get("name")},
                   play_on_tv=True)
        cards.append(card)

    for card in cards:
        card.request = _request_block(card, s, waiting, kids)  # type: ignore[attr-defined]

    cards.sort(key=lambda c: -c.score)
    return {"query": q, "titles": [c.out(c.request) for c in cards[:MAX_CARDS]]}  # type: ignore[attr-defined]


def _status_from_seerr(card: _Card, info: dict[str, Any]) -> None:
    status = info.get("status")
    if status == 5 or info.get("ratingKey"):
        card.request_state = "available"  # type: ignore[attr-defined]
    elif info.get("downloadStatus"):
        card.request_state = "downloading"  # type: ignore[attr-defined]
    elif status in (2, 3, 4):
        card.request_state = "downloading" if status == 4 else "requested"  # type: ignore[attr-defined]


def _request_block(card: _Card, s: Subject, waiting: dict[str, str], kids: bool) -> dict[str, Any] | None:
    if card.kind == "channel" or not card.tmdb:
        return None
    media_kind = "movie" if card.kind == "movie" else "tv"
    media_id = f"{media_kind}:{card.tmdb}"
    state = getattr(card, "request_state", None)
    if media_id in waiting and state not in ("available", "downloading"):
        state = "waiting"
    can = state not in ("available", "downloading", "requested", "waiting") and _may_ask(s)
    return {
        "media_id": media_id,
        "status": state,
        "can_request": bool(can),
        "ask_parent": bool(can and kids),
        "request_id": waiting.get(media_id),
    }


def _may_ask(s: Subject) -> bool:
    from .policy import ROLE_CAPABILITIES

    caps = set(ROLE_CAPABILITIES.get(s.role, ()))
    for grant in s.grants:
        if "capability" in grant:
            caps.add(grant["capability"])
    for deny in s.denies:
        if "capability" in deny:
            caps.discard(deny["capability"])
    return "media.request" in caps and not s.tv


def _waiting(h: "Hub", s: Subject) -> dict[str, str]:
    rows = h.db.all(
        "SELECT id, payload, status, requester_id FROM requests WHERE kind='media' AND status IN ('waiting','approved','downloading')"
    )
    out = {}
    for row in rows:
        if s.is_child_like and row["requester_id"] != s.person_id:
            continue
        payload = row.get("payload") or {}
        media_id = payload.get("media_id") if isinstance(payload, dict) else None
        if media_id and row["status"] == "waiting":
            out[str(media_id)] = row["id"]
    return out


async def _plex(h: "Hub", s: Subject, q: str) -> list[dict[str, Any]]:
    if not h.plex.enabled:
        return []
    try:
        rows = await h.plex.search_with_ids(q)
    except Exception as exc:  # noqa: BLE001
        log.info("plex search: %s", exc)
        return []
    if s.is_child_like:
        allowed = set(h.config.child_policy.ratings)
        rows = [row for row in rows if row.get("rating") in allowed]
    return rows


async def _seerr(h: "Hub", s: Subject, q: str) -> list[dict[str, Any]]:
    if not h.jellyseerr.enabled:
        return []
    try:
        rows = await h.jellyseerr.search_raw(q)
    except Exception as exc:  # noqa: BLE001
        log.info("jellyseerr search: %s", exc)
        return []
    rows = rows[:12]
    if not s.is_child_like:
        return rows
    return await _kids_only(h, rows)


async def _kids_only(h: "Hub", rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = set(h.config.child_policy.ratings)
    sem = asyncio.Semaphore(4)

    async def one(row: dict[str, Any]) -> dict[str, Any] | None:
        kind = row.get("mediaType")
        if kind not in ("movie", "tv") or row.get("id") is None:
            return None
        async with sem:
            try:
                detail = await h.jellyseerr.details(kind, int(row["id"]))
            except Exception:  # noqa: BLE001
                return None
        rating = h.jellyseerr.content_rating(kind, detail)
        if rating not in allowed:
            return None
        return row

    return [row for row in await asyncio.gather(*(one(r) for r in rows)) if row]


async def search(h: "Hub", s: Subject, q: str) -> dict[str, Any]:
    q = q.strip()
    if len(q) < 2:
        raise TitleError(400, "bad_request", "Type at least two letters")
    if len(q) > 100:
        q = q[:100]
    kids = s.is_child_like or (s.tv and s.role == "child")
    subject = s

    async def iptv() -> dict[str, Any]:
        try:
            await h.tv_catalog.ensure()
        except Exception as exc:  # noqa: BLE001
            log.info("tv catalog: %s", exc)
        return h.tv_catalog.search(q, kids_only=kids)

    plex, seerr, guide = await asyncio.gather(_plex(h, subject, q), _seerr(h, subject, q), iptv())
    return merge(h, subject, q, plex=plex, seerr=seerr, iptv=guide, waiting=_waiting(h, subject))


async def request_title(h: "Hub", s: Subject, media_id: str, device_id: str | None) -> dict[str, Any]:
    """Send a title into Jellyseerr, or ask a parent when the caller is a child."""
    if s.tv or not s.person_id:
        raise TitleError(403, "forbidden", "Ask from a phone")
    if not _may_ask(s):
        raise TitleError(403, "forbidden", "Not available", ask_parent=s.is_child_like)
    try:
        kind, tmdb_s = media_id.split(":", 1)
        tmdb = int(tmdb_s)
        if kind not in ("movie", "tv"):
            raise ValueError
    except ValueError:
        raise TitleError(400, "bad_request", "media_id looks like movie:603 or tv:1399") from None
    if not h.jellyseerr.enabled:
        raise TitleError(503, "unavailable", "Requests aren't set up on the hub")
    try:
        detail = await h.jellyseerr.details(kind, tmdb, fresh=True)
    except Exception as exc:  # noqa: BLE001
        raise TitleError(503, "unavailable", "Couldn't look up that title") from exc
    rating = h.jellyseerr.content_rating(kind, detail)
    if s.is_child_like and rating not in set(h.config.child_policy.ratings):
        raise TitleError(403, "forbidden", "That one needs a grown-up", ask_parent=True)
    name = detail.get("title") or detail.get("name") or "A title"
    poster = f"https://image.tmdb.org/t/p/w342{detail['posterPath']}" if detail.get("posterPath") else None
    from .db import new_id

    if s.is_child_like:
        rid = new_id("req")
        h.db.insert("requests", {
            "id": rid, "created_at": time.time(), "kind": "media", "requester_id": s.person_id,
            "title": name, "payload": {"media_id": media_id}, "poster_url": poster, "status": "waiting",
        })
        h.db.log("request.create", "ok", person_id=s.person_id, device_id=device_id, target=rid,
                 params={"kind": "media", "title": name, "via": "titles"})
        who = (h.db.person(s.person_id) or {}).get("name") or "Someone"
        await h.notify.send(
            roles=("admin", "adult"), level="decision", cat="requests", title=f"{who} asked for {name}",
            body=None, actions=[{"id": "approve", "title": "Approve"}, {"id": "deny", "title": "Not now", "destructive": True}],
            ref=f"thomascreek://requests/{rid}", group_key=f"request:{rid}", push_category="REQUEST",
        )
        return {"status": "waiting", "request_id": rid, "media_id": media_id, "title": name, "ask_parent": True}

    if not h.actions.is_live("media.approve"):
        raise TitleError(503, "dry_run", "Requests are switched off while the hub is in test mode")
    try:
        res = await h.jellyseerr.submit(kind, tmdb, None)
    except Exception as exc:  # noqa: BLE001
        raise TitleError(502, "failed", "Jellyseerr didn't take that request") from exc
    ext = str((res or {}).get("id") or "")
    h.db.insert("requests", {
        "id": new_id("req"), "created_at": time.time(), "kind": "media", "requester_id": s.person_id,
        "title": name, "payload": {"media_id": media_id}, "poster_url": poster, "status": "approved",
        "decided_by": s.person_id, "decided_at": time.time(), "external_id": ext or None,
    })
    h.db.log("media.request", "ok", person_id=s.person_id, device_id=device_id, target=media_id,
             params={"title": name, "via": "titles"})
    return {"status": "requested", "request_id": ext or None, "media_id": media_id, "title": name, "ask_parent": False}
