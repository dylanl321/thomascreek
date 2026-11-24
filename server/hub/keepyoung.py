"""Keeps the slow pages' last answers young, so the first open of the day is as quick as the second.

The kept answers (NAS overview, download pipeline, forecast, each person's calendar) made a repeat open quick,
but the first open after a quiet spell still waited one to four seconds for the services behind them (Nov 24
2026, timed on the live hub). This reads them every two minutes whether or not anyone is looking; the pages
then always find an answer to show.
A read that fails is skipped: the page itself says why when someone opens it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from . import kept
from .policy import Subject

log = logging.getLogger("hub.keepyoung")

FIRST_S = 20
EVERY_S = 120
CALENDAR_DAYS = 7  # what the house app asks for


class KeepYoung:
    def __init__(self, hub: Any):
        self.hub = hub
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_S)
        while True:
            await self.once()
            await asyncio.sleep(EVERY_S)

    def reads(self) -> dict[str, Callable[[], Awaitable[Any]]]:
        h = self.hub
        out: dict[str, Callable[[], Awaitable[Any]]] = {}
        if h.config.truenas.enabled:
            out["nas"] = h.truenas.overview
        out["pipeline"] = self._pipeline
        out["calendar"] = self._calendars
        out["forecast"] = lambda: h.forecasts.page(Subject(person_id=None, role="admin"), "home")
        return out

    async def _pipeline(self) -> Any:
        from .routes.media import _pipeline

        return await kept.answer(self.hub, "pipeline", 10, 300, lambda: _pipeline(self.hub))

    async def _calendars(self) -> None:
        """The week the Family tab opens with, for each person who may see it. One person's failure is theirs."""
        from .routes.deps import Caller
        from .routes.family import calendar_for

        h = self.hub
        for person in h.db.people():
            subject = h.subject_for_person(person)
            if not h.policy.has(subject, "family.view"):
                continue
            try:
                await calendar_for(h, Caller(subject=subject, device={}, ip=None), CALENDAR_DAYS)
            except Exception as exc:  # noqa: BLE001
                log.debug("keep young: calendar for one person failed: %s", type(exc).__name__)

    async def once(self) -> dict[str, bool]:
        reads = self.reads()
        got = await asyncio.gather(*(make() for make in reads.values()), return_exceptions=True)
        done: dict[str, bool] = {}
        for name, value in zip(reads, got, strict=True):
            done[name] = not isinstance(value, BaseException)
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                log.debug("keep young: %s failed: %s", name, type(value).__name__)
        return done
