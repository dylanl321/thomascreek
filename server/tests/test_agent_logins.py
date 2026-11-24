"""Agent logins: Claude on the home network reads the admin pages and approves the cloud worker, nothing else
(hub/agentlogins.py, Nov 23 2026)."""

from __future__ import annotations

import json

from hub import agentlogins, cloudmedia
from hub.cli import main as cli_main

SECRET = "worker-secret-for-tests-only"


def _h(key):
    return {"authorization": f"Bearer {key}"}


def _waiting(home_client):
    return home_client.post("/v1/cloud/join", json={"secret_hash": cloudmedia.secret_hash(SECRET),
                                                    "name": "Cloud box"}).json()


def _log(hub):
    return [dict(r) for r in hub.db.execute("SELECT * FROM activity").fetchall()]


def test_the_key_is_kept_only_as_a_hash(hub):
    row, key = agentlogins.create(hub.db, "claude-mac")
    assert key.startswith(agentlogins.PREFIX) and "hash" not in row
    assert key not in json.dumps(hub.db.kv_get(agentlogins.KV)) and key not in json.dumps(_log(hub))
    assert agentlogins.check(hub.db, key, "admin.read")["name"] == "claude-mac"
    assert agentlogins.check(hub.db, "tcagent_wrong", "admin.read") is None


def test_an_agent_approves_the_waiting_worker_by_its_code(hub, home_client):
    _, key = agentlogins.create(hub.db, "claude-mac")
    made = _waiting(home_client)
    seen = home_client.get("/v1/agent/cloud", headers=_h(key)).json()
    assert [w["code"] for w in seen["waiting"]] == [made["code"]] and seen["status"]["approved"] is False
    assert home_client.post("/v1/agent/cloud/approve", headers=_h(key), json={"code": "ZZZZZZ"}).status_code == 404
    r = home_client.post("/v1/agent/cloud/approve", headers=_h(key), json={"code": made["code"].lower()})
    assert r.status_code == 200, r.text
    assert cloudmedia.status(hub)["approved"] is True
    entry = next(e for e in _log(hub) if e["action"] == "agent.cloud_approve")
    assert entry["source"] == "agent" and json.loads(entry["params"])["agent"] == "claude-mac"


def test_never_from_away_never_without_the_scope_never_once_off(hub, home_client, away_client):
    _, key = agentlogins.create(hub.db, "claude-mac")
    made = _waiting(home_client)
    assert away_client.get("/v1/agent/cloud", headers=_h(key)).status_code == 403
    _, reader = agentlogins.create(hub.db, "reader", scopes=("admin.read",))
    denied = home_client.post("/v1/agent/cloud/approve", headers=_h(reader), json={"code": made["code"]})
    assert denied.status_code == 401
    assert home_client.get("/v1/agent/cloud", headers=_h("nope")).status_code == 401
    row = next(a for a in agentlogins.logins(hub.db) if a["name"] == "claude-mac")
    assert agentlogins.revoke(hub.db, row["id"])
    assert home_client.post("/v1/agent/cloud/approve", headers=_h(key), json={"code": made["code"]}).status_code == 401
    assert cloudmedia.status(hub)["approved"] is False
    assert any(e["action"] == "agent.cloud" and e["outcome"] == "refused" for e in _log(hub))


def test_admin_pages_open_read_only_for_an_agent(hub, home_client):
    _, key = agentlogins.create(hub.db, "claude-mac")
    plain = home_client.get("/admin/cloud")
    page = home_client.get("/admin/cloud", headers=_h(key))
    assert page.status_code == 200 and page.text != plain.text
    assert home_client.get("/admin/security", headers=_h(key)).status_code == 200
    # A form post never takes an agent login.
    made = _waiting(home_client)
    r = home_client.post("/admin/cloud/approve", headers=_h(key), data={"id": made["join_id"], "csrf": "x"})
    assert r.status_code == 401
    assert cloudmedia.status(hub)["approved"] is False
    assert any(e["action"] == "agent.read" and e["target"] == "/admin/cloud" for e in _log(hub))


def test_the_cli_makes_lists_and_turns_off(hub, capsys, monkeypatch):
    monkeypatch.setattr("hub.cli._db", lambda: hub.db)
    try:
        cli_main(["agent-login", "create", "claude-mac"])
    except SystemExit as done:
        assert done.code == 0
    key = capsys.readouterr().out.strip()
    assert agentlogins.check(hub.db, key, "cloud.approve") is not None
    login_id = agentlogins.logins(hub.db)[0]["id"]
    try:
        cli_main(["agent-login", "revoke", login_id])
    except SystemExit:
        pass
    assert agentlogins.check(hub.db, key, "cloud.approve") is None


def test_the_nas_env_adds_a_login_once_and_never_brings_back_one_turned_off(hub):
    key = agentlogins.PREFIX + "from-the-mac-for-tests"
    env = f"claude-mac:{agentlogins._hash(key)},bad:nothex"
    assert len(agentlogins.seed_from_env(hub.db, env)) == 1
    assert agentlogins.seed_from_env(hub.db, env) == []
    row = agentlogins.check(hub.db, key, "cloud.approve")
    assert row and row["name"] == "claude-mac"
    agentlogins.revoke(hub.db, row["id"])
    assert agentlogins.seed_from_env(hub.db, env) == []
    assert agentlogins.check(hub.db, key, "cloud.approve") is None


# -- test.pair: agents let in their own test TVs, emulators and phones (routes/agentpair.py, Nov 24 2026) -------------
def _tv_waiting(home_client):
    from tests.test_tv_api import Key

    body = {"install_id": "tv_emulator_0123456789", "name": "Emulator TV", "model": "sdk_google_atv",
            "app_version": "1.5.0", "public_key": Key().pub}
    r = home_client.post("/v1/tv/pair", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _phone_waiting(home_client):
    from tests.conftest import Phone

    ph = Phone(home_client)
    r = home_client.post("/v1/enroll", json={"device_name": "Pixel emulator", "model": "sdk_gphone64",
                                            "platform": "Android", "public_key": ph.pub(ph.key)})
    assert r.status_code == 200 and r.json()["status"] == "pending", r.text
    return r.json()["device_id"]


def test_an_agent_lets_in_a_waiting_tv_and_it_is_logged(hub, home_client):
    _, key = agentlogins.create(hub.db, "claude-mac")
    pair = _tv_waiting(home_client)
    seen = home_client.get("/v1/agent/pairings", headers=_h(key)).json()
    assert [t["pair_id"] for t in seen["tvs"]] == [pair["pair_id"]]
    r = home_client.post(f"/v1/agent/tv/pair/{pair['pair_id']}/approve", headers=_h(key), json={})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert home_client.get("/v1/agent/pairings", headers=_h(key)).json()["tvs"] == []
    entry = next(e for e in _log(hub) if e["action"] == "agent.tv_pair_approve")
    assert entry["source"] == "agent" and json.loads(entry["params"])["agent"] == "claude-mac"


def test_an_agent_lets_in_a_waiting_phone_only_as_dylans_test_device(hub, home_client, people):
    _, key = agentlogins.create(hub.db, "claude-mac")
    did = _phone_waiting(home_client)
    assert [p["id"] for p in home_client.get("/v1/agent/pairings", headers=_h(key)).json()["phones"]] == [did]
    r = home_client.post(f"/v1/agent/devices/{did}/approve", headers=_h(key))
    assert r.status_code == 200, r.text
    dev = hub.db.device(did)
    admin = hub.db.one("SELECT role FROM people WHERE id=?", [dev["person_id"]])
    assert dev["status"] == "active" and dev["kind"] == "test" and admin["role"] == "admin"
    # Only a waiting device: an active one is never moved.
    assert home_client.post(f"/v1/agent/devices/{did}/approve", headers=_h(key)).status_code == 404
    assert any(e["action"] == "agent.device_approve" for e in _log(hub))


def test_test_pair_needs_its_scope_the_home_network_and_a_live_login(hub, home_client, away_client):
    pair = _tv_waiting(home_client)
    _, cloud_only = agentlogins.create(hub.db, "cloud-only", scopes=("admin.read", "cloud.approve"))
    r = home_client.post(f"/v1/agent/tv/pair/{pair['pair_id']}/approve", headers=_h(cloud_only), json={})
    assert r.status_code == 401
    _, key = agentlogins.create(hub.db, "claude-mac")
    assert away_client.get("/v1/agent/pairings", headers=_h(key)).status_code == 403
    assert home_client.get("/v1/agent/pairings").status_code == 401
    assert any(e["action"] == "agent.pair" and e["outcome"] == "refused" for e in _log(hub))


def test_the_nas_env_gives_an_existing_login_the_new_scope_but_not_one_turned_off(hub):
    key = agentlogins.PREFIX + "older-login-for-tests"
    rows = [{"id": "ag_old", "name": "claude-mac", "hash": agentlogins._hash(key), "scopes": ["admin.read"],
             "created_at": 1.0, "expires_at": None, "last_used_at": None, "revoked_at": None}]
    hub.db.kv_set(agentlogins.KV, rows)
    env = f"claude-mac:{agentlogins._hash(key)}"
    assert agentlogins.seed_from_env(hub.db, env) == []
    assert agentlogins.check(hub.db, key, "test.pair") is not None
    agentlogins.revoke(hub.db, "ag_old")
    agentlogins.seed_from_env(hub.db, env)
    assert agentlogins.check(hub.db, key, "test.pair") is None
