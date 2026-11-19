"""Where an episode's recap, intro and credits are, for the player's Skip button.

The hub asks TheIntroDB once per episode (by the series' TMDB id, season and episode number) and keeps the
answer, so no screen calls an outside service (rule 2) and every screen shows the same button. Nothing known, no
TMDB id or the service down all give an empty list: the player just shows no button.

Only plausible sections are kept (the same rules the Android TV used on its own): a recap or intro needs an end,
lasts 5 seconds to 3 or 4 minutes and sits early in the episode; credits start in its last quarter."""

from __future__ import annotations

import time
from typing import Any

import httpx

API = "https://api.theintrodb.org/v3/media"
KV = "player.segments"
KEEP_FOUND_S = 30 * 86400
KEEP_EMPTY_S = 86400          # ask again tomorrow: new episodes get their times within days
KEEP_FAILED_S = 600
MAX_KEPT = 3000
KINDS = ("recap", "intro", "credits")


def clean(kind: str, start_ms: object, end_ms: object, duration_ms: int) -> tuple[int, int] | None:
    """One section in whole milliseconds, or None when it can't be right."""
    def ms(value: object) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

    start, end = ms(start_ms), ms(end_ms)
    limit = duration_ms if duration_ms > 0 else 10**12
    if kind in ("intro", "recap"):
        if end is None:
            return None
        start = start or 0
        length = end - start
        if length < 5_000 or length > (180_000 if kind == "intro" else 240_000) or end > limit + 5_000:
            return None
        if duration_ms > 0 and start > duration_ms * (0.5 if kind == "intro" else 0.35):
            return None
        return start, min(end, limit)
    if kind == "credits":
        if start is None:
            return None
        end = limit if end is None else end
        if end - start < 5_000 or (duration_ms > 0 and (start < duration_ms * 0.75 or start >= duration_ms)):
            return None
        if duration_ms <= 0 and end >= 10**12:
            return None   # credits without an end need the length to mean anything
        return start, min(end, limit)
    return None


def parse(body: object, duration_ms: int) -> list[dict[str, Any]]:
    """TheIntroDB's `{"intro":[{"start_ms","end_ms"}],"recap":[…],"credits":[…]}` as our sections, in play order."""
    out: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return out
    for kind in KINDS:
        rows = body.get(kind)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            kept = clean(kind, row.get("start_ms"), row.get("end_ms"), duration_ms)
            if kept:
                out.append({"kind": kind, "start_s": round(kept[0] / 1000, 1), "end_s": round(kept[1] / 1000, 1)})
    out.sort(key=lambda s: s["start_s"])
    return out[:6]


def _cached(h: Any, key: str, now: float) -> list[dict[str, Any]] | None:
    doc = h.db.kv_get(KV)
    row = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(row, dict) or not isinstance(row.get("segments"), list):
        return None
    return row["segments"] if float(row.get("until") or 0) > now else None


def _keep(h: Any, key: str, segments: list[dict[str, Any]], keep_s: int, now: float) -> None:
    doc = h.db.kv_get(KV)
    doc = doc if isinstance(doc, dict) else {}
    doc[key] = {"segments": segments, "until": now + keep_s}
    if len(doc) > MAX_KEPT:
        for old in sorted(doc, key=lambda k: float((doc[k] or {}).get("until") or 0))[:len(doc) - MAX_KEPT]:
            doc.pop(old, None)
    h.db.kv_set(KV, doc)


async def segments(h: Any, *, tmdb: str | None, season: int | None, number: int | None, duration_s: int | None,
                   client: httpx.AsyncClient, now: float | None = None) -> list[dict[str, Any]]:
    """The sections of one episode (or of a film, when `season` and `number` are None). Never raises."""
    import asyncio

    if not tmdb or not str(tmdb).isdigit():
        return []
    episode = bool(season and number and season > 0 and number > 0)
    duration_ms = int(duration_s * 1000) if duration_s and duration_s > 0 else 0
    # The length is part of the key in minutes: two cuts of an episode have their sections in different places.
    key = f"{'tv' if episode else 'movie'}:{tmdb}:{season or 0}:{number or 0}:{duration_ms // 60000}"
    now = time.time() if now is None else now
    cached = await asyncio.to_thread(_cached, h, key, now)
    if cached is not None:
        return cached
    params: dict[str, Any] = {"tmdb_id": tmdb}
    if episode:
        params.update(season=season, episode=number)
    if duration_ms:
        params["duration_ms"] = duration_ms
    found: list[dict[str, Any]] = []
    keep = KEEP_FAILED_S
    try:
        answer = await client.get(API, params=params, timeout=4)
        if answer.status_code == 200:
            found = parse(answer.json(), duration_ms)
            keep = KEEP_FOUND_S if found else KEEP_EMPTY_S
        elif answer.status_code == 404:
            keep = KEEP_EMPTY_S
    except (httpx.HTTPError, ValueError):
        pass
    await asyncio.to_thread(_keep, h, key, found, keep, now)
    return found
