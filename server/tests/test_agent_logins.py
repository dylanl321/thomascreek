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
