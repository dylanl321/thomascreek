"""Live updates over Server-Sent Events.

Each connected app has a queue and a filter; the filter decides, per event,
whether that person may see it (a child never receives a camera or garage
event, for instance).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Subscriber:
    queue: asyncio.Queue
    accept: Callable[[dict[str, Any]], dict[str, Any] | None]
    person_id: str | None


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()  # type: ignore[arg-type]

    def subscribe(self, accept: Callable[[dict[str, Any]], dict[str, Any] | None], person_id: str | None) -> Subscriber:
        sub = Subscriber(asyncio.Queue(maxsize=500), accept, person_id)
        self._subs.add(sub)  # type: ignore[arg-type]
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)  # type: ignore[arg-type]

    def publish(self, event: dict[str, Any], person_id: str | None = None) -> None:
        """Send to every subscriber whose filter accepts it (or only to one person)."""
        for sub in list(self._subs):
            if person_id is not None and sub.person_id != person_id:
                continue
            shaped = sub.accept(event)
            if shaped is None:
                continue
            try:
                sub.queue.put_nowait(shaped)
            except asyncio.QueueFull:
                # A stalled client: tell it to re-read the snapshot.
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait({"type": "snapshot", "data": {}})

    @property
    def count(self) -> int:
        return len(self._subs)


def sse(event: dict[str, Any]) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


# Make Subscriber hashable by identity.
Subscriber.__hash__ = object.__hash__  # type: ignore[assignment]
Subscriber.__eq__ = object.__eq__  # type: ignore[assignment]
