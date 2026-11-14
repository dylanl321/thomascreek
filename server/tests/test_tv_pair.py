"""A new TV asks to join from the sofa. A parent allows it once."""

from __future__ import annotations

import time

from hub.crypto import normalize_public_key
from hub.routes.home import shape_event

from tests.test_tv_api import Key

INSTALL = "tv_office_0123456789"


def _body(key: Key, room: str = "Living Room") -> dict:
    return {"install_id": INSTALL, "name": "Living room TV", "model": "onn 4K", "app_version": "1.5.0",
            "suggested_room": room, "public_key": key.pub}


def test_a_new_tv_waits_for_one_tap_and_then_gets_its_token(hub, home_client, away_client, people):
    dylan = people["phones"]["Dylan"]
    subject = hub.subject_for(hub.db.device(dylan.device_id), True)
    phones = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), subject.person_id)
    key = Key()
    first = home_client.post("/v1/tv/pair", json=_body(key))
    assert first.status_code == 200, first.text
    pending = first.json()
    assert pending["status"] == "pending" and pending["pair_id"].startswith("pair_")
    assert pending["poll_after_s"] == 2
    assert phones.queue.get_nowait()["type"] == "tv_pair"
    again = home_client.post("/v1/tv/pair", json=_body(key))
    assert again.json()["pair_id"] == pending["pair_id"]
    while not phones.queue.empty():
        assert phones.queue.get_nowait()["type"] != "tv_pair"

    listed = dylan.client.get("/v1/tv/pair", headers=dylan.h).json()["pending"]
    assert listed[0]["suggested_room"] == "living_room" and listed[0]["suggested_room_name"] == "Living Room"
    assert people["phones"]["Avery"].client.get("/v1/tv/pair", headers=people["phones"]["Avery"].h).status_code == 403
    assert away_client.post("/v1/tv/pair", json=_body(key)).status_code == 403

    waiting = home_client.get(f"/v1/tv/pair/{pending['pair_id']}").json()
    assert waiting["status"] == "pending"
    approved = dylan.client.post(f"/v1/tv/pair/{pending['pair_id']}/approve", headers=dylan.h, json={})
    assert approved.json() == {"ok": True, "status": "approved"}
    ready = home_client.get(f"/v1/tv/pair/{pending['pair_id']}").json()
    assert ready["status"] == "approved" and ready["token_type"] == "bearer"
    assert ready["room"] == "living_room" and ready["config"]["room"] == "living_room"
    assert ready["config"]["playlists"] == [] or "password" in str(ready["config"]["playlists"])
    assert ready["tv"]["id"] == INSTALL

    wiped = Key()
    back = home_client.post("/v1/tv/pair", json=_body(wiped)).json()
    assert back["status"] == "approved" and "pair_id" not in back
    assert back["room"] == "living_room"
    assert hub.tv_app.tvs()[INSTALL]["public_key"] == normalize_public_key(wiped.pub)
    assert hub.tv_app.tvs()[INSTALL]["name"] == "Living room TV"


def test_deny_and_expiry_are_quiet(hub, home_client, people):
    dylan = people["phones"]["Dylan"]
    key = Key()
    pair_id = home_client.post("/v1/tv/pair", json={**_body(key), "install_id": "tv_bed_0123456789ab"}).json()["pair_id"]
    denied = dylan.client.post(f"/v1/tv/pair/{pair_id}/deny", headers=dylan.h)
    assert denied.json() == {"ok": True, "status": "denied"}
    assert home_client.get(f"/v1/tv/pair/{pair_id}").json() == {"status": "denied"}
    assert dylan.client.post(f"/v1/tv/pair/{pair_id}/approve", headers=dylan.h, json={}).json()["status"] == "expired"

    other = home_client.post("/v1/tv/pair", json={**_body(Key()), "install_id": "tv_loft_0123456789ab"}).json()
    rows = hub.db.kv_get("tv.pair")
    for row in rows:
        if row["pair_id"] == other["pair_id"]:
            row["expires_at"] = time.time() - 5
    hub.db.kv_set("tv.pair", rows)
    assert home_client.get(f"/v1/tv/pair/{other['pair_id']}").json() == {"status": "expired"}
    assert dylan.client.post(f"/v1/tv/pair/{other['pair_id']}/deny", headers=dylan.h).json() == {"ok": False, "status": "expired"}


def test_pairing_is_rate_limited(home_client):
    key = Key()
    last = None
    for i in range(9):
        last = home_client.post("/v1/tv/pair", json={**_body(key), "install_id": f"tv_rate_{i:02d}_0123456789"})
    assert last is not None and last.status_code == 429
    assert last.json()["message"] == "Wait a moment and try again."
