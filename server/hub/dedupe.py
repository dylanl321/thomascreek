"""Run each app request once, however many times it arrives.

The app gives every action an id (X-TC-Request-Id). If the same phone sends that id again within the window
(a retry after a dropped connection, or the same request reaching the hub by both the home and the published
address), the second copy waits for the first one's result instead of running the action again.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable

REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{8,64}")


class RequestDedupe:
    def __init__(self, ttl_s: float = 30.0):
        self.ttl_s = ttl_s
        self._seen: dict[tuple[str, str], tuple[float, asyncio.Future]] = {}

    async def run(self, device_id: str | None, request_id: str | None,
                  make: Callable[[], Awaitable[Any]]) -> Any:
        if not request_id or not REQUEST_ID.fullmatch(request_id):
            return await make()
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._seen.items() if exp < now]:
            self._seen.pop(k, None)
        key = (device_id or "", request_id)
        hit = self._seen.get(key)
        if hit is None:
            # Its own task, so a phone that hangs up mid-call doesn't cancel the action half-done.
            fut = asyncio.ensure_future(make())
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # never "exception was never retrieved"
            self._seen[key] = (now + self.ttl_s, fut)
        else:
            fut = hit[1]
        return await asyncio.shield(fut)

    def __len__(self) -> int:
        return len(self._seen)
