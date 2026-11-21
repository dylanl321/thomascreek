"""Warm the hub before it takes calls.

Right after a restart the first TV start-up set (lineup, names, copies, Plex library) parsed the catalog and the
folded titles on demand, all at once, and two TV calls took 25 and 27 s (Nov 21 2026 review). The lifespan now
reads those from disk in a worker before the server opens its port, so the first caller gets what the hundredth
does. Bounded: past `WARM_S` the hub opens anyway and the rest finishes behind it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

WARM_S = 30.0


async def before_serving(h: Any, limit: float = WARM_S) -> None:
    started = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.to_thread(_from_disk, h), limit)
    except TimeoutError:
        log.info("warm: still reading after %.0f s; serving, the rest finishes behind", limit)
        return
    except Exception:  # noqa: BLE001 - a failed warm only means the first caller builds
        log.exception("warm: failed; the first callers build instead")
        return
    log.info("warm: ready in %.1f s", time.monotonic() - started)


async def after_serving(h: Any) -> None:
    """Behind the open port: the basic lineup for the house and the sport channels, which need the house's place
    and ESPN's boards. The first /v1/tv/basic and /v1/tv/sport after a restart took 6.3 and 9 s (Nov 21 2026)."""
    started = time.monotonic()
    cat = getattr(h, "tv_catalog", None)
    if cat is None:
        return
    try:
        home = await h.smart.weather._location()
    except Exception:  # noqa: BLE001 - the lineup without a home market, as the route does
        home = None
    try:
        await asyncio.to_thread(cat.basic, home)
    except Exception as exc:  # noqa: BLE001
        log.info("warm: basic lineup failed (%s)", type(exc).__name__)
    try:
        from . import tvsport
        from .routes.tvguide import _sport_channels

        feed = getattr(h, "_sport", None)
        if feed is None:
            feed = h._sport = tvsport.Sport(h)
        now = time.time()
        games = await feed.games(now)
        for kids in (False, True):
            await asyncio.to_thread(_sport_channels, h, games, home, kids, now)
    except Exception as exc:  # noqa: BLE001
        log.info("warm: sport failed (%s)", type(exc).__name__)
    log.info("warm: lineup and sport ready %.1f s after the port opened", time.monotonic() - started)


async def after_refresh(h: Any) -> None:
    """A new snapshot was read: build the answers made from it again (names, lineup, copies, basic, sport) before a
    TV asks, one step at a time in a worker."""
    started = time.monotonic()
    try:
        await asyncio.to_thread(_from_disk, h, False)
    except Exception:  # noqa: BLE001
        log.exception("warm: rebuilding after a refresh failed; the first callers build instead")
    await after_serving(h)
    log.info("warm: rebuilt after a refresh in %.1f s", time.monotonic() - started)


def _from_disk(h: Any, plex: bool = True) -> None:
    steps = []
    cat = getattr(h, "tv_catalog", None)
    if cat is not None:
        steps.append(("catalog", cat.load))
        from .routes.tvguide import names_reply

        steps += [("names", lambda: names_reply(h, False)), ("kids names", lambda: names_reply(h, True))]
        from .routes.tvapp import _lineup_reply

        steps += [("lineup", lambda: _lineup_reply(h, False)), ("kids lineup", lambda: _lineup_reply(h, True))]
    db = getattr(h, "db", None)
    if db is not None:
        from .iptvcanon import copies

        steps.append(("copies", lambda: copies(db)))
    library = getattr(h, "plex_library", None) if plex else None
    if library is not None:
        steps.append(("plex", library._read_saved))
    for name, step in steps:
        began = time.monotonic()
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - one step failing leaves the others warm
            log.info("warm: %s failed (%s)", name, type(exc).__name__)
            continue
        took = time.monotonic() - began
        if took > 0.5:
            log.info("warm: %s took %.1f s", name, took)
