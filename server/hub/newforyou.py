"""New for you: a title someone asked for that has arrived, or a new episode of a series this person watches.

Home shows these as a card (smart.r_new_for_you) with one tap to play here or on a TV. Everything is decided
here from the person's own watch history (upnext), so a card goes away by itself once the title is started.

"New episode" means an episode the hub first saw after this person last played the series, not just the next
one in line: the hub notes when each episode id first appeared (KV `newforyou.episodes`). A series seen for the
first time is noted as old, so a deploy or a newly opened title page doesn't flood Home with cards.
"""

from __future__ import annotations

import time
from typing import Any

from . import upnext
from .tvexplore import ready_items

KV_EPISODES = "newforyou.episodes"   # {"series": {series_id: {episode_id: first_seen (0 = known before)}}}
KV_ARRIVED = "newforyou.arrived"     # {"<profile>:<key>": first_seen}
EPISODE_WINDOW_S = 7 * 86400
ARRIVED_WINDOW_S = 3 * 86400
MAX_SERIES = 400


def observe(h: Any, now: float | None = None) -> dict[str, dict[str, float]]:
    """Note every episode id the hub knows (provider series pages and Plex shows) with when it first appeared."""
    now = time.time() if now is None else now
    raw = h.db.kv_get(KV_EPISODES) or {}
    known: dict[str, dict[str, float]] = raw.get("series") if isinstance(raw, dict) and isinstance(raw.get("series"), dict) else {}
    changed = False
    for series_id, ids in _episodes(h).items():
        old = known.get(series_id)
        if old is None:
            known[series_id] = {e: 0.0 for e in ids}
            changed = True
            continue
        for e in ids:
            if e not in old:
                old[e] = now
                changed = True
    if changed:
        if len(known) > MAX_SERIES:
            newest = sorted(known.items(), key=lambda kv: max(kv[1].values(), default=0.0), reverse=True)
            known = dict(newest[:MAX_SERIES])
        h.db.kv_set(KV_EPISODES, {"series": known})
    return known


def _episodes(h: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    catalog = getattr(h, "tv_catalog", None)
    if catalog is not None:
        for ep in (catalog._details().get("episodes") or {}).values():
            if isinstance(ep, dict) and ep.get("series_id") and ep.get("id"):
                out.setdefault(str(ep["series_id"]), set()).add(str(ep["id"]))
    for show_id, show in (upnext._plex_doc(h).get("shows") or {}).items():
        if isinstance(show, dict):
            ids = {str(e["id"]) for e in show.get("episodes") or [] if isinstance(e, dict) and e.get("id")}
            if ids:
                out.setdefault(f"series:plex:{show_id}", set()).update(ids)
    return out


def items(h: Any, person_id: str | None, kids: bool, now: float | None = None) -> list[dict[str, Any]]:
    """What is new for this person, newest first. Empty for a caller without a person (nothing is "for you")."""
    if not person_id:
        return []
    now = time.time() if now is None else now
    history = upnext._history(h, upnext.who_key(person=person_id) or "")
    out = _new_episodes(h, history, kids, now) + _arrived(h, history, kids, now)
    out.sort(key=lambda i: -float(i["at"]))
    return out


def _new_episodes(h: Any, history: dict[str, Any], kids: bool, now: float) -> list[dict[str, Any]]:
    series = history.get("series") or {}
    if not series:
        return []
    known = observe(h, now)
    last = _last_play(h, series)
    out = []
    for item in upnext._next_episodes(h, history, kids):
        if item.get("reason") != upnext.REASONS["next"]:
            continue  # a shuffle's next pick isn't news
        sid, eid = str(item.get("series_id") or ""), str(item.get("episode_id") or "")
        seen = (known.get(sid) or {}).get(eid)
        if not seen or now - seen > EPISODE_WINDOW_S or seen <= last.get(sid, 0.0):
            continue
        out.append({**item, "at": seen, "news": "episode",
                    "key": f"ep:{eid}", "why": "A new episode of a series you watch"})
    return out


def _last_play(h: Any, series: dict[str, Any]) -> dict[str, float]:
    """When this person last played each series, under the id its next episode carries (Plex shows included)."""
    doc = upnext._plex_doc(h)
    out: dict[str, float] = {}
    for key, row in series.items():
        if not isinstance(row, dict):
            continue
        at = float(row.get("last") or 0)
        ids = {str(key)}
        current = str(row.get("episode_id") or "")
        if current.isdigit():
            show = upnext._plex_show_of(doc, current, str(key))
            if show:
                ids.add(f"series:plex:{show}")
        for sid in ids:
            out[sid] = max(out.get(sid, 0.0), at)
    return out


def _arrived(h: Any, history: dict[str, Any], kids: bool, now: float) -> list[dict[str, Any]]:
    profile = "kids" if kids else "adult"
    rows = ready_items(h, profile)
    if not rows:
        return []
    seen = h.db.kv_get(KV_ARRIVED) or {}
    seen = seen if isinstance(seen, dict) else {}
    changed = False
    out = []
    for row in rows:
        key = row.get("movie_id") or row.get("series_id")
        if not key:
            continue
        mark = f"{profile}:{key}"
        first = seen.get(mark)
        if first is None:
            seen[mark] = first = now
            changed = True
        if now - float(first) > ARRIVED_WINDOW_S or _started(history, row):
            continue
        out.append({**row, "at": float(first), "news": "arrived", "key": f"in:{key}",
                    "why": "Something the house asked for is ready to watch"})
    if changed:
        h.db.kv_set(KV_ARRIVED, {k: v for k, v in seen.items() if now - float(v) < 14 * 86400})
    return out


def _started(history: dict[str, Any], row: dict[str, Any]) -> bool:
    if row.get("movie_id") and str(row["movie_id"]) in (history.get("movies") or {}):
        return True
    return bool(row.get("series_id")) and str(row["series_id"]) in (history.get("series") or {})
