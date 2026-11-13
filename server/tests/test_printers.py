"""Bambu printers over the LAN: reading reports, controls, the library and permissions."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from hub import bambu
from hub.bambu import read_3mf, status

H2_REPORT = {"print": {
    "gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 72, "layer_num": 112, "total_layer_num": 268,
    "subtask_name": "Pine planter", "spd_lvl": 2, "stg_cur": 0,
    "bed_temper": 64.8, "bed_target_temper": 65,
    "device": {"extruder": {"info": [{"id": 0, "temp": 220 | (220 << 16)}]}, "ctc": {"info": {"temp": 38}}},
    "lights_report": [{"node": "chamber_light", "mode": "on"}],
    "ams": {"tray_now": "2", "ams": [{"id": "0", "humidity": "2", "humidity_raw": "18", "temp": "24.5", "tray": [
        {"id": "0", "tray_color": "2F6B4FFF", "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "remain": 62},
        {"id": "1", "tray_color": "EFEADFFF", "tray_type": "PLA", "tray_sub_brands": "PLA Matte", "remain": 88},
        {"id": "2", "tray_color": "D8572AFF", "tray_type": "PETG", "tray_sub_brands": "PETG HF", "remain": 35},
        {"id": "3"},
    ]}]},
}}


def sliced_3mf(sliced: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        if sliced:
            z.writestr("Metadata/plate_1.gcode", "G28\n")
            z.writestr("Metadata/plate_1.png", b"\x89PNG fake")
            z.writestr("Metadata/slice_info.config", """<?xml version="1.0"?><config><plate>
<metadata key="index" value="1"/><metadata key="printer_model_id" value="O1S"/>
<metadata key="prediction" value="7500"/><metadata key="weight" value="64.2"/>
<filament id="1" type="PLA" color="#2F6B4F" used_g="64.2"/></plate></config>""")
    return buf.getvalue()


class FakeMQTT:
    def __init__(self):
        self.sent = []

    async def publish(self, topic, payload):
        self.sent.append((topic, json.loads(payload)))


@pytest.fixture
def ws(hub):
    hub.config.hub.dry_run = False
    hub.vault.set("printer.h2s.access_code", "12345678")
    hub.vault.set("printer.h2s.serial", "0938AB000000001")
    hub.vault.set("printer.h2s.host", "10.0.1.50")
    p = hub.workshop.get("h2s")
    p._client = FakeMQTT()
    p.connected = True
    p.ingest(json.loads(json.dumps(H2_REPORT)))
    sent = []

    async def send(**kw):
        sent.append(kw)
        return []

    hub.notify.send = send  # type: ignore[method-assign]
    hub.sent = sent  # type: ignore[attr-defined]
    return hub


def _act(client, ph, action, params, target="h2s"):
    return client.post("/v1/actions", json={"action": action, "target": target, "params": params}, headers=ph.h)


def test_reads_an_h2_report():
    s = status(H2_REPORT, connected=True, last_report=1.0)
    assert s["state"] == "printing"
    assert s["job"]["name"] == "Pine planter" and s["job"]["progress"] == 42 and s["job"]["minutes_left"] == 72
    assert s["temps"]["nozzle"] == {"current": 220.0, "target": 220.0}
    assert s["temps"]["bed"]["target"] == 65.0
    assert s["temps"]["chamber"]["current"] == 38.0
    assert s["light"] is True and s["speed"] == "standard"
    trays = s["ams"][0]["trays"]
    assert [t["color"] for t in trays] == ["#2F6B4F", "#EFEADF", "#D8572A", None]
    assert trays[3]["empty"] is True and trays[0]["remain"] == 62
    assert s["active_slot"] == 2 and s["ams"][0]["humidity"] == 18.0


def test_offline_and_partial_updates_merge(hub):
    p = hub.workshop.get("h2s")
    assert p.status()["state"] == "offline"
    assert set(p.missing()) == {"printer.h2s.host", "printer.h2s.serial", "printer.h2s.access_code"}
    p.connected = True
    p.ingest(json.loads(json.dumps(H2_REPORT)))
    p.ingest({"print": {"mc_percent": 43, "command": "push_status"}})
    s = p.status()
    assert s["job"]["progress"] == 43 and s["ams"][0]["trays"][0]["color"] == "#2F6B4F"


async def test_finish_notifies_and_publishes(ws):
    import asyncio

    p = ws.workshop.get("h2s")
    events = []
    ws.bus.publish = lambda e, **kw: events.append(e)  # type: ignore[method-assign]
    p.ingest({"print": {"gcode_state": "FINISH", "mc_percent": 100}})
    await asyncio.sleep(0)
    assert any(e["type"] == "printer" for e in events)
    assert ws.sent[-1]["title"] == "Pine planter is done" and ws.sent[-1]["roles"] == ["admin"]


def test_controls_through_actions(ws, people, home_client):
    dylan = people["phones"]["Dylan"]
    mq = ws.workshop.get("h2s")._client
    r = _act(home_client, dylan, "printer.control", {"command": "pause"})
    assert r.status_code == 200, r.text
    assert mq.sent[-1][0] == "device/0938AB000000001/request"
    assert mq.sent[-1][1]["print"]["command"] == "pause"
    assert _act(home_client, dylan, "printer.light", {"on": False}).status_code == 200
    assert mq.sent[-1][1]["system"]["led_mode"] == "off"
    assert _act(home_client, dylan, "printer.speed", {"speed": "sport"}).status_code == 200
    assert mq.sent[-1][1]["print"] == {"command": "print_speed", "param": "3", "sequence_id": mq.sent[-1][1]["print"]["sequence_id"]}
    assert _act(home_client, dylan, "printer.temp", {"heater": "bed", "temp": 70}).status_code == 200
    assert mq.sent[-1][1]["print"]["param"] == "M140 S70\n"
    assert _act(home_client, dylan, "printer.temp", {"heater": "nozzle", "temp": 400}).status_code == 400
    assert _act(home_client, dylan, "printer.speed", {"speed": "warp"}).status_code == 400
    assert _act(home_client, dylan, "printer.light", {"on": True}, target="nope").status_code == 404


def test_stop_needs_face_id(ws, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = _act(home_client, dylan, "printer.control", {"command": "stop"})
    assert r.status_code == 428
    r = dylan.secure_post("/v1/actions", {"action": "printer.control", "target": "h2s", "params": {"command": "stop"}})
    assert r.status_code == 200, r.text
    assert ws.workshop.get("h2s")._client.sent[-1][1]["print"]["command"] == "stop"


def test_children_watch_but_cannot_control(ws, people, home_client):
    theo = people["phones"]["Theo"]
    r = home_client.get("/v1/printers", headers=theo.h)
    assert r.status_code == 200
    p = r.json()["printers"][0]
    assert p["state"] == "printing" and p["actions"] == [] and "temps" not in p
    assert _act(home_client, theo, "printer.control", {"command": "pause"}).status_code == 403
    snap = home_client.get("/v1/home", headers=theo.h).json()
    assert snap["printers"][0]["job"]["name"] == "Pine planter"
    full = home_client.get("/v1/printers/h2s", headers=people["phones"]["Dylan"].h).json()
    assert full["temps"]["nozzle"]["current"] == 220.0 and "printer.print" in full["actions"]


def test_offline_printer_says_so(ws, people, home_client):
    p = ws.workshop.get("h2s")
    p.connected = False
    r = _act(home_client, people["phones"]["Dylan"], "printer.light", {"on": True})
    assert r.status_code == 409 and r.json()["error"] == "printer_offline"


def test_read_3mf():
    info = read_3mf(sliced_3mf())
    assert info["plates"][0]["seconds"] == 7500 and info["plates"][0]["grams"] == 64.2
    assert info["plates"][0]["filaments"][0]["color"] == "#2F6B4F"
    assert info["thumbnail"].startswith(b"\x89PNG")
    with pytest.raises(bambu.PrinterError) as e:
        read_3mf(sliced_3mf(False))
    assert e.value.error == "not_sliced"
    with pytest.raises(bambu.PrinterError):
        read_3mf(b"not a zip")


def test_library_and_print(ws, people, home_client, monkeypatch):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    r = home_client.post("/v1/printers/library", headers=dylan.h,
                         files={"file": ("Pine planter.gcode.3mf", sliced_3mf(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["title"] == "Pine planter" and m["thumbnail_url"].endswith("/thumb.png")
    assert home_client.post("/v1/printers/library", headers=theo.h,
                            files={"file": ("x.3mf", sliced_3mf(), "application/octet-stream")}).status_code == 403
    assert home_client.get(m["thumbnail_url"], headers=theo.h).status_code == 200
    bad = home_client.post("/v1/printers/library", headers=dylan.h, files={"file": ("raw.3mf", sliced_3mf(False), "x")})
    assert bad.status_code == 400 and bad.json()["error"] == "not_sliced"

    uploads = []
    monkeypatch.setattr(bambu, "ftps_upload", lambda host, code, name, data, timeout=60: uploads.append((host, name, len(data))))
    body = {"action": "printer.print", "target": "h2s", "params": {"model_id": m["id"], "ams_mapping": [0]}}
    # Busy: the fixture printer is mid-print.
    r = dylan.secure_post("/v1/actions", body)
    assert r.status_code == 409 and r.json()["error"] == "printer_busy"
    ws.workshop.get("h2s").ingest({"print": {"gcode_state": "IDLE"}})
    r = dylan.secure_post("/v1/actions", body)
    assert r.status_code == 200, r.text
    assert uploads == [("10.0.1.50", f"tc-{m['id']}.gcode.3mf", len(sliced_3mf()))]
    cmd = ws.workshop.get("h2s")._client.sent[-1][1]["print"]
    assert cmd["command"] == "project_file" and cmd["param"] == "Metadata/plate_1.gcode"
    assert cmd["ams_mapping"] == [0] and cmd["use_ams"] is True and cmd["subtask_name"] == "Pine planter"
    lib = home_client.get("/v1/printers/library", headers=dylan.h).json()["models"]
    assert lib[0]["prints"] == 1
    # Kids ask instead.
    assert home_client.post(f"/v1/printers/library/{m['id']}/ask", headers=theo.h).status_code == 200
    assert ws.sent[-1]["title"] == "Theo would like a print"
    assert home_client.delete(f"/v1/printers/library/{m['id']}", headers=dylan.h).status_code == 200
    assert home_client.get("/v1/printers/library", headers=dylan.h).json()["models"] == []


def test_assistant_cannot_fetch_camera_or_pictures():
    from hub.assistant import DENY

    assert DENY.search("/v1/printers/h2s/snapshot.jpg")
    assert DENY.search("/v1/printers/h2s/mjpeg")
    assert DENY.search("/v1/printers/library/mdl_1/thumb.png")
    assert not DENY.search("/v1/printers")


def test_printer_without_keys_stays_off_house_status(hub):
    h = next(x for x in hub.workshop.health() if x["id"] == "printer_h2s")
    assert h["enabled"] is False and h["error"]
    assert not any(x["enabled"] and x["error"] == "Not set up yet" for x in hub.health())
