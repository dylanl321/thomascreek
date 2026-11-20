"""What plays after an episode: the hub's answer for the player's "Next episode" card.

The order is the series' own (season, then episode number). Specials (season 0) are not offered after a numbered
season, and a kids-only caller is only offered a kids' episode. Screens draw `next`; they do not work it out."""

from __future__ import annotations

import re
import threading
from typing import Any

# Episodes by series, built once per version of the catalog's episode pages (the details file's stamp, which every
# write changes; writers change the dict in place). Finding the next one walked every episode the hub ever read,
# on each episode play (Nov 20 2026).
_BY_SERIES: dict[str, Any] = {"mark": None, "map": {}}
_LOCK = threading.Lock()


def _series_episodes(cat: Any, episodes: dict[str, Any], series_id: str) -> list[dict[str, Any]]:
    loaded = getattr(cat, "__dict__", {}).get("_details_loaded")
    mark = (id(episodes), len(episodes), loaded[0] if isinstance(loaded, tuple) and loaded else None)
    with _LOCK:
        if _BY_SERIES["mark"] != mark or mark[2] is None:
            by: dict[str, list[dict[str, Any]]] = {}
            for ep in episodes.values():
                if isinstance(ep, dict) and ep.get("series_id") and ep.get("id"):
                    by.setdefault(str(ep["series_id"]), []).append(ep)
            _BY_SERIES["mark"], _BY_SERIES["map"] = mark, by
        return list(_BY_SERIES["map"].get(str(series_id)) or [])


def _order(ep: dict[str, Any]) -> tuple[int, int]:
    def whole(value: object) -> int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 10**6
    return whole(ep.get("season")), whole(ep.get("number"))


_MARK = re.compile(r"\bS\d{1,2}\s*[.\-]?\s*E\d{1,4}\b[\s\-\u2013:.)\]]*", re.I)


def episode_name(raw: object, number: object = None) -> str:
    """An episode's own name from what the provider calls it: "EN - Show (2009) - S01E02 - Second" is "Second".
    A name with no episode mark is cleaned like a title; one with nothing after the mark is "Episode 2"."""
    from .tvnames import title

    text = str(raw or "").strip()
    mark = _MARK.search(text)
    if mark:
        rest = text[mark.end():].strip()
        if rest:
            return rest[:120]
        return f"Episode {number}" if isinstance(number, int) else "Episode"
    return str(title(text).get("name") or text or "Episode")[:120]


def next_episode(cat: Any, episode_id: str, *, kids_only: bool = False) -> dict[str, Any] | None:
    """`{episode_id, series_id, title, season?, number?, still_url?}` for the episode after `episode_id`, or None
    at the end of the series (or when the series was never read)."""
    episodes = cat._details().get("episodes") or {}
    current = episodes.get(episode_id)
    if not isinstance(current, dict) or not current.get("series_id"):
        return None
    series_id = current["series_id"]
    here = _order(current)
    later = [ep for ep in _series_episodes(cat, episodes, series_id)
             if _order(ep) > here and (ep.get("season") != 0 or current.get("season") == 0)]
    if not later:
        return None
    nxt = min(later, key=_order)
    if kids_only and not (cat.episode(str(nxt["id"])) or {}).get("kids"):
        return None
    out: dict[str, Any] = {"episode_id": nxt["id"], "series_id": series_id,
                           "title": episode_name(nxt.get("title"), nxt.get("number"))}
    for field in ("season", "number"):
        if isinstance(nxt.get(field), int):
            out[field] = nxt[field]
    still = cat._art("still", str(nxt["id"]))
    if still:
        out["still_url"] = still
    return out
