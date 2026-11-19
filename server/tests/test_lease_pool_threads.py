"""Leases run in worker threads now (hub rebuild phase 4): many players asking at once never share a login."""

from __future__ import annotations

import threading

from hub import iptvplay
from hub.iptvplay import lease

from tests.test_tv_api import _library
from tests.test_tv_phone import _login


def test_many_players_at_once_get_one_login_between_them(hub, people, monkeypatch):
    _library(hub)
    _login(hub)
    real = iptvplay._sessions

    def slow_sessions(h):  # widen the read-then-write window, as a busy hub would
        rows = real(h)
        threading.Event().wait(0.002)
        return rows

    monkeypatch.setattr(iptvplay, "_sessions", slow_sessions)
    got: list[object] = []
    start = threading.Barrier(8)

    def play(i: int) -> None:
        start.wait()
        got.append(lease(hub, "vod:thomas-creek:101", f"phone:dev{i}"))

    threads = [threading.Thread(target=play, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    held = [row for row in got if row is not None]
    assert len(held) == 1  # one login, one stream
    assert len(real(hub)) == 1
