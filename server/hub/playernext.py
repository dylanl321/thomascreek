"""What plays after an episode: the hub's answer for the player's "Next episode" card.

The order is the series' own (season, then episode number). Specials (season 0) are not offered after a numbered
season, and a kids-only caller is only offered a kids' episode. Screens draw `next`; they do not work it out."""

from __future__ import annotations

from typing import Any


def _order(ep: dict[str, Any]) -> tuple[int, int]:
    def whole(value: object) -> int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 10**6
    return whole(ep.get("season")), whole(ep.get("number"))


def next_episode(cat: Any, episode_id: str, *, kids_only: bool = False) -> dict[str, Any] | None:
    """`{episode_id, series_id, title, season?, number?, still_url?}` for the episode after `episode_id`, or None
    at the end of the series (or when the series was never read)."""
    episodes = cat._details().get("episodes") or {}
    current = episodes.get(episode_id)
    if not isinstance(current, dict) or not current.get("series_id"):
        return None
    series_id = current["series_id"]
    here = _order(current)
    later = [ep for ep in episodes.values()
             if isinstance(ep, dict) and ep.get("series_id") == series_id and ep.get("id")
             and _order(ep) > here and (ep.get("season") != 0 or current.get("season") == 0)]
    if not later:
        return None
    nxt = min(later, key=_order)
    if kids_only and not (cat.episode(str(nxt["id"])) or {}).get("kids"):
        return None
    out: dict[str, Any] = {"episode_id": nxt["id"], "series_id": series_id,
                           "title": nxt.get("title") or "Next episode"}
    for field in ("season", "number"):
        if isinstance(nxt.get(field), int):
            out[field] = nxt[field]
    still = cat._art("still", str(nxt["id"]))
    if still:
        out["still_url"] = still
    return out
