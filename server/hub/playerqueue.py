""""Play next": a person's own queue of films and episodes, kept on the hub so it follows them to every screen.

The player's Up next tray shows it after the hub's next episode; a title leaves the queue when it starts. The
list is ids only; what a screen shows for each is built when it is read, so a title that left the library or
that a child may not see simply isn't there."""

from __future__ import annotations

import threading
from typing import Any, Callable

KV = "player.queue"
MAX_ITEMS = 50
MAX_PEOPLE = 200
_LOCK = threading.Lock()


def _valid(item_id: object) -> bool:
    return isinstance(item_id, str) and 4 <= len(item_id) <= 120 and item_id.startswith(("vod:", "ep:"))


def ids(h: Any, who: str) -> list[str]:
    doc = h.db.kv_get(KV)
    row = doc.get(who) if isinstance(doc, dict) else None
    return [i for i in row if _valid(i)] if isinstance(row, list) else []


def change(h: Any, who: str, *, add: str | None = None, first: bool = False, remove: str | None = None,
           order: list[str] | None = None, clear: bool = False) -> list[str]:
    """One change to `who`'s queue; answers the new list. `add` moves a title already queued; `order` keeps only
    ids that are queued (it reorders, it does not add)."""
    with _LOCK:
        doc = h.db.kv_get(KV)
        doc = doc if isinstance(doc, dict) else {}
        current = [i for i in doc.get(who) or [] if _valid(i)] if isinstance(doc.get(who), list) else []
        if clear:
            current = []
        if order is not None:
            wanted = [i for i in dict.fromkeys(order) if i in current]
            current = wanted + [i for i in current if i not in wanted]
        if remove:
            current = [i for i in current if i != remove]
        if add and _valid(add):
            current = [i for i in current if i != add]
            current = [add, *current] if first else [*current, add]
        current = current[:MAX_ITEMS]
        if current:
            doc.pop(who, None)
            doc[who] = current                # the most recently changed queue is last
            for old in list(doc)[:-MAX_PEOPLE]:
                doc.pop(old, None)
        else:
            doc.pop(who, None)
        h.db.kv_set(KV, doc)
        return current


def present(cat: Any, item_ids: list[str], allowed: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """What the tray draws for each queued id, leaving out what is gone or not `allowed`."""
    from .episodenames import better
    from .playernext import episode_name
    from .tvnames import title

    out: list[dict[str, Any]] = []
    for item_id in item_ids:
        if item_id.startswith("ep:"):
            row = cat.episode(item_id)
            if not row or not allowed(row):
                continue
            item: dict[str, Any] = {"id": item_id, "kind": "episode", "series_id": row.get("series_id"),
                                    "title": better(cat, row, episode_name(row.get("title"), row.get("number")))}
            numbers = [row.get("season"), row.get("number")]
            if all(isinstance(n, int) for n in numbers):
                item["season"], item["number"] = numbers
            show = title(row.get("series_name")).get("name")
            if show:
                item["subtitle"] = show
            # An episode the provider sent no picture for shows its series' poster.
            art = cat._art("still", item_id) or cat._art("poster", str(row.get("series_id") or "") or None)
        else:
            row = cat.movie(item_id)
            if not row or not allowed(row):
                continue
            raw = row.get("name") or row.get("title")
            item = {"id": item_id, "kind": "movie", "title": title(raw).get("name") or raw or "Film"}
            if row.get("year"):
                item["subtitle"] = str(row["year"])[:4]
            art = cat._art("poster", item_id)
        if art:
            item["image_url"] = art
        if isinstance(row.get("duration_s"), (int, float)) and row["duration_s"] > 0:
            item["duration_s"] = int(row["duration_s"])
        out.append(item)
    return out
