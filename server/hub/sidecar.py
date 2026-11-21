"""Heavy matching work in a helper process that stays up (hub rebuild, Nov 21 2026).

The hub answers every TV and phone from one Python process. Work that holds the interpreter lock for seconds,
even in a worker thread, makes every other call wait its turn, though the NAS has cores to spare: on the live
hub a TV start during the sport channel match took 3 to 13 s per call (ffea419, /v1/ping/stalls showed the
`sport-match` thread busy behind each stall).

This module keeps one helper process (`spawn`, lower CPU priority) with its own copy of the TV catalog, read
from the same snapshot file and database the hub writes. The hub sends it small questions (the day's games,
the house's place) and gets small answers back (each game with its channels), so the matching runs on another
core. The helper re-reads the snapshot when the file changes, as the hub does.

`hub serve` turns it on (HUB_SIDECAR=1). Tests and other callers keep the in-process path. Any failure in the
helper falls back to the in-process path for that call, and the helper is started again on the next one.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

log = logging.getLogger("hub.sidecar")

ENV = "HUB_SIDECAR"
NICE = 5
TIMEOUT_S = 120.0  # the first call after a start parses the snapshot (tens of MB) in the helper

_LOCK = threading.Lock()
_POOL: ProcessPoolExecutor | None = None
_CHILD: dict[str, Any] = {}
# Since the hub started (counts and the slowest answer only), read on /v1/ping/timings `sidecar`.
TALLY: dict[str, Any] = {"ok": 0, "failed": 0, "restarts": 0, "max_ms": 0.0}


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def _pool() -> ProcessPoolExecutor:
    global _POOL
    with _LOCK:
        if _POOL is None:
            import multiprocessing

            _POOL = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_init)
        return _POOL


def _reset() -> None:
    global _POOL
    with _LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def call(job: str, *args: Any, timeout: float = TIMEOUT_S) -> Any:
    """Run `job` in the helper and return its answer. Raises on any failure; the caller falls back."""
    import time

    began = time.perf_counter()
    try:
        answer = _pool().submit(_run, job, args).result(timeout)
    except BrokenProcessPool:
        TALLY["failed"] += 1
        TALLY["restarts"] += 1
        log.warning("sidecar: the helper stopped; starting it again on the next call")
        _reset()
        raise
    except Exception:
        TALLY["failed"] += 1
        raise
    TALLY["ok"] += 1
    TALLY["max_ms"] = max(TALLY["max_ms"], round((time.perf_counter() - began) * 1000, 1))
    return answer


def tally() -> dict[str, Any]:
    return {"on": enabled(), "running": _POOL is not None, **TALLY}


def shutdown() -> None:
    _reset()


def sport_match(games: list[dict[str, Any]], home: Any, kids: bool, now: float) -> list[dict[str, Any]]:
    """`tvsport.match` against the house's basic lineup, in the helper."""
    result: list[dict[str, Any]] = call("sport", games, home, kids, now)
    return result


# -- in the helper ---------------------------------------------------------------------------------------------
def _init() -> None:
    from .config import data_dir, load_config
    from .db import DB
    from .tvapp_common import KV_KIDS
    from .tvcatalog import TvCatalog

    logging.basicConfig(level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs full addresses, query strings included
    try:
        os.nice(NICE)
    except OSError:
        pass
    load_config()
    data = data_dir()
    db = DB(data / "hub.db")
    # Matching reads the snapshot and the lineup only: the helper never holds a key or a provider login.
    catalog = TvCatalog(_NoSecrets(), db, data)
    catalog.kids_categories = lambda: list(db.kv_get(KV_KIDS) or [])
    catalog.__dict__["artwork"] = _Art()  # the art addresses on each channel, as the hub's own Artwork writes them
    _CHILD["catalog"] = catalog


class _Art:
    def public_url(self, kind: str, key: str) -> str | None:
        from .artwork import Artwork

        return Artwork.public_url(self, kind, key)  # type: ignore[arg-type]  # it reads no state


class _NoSecrets:
    def has(self, name: str) -> bool:
        return False

    def get(self, name: str) -> None:
        return None


def _run(job: str, args: tuple[Any, ...]) -> Any:
    if job == "sport":
        return _sport(*args)
    raise ValueError(f"unknown sidecar job {job}")


def _sport(games: list[dict[str, Any]], home: Any, kids: bool, now: float) -> list[dict[str, Any]]:
    from . import tvsport

    catalog = _CHILD["catalog"]
    return tvsport.match(games, catalog, catalog.basic(home), kids_only=kids, now=now)
