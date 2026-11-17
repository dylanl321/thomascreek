"""The hub's MCP server: AI keys, the same tools as Ask, Face ID approvals on the iPhone, OAuth sign-in."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from hub import aikeys


def rpc(client, key, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg, headers={"Authorization": f"Bearer {key}"} if key else {})


def call(client, key, name, args):
    r = rpc(client, key, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res, json.loads(res["content"][0]["text"]) if res["content"][0]["type"] == "text" else None


@pytest.fixture
def keys(hub, people):
    hub.config.hub.dry_run = False
    out = {}
    for name in ("Dylan", "Avery", "Theo"):
        _, out[name] = aikeys.create(hub, people[name.lower()]["id"], f"Claude for {name}", created_by=people["dylan"]["id"])
    return out


def test_needs_a_key(hub, home_client, people):
    r = rpc(home_client, None, "initialize")
    assert r.status_code == 401 and "oauth-protected-resource" in r.headers["www-authenticate"]
    assert rpc(home_client, "tcai_nope", "initialize").status_code == 401
    assert home_client.get("/mcp").status_code == 405


def test_initialize_and_tools(hub, home_client, keys):
    r = rpc(home_client, keys["Avery"], "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                       "clientInfo": {"name": "t", "version": "1"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and "tools" in res["capabilities"]
    assert "Avery (adult)" in res["instructions"]
    note = home_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                            headers={"Authorization": f"Bearer {keys['Avery']}"})
    assert note.status_code == 202
    tools = {t["name"]: t for t in rpc(home_client, keys["Avery"], "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"house", "look", "act", "hub_get", "hub_change", "approval"}
    assert tools["act"]["inputSchema"]["required"] == ["action", "summary"]
    assert rpc(home_client, keys["Avery"], "nope").json()["error"]["code"] == -32601


def test_adult_turns_off_a_light_as_themselves(hub, home_client, keys):
    res, out = call(home_client, keys["Avery"], "house", {"query": "kitchen"})
    assert [d["id"] for d in out["devices"]] == ["light.kitchen"]
    res, out = call(home_client, keys["Avery"], "act", {"action": "light.set", "target": "light.kitchen",
                                                        "params": {"on": False}, "summary": "Kitchen off"})
    assert out["ok"] is True and res["isError"] is False
    assert ("light", "turn_off", {"entity_id": "light.kitchen"}) in hub.calls
    row = hub.db.one("SELECT source, device_id FROM activity WHERE action='light.set' ORDER BY rowid DESC LIMIT 1")
    assert row["source"] == "mcp" and hub.db.device(row["device_id"])["kind"] == "ai"


def test_child_key_has_a_childs_limits(hub, home_client, keys):
    res, out = call(home_client, keys["Theo"], "act", {"action": "garage.set", "target": "cover.large_garage",
                                                       "params": {"open": True}, "summary": "Open garage"})
    assert res["isError"] and out["error"]
    _, cams = call(home_client, keys["Theo"], "hub_get", {"path": "/v1/cameras"})
    assert cams["error"]
    assert hub.calls == []
    assert hub.db.all("SELECT * FROM requests WHERE kind='ai'") == []


def test_lock_waits_for_face_id_on_the_iphone(hub, home_client, people, keys):
    sent = []

    async def fake_send(**kw):
        sent.append(kw)
    hub.notify.send = fake_send
    _, out = call(home_client, keys["Dylan"], "act", {"action": "lock.set", "target": "lock.front_door",
                                                      "params": {"locked": False}, "summary": "Unlock the front door"})
    assert out["waiting"] is True and hub.calls == []
    rid = out["approval_id"]
    assert sent[0]["person_ids"] == [people["dylan"]["id"]] and sent[0]["push_category"] == "REQUEST"
    assert call(home_client, keys["Dylan"], "approval", {"id": rid})[1]["status"] == "waiting"
    # Someone else's approval is invisible and can't be answered by them
    assert call(home_client, keys["Avery"], "approval", {"id": rid})[1]["error"]
    avery = people["phones"]["Avery"]
    assert avery.secure_post(f"/v1/requests/{rid}/approve", {}).status_code == 403
    listed = home_client.get("/v1/requests", headers=avery.h).json()["items"]
    assert rid not in [i["id"] for i in listed]
    # Without Face ID nothing runs
    dylan = people["phones"]["Dylan"]
    assert home_client.post(f"/v1/requests/{rid}/approve", headers=dylan.h).status_code == 428
    assert hub.calls == []
    r = dylan.secure_post(f"/v1/requests/{rid}/approve", {})
    assert r.status_code == 200, r.text
    assert ("lock", "unlock", {"entity_id": "lock.front_door"}) in hub.calls
    st = call(home_client, keys["Dylan"], "approval", {"id": rid})[1]
    assert st["status"] == "done"
    assert dylan.secure_post(f"/v1/requests/{rid}/approve", {}).status_code == 409


def test_declined_and_expired(hub, home_client, people, keys):
    async def fake_send(**kw):
        return None
    hub.notify.send = fake_send
    args = {"action": "lock.set", "target": "lock.front_door", "params": {"locked": False}, "summary": "Unlock"}
    rid = call(home_client, keys["Dylan"], "act", args)[1]["approval_id"]
    dylan = people["phones"]["Dylan"]
    assert home_client.post(f"/v1/requests/{rid}/deny", headers=dylan.h).status_code == 200
    assert call(home_client, keys["Dylan"], "approval", {"id": rid})[1]["status"] == "declined"
    rid2 = call(home_client, keys["Dylan"], "act", args)[1]["approval_id"]
    hub.db.update("requests", "id", rid2, {"created_at": 1.0})
    assert dylan.secure_post(f"/v1/requests/{rid2}/approve", {}).status_code == 409
    assert call(home_client, keys["Dylan"], "approval", {"id": rid2})[1]["status"] == "expired"
    assert hub.calls == []


def test_deletes_wait_for_the_iphone(hub, home_client, people, keys):
    async def fake_send(**kw):
        return None
    hub.notify.send = fake_send
    _, out = call(home_client, keys["Avery"], "hub_change", {"method": "DELETE", "path": "/v1/family/items/swim",
                                                             "summary": "Remove swim lesson"})
    assert out["waiting"] is True
    r = hub.db.one("SELECT * FROM requests WHERE id=?", [out["approval_id"]])
    assert r["payload"]["method"] == "DELETE" and r["payload"]["path"] == "/v1/family/items/swim"


def test_away_and_turned_off(hub, home_client, away_client, people, keys):
    hub.config.mcp.away = False
    r = rpc(away_client, keys["Avery"], "initialize")
    assert r.status_code == 403 and r.json()["error"] == "home_only"
    hub.config.mcp.away = True
    assert rpc(away_client, keys["Avery"], "initialize").status_code == 200
    row = aikeys.keys(hub)[1]
    assert aikeys.revoke(hub, row["id"], by=None)
    assert rpc(home_client, keys["Avery"], "initialize").status_code == 401
    hub.config.mcp.enabled = False
    assert rpc(home_client, keys["Dylan"], "initialize").status_code == 503


def test_rate_limit(hub, home_client, keys):
    hub.config.mcp.calls_per_minute = 3
    codes = [rpc(home_client, keys["Avery"], "ping").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# -- OAuth -------------------------------------------------------------------
def _pkce():
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def test_only_claude_apps_can_register(hub, away_client):
    assert away_client.get("/.well-known/oauth-authorization-server").json()["code_challenge_methods_supported"] == ["S256"]
    assert away_client.get("/.well-known/oauth-protected-resource").json()["resource"].endswith("/mcp")
    r = away_client.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert r.status_code == 400
    r = away_client.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                                  "client_name": "Claude"})
    assert r.status_code == 201 and r.json()["client_id"].startswith("tcmcp_")
    assert away_client.post("/oauth/register", json={"redirect_uris": ["http://localhost:33418/callback"]}).status_code == 201


def _authorize(client, redirect="https://claude.ai/api/mcp/auth_callback"):
    cid = client.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": "Claude"}).json()["client_id"]
    verifier, challenge = _pkce()
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": redirect, "state": "xyz",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert page.status_code == 200
    code = re.search(r"class=code>([A-Z0-9]{4})-([A-Z0-9]{4})<", page.text)
    pid = re.search(r"const id='([^']+)'", page.text).group(1)
    return cid, verifier, code.group(1) + code.group(2), pid


def test_oauth_sign_in_with_the_app(hub, home_client, away_client, people):
    hub.config.hub.dry_run = False
    cid, verifier, code, pid = _authorize(away_client)
    assert away_client.get("/oauth/poll", params={"id": pid}).json() == {"waiting": True}
    # A child can't connect an AI app
    theo = people["phones"]["Theo"]
    assert theo.secure_post("/v1/admin/web-approve", {"code": code}).status_code == 403
    avery = people["phones"]["Avery"]
    r = avery.secure_post("/v1/admin/web-approve", {"code": code})
    assert r.status_code == 200 and r.json()["connected"] == "Claude"
    back = away_client.get("/oauth/poll", params={"id": pid}).json()["redirect"]
    u = urlsplit(back)
    q = parse_qs(u.query)
    assert back.startswith("https://claude.ai/api/mcp/auth_callback?") and q["state"] == ["xyz"]
    form = {"grant_type": "authorization_code", "code": q["code"][0], "client_id": cid,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "code_verifier": "wrong"}
    assert away_client.post("/oauth/token", data=form).status_code == 400
    # The code is single-use, even after a failed try
    assert away_client.post("/oauth/token", data={**form, "code_verifier": verifier}).status_code == 400

    cid, verifier, code, pid = _authorize(away_client)
    assert avery.secure_post("/v1/admin/web-approve", {"code": code}).status_code == 200
    q = parse_qs(urlsplit(away_client.get("/oauth/poll", params={"id": pid}).json()["redirect"]).query)
    tok = away_client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "client_id": cid,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    key = tok.json()["access_token"]
    init = rpc(away_client, key, "initialize").json()["result"]
    assert "Avery (adult)" in init["instructions"]
    row = [k for k in aikeys.keys(hub) if k["person_id"] == people["avery"]["id"]][0]
    assert row["label"] == "Claude"


def test_admin_web_code_still_signs_in_the_page(hub, home_client, people):
    # The page code path is unchanged for codes that aren't AI sign-ins
    r = people["phones"]["Dylan"].secure_post("/v1/admin/web-approve", {"code": "ZZZZZZZZ"})
    assert r.status_code == 404
