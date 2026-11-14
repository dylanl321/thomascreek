"""Repeat alerts collapse, and Frigate events are not stuck behind a 10s poll."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hub.adapters.cameras import mqtt_to_event, parse_frigate_mqtt
from hub.push import APNs, PushResult, collapse_identifier


def test_collapse_identifier_fits_the_header():
    assert collapse_identifier(None) is None
    assert collapse_identifier("") is None
    assert collapse_identifier("cam:140_frontdoor") == "cam:140_frontdoor"
    exact = "c" * 64
    assert collapse_identifier(exact) == exact and len(exact.encode()) == 64
    long = "cam:" + "x" * 80
    hashed = collapse_identifier(long)
    assert hashed == collapse_identifier(long)
    assert hashed != collapse_identifier(long + "y")
    assert len(hashed.encode()) == 64 and hashed != long[:64]
    # A space, a newline, or a non-ASCII name can't go in the header as-is.
    for bad in ("cam:front door", "cam:front\r\nX-Other: 1", "cam:前门"):
        safe = collapse_identifier(bad)
        assert safe and len(safe.encode()) <= 64 and safe != bad
        assert collapse_identifier(safe) == safe  # the hash is already a legal id


def _arm_push(hub, people):
    hub.vault.set("apns.auth_key_p8", "k")
    hub.vault.set("apns.key_id", "ABC")
    hub.config.apns.team_id = "T"
    sent = []

    async def fake_send(token, payload, **kw):
        sent.append((token, payload, kw))
        return PushResult(True, 200)

    hub.apns.send = fake_send  # type: ignore[method-assign]
    hub.db.update("devices", "id", people["phones"]["Dylan"].device_id,
                  {"apns_token": "ab12", "apns_env": "sandbox"})
    return sent


async def test_group_key_collapses_and_threads(hub, people):
    sent = _arm_push(hub, people)
    ids = await hub.notify.send(
        title="Person at the front door", body="Front door camera", cat="cameras",
        person_ids=[people["dylan"]["id"]], group_key="cam:140_frontdoor",
        push_category="CAMERA_PERSON", image="/v1/cameras/events/a/snapshot.jpg")
    payload, kw = sent[0][1], sent[0][2]
    assert kw["collapse_id"] == "cam:140_frontdoor"
    assert payload["aps"]["thread-id"] == "cam:140_frontdoor"
    assert kw["collapse_id"] != ids[0]  # the inbox id is a new row, not what the phone collapses
    sent.clear()
    await hub.notify.send(
        title="Person at the front door", body="Still there", cat="cameras",
        person_ids=[people["dylan"]["id"]], group_key="cam:140_frontdoor")
    assert sent[0][2]["collapse_id"] == "cam:140_frontdoor"
    assert sent[0][1]["aps"]["thread-id"] == "cam:140_frontdoor"
    sent.clear()
    long = "ha:" + "sensor.freezer_" * 8
    await hub.notify.send(title="Freezer", body=None, cat="house", person_ids=[people["dylan"]["id"]], group_key=long)
    assert sent[0][2]["collapse_id"] == collapse_identifier(long) == sent[0][1]["aps"]["thread-id"]
    sent.clear()
    await hub.notify.send(title="Note", body=None, cat="house", person_ids=[people["dylan"]["id"]])
    assert sent[0][2]["collapse_id"] is None and sent[0][1]["aps"]["thread-id"] == "house"


@pytest.mark.asyncio
async def test_apns_header_is_the_safe_collapse_id(hub):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    hub.vault.set("apns.auth_key_p8", pem.decode())
    hub.config.apns.enabled, hub.config.apns.team_id, hub.config.apns.key_id = True, "TEAM123456", "KEY1234567"
    apns = APNs(hub.config.apns, hub.vault)
    long = "cam:" + ("ñ" * 40)
    with respx.mock() as mock:
        route = mock.post("https://api.sandbox.push.apple.com/3/device/abcd").mock(return_value=httpx.Response(200))
        await apns.send("abcd", {"aps": {"alert": "Hi"}}, env="sandbox", collapse_id=long)
        header = route.calls[0].request.headers["apns-collapse-id"]
        assert header == collapse_identifier(long) and len(header.encode()) <= 64
    await apns.close()


def _front_door(hub, people):
    hub.frigate._info = {"140_frontdoor": {"enabled": True, "width": 1280, "height": 720, "name": "Front Door"}}
    for name in ("dylan", "avery"):
        hub.alerts.set_prefs(people[name]["id"], {"on": True})
    sent = []
    orig = hub.notify.send

    async def send(**kw):
        sent.append(kw)
        return await orig(**kw)

    hub.notify.send = send  # type: ignore[method-assign]
    return sent


def _event(eid="e1", **kw):
    base = {"id": eid, "camera": "140_frontdoor", "label": "person", "has_snapshot": True,
            "start_time": time.time(), "false_positive": False}
    base.update(kw)
    return base


async def test_poll_skips_the_backlog_then_alerts_within_a_second(hub, people):
    sent = _front_door(hub, people)
    rows = [_event("old", start_time=1, has_snapshot=True)]

    async def camera_info(max_age=300):
        return hub.frigate._info

    async def events(**_kw):
        return list(rows)

    hub.frigate.camera_info = camera_info  # type: ignore[method-assign]
    hub.frigate.events = events  # type: ignore[method-assign]
    assert hub.frigate_poll_delay() == 1.0
    hub.config.frigate.poll_s = 0.1
    assert hub.frigate_poll_delay() == 0.5
    hub.frigate.mqtt_connected = True
    assert hub.frigate_poll_delay() == 30.0
    hub.frigate.mqtt_connected = False

    await hub._poll_frigate_once()
    assert sent == [] and hub._frigate_primed and "old" in hub._frigate_seen
    rows.append(_event("new", start_time=2, has_snapshot=False))
    await hub._poll_frigate_once()
    assert len(sent) == 2 and sent[0]["image"] is None and sent[0]["group_key"] == "cam:140_frontdoor"
    assert sent[0]["extra"]["event_id"] == "new"
    # The same detection, now with a picture: replace the push, don't add another alert.
    updates = []
    orig = hub.notify.push_update

    async def push_update(*args, **kw):
        updates.append(kw)
        return await orig(*args, **kw)

    hub.notify.push_update = push_update  # type: ignore[method-assign]
    rows[-1] = _event("new", start_time=2, has_snapshot=True, frame_time=2.5)
    await hub._poll_frigate_once()
    assert len(sent) == 2 and len(updates) == 2
    assert updates[0]["group_key"] == "cam:140_frontdoor"
    assert updates[0]["image"] == "/v1/cameras/events/new/snapshot.jpg?v=2.500"
    assert updates[0]["silent"] is False
    # A second poll of the same picture does not push again.
    await hub._poll_frigate_once()
    assert len(updates) == 2 and len(sent) == 2


async def test_mqtt_event_alerts_once_and_keeps_cooldown_and_quiet_hours(hub, people):
    sent = _front_door(hub, people)
    hub.alerts.set_prefs(people["dylan"]["id"], {"quiet": {"on": True, "from": "00:00", "to": "23:59"}})
    raw = {
        "id": "m1", "camera": "140_frontdoor", "label": "person", "sub_label": None,
        "box": [0, 0, 640, 360], "has_snapshot": False, "has_clip": False, "false_positive": False,
        "start_time": 10, "top_score": 0.9, "entered_zones": ["door"],
    }
    kind, after = parse_frigate_mqtt(json.dumps({"type": "new", "before": {}, "after": raw}).encode())
    assert kind == "new"
    event = mqtt_to_event(after, (1280, 720))
    assert event["data"]["box"] == [0.0, 0.0, 0.5, 0.5] and event["zones"] == ["door"]
    assert mqtt_to_event({"id": "z", "camera": "c", "box": [0.1, 0.2, 0.3, 0.4]})["data"]["box"] == [0.1, 0.2, 0.3, 0.4]
    assert "box" not in mqtt_to_event({"id": "z", "camera": "c", "box": [10, 20, 30, 40]})["data"]
    assert parse_frigate_mqtt(b"not json") is None and parse_frigate_mqtt(b"{}") is None

    await hub._on_frigate_mqtt(json.dumps({"type": "new", "after": raw}).encode())
    assert len(sent) == 2 and {s["silent"] for s in sent} == {True, False}
    by = {hub.db.person(s["person_ids"][0])["name"]: s for s in sent}
    assert by["Dylan"]["silent"] is True and by["Avery"]["silent"] is False
    assert hub.frigate.last_event["140_frontdoor"]["id"] == "m1"
    # The same id again is not a second alert. A different id inside the cooldown isn't either.
    await hub._on_frigate_mqtt(json.dumps({"type": "update", "after": raw}).encode())
    assert len(sent) == 2
    await hub._on_frigate_mqtt(json.dumps({
        "type": "new", "after": {**raw, "id": "m2", "start_time": 11}}).encode())
    assert len(sent) == 2


async def test_mqtt_snapshot_replaces_and_false_positive_waits(hub, people):
    sent = _front_door(hub, people)
    updates = []

    async def push_update(*_a, **kw):
        updates.append(kw)

    hub.notify.push_update = push_update  # type: ignore[method-assign]
    fp = {"id": "fp1", "camera": "140_frontdoor", "label": "person", "has_snapshot": False,
          "false_positive": True, "start_time": 1, "box": [0.2, 0.2, 0.1, 0.2]}
    assert await hub.note_frigate_event(mqtt_to_event(fp, (1280, 720))) == "false"
    assert sent == []
    real = {**fp, "false_positive": False}
    assert await hub.note_frigate_event(mqtt_to_event(real, (1280, 720))) == "new"
    assert len(sent) == 2 and sent[0]["image"] is None
    pictured = {**real, "has_snapshot": True, "frame_time": 5.25}
    assert await hub.note_frigate_event(mqtt_to_event(pictured, (1280, 720))) == "snapshot"
    assert len(sent) == 2 and len(updates) == 2
    assert updates[0]["image"] == "/v1/cameras/events/fp1/snapshot.jpg?v=5.250"
    assert updates[0]["group_key"] == "cam:140_frontdoor"
    assert await hub.note_frigate_event(mqtt_to_event(pictured, (1280, 720))) == "dup"
    assert len(updates) == 2
