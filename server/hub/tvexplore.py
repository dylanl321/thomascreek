"""A Netflix-style shelf of titles this house can actually play or ask for.

The feed is built ahead of time and served from that copy. A request never
walks the channel lineup. With no TMDB key, the shelf is just the library.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from .db import new_id

log = logging.getLogger("hub.explore")

KV = "tv.explore"
TMDB = "https://api.themoviedb.org/3"
REFRESH_S = 6 * 3600
ROW_LIMIT = 12
_WORD = re.compile(r"[^a-z0-9]+")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_SKIP = frozenset({"the", "a", "an"})
_BADGES = ("New", "Popular", "Top 10", "Requested")
_REASONS = {
    "new": "New this week",
    "popular": "Popular this week",
    "acclaimed": "Acclaimed",
    "library": "In your library",
}
_ROW_TITLES = {"new": "New releases", "popular": "Popular", "acclaimed": "Must watch", "library": "Recently added"}


class ExploreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


async def refresh(h: Any) -> None:
    """Rebuild both feeds. A TMDB miss keeps the previous copy."""
    secret = h.config.artwork.tmdb_key_secret
    key = h.vault.get(secret) if h.vault.has(secret) else None
    if not key:
        publish(h, None, can_request=bool(h.jellyseerr.enabled))
        return
    try:
        lists = await _tmdb(str(key))
    except Exception as exc:  # noqa: BLE001
        log.info("tmdb: %s", exc)
        if not isinstance(_cache(h).get("adult"), dict):
            publish(h, None, can_request=bool(h.jellyseerr.enabled))
        return
    publish(h, lists, can_request=bool(h.jellyseerr.enabled))


async def _tmdb(key: str) -> dict[str, list[dict[str, Any]]]:
    async with httpx.AsyncClient(timeout=20) as client:
        new, popular, acclaimed = await _gather(_get(client, key, "/trending/all/week"),
                                                _get(client, key, "/movie/now_playing"),
                                                _get(client, key, "/movie/top_rated"))
    return {
        "new": [_tmdb_card(row) for row in (new + popular)[:20]],
        "popular": [_tmdb_card(row) for row in popular[:20]],
        "acclaimed": [_tmdb_card(row) for row in acclaimed[:20]],
    }


async def _gather(*calls: Any) -> list[list[dict[str, Any]]]:
    return list(await asyncio.gather(*calls))


async def _get(client: httpx.AsyncClient, key: str, path: str) -> list[dict[str, Any]]:
    response = await client.get(TMDB + path, params={"api_key": key})
    response.raise_for_status()
    return [row for row in response.json().get("results") or [] if isinstance(row, dict)]


def _tmdb_card(row: dict[str, Any]) -> dict[str, Any]:
    kind = "series" if row.get("media_type") == "tv" or row.get("name") else "movie"
    if row.get("title") and not row.get("name"):
        kind = "movie"
    title = row.get("title") or row.get("name")
    return {
        "tmdb_id": row.get("id"), "kind": kind, "title": title,
        "year": (row.get("release_date") or row.get("first_air_date") or "")[:4],
        "overview": row.get("overview"), "rating": None,
        "genres": [], "poster_path": row.get("poster_path"), "backdrop_path": row.get("backdrop_path"),
    }


async def cached_art(h: Any, kind: str, media: str, tmdb_id: str) -> tuple[bytes, str] | None:
    """A poster, backdrop or logo kept on the hub. The first ask downloads it."""
    if kind not in ("poster", "backdrop", "logo") or media not in ("movie", "tv") or not tmdb_id.isdigit():
        return None
    folder = h.tv_app.dir / "explore-art"
    folder.mkdir(parents=True, exist_ok=True)
    types = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    for ext, mime in types.items():
        path = folder / f"{kind}-{media}-{tmdb_id}.{ext}"
        if path.is_file():
            return path.read_bytes(), mime
    secret = h.config.artwork.tmdb_key_secret
    key = h.vault.get(secret) if h.vault.has(secret) else None
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if kind == "logo":
                info = await client.get(f"{TMDB}/{media}/{tmdb_id}/images", params={"api_key": key})
                info.raise_for_status()
                logos = info.json().get("logos") or []
                file_path = next((row.get("file_path") for row in logos if row.get("file_path")), None)
                size = "w500"
            else:
                info = await client.get(f"{TMDB}/{media}/{tmdb_id}", params={"api_key": key})
                info.raise_for_status()
                file_path = info.json().get("poster_path" if kind == "poster" else "backdrop_path")
                size = "w780" if kind == "poster" else "w1280"
            if not isinstance(file_path, str) or not file_path.startswith("/"):
                return None
            image = await client.get(f"https://image.tmdb.org/t/p/{size}{file_path}")
            image.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.info("explore art: %s", exc)
        return None
    ext = "png" if "png" in image.headers.get("content-type", "") else "webp" if "webp" in image.headers.get("content-type", "") else "jpg"
    (folder / f"{kind}-{media}-{tmdb_id}.{ext}").write_bytes(image.content)
    return image.content, types[ext]


def feed(h: Any, profile: str) -> dict[str, Any]:
    """The last built feed. A missing copy is the library, with no network call."""
    cached = _cache(h).get(profile)
    if isinstance(cached, dict) and "rows" in cached:
        return {"hero": cached.get("hero"), "rows": cached.get("rows") or []}
    built = library_feed(h, profile)
    _store_profile(h, profile, built)
    return built


def library_feed(h: Any, profile: str) -> dict[str, Any]:
    built, _links = build_feed(_library(h, profile == "kids"), None, can_request=False, kids=profile == "kids",
                               allowed=_allowed(h))
    return built


def build_feed(library: list[dict[str, Any]], lists: dict[str, list[dict[str, Any]]] | None, *,
               can_request: bool, kids: bool, allowed: set[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Turn a library and optional TMDB lists into the public feed. Nothing here touches the network."""
    if not lists:
        items = [_library_card(row) for row in _recent(library)]
        return _pack("library", items), {}
    index = _index(library)
    rows = []
    links: dict[str, dict[str, Any]] = {}
    for row_id in ("new", "popular", "acclaimed"):
        cards = []
        for place, raw in enumerate(lists.get(row_id) or []):
            card, link = _resolve(raw, index, can_request=can_request, kids=kids, allowed=allowed, row_id=row_id, place=place)
            if card is None:
                continue
            cards.append(card)
            if link:
                links[link[0]] = link[1]
        rows.append((row_id, cards))
    return _dedupe(rows), links


def publish(h: Any, lists: dict[str, list[dict[str, Any]]] | None, *, can_request: bool) -> dict[str, dict[str, Any]]:
    """Build both profiles and keep them. `lists` is None when there is no TMDB key."""
    allowed = _allowed(h)
    adult, links_a = build_feed(_library(h, False), lists, can_request=can_request, kids=False, allowed=allowed)
    kids, links_k = build_feed(_library(h, True), lists, can_request=can_request, kids=True, allowed=allowed)
    raw = _cache(h)
    raw["adult"] = adult
    raw["kids"] = kids
    raw["links"] = {**links_k, **links_a}
    h.db.kv_set(KV, raw)
    return {"adult": adult, "kids": kids}


def ask(h: Any, subject: Any, body: dict[str, Any]) -> dict[str, Any]:
    """One tap. The answer is a sentence the TV can show."""
    kind, tmdb = _asked(body)
    profile = _profile(h, subject)
    card = _card(h, profile, kind, tmdb)
    if card is None:
        other = _card(h, "adult" if profile == "kids" else "kids", kind, tmdb)
        if profile == "kids" and other is not None and other.get("rating") not in _allowed(h):
            return {"ok": False, "status": "unavailable", "message": "That one needs a grown-up."}
        return {"ok": False, "status": "unavailable", "message": "That isn't something we can ask for yet."}
    if card.get("availability") == "play":
        out = {"ok": True, "status": "play", "message": "It's already in the library."}
        for key in ("movie_id", "series_id", "plex_id", "recording_id"):
            if card.get(key):
                out[key] = card[key]
        return out
    if not h.jellyseerr.enabled:
        return {"ok": False, "status": "unavailable", "message": "That isn't something we can ask for yet."}
    media_id = card.get("media_id") or f"{'tv' if kind == 'series' else 'movie'}:{tmdb}"
    if getattr(subject, "is_child_like", False):
        rid = new_id("req")
        h.db.insert("requests", {
            "id": rid, "created_at": _now(), "kind": "media", "requester_id": subject.person_id,
            "title": card.get("title"), "payload": {"media_id": media_id}, "status": "waiting",
        })
        _track(h, card, "waiting", rid, profile)
        return {"ok": True, "status": "waiting", "request_id": rid, "tmdb_id": tmdb, "kind": kind,
                "media_id": media_id, "message": "Asked a parent."}
    if not h.actions.is_live("media.approve"):
        return {"ok": False, "status": "unavailable", "message": "Requests are paused right now."}
    return {"ok": True, "pending_submit": True, "tmdb_id": tmdb, "kind": kind, "media_id": media_id,
            "title": card.get("title"), "profile": profile, "card": card}


async def submit(h: Any, prepared: dict[str, Any]) -> dict[str, Any]:
    """The network half of an adult request. Kept separate so the decision stays easy to test."""
    kind = "tv" if prepared["kind"] == "series" else "movie"
    try:
        res = await h.jellyseerr.submit(kind, int(prepared["tmdb_id"]), None)
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": "unavailable", "message": "That isn't something we can ask for yet."}
    rid = str((res or {}).get("id") or "")
    _track(h, prepared["card"], "requested", rid or None, prepared["profile"])
    _tell(h, prepared["card"], "requested", prepared["profile"], request_id=rid or None)
    out = {"ok": True, "status": "requested", "tmdb_id": prepared["tmdb_id"], "kind": prepared["kind"],
           "media_id": prepared["media_id"], "message": f"Asked for {prepared['title']}."}
    if rid:
        out["request_id"] = rid
    return out


def advance(h: Any, tmdb_id: int, kind: str, status: str, *, movie_id: str | None = None,
            series_id: str | None = None) -> None:
    """Record request progress and tell the open apps. The same status twice does not tell them again."""
    if status not in ("requested", "downloading", "ready", "waiting"):
        return
    raw = _cache(h)
    rows = list(raw.get("requests") or [])
    changed = False
    for found in rows:
        if found.get("tmdb_id") != tmdb_id or found.get("kind") != kind:
            continue
        if found.get("status") == status and found.get("movie_id") == movie_id and found.get("series_id") == series_id:
            continue
        found["status"] = status
        if movie_id:
            found["movie_id"] = movie_id
        if series_id:
            found["series_id"] = series_id
        changed = True
        if status in ("requested", "downloading", "ready"):
            _tell(h, found, status, found.get("profile") or "adult", movie_id=movie_id, series_id=series_id)
    if not changed:
        return
    raw["requests"] = rows
    h.db.kv_set(KV, raw)


def ready_items(h: Any, profile: str) -> list[dict[str, Any]]:
    """Titles that were asked for and can now be played. Up Next adds them to Ready to watch."""
    items = []
    for row in _cache(h).get("requests") or []:
        if row.get("status") != "ready" or row.get("profile") not in (None, profile):
            continue
        if not row.get("movie_id") and not row.get("series_id"):
            continue
        item: dict[str, Any] = {
            "kind": "movie" if row.get("movie_id") else "series",
            "title": row.get("title") or "Ready",
            "reason": "ready to watch",
        }
        if row.get("movie_id"):
            item["movie_id"] = row["movie_id"]
        if row.get("series_id"):
            item["series_id"] = row["series_id"]
        if row.get("poster_url"):
            item["poster_url"] = row["poster_url"]
        items.append(item)
    return items


def _library(h: Any, kids: bool) -> list[dict[str, Any]]:
    allowed = _allowed(h)
    items = []
    catalog = getattr(h, "tv_catalog", None)
    doc = catalog.load() if catalog is not None else {}
    for kind, bucket in (("movie", "movies"), ("series", "series")):
        for row in doc.get(bucket) or []:
            if not isinstance(row, dict) or not row.get("id") or not row.get("name"):
                continue
            kid = bool(catalog._is_kids(row.get("category"))) if catalog is not None else False
            rating = _text(row.get("rating"))
            if kids and not kid and rating not in allowed:
                continue
            items.append(_from_library(h, row, kind, kid))
    for row in _recordings(h):
        if kids and row.get("rating") not in allowed and not row.get("kids"):
            continue
        items.append(row)
    return items


def _from_library(h: Any, row: dict[str, Any], kind: str, kids: bool) -> dict[str, Any]:
    title = str(row.get("name"))
    ident = str(row["id"])
    art = getattr(h, "artwork", None)
    poster = art.public_url("poster", ident) if art is not None else None
    backdrop = art.public_url("backdrop", ident) if art is not None else None
    genre = _text(row.get("genre"))
    item = {
        "kind": kind, "title": title, "year": _year(row.get("year")), "overview": _text(row.get("plot")),
        "tagline": None, "poster_url": poster, "backdrop_url": backdrop, "logo_url": None,
        "rating": _text(row.get("rating")), "genres": [genre] if genre else [], "availability": "play",
        "kids": kids, "tmdb_id": row.get("tmdb_id") if isinstance(row.get("tmdb_id"), int) else None,
    }
    if kind == "movie":
        item["movie_id"] = ident
    else:
        item["series_id"] = ident
    return item


def _recordings(h: Any) -> list[dict[str, Any]]:
    raw = h.db.kv_get("dvr.recordings") or []
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict) or row.get("status") != "done" or not row.get("title") or not row.get("id"):
            continue
        poster = row.get("poster_url") if isinstance(row.get("poster_url"), str) and row["poster_url"].startswith("/v1/") else None
        out.append({
            "kind": "movie", "title": str(row["title"]), "year": _year(row.get("year")), "overview": None,
            "tagline": None, "poster_url": poster, "backdrop_url": None, "logo_url": None, "rating": None,
            "genres": [], "availability": "play", "recording_id": str(row["id"]), "kids": False, "tmdb_id": None,
        })
    return out


def _recent(library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(library, key=lambda row: (-(row.get("year") or 0), str(row.get("title") or "").casefold()))[:ROW_LIMIT]


def _library_card(row: dict[str, Any]) -> dict[str, Any]:
    card = _view(row, reason="library", badges=["New"])
    return card


def _index(library: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    found: dict[tuple, dict[str, Any]] = {}
    for row in library:
        name = _norm(row.get("title") or "")
        if row.get("tmdb_id"):
            found[("tmdb", row["kind"], int(row["tmdb_id"]))] = row
        if name:
            found[("name", name, row.get("year"))] = row
            found.setdefault(("name", name, None), row)
    return found


def _resolve(raw: dict[str, Any], index: dict[tuple, dict[str, Any]], *, can_request: bool, kids: bool,
             allowed: set[str], row_id: str, place: int) -> tuple[dict[str, Any] | None, tuple[str, dict[str, Any]] | None]:
    title = _text(raw.get("title"))
    if not title:
        return None, None
    kind = "series" if raw.get("kind") == "series" else "movie"
    tmdb = raw.get("tmdb_id") if isinstance(raw.get("tmdb_id"), int) and not isinstance(raw.get("tmdb_id"), bool) else None
    year = _year(raw.get("year"))
    rating = _text(raw.get("rating"))
    hit = None
    if tmdb is not None:
        hit = index.get(("tmdb", kind, tmdb))
    if hit is None and title:
        hit = index.get(("name", _norm(title), year)) or index.get(("name", _norm(title), None))
    badges = _badges(row_id, place, requested=False)
    if hit is not None:
        if kids and not hit.get("kids") and (rating or hit.get("rating")) not in allowed:
            return None, None
        shown = dict(hit)
        shown.pop("tmdb_id", None)
        card = _view({**shown, "title": title, "year": year or hit.get("year"),
                      "overview": _text(raw.get("overview")) or hit.get("overview"),
                      "tagline": _text(raw.get("tagline")),
                      "rating": rating or hit.get("rating"),
                      "genres": raw.get("genres") or hit.get("genres") or [],
                      "poster_url": _tmdb_art("poster", kind, tmdb, raw.get("poster_path")) or hit.get("poster_url"),
                      "backdrop_url": _tmdb_art("backdrop", kind, tmdb, raw.get("backdrop_path")) or hit.get("backdrop_url"),
                      "logo_url": _tmdb_art("logo", kind, tmdb, raw.get("logo_path")) or hit.get("logo_url")},
                     reason=row_id, badges=badges)
        link = None
        if tmdb is not None:
            link = (f"{kind}:{tmdb}", {key: card[key] for key in ("movie_id", "series_id", "recording_id", "plex_id") if card.get(key)})
        return card, link
    if not can_request or tmdb is None:
        return None, None
    if kids and rating not in allowed:
        return None, None
    card = _view({
        "kind": kind, "title": title, "year": year, "overview": _text(raw.get("overview")),
        "tagline": _text(raw.get("tagline")), "rating": rating, "genres": raw.get("genres") or [],
        "poster_url": _tmdb_art("poster", kind, tmdb, raw.get("poster_path")),
        "backdrop_url": _tmdb_art("backdrop", kind, tmdb, raw.get("backdrop_path")),
        "logo_url": _tmdb_art("logo", kind, tmdb, raw.get("logo_path")),
        "availability": "request", "tmdb_id": tmdb,
        "media_id": f"{'tv' if kind == 'series' else 'movie'}:{tmdb}",
    }, reason=row_id, badges=badges)
    return card, None


def _view(row: dict[str, Any], *, reason: str, badges: list[str]) -> dict[str, Any]:
    card: dict[str, Any] = {
        "kind": "series" if row.get("kind") == "series" else "movie",
        "title": row.get("title"),
        "year": row.get("year"),
        "overview": row.get("overview"),
        "tagline": row.get("tagline"),
        "poster_url": row.get("poster_url"),
        "backdrop_url": row.get("backdrop_url"),
        "logo_url": row.get("logo_url"),
        "rating": row.get("rating"),
        "genres": list(row.get("genres") or []),
        "reason": _REASONS[reason],
        "badges": [name for name in _BADGES if name in badges],
        "availability": row.get("availability") or "play",
    }
    for key in ("movie_id", "series_id", "plex_id", "recording_id", "tmdb_id", "media_id", "request_id"):
        if row.get(key) not in (None, ""):
            card[key] = row[key]
    return card


def _badges(row_id: str, place: int, *, requested: bool) -> list[str]:
    names = []
    if row_id in ("new", "library"):
        names.append("New")
    if row_id == "popular":
        names.append("Popular")
    if row_id in ("popular", "acclaimed") and place < 10:
        names.append("Top 10")
    if requested:
        names.append("Requested")
    return names


def _pack(row_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    items = items[:ROW_LIMIT]
    hero = _hero(items)
    if not items:
        return {"hero": None, "rows": []}
    return {"hero": hero, "rows": [{"id": row_id, "title": _ROW_TITLES[row_id], "items": items}]}


def _dedupe(rows: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    seen: set[str] = set()
    kept = []
    hero_pool: list[dict[str, Any]] = []
    for row_id, cards in rows:
        fresh = []
        for card in cards:
            key = str(card.get("tmdb_id") or card.get("movie_id") or card.get("series_id") or card.get("title"))
            if key in seen:
                continue
            seen.add(key)
            fresh.append(card)
            if len(fresh) >= ROW_LIMIT:
                break
        if fresh:
            kept.append({"id": row_id, "title": _ROW_TITLES[row_id], "items": fresh})
            if row_id == "new":
                hero_pool = fresh
    if not hero_pool and kept:
        hero_pool = kept[0]["items"]
    return {"hero": _hero(hero_pool), "rows": kept}


def _hero(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if item.get("backdrop_url"):
            return dict(item)
    return dict(items[0]) if items else None


def _tmdb_art(kind: str, media_kind: str, tmdb: int | None, path: object) -> str | None:
    if tmdb is None or not isinstance(path, str) or not path.startswith("/"):
        return None
    media = "tv" if media_kind == "series" else "movie"
    return f"/v1/tv/explore/art/{kind}/{media}/{tmdb}"


def _cache(h: Any) -> dict[str, Any]:
    raw = h.db.kv_get(KV) or {}
    return raw if isinstance(raw, dict) else {}


def _store_profile(h: Any, profile: str, built: dict[str, Any]) -> None:
    raw = _cache(h)
    raw[profile] = built
    h.db.kv_set(KV, raw)


def _card(h: Any, profile: str, kind: str, tmdb: int) -> dict[str, Any] | None:
    cached = _cache(h).get(profile) or {}
    want = f"{kind}:{tmdb}"
    links = (_cache(h).get("links") or {}).get(want)
    for item in _walk(cached):
        if _same(item, kind, tmdb) or _ids_match(item, links):
            return item
    return None


def _walk(cached: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    if isinstance(cached.get("hero"), dict):
        items.append(cached["hero"])
    for row in cached.get("rows") or []:
        items.extend(item for item in (row.get("items") or []) if isinstance(item, dict))
    return items


def _same(item: dict[str, Any], kind: str, tmdb: int) -> bool:
    return item.get("kind") == kind and item.get("tmdb_id") == tmdb


def _ids_match(item: dict[str, Any], links: object) -> bool:
    if not isinstance(links, dict):
        return False
    for key in ("movie_id", "series_id", "recording_id", "plex_id"):
        if links.get(key) and item.get(key) == links[key]:
            return True
    return False


def remember_links(h: Any, links: dict[str, dict[str, Any]]) -> None:
    raw = _cache(h)
    raw["links"] = links
    h.db.kv_set(KV, raw)


def _track(h: Any, card: dict[str, Any], status: str, request_id: str | None, profile: str) -> None:
    raw = _cache(h)
    rows = [row for row in (raw.get("requests") or [])
            if not (row.get("tmdb_id") == card.get("tmdb_id") and row.get("kind") == card.get("kind") and row.get("profile") == profile)]
    rows.append({
        "tmdb_id": card.get("tmdb_id"), "kind": card.get("kind"), "title": card.get("title"),
        "status": status, "request_id": request_id, "profile": profile,
        "poster_url": card.get("poster_url"), "media_id": card.get("media_id"),
    })
    raw["requests"] = rows[-40:]
    h.db.kv_set(KV, raw)


def _tell(h: Any, card: dict[str, Any], status: str, profile: str, *, request_id: str | None = None,
          movie_id: str | None = None, series_id: str | None = None) -> None:
    data: dict[str, Any] = {
        "tmdb_id": card.get("tmdb_id"), "kind": card.get("kind"), "title": card.get("title"),
        "status": status, "profile": profile,
    }
    if request_id:
        data["request_id"] = request_id
    if movie_id or card.get("movie_id"):
        data["movie_id"] = movie_id or card.get("movie_id")
    if series_id or card.get("series_id"):
        data["series_id"] = series_id or card.get("series_id")
    h.bus.publish({"type": "explore", "data": data})


def _asked(body: dict[str, Any]) -> tuple[str, int]:
    if not isinstance(body, dict):
        raise ExploreError(400, "Say which title.")
    kind = body.get("kind")
    tmdb = body.get("tmdb_id")
    if kind not in ("movie", "series") or isinstance(tmdb, bool) or not isinstance(tmdb, int) or tmdb < 1:
        raise ExploreError(400, "Say which title.")
    return kind, tmdb


def _profile(h: Any, subject: Any) -> str:
    if getattr(subject, "is_child_like", False):
        return "kids"
    if getattr(subject, "tv", False):
        tv = h.tv_app.tvs().get(subject.tv_id or "") or {}
        if tv.get("profile") == "kids":
            return "kids"
    return "adult"


def _allowed(h: Any) -> set[str]:
    return {str(item) for item in (h.config.child_policy.ratings or [])}


def _norm(title: str) -> str:
    words = [word for word in _WORD.sub(" ", title.casefold()).split() if word not in _SKIP]
    return " ".join(words)


def _year(value: object) -> int | None:
    match = _YEAR.search(str(value or ""))
    return int(match.group(0)) if match else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:400] or None


def _now() -> float:
    return time.time()
