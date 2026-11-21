"""How long each route takes, and how busy the NAS is, kept in memory for the admin page and the timing tool.

Every answer gets `total;dur=<ms>` added to its `Server-Timing` header (after any parts a route set itself), and
its time is kept per route template (`/v1/tv/library/{kind}`, never the address a caller typed, so no id, code or
query string is ever stored). The last `KEEP` answers per route give p50, p95 and the slowest.

The time is until the answer starts (its headers), which is what a person waits for; a long video or HLS stream
is not counted as slow for streaming.

`HostLoad` reads `/proc/stat` (system wide, also inside a container) so the page can say whether the CPU is busy
or waiting on disk, and how much of it the hub itself uses. Linux counts processes waiting on disk in the load
average, so a ZFS scrub raises the load without using the CPU.
"""

from __future__ import annotations

import collections
import os
import re
import threading
import time
from typing import Any, Callable

KEEP = 500
SLOW_MS = 1000.0
SLOW_KEEP = 40
_PART = re.compile(r"^([A-Za-z][\w.-]{0,40});dur=(\d+(?:\.\d+)?)$")
HOST_EVERY_S = 10.0
# Long-polls: the TV asks and the hub holds the call open until a command arrives or `timeout` (20-25 s) passes,
# so their time is the wait by design, not a slow answer. They are listed with `long_poll: true`, last, and never
# counted as over a second (Nov 21 2026: /v1/tv/commands 25 s and /v1/tv/manage 20 s topped the slow list).
LONG_POLL = frozenset({"GET /v1/tv/commands", "GET /v1/tv/manage"})


def _pct(sorted_ms: list[float], p: float) -> float:
    if not sorted_ms:
        return 0.0
    i = min(len(sorted_ms) - 1, max(0, round(p * (len(sorted_ms) - 1))))
    return sorted_ms[i]


class Timings:
    def __init__(self, keep: int = KEEP, clock: Callable[[], float] = time.time):
        self.keep = keep
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[str, collections.deque[tuple[float, float, int]]] = {}
        self._count: collections.Counter[str] = collections.Counter()
        self._slow: collections.Counter[str] = collections.Counter()
        # The step times (`Server-Timing` names and milliseconds, never a value) of the last slow answers, so a
        # slow route says which of its steps was slow without a log (Nov 20 2026, phone plays over a second).
        self._slow_parts: collections.deque[dict[str, Any]] = collections.deque(maxlen=SLOW_KEEP)
        self.since = clock()

    def record(self, method: str, route: str, ms: float, status: int, parts: str | None = None) -> None:
        key = f"{method} {route}"
        waits = key in LONG_POLL
        # Failed answers keep their steps too: a relay's 502 names what the provider answered (`up0.http403`).
        steps = _steps(parts) if parts and ((ms >= SLOW_MS and not waits) or status >= 500) else None
        with self._lock:
            if steps:
                row = {"at": round(self._clock()), "method": method, "route": route, "ms": round(ms), "steps": steps}
                if status >= 500:
                    row["status"] = status
                self._slow_parts.append(row)
            rows = self._rows.get(key)
            if rows is None:
                rows = self._rows[key] = collections.deque(maxlen=self.keep)
            rows.append((self._clock(), ms, status))
            self._count[key] += 1
            if ms >= SLOW_MS and not waits:
                self._slow[key] += 1

    def summary(self, *, window_s: float | None = None) -> list[dict[str, Any]]:
        """One row per route, slowest p95 first. `window_s` keeps only answers from that many seconds back."""
        now = self._clock()
        out: list[dict[str, Any]] = []
        with self._lock:
            items = [(k, list(v)) for k, v in self._rows.items()]
            count, slow = dict(self._count), dict(self._slow)
        for key, rows in items:
            if window_s is not None:
                rows = [r for r in rows if now - r[0] <= window_s]
            if not rows:
                continue
            ms = sorted(r[1] for r in rows)
            method, route = key.split(" ", 1)
            waits = key in LONG_POLL
            row: dict[str, Any] = {
                "method": method, "route": route, "n": len(ms), "total": count.get(key, 0),
                "p50_ms": round(_pct(ms, 0.50), 1), "p95_ms": round(_pct(ms, 0.95), 1), "max_ms": round(ms[-1], 1),
                "over_1s": 0 if waits else sum(1 for v in ms if v >= SLOW_MS), "over_1s_total": slow.get(key, 0),
                "errors": sum(1 for r in rows if r[2] >= 500), "last": round(rows[-1][0]),
            }
            if waits:
                row["long_poll"] = True
            out.append(row)
        out.sort(key=lambda r: (bool(r.get("long_poll")), -r["p95_ms"]))
        return out


    def slow(self) -> list[dict[str, Any]]:
        """The last slow or failed answers that named their steps, newest first (a failed one has `status`)."""
        with self._lock:
            return list(self._slow_parts)[::-1]


def _steps(header: str) -> dict[str, float]:
    """`a;dur=12, b;dur=900` -> {a: 12, b: 900}. Only a step's name and its time are kept."""
    out: dict[str, float] = {}
    for part in header.split(","):
        found = _PART.match(part.strip())
        if found:
            out[found.group(1)] = float(found.group(2))
    return out


def _route_of(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None) or getattr(route, "path_format", None)
    if isinstance(path, str) and path:
        return path
    # Unmatched (404) or a mounted app: never the raw path, which can carry ids and codes.
    return "(unmatched)"


class TimingMiddleware:
    """Pure ASGI, so streaming answers pass straight through. Adds `total` to Server-Timing."""

    def __init__(self, app: Any, timings: Timings):
        self.app = app
        self.timings = timings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        state: dict[str, Any] = {"done": False}

        async def wrapped(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start" and not state["done"]:
                state["done"] = True
                ms = (time.monotonic() - started) * 1000
                status = int(message.get("status") or 0)
                headers = list(message.get("headers") or [])
                total = f"total;dur={ms:.0f}".encode()
                parts: str | None = None
                for i, (k, v) in enumerate(headers):
                    if k.lower() == b"server-timing":
                        parts = v.decode("latin-1")
                        headers[i] = (k, v + b", " + total)
                        break
                else:
                    headers.append((b"server-timing", total))
                message = {**message, "headers": headers}
                try:
                    self.timings.record(scope.get("method", "GET"), _route_of(scope), ms, status, parts)
                except Exception:  # noqa: BLE001 - timing must never break an answer
                    pass
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            if not state["done"]:  # the route raised before answering; uvicorn will send a 500
                self.timings.record(scope.get("method", "GET"), _route_of(scope),
                                    (time.monotonic() - started) * 1000, 500)


class HostLoad:
    """CPU use of the whole box and of this process, from /proc. `sample()` is called every HOST_EVERY_S by the
    hub's loop; each call covers the time since the one before, and the last hour is kept in `history`."""

    def __init__(self, proc: str = "/proc", clock: Callable[[], float] = time.monotonic):
        self.proc = proc
        self._clock = clock
        self._lock = threading.Lock()
        self._prev: tuple[float, list[int], int] | None = None
        self._last: dict[str, Any] = {}
        self._tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=360)

    def _read(self) -> tuple[list[int], int] | None:
        try:
            with open(f"{self.proc}/stat") as f:
                cpu = [int(x) for x in f.readline().split()[1:9]]
            with open(f"{self.proc}/self/stat") as f:
                parts = f.read().rsplit(")", 1)[1].split()
            mine = int(parts[11]) + int(parts[12])  # utime + stime, after the ")" that ends the name
            return cpu, mine
        except (OSError, ValueError, IndexError):
            return None

    def sample(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            if self._prev is not None and now - self._prev[0] < 1.0 and self._last:
                return dict(self._last)
            got = self._read()
            if got is None:
                return {}
            cpu, mine = got
            out: dict[str, Any] = {"cpus": os.cpu_count()}
            try:
                with open(f"{self.proc}/loadavg") as f:
                    out["load"] = [float(x) for x in f.read().split()[:3]]
            except (OSError, ValueError):
                pass
            if self._prev is not None:
                _, pcpu, pmine = self._prev
                d = [a - b for a, b in zip(cpu, pcpu, strict=True)]
                total = sum(d) or 1
                # user nice system idle iowait irq softirq steal
                out["busy_pct"] = round(100 * (total - d[3] - d[4]) / total, 1)
                out["iowait_pct"] = round(100 * d[4] / total, 1)
                wall = now - self._prev[0]
                if wall > 0:
                    out["hub_cpu_pct"] = round(100 * (mine - pmine) / self._tick / wall, 1)  # of one thread
                out["over_s"] = round(wall, 1)
            self._prev = (now, cpu, mine)
            self._last = out
            if "busy_pct" in out:
                self.history.append({"at": round(time.time()), **out})
            return dict(out)

    def last(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last)

    def hour(self) -> dict[str, Any]:
        """Average and peak over the kept samples (about the last hour)."""
        with self._lock:
            rows = list(self.history)
        if not rows:
            return {}
        busy = [r["busy_pct"] for r in rows]
        io = [r.get("iowait_pct", 0.0) for r in rows]
        hub = [r.get("hub_cpu_pct", 0.0) for r in rows]
        return {"samples": len(rows), "minutes": round(len(rows) * HOST_EVERY_S / 60),
                "busy_avg_pct": round(sum(busy) / len(busy), 1), "busy_max_pct": max(busy),
                "iowait_avg_pct": round(sum(io) / len(io), 1),
                "hub_cpu_avg_pct": round(sum(hub) / len(hub), 1), "hub_cpu_max_pct": max(hub)}
