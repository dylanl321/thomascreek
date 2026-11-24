"""The last answer of a slow page, shown at once while a new one is read behind it.

Pages that ask services outside the hub (the download pipeline, the family calendar) took one to two seconds on
every open (Nov 24 2026, the house app walk). The hub keeps the last answer per key: young, it is the answer;
older, it is still the answer and a new one is read behind it; past `keep_s` the caller waits for a new one.
Only for reads that are the same for every caller of the key, and never for anything a child must not see
through another person's key.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Awaitable, Callable

MAX_KEYS = 200


def _store(h: Any) -> dict[str, Any]:
    store = h.__dict__.get("_kept_answers")
    if store is None:
        store = h.__dict__["_kept_answers"] = {"answers": {}, "behind": {}}
    return store  # type: ignore[no-any-return]


async def answer(h: Any, key: str, fresh_s: float, keep_s: float, make: Callable[[], Awaitable[Any]]) -> Any:
    store = _store(h)
    answers: dict[str, tuple[float, Any]] = store["answers"]
    behind: dict[str, asyncio.Task[None]] = store["behind"]
    now = time.time()
    hit = answers.get(key)
    if hit and now - hit[0] < keep_s:
        if now - hit[0] >= fresh_s and key not in behind:
            behind[key] = asyncio.create_task(_behind(answers, behind, key, make))
        return copy.deepcopy(hit[1])
    value = await make()
    _put(answers, key, value)
    return copy.deepcopy(value)


def _put(answers: dict[str, tuple[float, Any]], key: str, value: Any) -> None:
    answers[key] = (time.time(), value)
    if len(answers) > MAX_KEYS:
        for old in sorted(answers, key=lambda k: answers[k][0])[:MAX_KEYS // 4]:
            answers.pop(old, None)


async def _behind(answers: dict[str, tuple[float, Any]], behind: dict[str, "asyncio.Task[None]"], key: str,
                  make: Callable[[], Awaitable[Any]]) -> None:
    try:
        _put(answers, key, await make())
    except Exception:  # noqa: BLE001  the kept answer stays until it ages out; the next wait shows the reason
        pass
    finally:
        behind.pop(key, None)


def forget(h: Any, prefix: str) -> None:
    answers = _store(h)["answers"]
    for key in [k for k in answers if k.startswith(prefix)]:
        answers.pop(key, None)
