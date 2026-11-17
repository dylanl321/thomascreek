"""A failure a route survives still reaches the log, with its cause and without an address (#210 step 4)."""

from __future__ import annotations

import logging

import httpx

from hub.faults import describe, note


def test_describe_drops_addresses():
    exc = RuntimeError("GET http://user:pw@provider.example:8080/live.m3u8 via 10.9.9.9:8443 and tv.example.net:9000")
    text = describe(exc)
    assert text.startswith("RuntimeError: ")
    for leak in ("provider.example", "user:pw", "10.9.9.9", "8443", "tv.example.net"):
        assert leak not in text
    assert describe(ValueError()) == "ValueError"


def test_network_trouble_is_info_and_a_real_fault_is_a_warning_with_where(caplog):
    caplog.set_level(logging.INFO, logger="hub.routes")
    note("radar", httpx.ConnectError("refused by 192.168.1.5"))
    try:
        {}["missing"]
    except KeyError as exc:
        note("trip card", exc)
    routine, fault = caplog.records[-2:]
    assert routine.levelno == logging.INFO and "192.168" not in routine.getMessage()
    assert fault.levelno == logging.WARNING
    assert "trip card failed: KeyError" in fault.getMessage() and "test_faults.py" in fault.getMessage()


def test_a_radar_tile_that_breaks_is_logged_not_swallowed(hub, people, caplog):
    async def broken(*_a, **_k):
        raise RuntimeError("bad frame from https://tiles.example/abc")

    hub.forecasts.tile = broken
    caplog.set_level(logging.INFO, logger="hub.routes")
    ph = people["phones"]["Dylan"]
    r = ph.client.get("/v1/weather/radar/nexrad/1/2/3/4.png", headers=ph.h)
    assert r.status_code == 204
    line = next(rec.getMessage() for rec in caplog.records if "radar tile (nexrad)" in rec.getMessage())
    assert "RuntimeError" in line and "tiles.example" not in line
