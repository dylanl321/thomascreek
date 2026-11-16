"""What to put on next, ranked from what this profile actually watches.

Nothing is configured. A play on a TV is remembered, and the shelf is built from
that: resume, the next episode, a finished recording, what's just started on a
channel they watch, then whatever they put on most. A profile with no history
gets an empty shelf, not a guess.
"""

from __future__ import annotations

import time
from typing import Any

from .artwork import programme_key
from .tvexplore import _clean_name, ready_items

KV = "tv.watch"
KV_RECORDINGS = "dvr.recordings"
RECENT_S = 30 * 86400
COUNT_GAP_S = 1800
STARTING_S = 20 * 60
ROW_LIMIT = 8

REASONS = {
    "continue": "continue watching",
    "next": "new episode of a series you watch or record",
    "shuffle": "next in your shuffle",
    "recordings": "recording ready",
    "arrived": "ready to watch",
    "onnow": "starting now on a channel you watch",
    "trending": "trending in your history",
}
TITLES = {
    "continue": "Continue watching",
    "next": "New episodes",
    "recordings": "Ready to watch",
    "onnow": "On now",
    "trending": "For you",
}


def remember(db: Any, profile: str, playing: dict[str, Any] | None, now: float | None = None, *,
             person: str | None = None, tv: str | None = None) -> None:
    """Count a play. The same title reporting progress does not count twice in half an hour.

    The profile bucket (adult or kids) always counts. A named person gets their own bucket, and so does
    the TV, which is what a TV with nobody picked reads back."""
    if profile not in ("adult", "kids") or not isinstance(playing, dict):
        return
    now = time.time() if now is None else now
    doc = db.kv_get(KV) or {}
    if not isinstance(doc, dict):
        doc = {}
    changed = False
    for key in (profile, who_key(person=person), who_key(tv=tv)):
        if not key:
            continue
        bucket = doc.get(key) if isinstance(doc.get(key), dict) else {}
        updated = _add(bucket, playing, now)
        if updated is not None:
            doc[key] = updated
            changed = True
    if changed:
        db.kv_set(KV, doc)


def who_key(*, person: str | None = None, tv: str | None = None) -> str | None:
    """The history bucket of one person or one TV."""
    if person:
        return f"person:{person}"[:80]
    if tv:
        return f"tv:{tv}"[:80]
    return None


def _add(bucket: dict[str, Any], playing: dict[str, Any], now: float) -> dict[str, Any] | None:
    channels = dict(bucket.get("channels") or {})
    series = dict(bucket.get("series") or {})
    movies = dict(bucket.get("movies") or {})
    position = _position(playing)
    duration = _duration(playing)
    title = _text(playing.get("title") or playing.get("name"))
    changed = False
    channel = _text(playing.get("channel_key"))
    if channel:
        channels[channel] = _touch(channels.get(channel), now, title=title or channels.get(channel, {}).get("title"))
        changed = True
    movie = _text(playing.get("movie_id"))
    if movie:
        movies[movie] = _touch(movies.get(movie), now, title=title, position_s=position, duration_s=duration)
        changed = True
    episode = _text(playing.get("episode_id"))
    series_id = _text(playing.get("series_id"))
    if episode or series_id:
        key = series_id or episode
        extra: dict[str, Any] = {"title": title}
        if episode:
            extra["episode_id"] = episode
        if position is not None:
            extra["position_s"] = position
        if duration is not None:
            extra["duration_s"] = duration
        row = _touch(series.get(key), now, **extra)
        # A shuffled play keeps the TV's order so any screen continues it; a play in order ends the shuffle.
        if episode and isinstance(playing.get("shuffle"), dict):
            row["shuffle"] = playing["shuffle"]
        elif episode:
            row.pop("shuffle", None)
        series[key] = row
        changed = True
    if not changed:
        return None
    return {
        "channels": _trim(channels),
        "series": _trim(series),
        "movies": _trim(movies),
    }


def shelf(h: Any, profile: str, now: float | None = None, *, who: str | None = None) -> dict[str, Any]:
    """Rows for one profile. Kids only see kids' titles. Empty groups are left out.

    `who` is a person's or a TV's bucket (see who_key). It is used when it has any history; a person or TV
    with none yet gets the profile's rows, so a new TV is not empty."""
    now = time.time() if now is None else now
    kids = profile == "kids"
    history = (_history(h, who) if who else {}) or _history(h, profile)
    seen: set[str] = set()
    rows = [
        _row("continue", _continue(h, history, kids), seen),
        _row("next", _next_episodes(h, history, kids), seen),
        _row("recordings", _recordings(h, kids, now) + _ready(h, kids), seen),
        _row("onnow", _starting(h, history, kids, now), seen),
        _row("trending", _trending(h, history, kids, now), seen),
    ]
    return {"rows": [row for row in rows if row]}


def _ready(h: Any, kids: bool) -> list[dict[str, Any]]:
    return ready_items(h, "kids" if kids else "adult")


def _row(row_id: str, items: list[dict[str, Any]], seen: set[str]) -> dict[str, Any] | None:
    fresh = []
    for item in items:
        key = item.get("episode_id") or item.get("movie_id") or item.get("recording_id") or item.get("channel_key") or item.get("title")
        if not key or key in seen:
            continue
        seen.add(str(key))
        fresh.append(item)
        if len(fresh) >= ROW_LIMIT:
            break
    if not fresh:
        return None
    return {"id": row_id, "title": TITLES[row_id], "items": fresh}


def _continue(h: Any, history: dict[str, Any], kids: bool) -> list[dict[str, Any]]:
    items = []
    for movie_id, row in (history.get("movies") or {}).items():
        movie = h.tv_catalog.movie(str(movie_id)) if h.tv_catalog else None
        if movie is None:
            movie = _plex_movie(h, str(movie_id))
        if movie is None or (kids and not movie.get("kids")):
            continue
        position = row.get("position_s")
        if not isinstance(position, (int, float)) or position < 30:
            continue
        duration = movie.get("duration_s") or row.get("duration_s")
        if isinstance(duration, (int, float)) and duration > 0 and position >= duration - 60:
            continue
        items.append(_item(
            h, kind="movie", title=movie.get("name") or row.get("title") or "Movie",
            subtitle=_left(position), reason="continue", movie_id=movie["id"],
            poster=movie.get("poster_url"), backdrop=movie.get("backdrop_url"),
            progress=_progress(position, duration), rank=float(row.get("last") or 0),
        ))
    for key, row in (history.get("series") or {}).items():
        episode_id = row.get("episode_id")
        episode = h.tv_catalog.episode(str(episode_id)) if episode_id and h.tv_catalog else None
        if episode is None or (kids and not episode.get("kids")):
            continue
        position = row.get("position_s")
        if not isinstance(position, (int, float)) or position < 30:
            continue
        items.append(_item(
            h, kind="episode", title=episode.get("title") or row.get("title") or "Episode",
            subtitle=episode.get("series_name"), reason="continue",
            episode_id=episode["id"], series_id=episode.get("series_id") or key,
            progress=_progress(position, row.get("duration_s")), rank=float(row.get("last") or 0),
        ))
    items.sort(key=lambda item: float(item.get("_rank") or 0), reverse=True)
    for item in items:
        item.pop("_rank", None)
    return items


_KID_RATINGS = frozenset({"G", "PG", "TV-Y", "TV-Y7", "TV-G"})


def _plex_movie(h: Any, movie_id: str) -> dict[str, Any] | None:
    """A Plex film the TVs report as vod:plex:<id>, from the hub's Plex lists. None when unknown."""
    if not movie_id.startswith("vod:plex:"):
        return None
    from .tvexplore import _plex_index, _plex_shelf

    plex_id = movie_id.removeprefix("vod:plex:")
    for row in _plex_shelf(h) + _plex_index(h):
        if row.get("kind") != "movie" or str(row.get("plex_id") or "") != plex_id:
            continue
        tmdb = row.get("tmdb_id")
        art = (lambda kind: f"/v1/tv/explore/art/{kind}/movie/{tmdb}") if isinstance(tmdb, int) else (lambda kind: None)
        return {
            "id": movie_id, "name": row.get("title"),
            "poster_url": row.get("poster_url") or art("poster"),
            "backdrop_url": row.get("backdrop_url") or art("backdrop"),
            "kids": bool(row.get("kids")) or row.get("rating") in _KID_RATINGS,
        }
    return None


def _next_episodes(h: Any, history: dict[str, Any], kids: bool) -> list[dict[str, Any]]:
    if not h.tv_catalog:
        return []
    details = h.tv_catalog._details().get("episodes") or {}
    by_series: dict[str, list[dict[str, Any]]] = {}
    for episode in details.values():
        if isinstance(episode, dict) and episode.get("series_id"):
            by_series.setdefault(str(episode["series_id"]), []).append(episode)
    items = []
    for series_id, row in (history.get("series") or {}).items():
        episodes = by_series.get(str(series_id)) or []
        if not episodes:
            continue
        current = row.get("episode_id")
        shuffled = _shuffle_next(row, episodes)
        if shuffled is not None:
            nxt, shuffle = shuffled
            if kids and not nxt.get("kids"):
                continue
            item = _item(
                h, kind="episode", title=nxt.get("title") or "Next episode",
                subtitle=nxt.get("series_name"), reason="shuffle",
                episode_id=nxt.get("id"), series_id=series_id,
                rank=float(row.get("n") or 0) * 1000 + float(row.get("last") or 0),
            )
            item["shuffle"] = shuffle
            items.append(item)
            continue
        if isinstance(row.get("shuffle"), dict):
            continue  # the shuffle reached its end: nothing is "next" until a new play
        watched = 0
        if current:
            found = next((ep for ep in episodes if ep.get("id") == current), None)
            watched = int((found or {}).get("number") or 0)
        newer = [ep for ep in episodes if int(ep.get("number") or 0) > watched]
        if not newer:
            continue
        nxt = sorted(newer, key=lambda ep: int(ep.get("number") or 0))[0]
        if kids and not nxt.get("kids"):
            continue
        items.append(_item(
            h, kind="episode", title=nxt.get("title") or "Next episode",
            subtitle=nxt.get("series_name"), reason="next",
            episode_id=nxt.get("id"), series_id=series_id,
            rank=float(row.get("n") or 0) * 1000 + float(row.get("last") or 0),
        ))
    items.sort(key=lambda item: float(item.get("_rank") or 0), reverse=True)
    for item in items:
        item.pop("_rank", None)
    return items


def _shuffle_next(row: dict[str, Any], episodes: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The episode after the current one in the TV's shuffle order, and the shuffle to hand back."""
    shuffle = row.get("shuffle")
    if not isinstance(shuffle, dict):
        return None
    order = [str(item) for item in shuffle.get("order") or []]
    current = str(row.get("episode_id") or "")
    if current not in order:
        return None
    known = {str(ep.get("id")): ep for ep in episodes}
    for episode_id in order[order.index(current) + 1:]:
        if episode_id in known:
            return known[episode_id], shuffle
    return None


def _recordings(h: Any, kids: bool, now: float) -> list[dict[str, Any]]:
    raw = h.db.kv_get(KV_RECORDINGS) or []
    if not isinstance(raw, list):
        return []
    items = []
    for row in raw:
        if not isinstance(row, dict) or row.get("status") != "done" or not row.get("id") or not row.get("title"):
            continue
        if kids and not row.get("kids"):
            continue
        items.append(_item(
            h, kind="recording", title=str(row.get("title")), subtitle="Recorded",
            reason="recordings", recording_id=str(row["id"]), channel_key=row.get("channel_key"),
            poster=row.get("poster_url"), rank=float(row.get("capture_end") or now),
        ))
    items.sort(key=lambda item: float(item.get("_rank") or 0), reverse=True)
    for item in items:
        item.pop("_rank", None)
    return items


def _starting(h: Any, history: dict[str, Any], kids: bool, now: float) -> list[dict[str, Any]]:
    items = []
    guide = h.tv_catalog.guide(start=now - 3600, end=now + 3600, channel=None, kids_only=kids, now=now) if h.tv_catalog else {"channels": []}
    watched = history.get("channels") or {}
    for channel in guide.get("channels") or []:
        row = watched.get(channel.get("key"))
        if not row or now - float(row.get("last") or 0) > RECENT_S:
            continue
        current = channel.get("now")
        if not current:
            continue
        start = _unix(current.get("start"))
        if start is None or not 0 <= now - start <= STARTING_S:
            continue
        items.append(_item(
            h, kind="live", title=current.get("title") or channel.get("name") or "On now",
            subtitle=channel.get("name"), reason="onnow", channel_key=channel.get("key"),
            poster=current.get("poster_url"), backdrop=current.get("backdrop_url"),
            rank=float(row.get("n") or 0),
        ))
    items.sort(key=lambda item: float(item.get("_rank") or 0), reverse=True)
    for item in items:
        item.pop("_rank", None)
    return items


def _trending(h: Any, history: dict[str, Any], kids: bool, now: float) -> list[dict[str, Any]]:
    items = []
    for key, row in (history.get("channels") or {}).items():
        if now - float(row.get("last") or 0) > RECENT_S:
            continue
        channel = h.tv_catalog.channel(str(key)) if h.tv_catalog else None
        if channel is None or (kids and not channel.get("kids")):
            continue
        items.append(_item(
            h, kind="live", title=channel.get("name") or row.get("title") or "Channel",
            subtitle="You watch this", reason="trending", channel_key=channel["key"],
            rank=float(row.get("n") or 0),
        ))
    for movie_id, row in (history.get("movies") or {}).items():
        movie = h.tv_catalog.movie(str(movie_id)) if h.tv_catalog else None
        if movie is None:
            movie = _plex_movie(h, str(movie_id))
        if movie is None or (kids and not movie.get("kids")):
            continue
        items.append(_item(
            h, kind="movie", title=movie.get("name") or row.get("title") or "Movie",
            subtitle="You watched this", reason="trending", movie_id=movie["id"],
            poster=movie.get("poster_url"), backdrop=movie.get("backdrop_url"),
            rank=float(row.get("n") or 0),
        ))
    items.sort(key=lambda item: float(item.get("_rank") or 0), reverse=True)
    for item in items:
        item.pop("_rank", None)
    return items


def _item(h: Any, *, kind: str, title: str, subtitle: str | None, reason: str, rank: float,
          channel_key: str | None = None, movie_id: str | None = None, series_id: str | None = None,
          episode_id: str | None = None, recording_id: str | None = None,
          poster: str | None = None, backdrop: str | None = None, progress: float | None = None) -> dict[str, Any]:
    shown = _clean_name(title) if kind in ("movie", "episode") else title
    item: dict[str, Any] = {
        "kind": kind, "title": shown, "reason": REASONS[reason], "_rank": rank,
    }
    if subtitle and kind == "episode":
        subtitle = _clean_name(subtitle)
    if subtitle:
        item["subtitle"] = subtitle
    if channel_key:
        item["channel_key"] = channel_key
    if movie_id:
        item["movie_id"] = movie_id
    if series_id:
        item["series_id"] = series_id
    if episode_id:
        item["episode_id"] = episode_id
    if recording_id:
        item["recording_id"] = recording_id
    poster_url = poster or _art(h, "poster", movie_id or series_id or (programme_key(title) if title else None))
    backdrop_url = backdrop or _art(h, "backdrop", movie_id or series_id)
    if poster_url:
        item["poster_url"] = poster_url
    if backdrop_url:
        item["backdrop_url"] = backdrop_url
    if progress is not None:
        item["progress"] = progress
    return item


def _art(h: Any, kind: str, key: str | None) -> str | None:
    artwork = getattr(h, "artwork", None)
    if artwork is None or not key:
        return None
    return artwork.public_url(kind, key)


def _history(h: Any, profile: str) -> dict[str, Any]:
    doc = h.db.kv_get(KV) or {}
    bucket = doc.get(profile) if isinstance(doc, dict) else None
    return bucket if isinstance(bucket, dict) else {}


def _touch(row: dict[str, Any] | None, now: float, **extra: Any) -> dict[str, Any]:
    current = dict(row or {})
    if now - float(current.get("counted") or 0) > COUNT_GAP_S:
        current["n"] = int(current.get("n") or 0) + 1
        current["counted"] = now
    current["last"] = now
    for key, value in extra.items():
        if value is not None:
            current[key] = value
    return current


def _trim(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if len(rows) <= 200:
        return rows
    ranked = sorted(rows.items(), key=lambda kv: float((kv[1] or {}).get("last") or 0), reverse=True)
    return dict(ranked[:200])


def _duration(playing: dict[str, Any]) -> int | None:
    """The player's length of a film or episode, in seconds; None when unknown or not believable."""
    value = playing.get("duration_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(value)
    return seconds if 60 <= seconds <= 24 * 3600 else None


def _position(playing: dict[str, Any]) -> int | None:
    value = playing.get("position_s")
    if value is None:
        value = playing.get("resume")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(value)
    if seconds <= 0 or seconds > 24 * 3600:
        return None
    return seconds


def _progress(position: float, duration: float | None) -> float | None:
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None
    return round(min(0.99, max(0.01, float(position) / float(duration))), 3)


def _left(position: float) -> str:
    minutes = max(1, int(position) // 60)
    return f"{minutes} min in"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "://" in text:
        return None
    return text[:120]


def _unix(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or "T" not in value:
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()
