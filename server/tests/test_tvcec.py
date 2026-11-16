"""HDMI-CEC power, input and volume from the phone, run by the hub over adb (hub/tvcec.py)."""

from __future__ import annotations

import time

from hub import tvcec
from hub.tvapp import AdbResult
from hub.tvfleet import report

from tests.test_tv_api import TV_ID
from tests.test_tv_fleet import BODY
from tests.test_tvs import _drain, _screen, _tv

# What a Shield on Android 11 answers today: CEC on, CEC volume off, awake, and only its own echoes (TV silent).
SILENT = """1
0
null
  mWakefulness=Awake
  port_id: 1, type: HDMI_OUT, address: 0xffffffff, cec: true, arc: false, mhl: false
    mAddress: 8
    [R] time=2026-11-15 00:47:38.869 message=<Active Source> 8F:82:20:00
    [R] time=2026-11-15 00:57:54.837 message=<Standby> 8F:36
"""
# The same box once the TV has CEC on: it answers the power query, and volume goes over CEC.
TALKING = """1
1
1
  mWakefulness=Asleep
  port_id: 1, type: HDMI_OUT, address: 0xffffffff, cec: true, arc: false, mhl: false
    mAddress: 8
    [R] time=2026-11-16 20:01:02.000 message=<Report Power Status> 08:90:00
"""


def test_parse_reads_what_the_box_knows():
    silent = tvcec.parse(SILENT)
    assert silent == {"enabled": True, "volume": False, "auto_off": True, "awake": True, "tv_answers": False}
    talking = tvcec.parse(TALKING)
    assert talking == {"enabled": True, "volume": True, "auto_off": True, "awake": False, "tv_answers": True}
    off = tvcec.parse("0\n0\n0\n  mWakefulness=Dozing\n")
    assert off["enabled"] is False and off["awake"] is False and off["auto_off"] is False
    assert tvcec.parse("") == {"enabled": False, "volume": False, "auto_off": True, "awake": None, "tv_answers": False}
    assert "CEC is off" in tvcec.note(off, "Den")
    assert "Anynet+" in tvcec.note(silent, "Den")
    assert tvcec.note(talking, "Den") is None


def _adb(hub, probe: str):
    tvs = hub.tv_app.tvs()
    tvs[TV_ID].update(ip="10.0.1.21")
    hub.tv_app._save_tvs(tvs)
    calls: list[list[str]] = []
    state = {"probe": probe, "keys": 0}

    async def fake(args, timeout):
        calls.append(args)
        if args[0] == "connect":
            return AdbResult(0, "connected to 10.0.1.21:5555")
        if "get-state" in args:
            return AdbResult(0, "device")
        if args[-1].startswith("settings get global hdmi_control_enabled"):
            return AdbResult(0, state["probe"])
        if "keyevent" in args:
            state["keys"] += 1
        return AdbResult(0, "")

    hub.tv_app.adb._run = fake
    hub.tv_app.adb.binary = "sh"  # anything on PATH, so `available` is true
    return calls


def _keys(calls):
    return [c[-1] for c in calls if "keyevent" in c]


def test_power_input_and_volume_go_over_cec(hub, home_client, people, monkeypatch):
    monkeypatch.setattr(tvcec, "CYCLE_S", 0)
    _tv(hub, home_client)
    calls = _adb(hub, TALKING)
    dylan = people["phones"]["Dylan"]
    url = f"/v1/tvs/{TV_ID}/actions"

    assert dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()["can_cec"] is True
    on = dylan.client.post(url, headers=dylan.h, json={"action": "tv_power", "power": "on"})
    assert on.status_code == 200, on.text
    assert on.json() == {"ok": True, "via": "adb", "message": "Turned the TV on over HDMI-CEC."}
    assert _keys(calls) == ["KEYCODE_WAKEUP"]  # asleep: waking sends One Touch Play

    calls.clear()
    off = dylan.client.post(url, headers=dylan.h, json={"action": "tv_power", "power": "off"})
    assert off.json()["message"] == "Sent Standby over HDMI-CEC."
    assert _keys(calls) == ["KEYCODE_SLEEP"]

    calls.clear()
    loud = dylan.client.post(url, headers=dylan.h, json={"action": "tv_volume", "volume": "up"})
    assert loud.json()["message"] == "Sent the volume over HDMI-CEC."
    hush = dylan.client.post(url, headers=dylan.h, json={"action": "tv_volume", "volume": "mute"})
    assert hush.status_code == 200
    assert _keys(calls) == ["KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_MUTE"]

    cec = dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()["cec"]
    assert cec["tv_answers"] is True and cec["volume"] is True and cec["note"] is None and cec["checked"]

    bad = dylan.client.post(url, headers=dylan.h, json={"action": "tv_power", "power": "sideways"})
    assert bad.status_code == 400 and bad.json()["message"] == "Say on or off."
    assert dylan.client.post(url, headers=dylan.h, json={"action": "tv_volume"}).status_code == 400


def test_an_awake_box_switches_the_tv_without_sending_it_standby(hub, home_client, people, monkeypatch):
    monkeypatch.setattr(tvcec, "CYCLE_S", 0)
    _tv(hub, home_client)
    calls = _adb(hub, SILENT)
    dylan = people["phones"]["Dylan"]
    switch = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "tv_input"})
    assert switch.status_code == 200, switch.text
    body = switch.json()
    assert body["message"].startswith("Switched the TV to this box over HDMI-CEC.")
    assert "Anynet+" in body["message"]  # the TV never answered, so say what to turn on
    shell = [c[3:] for c in calls if len(c) > 3 and c[2] == "shell" and not c[3].startswith("settings get")]
    assert shell == [
        ["settings", "put", "global", tvcec.AUTO_OFF, "0"],
        ["input", "keyevent", "KEYCODE_SLEEP"],
        ["input", "keyevent", "KEYCODE_WAKEUP"],
        ["settings", "put", "global", tvcec.AUTO_OFF, "1"],
    ]
    check = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "cec_check"})
    assert check.json()["cec"]["tv_answers"] is False and "Anynet+" in check.json()["message"]
    loud = dylan.client.post(f"/v1/tvs/{TV_ID}/actions", headers=dylan.h, json={"action": "tv_volume", "volume": "down"})
    assert "Volume over HDMI-CEC is off" in loud.json()["message"]


def test_without_adb_volume_goes_to_the_app_and_power_says_why(hub, home_client, people):
    _tv(hub, home_client)
    report(hub, TV_ID, BODY, time.time())
    screen = _screen(hub)
    dylan = people["phones"]["Dylan"]
    url = f"/v1/tvs/{TV_ID}/actions"
    assert dylan.client.get(f"/v1/tvs/{TV_ID}", headers=dylan.h).json()["can_cec"] is False
    loud = dylan.client.post(url, headers=dylan.h, json={"action": "tv_volume", "volume": "mute"})
    assert loud.status_code == 200, loud.text
    assert loud.json()["via"] == "app"
    sent = _drain(screen.queue)
    assert sent[0]["data"]["action"] == "tv_volume" and sent[0]["data"]["params"] == {"mute": True}
    power = dylan.client.post(url, headers=dylan.h, json={"action": "tv_power", "power": "on"})
    assert power.status_code == 409 and power.json()["error"] in ("no_adb", "no_address")


def test_children_cannot_use_tv_controls(hub, home_client, people):
    _tv(hub, home_client)
    kid = people["phones"]["Theo"]
    reply = kid.client.post(f"/v1/tvs/{TV_ID}/actions", headers=kid.h, json={"action": "tv_power", "power": "off"})
    assert reply.status_code == 403
