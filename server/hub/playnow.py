"""Can this title play right now, and from where? Asked by a details page before Play, without taking a login
(Nov 19 2026: a press on Play was the first time anyone learnt that every login carrying a title was busy)."""

from __future__ import annotations

import time
from typing import Any

from . import iptvplay as play


def availability(h: Any, ref: str, holder: str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """{"state": "ready" | "busy", "copies": n, "providers": [names], "reason": words when busy}.

    The same choice /play makes (every copy, every login, the provider's counts, failed copies skipped), so
    "ready" means Play will get a login unless someone else presses first. A login this holder already has counts
    as free for it."""
    now = time.time() if now is None else now
    _kind, provider, _stream = play._split_ref(ref)
    item = play._holding(h, ref) or play._episode(h, ref) or {
        "id": ref, "sources": [{"provider": provider, "ref": ref, "quality": None}]}
    mine = {play._key(row) for row in play._sessions(h) if holder and row.get("tv") == holder}
    sources = item.get("sources") or []
    names = {lg["provider"]: lg.get("name") or lg["provider"] for lg in play.logins(h)}
    names.update(play.provider_names(h))
    providers = sorted({str(names.get(src.get("provider")) or src.get("provider"))
                        for src in sources if src.get("provider")})
    carried = {str(src.get("provider")) for src in sources}
    # Play first gives back this holder's own lease, so a login it holds for a provider of this title is free for it.
    own = bool(holder) and any(row.get("tv") == holder and row.get("provider") in carried
                               for row in play._sessions(h))
    chosen = own or play._pick(h, item, play._failed(h, now), now, mine=mine)
    out: dict[str, Any] = {"state": "ready" if chosen else "busy", "copies": len(sources), "providers": providers}
    if chosen is None:
        out["reason"] = play.busy_reason(h, ref)
    return out
