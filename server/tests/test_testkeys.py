"""Test keys: Simulator builds join as Dylan without approval; real phones are unchanged."""

from __future__ import annotations

import re

from hub import testkeys

from .conftest import Phone


def _sim(client, key, simulator=True, name="Thomas Creek QA"):
    ph = Phone(client, name)
    r = client.post("/v1/enroll", json={
        "device_name": name, "model": "arm64", "platform": "ios", "app_version": "1.0 (21)",
        "public_key": ph.pub(ph.key), "secure_public_key": ph.pub(ph.secure), "simulator": simulator, "test_key": key,
    })
    if r.status_code == 200:
        ph.device_id = r.json()["device_id"]
    return ph, r


def test_simulator_with_key_joins_as_dylan(hub, people, home_client):
    _, key = testkeys.create(hub, people["dylan"]["id"], "Mac", created_by=people["dylan"]["id"])
    inbox_before = hub.db.one("SELECT COUNT(*) AS n FROM inbox")["n"]
    ph, r = _sim(home_client, key)
    assert r.status_code == 200 and r.json()["status"] == "active"
    me = home_client.get("/v1/me", headers=(ph.sign_in() and ph.h)).json()
    assert me["role"] == "admin" and me["person"]["name"] == "Dylan"
    # No approval card for Dylan.
    assert hub.db.one("SELECT COUNT(*) AS n FROM inbox")["n"] == inbox_before
    # Tagged as a test device in People.
    dylan = people["phones"]["Dylan"]
    devs = next(p for p in home_client.get("/v1/people", headers=dylan.h).json()["people"] if p["name"] == "Dylan")["devices"]
    assert [d["test"] for d in devs] == [False, True]
    # No VPN client from the pool.
    r = home_client.post("/v1/vpn/config", json={"wg_public_key": None}, headers=ph.h)
    assert r.status_code == 403 and r.json()["error"] == "test_device"


def test_key_rules(hub, people, home_client, away_client):
    _, key = testkeys.create(hub, people["dylan"]["id"], "Mac", created_by=None)
    # Wrong key, or the key from something that isn't a simulator: refused, nothing created.
    for k, sim in (("tctest_nope", True), (key, False)):
        _, r = _sim(home_client, k, simulator=sim)
        assert r.status_code == 403 and r.json()["error"] == "bad_test_key"
    # Still home network only.
    _, r = _sim(away_client, key)
    assert r.status_code == 403 and r.json()["error"] == "not_home"
    # A real phone without a key still waits for Dylan.
    assert Phone(home_client, "New iPhone").enroll()["status"] == "pending"
    # Expired key.
    row = hub.db.one("SELECT * FROM test_keys")
    hub.db.update("test_keys", "id", row["id"], {"expires_at": 1.0})
    _, r = _sim(home_client, key)
    assert r.status_code == 403


def test_pending_simulator_is_upgraded(hub, people, home_client):
    ph = Phone(home_client, "Sim")
    assert ph.enroll()["status"] == "pending"
    _, key = testkeys.create(hub, people["dylan"]["id"], "Mac", created_by=None)
    r = home_client.post("/v1/enroll", json={"device_name": "Sim", "public_key": ph.pub(ph.key), "simulator": True,
                                             "test_key": key})
    assert r.json() == {"device_id": ph.device_id, "status": "active"}
    assert hub.db.device(ph.device_id)["kind"] == "test"


def test_admin_page_make_and_turn_off_key(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    url = dylan.secure_post("/v1/admin/web-link", {}).json()["url"]
    home_client.get(url, follow_redirects=False)
    page = home_client.get("/admin").text
    assert "Test simulators" in page
    csrf = re.search(r"name=csrf value=(\w+)", page).group(1)
    r = home_client.post("/admin/test-key", data={"csrf": csrf, "label": "Dylan's Mac"})
    key = re.search(r"(tctest_[\w-]+)", r.text).group(1)
    assert "~/.thomascreek/test-key" in r.text
    ph, r2 = _sim(home_client, key)
    assert r2.status_code == 200
    page = home_client.get("/admin").text
    assert key not in page, "the key is shown only once"
    assert "1 simulator" in page and "Thomas Creek QA" in page
    kid = hub.db.one("SELECT id FROM test_keys")["id"]
    r = home_client.post("/admin/test-key-revoke", data={"csrf": csrf, "id": kid})
    assert "removed 1 simulator" in r.text
    assert hub.db.device(ph.device_id)["status"] == "revoked"
    _, r3 = _sim(home_client, key, name="Another")
    assert r3.status_code == 403
