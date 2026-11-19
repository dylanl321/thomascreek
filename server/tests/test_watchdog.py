"""A hub whose event loop is stuck restarts itself and leaves a note on where it was stuck."""
from __future__ import annotations

import logging

from hub.watchdog import Watchdog


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_loop_stuck_past_the_limit_writes_a_note_and_exits(tmp_path):
    clock, exits = Clock(), []
    dog = Watchdog(tmp_path, stall_s=120, exit_fn=lambda: exits.append(1), clock=clock)
    dog.last = clock.now
    clock.now += 119
    assert dog.check() is False and exits == []
    clock.now += 2
    assert dog.check() is True and exits == [1]
    notes = list((tmp_path / "hangs").glob("hang-*.txt"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "did not run for 121 s" in text
    assert "--- thread" in text and "test_watchdog.py" in text  # where each thread was


def test_the_next_start_reports_the_note_once(tmp_path, caplog):
    clock = Clock()
    dog = Watchdog(tmp_path, exit_fn=lambda: None, clock=clock)
    dog.last = clock.now - 500
    dog.check()
    with caplog.at_level(logging.WARNING, logger="hub.watchdog"):
        Watchdog(tmp_path).report_last()
        Watchdog(tmp_path).report_last()
    said = [r for r in caplog.records if "restarted itself" in r.getMessage()]
    assert len(said) == 1


def test_only_the_last_few_notes_are_kept(tmp_path):
    dog = Watchdog(tmp_path, exit_fn=lambda: None)
    for i in range(8):
        (tmp_path / "hangs").mkdir(exist_ok=True)
        (tmp_path / "hangs" / f"hang-2026010{i}T000000Z.txt").write_text("x")
    dog._save("y")
    assert len(list((tmp_path / "hangs").glob("hang-*.txt"))) == 5


async def test_a_running_loop_keeps_the_watchdog_quiet(tmp_path):
    import asyncio

    exits: list[int] = []
    dog = Watchdog(tmp_path, stall_s=0.5, beat_s=0.05, exit_fn=lambda: exits.append(1))
    dog.start()
    await asyncio.sleep(0.8)
    await dog.stop()
    assert exits == []


def test_a_short_stall_keeps_where_the_loop_thread_was(tmp_path):
    import threading

    clock = Clock()
    dog = Watchdog(tmp_path, exit_fn=lambda: None, clock=clock)
    dog._loop_ident = threading.get_ident()
    dog.last = clock.now
    clock.now += 1
    dog.sample()
    assert not dog.samples
    clock.now += 4
    dog.sample()
    dog.sample()  # once per few seconds of one stall
    assert len(dog.samples) == 1
    row = dog.samples[0]
    assert row["stalled_s"] == 5.0
    assert any("test_watchdog.py" in line for line in row["stack"])


def test_stalls_are_read_from_home_only(home_client, away_client):
    res = home_client.get("/v1/ping/stalls")
    assert res.status_code == 200
    assert res.json()["stalls"] == []
    assert away_client.get("/v1/ping/stalls").status_code == 403


def test_the_beat_is_well_under_the_sample_limit():
    """The loop ticks every BEAT_S: a gap shorter than that is the loop idling between beats, not a stall."""
    from hub import watchdog

    assert watchdog.BEAT_S * 2 <= watchdog.SAMPLE_S


def _spin(stop):
    while not stop.is_set():
        sum(range(1000))


def test_a_stall_names_the_threads_that_were_working_and_not_the_waiting_ones(tmp_path):
    """A loop stuck on a trivial line is starved by another thread; the sample says which one."""
    import threading

    clock = Clock()
    dog = Watchdog(tmp_path, exit_fn=lambda: None, clock=clock)
    dog._loop_ident = threading.get_ident()
    stop = threading.Event()
    busy = threading.Thread(target=_spin, args=(stop,), name="busy-one", daemon=True)
    idle = threading.Thread(target=stop.wait, name="idle-one", daemon=True)
    busy.start()
    idle.start()
    try:
        dog.last = clock.now
        clock.now += 5
        dog.sample()
    finally:
        stop.set()
        busy.join()
        idle.join()
    seen = dog.samples[0]["busy"]
    assert "busy-one" in seen and any("_spin" in line for line in seen["busy-one"])
    assert "idle-one" not in seen
