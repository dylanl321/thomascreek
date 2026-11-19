"""A hub that stops answering restarts itself, and leaves a note saying where it was stuck.

On Nov 19 the hub stopped taking connections (even on the home network) until it was restarted by hand, and the
cause was lost with the container's log. Docker's health check marks such a container unhealthy but does not
restart it. This watchdog runs in its own thread: the event loop ticks a heartbeat every few seconds, and when the
loop has not ticked for `STALL_S` the watchdog writes every thread's stack (file, line and function only, no
values) to `<data>/hangs/`, logs it, and exits the process so the container restarts (`restart: unless-stopped`).

The next start logs the newest note as a WARNING, so it shows in the admin page's recent problems.

Shorter stalls are sampled too: when the loop has not ticked for `SAMPLE_S`, the loop thread's stack is kept (at
most once per `SAMPLE_S` of one stall) in a short in-memory list, which a caller on the home network reads at
`/v1/ping/stalls` while the hub is answering again. That names what blocks the hub for seconds at a time.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger("hub.watchdog")

BEAT_S = 5.0
STALL_S = 120.0  # longer than the slowest known step on the loop (a cold catalog parse took about 15 s)
KEEP = 5
SAMPLE_S = 3.0
SAMPLES = 40
EXIT_CODE = 70


def _frames(frame, limit: int = 25) -> list[str]:
    return [f"{f.filename.rsplit('/hub/', 1)[-1]}:{f.lineno} {f.name}" for f in traceback.extract_stack(frame)[-limit:]]


def stacks() -> str:
    """Every thread's stack as `file:line function`, innermost last. No variable values and no source text."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        out.append(f"--- thread {names.get(ident, ident)}")
        out.extend(f"  {line}" for line in _frames(frame))
    return "\n".join(out)


class Watchdog:
    def __init__(self, data: Path, *, stall_s: float = STALL_S, beat_s: float = BEAT_S,
                 exit_fn=None, clock=time.monotonic):
        self.dir = Path(data) / "hangs"
        self.stall_s = stall_s
        self.beat_s = beat_s
        self._exit = exit_fn or (lambda: os._exit(EXIT_CODE))
        self._clock = clock
        self.last = clock()
        self._stop = threading.Event()
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._loop_ident: int | None = None
        self._sampled_at = 0.0
        self.samples: collections.deque[dict[str, object]] = collections.deque(maxlen=SAMPLES)

    # -- the loop side ---------------------------------------------------------------------------------------------
    async def _beat(self) -> None:
        while True:
            self.last = self._clock()
            await asyncio.sleep(self.beat_s)

    def start(self) -> None:
        self.report_last()
        self.last = self._clock()
        self._loop_ident = threading.get_ident()
        self._task = asyncio.get_running_loop().create_task(self._beat())
        self._thread = threading.Thread(target=self._watch, name="hub-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # -- the watching thread ---------------------------------------------------------------------------------------
    def _watch(self) -> None:
        while not self._stop.wait(min(self.beat_s, 1.0)):
            self.sample()
            if self.check():
                return

    def sample(self) -> None:
        """Keep where the loop thread is while it has been stuck for a few seconds."""
        stalled = self._clock() - self.last
        if stalled < SAMPLE_S or self._loop_ident is None:
            return
        if self._clock() - self._sampled_at < SAMPLE_S:
            return
        frame = sys._current_frames().get(self._loop_ident)
        if frame is None:
            return
        self._sampled_at = self._clock()
        self.samples.append({"at": time.time(), "stalled_s": round(stalled, 1), "stack": _frames(frame, 30)})

    def check(self) -> bool:
        """True when the loop is stuck: the note is written and the process exits."""
        stalled = self._clock() - self.last
        if stalled < self.stall_s:
            return False
        text = f"The hub's event loop did not run for {stalled:.0f} s; restarting.\n{stacks()}\n"
        log.error("watchdog: %s", text)
        try:
            self._save(text)
        except OSError as exc:
            log.error("watchdog: could not save the note: %s", type(exc).__name__)
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001
                pass
        self._exit()
        return True

    def _save(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        (self.dir / f"hang-{stamp}.txt").write_text(text)
        for old in sorted(self.dir.glob("hang-*.txt"))[:-KEEP]:
            old.unlink(missing_ok=True)

    def report_last(self) -> None:
        """At start: say once that the last run ended stuck, with where it was."""
        try:
            notes = sorted(self.dir.glob("hang-*.txt"))
        except OSError:
            return
        if not notes:
            return
        newest = notes[-1]
        seen = newest.with_suffix(".seen")
        if seen.exists():
            return
        try:
            text = newest.read_text()[:4000]
            seen.write_text("")
        except OSError:
            return
        log.warning("The hub restarted itself after it stopped answering (%s). Where it was stuck:\n%s",
                    newest.name, text)
