"""Slow pages show their last answer at once and read a new one behind it (hub/kept.py, the NAS page, the
forecast), found slow in the house app walk of Nov 24 2026."""

from __future__ import annotations

import asyncio
import time
import types

from hub import kept
from hub.forecast import Forecasts, TTL


def test_a_kept_answer_is_shown_at_once_and_read_again_behind_it():
    async def run():
        h = types.SimpleNamespace()
        made = []

        async def make():
            made.append(1)
            return {"n": len(made)}

        first = await kept.answer(h, "page", 10, 300, make)
        first["n"] = 99  # a caller's copy is its own
        assert await kept.answer(h, "page", 10, 300, make) == {"n": 1} and len(made) == 1
        store = h._kept_answers["answers"]
        store["page"] = (time.time() - 60, store["page"][1])  # no longer young
        assert await kept.answer(h, "page", 10, 300, make) == {"n": 1}  # shown at once
        await asyncio.sleep(0.01)
        assert len(made) == 2 and await kept.answer(h, "page", 10, 300, make) == {"n": 2}
        store["page"] = (time.time() - 301, store["page"][1])  # too old to show
        assert await kept.answer(h, "page", 10, 300, make) == {"n": 3}
        kept.forget(h, "pa")
        assert await kept.answer(h, "page", 10, 300, make) == {"n": 4}

    asyncio.run(run())


def test_a_failed_read_behind_keeps_the_old_answer_and_a_failed_wait_says_so():
    async def run():
        h = types.SimpleNamespace()
        state = {"fail": False}

        async def make():
            if state["fail"]:
                raise RuntimeError("down")
            return "ok"

        assert await kept.answer(h, "k", 0, 300, make) == "ok"
        state["fail"] = True
        assert await kept.answer(h, "k", 0, 300, make) == "ok"
        await asyncio.sleep(0.01)
        assert not h._kept_answers["behind"] and h._kept_answers["answers"]["k"][1] == "ok"
        kept.forget(h, "k")
        try:
            await kept.answer(h, "k", 0, 300, make)
        except RuntimeError:
            return
        raise AssertionError("a caller with nothing kept must see the failure")

    asyncio.run(run())


def test_a_forecast_that_aged_out_is_shown_while_a_new_one_is_read_but_never_old_alerts(tmp_path):
    async def run():
        f = Forecasts(types.SimpleNamespace(), tmp_path)
        reads = []

        async def load():
            reads.append(1)
            return len(reads)

        assert await f._cached("forecast:1,2", TTL["forecast"], load) == 1
        f._c["forecast:1,2"] = (time.time() - TTL["forecast"] - 5, 1)
        assert await f._cached("forecast:1,2", TTL["forecast"], load) == 1  # at once
        await asyncio.sleep(0.01)
        assert await f._cached("forecast:1,2", TTL["forecast"], load) == 2 and not f._behind
        assert await f._cached("alerts:1,2", TTL["alerts"], load) == 3
        f._c["alerts:1,2"] = (time.time() - TTL["alerts"] - 5, 3)
        assert await f._cached("alerts:1,2", TTL["alerts"], load) == 4  # waited for

    asyncio.run(run())
