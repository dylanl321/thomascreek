"""Thermostat history: shaping Home Assistant's rows and who may see them."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import respx

from hub.climate import shape


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _row(t: float, state: str, **attrs):
    return {"entity_id": "climate.theo_room", "state": state, "attributes": attrs,
            "last_changed": _iso(t), "last_updated": _iso(t)}


def test_shape_runs_setpoints_and_summary():
    start = 1_000_000.0
    end = start + 4 * 3600
    rows = [
        _row(start, "heat_cool", current_temperature=66, current_humidity=40, target_temp_low=68, target_temp_high=74,
             hvac_action="idle", temperature_unit="°F"),
        _row(start + 600, "heat_cool", current_temperature=66, current_humidity=40, target_temp_low=68,
             target_temp_high=74, hvac_action="heating"),
        _row(start + 1800, "heat_cool", current_temperature=68, current_humidity=39, target_temp_low=68,
             target_temp_high=74, hvac_action="heating"),
        _row(start + 2400, "heat_cool", current_temperature=69, current_humidity=39, target_temp_low=68,
             target_temp_high=74, hvac_action="idle"),
        _row(start + 3600, "heat", current_temperature=69, current_humidity=39, temperature=70, hvac_action="heating"),
        _row(start + 7200, "unavailable"),
        _row(start + 9000, "heat", current_temperature="70", current_humidity=38, temperature=70, hvac_action="idle"),
    ]
    out = shape(rows, start, end)
    assert out["reports_action"] is True
    assert [(r["action"], r["start"] - start, r["end"] - start) for r in out["runs"]] == [
        ("heating", 600, 2400), ("heating", 3600, 7200)]
    s = out["summary"]
    assert s["heating_s"] == 1800 + 3600 and s["heating_cycles"] == 2 and s["cooling_s"] == 0
    assert s["temp_min"] == 66 and s["temp_max"] == 70 and s["humidity_now"] == 38
    # Auto holds both set points; Heat holds one; unavailable holds none.
    sp = [(p["t"] - start, p["heat"], p["cool"]) for p in out["setpoints"]]
    assert sp[:3] == [(0, 68, 74), (3600, 70, None), (7200, None, None)]
    assert out["setpoints"][-1]["t"] == end
    # The outage is a gap in the line, and the line runs on to "now".
    assert any(p["temp"] is None for p in out["points"])
    assert out["points"][-1]["t"] == end and out["points"][-1]["temp"] == 70
    assert [m["mode"] for m in out["modes"]] == ["heat_cool", "heat", "unavailable", "heat"]


def test_shape_without_hvac_action_and_downsampling():
    start = 0.0
    end = 7 * 86400.0
    rows = [_row(i * 60, "cool", current_temperature=70 + (i % 5), temperature=72) for i in range(5000)]
    out = shape(rows, start, end)
    assert out["reports_action"] is False and out["runs"] == []
    assert len(out["points"]) <= 601
    assert out["setpoints"][0]["cool"] == 72 and out["setpoints"][0]["heat"] is None


def test_history_route_roles_and_ha(hub, people, home_client):
    now = time.time()
    hub.config.smart.latitude, hub.config.smart.longitude = 40.0, -75.0
    dylan, theo, avery = (people["phones"][n] for n in ("Dylan", "Theo", "Avery"))
    with respx.mock(assert_all_called=False) as mock:
        ha = mock.get(url__regex=r"http://ha\.test/api/history/period/.*").mock(return_value=httpx.Response(200, json=[[
            _row(now - 3600, "heat", current_temperature=69, temperature=70, hvac_action="heating",
                 temperature_unit="°F"),
            _row(now - 1800, "heat", current_temperature=70, temperature=70, hvac_action="idle"),
        ]]))
        om = mock.get(url__regex=r"https://api\.open-meteo\.com/.*").mock(return_value=httpx.Response(200, json={
            "hourly": {"time": [int(now - 7200), int(now - 3600), int(now - 30 * 86400)], "temperature_2m": [41.0, 43.5, 10]}}))
        r = home_client.get("/v1/climate/climate.theo_room/history?range=6h", headers=dylan.h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["unit"] == "°F" and d["range"] == "6h" and d["name"] == "Theo's room"
        assert [x["action"] for x in d["runs"]] == ["heating"]
        assert [o["temp"] for o in d["outdoor"]] == [41.0, 43.5]
        q = dict(ha.calls[0].request.url.params)
        assert q["filter_entity_id"] == "climate.theo_room" and q["significant_changes_only"] == "0"
        assert om.calls[0].request.url.params["temperature_unit"] == "fahrenheit"
        # Cached for a minute: a second phone doesn't hit Home Assistant again.
        assert home_client.get("/v1/climate/climate.theo_room/history?range=6h", headers=avery.h).status_code == 200
        assert ha.call_count == 1
        # Theo's own room: allowed. Bad ranges and unknown or non-climate entities are refused.
        assert home_client.get("/v1/climate/climate.theo_room/history", headers=theo.h).status_code == 200
        assert home_client.get("/v1/climate/climate.theo_room/history?range=90d", headers=dylan.h).status_code in (400, 422)
        assert home_client.get("/v1/climate/climate.nope/history", headers=dylan.h).status_code == 404
        assert home_client.get("/v1/climate/light.kitchen/history", headers=dylan.h).status_code in (400, 404, 422)


def test_history_hidden_from_a_child_outside_their_rooms(hub, people, home_client):
    hub.home.entity_area["climate.theo_room"] = "kitchen_private"
    theo = people["phones"]["Theo"]
    assert home_client.get("/v1/climate/climate.theo_room/history", headers=theo.h).status_code == 404
