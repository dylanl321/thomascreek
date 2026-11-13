"""Private VPN (PIA): login stays on the hub, phones get a peer for their own key."""

from __future__ import annotations

import json

import httpx
import respx

from hub.vpn_pia import TOKEN_URL, _ca_context

from .test_services import wg_pub

SERVERLIST = json.dumps({"regions": [
    {"id": "us_chicago", "name": "US Chicago", "country": "US", "auto_region": True, "geo": False, "offline": False,
     "servers": {"wg": [{"ip": "198.51.100.10", "cn": "chicago401"}]}},
    {"id": "de-frankfurt", "name": "DE Frankfurt", "country": "DE", "auto_region": True, "geo": False, "offline": False,
     "servers": {"wg": [{"ip": "198.51.100.20", "cn": "frankfurt402"}]}},
    {"id": "us_alaska-pf", "name": "US Alaska", "country": "US", "auto_region": True, "geo": True, "offline": False,
     "servers": {"wg": [{"ip": "198.51.100.30", "cn": "alaska1"}]}},
    {"id": "gone", "name": "Gone", "country": "XX", "offline": True, "servers": {"wg": [{"ip": "1.1.1.1", "cn": "x"}]}},
]}) + "\n\nSIGNATURE"


def _setup(hub, answers=None):
    hub.vault.set("vpn.pia.username", "p1234567")
    hub.vault.set("vpn.pia.password", "hunter2")
    calls = []

    def add_key(ip, cn, token, pubkey):
        calls.append((ip, cn, token, pubkey))
        if answers:
            return answers.pop(0)
        return {"status": "OK", "server_key": wg_pub(), "server_port": 1337, "server_ip": ip,
                "peer_ip": "10.13.1.2", "dns_servers": ["10.0.0.243", "10.0.0.242"]}

    hub.pia._add_key = add_key
    return calls


def _mock(router, tokens=("tok1",)):
    toks = list(tokens)
    router.post(TOKEN_URL).mock(side_effect=lambda req: httpx.Response(200, json={"token": toks.pop(0)}))
    router.get(hub_serverlist()).mock(return_value=httpx.Response(200, text=SERVERLIST))


def hub_serverlist():
    return "https://serverlist.piaservers.net/vpninfo/servers/v6"


def test_private_hidden_until_login_is_in_vault(people, home_client):
    r = home_client.get("/v1/vpn/exits", headers=people["phones"]["Avery"].h)
    assert r.status_code == 200
    private = [e for e in r.json()["exits"] if e["id"] == "private"][0]
    assert private["available"] is False and "regions" not in r.json()
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "wg_public_key": wg_pub()},
                         headers=people["phones"]["Avery"].h)
    assert r.status_code == 503


@respx.mock
def test_private_config_for_adult(hub, people, home_client):
    calls = _setup(hub)
    _mock(respx)
    ph = people["phones"]["Avery"]
    r = home_client.get("/v1/vpn/exits", headers=ph.h)
    body = r.json()
    assert [e["available"] for e in body["exits"] if e["id"] == "private"] == [True]
    ids = {x["id"]: x for x in body["regions"]}
    assert set(ids) == {"us_chicago", "de-frankfurt", "us_alaska-pf"}          # offline one dropped
    assert ids["us_chicago"]["auto"] and not ids["us_alaska-pf"]["auto"]       # virtual location not probed
    assert ids["de-frankfurt"]["probe"] == "198.51.100.20:1337"
    key = wg_pub()
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "region": "de-frankfurt", "wg_public_key": key},
                         headers=ph.h)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["exit"] == "private" and cfg["region"]["id"] == "de-frankfurt"
    assert cfg["interface"] == {"addresses": ["10.13.1.2/32"], "dns": ["10.0.0.243", "10.0.0.242"]}
    assert cfg["peer"]["endpoint"] == "198.51.100.20:1337"
    assert cfg["peer"]["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert "private_key" not in cfg["interface"]
    # The hub registered the phone's public key with its own token; neither the login nor the token is in the reply.
    assert calls == [("198.51.100.20", "frankfurt402", "tok1", key)]
    text = r.text
    assert "hunter2" not in text and "tok1" not in text and "p1234567" not in text
    assert hub.pia.last_ok and hub.pia.last_error is None


@respx.mock
def test_private_not_for_children(hub, people, home_client):
    _setup(hub)
    _mock(respx)
    theo = people["phones"]["Theo"]
    r = home_client.get("/v1/vpn/exits", headers=theo.h)
    assert [e["available"] for e in r.json()["exits"] if e["id"] == "private"] == [False]
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "wg_public_key": wg_pub()}, headers=theo.h)
    assert r.status_code == 403


@respx.mock
def test_refused_token_signs_in_again(hub, people, home_client):
    calls = _setup(hub, answers=[{"status": "ERROR", "message": "Login failed!"}])
    _mock(respx, tokens=("old", "new"))
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "region": "us_chicago", "wg_public_key": wg_pub()},
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert [c[2] for c in calls] == ["old", "new"]


@respx.mock
def test_unknown_region_falls_back_and_bad_key_refused(hub, people, home_client):
    _setup(hub)
    _mock(respx)
    ph = people["phones"]["Dylan"]
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "region": "nowhere", "wg_public_key": wg_pub()},
                         headers=ph.h)
    assert r.status_code == 200 and r.json()["region"]["id"] == "us_chicago"
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "wg_public_key": "nope"}, headers=ph.h)
    assert r.status_code == 400


@respx.mock
def test_login_refused_is_reported(hub, people, home_client):
    _setup(hub)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
    respx.get(hub_serverlist()).mock(return_value=httpx.Response(200, text=SERVERLIST))
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "wg_public_key": wg_pub()},
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 502
    assert "login" in hub.pia.status()


def test_pia_root_loads():
    ctx = _ca_context()
    assert any("Private Internet Access" in str(c.get("subject")) for c in ctx.get_ca_certs())


@respx.mock
def test_admin_page_login_wins_over_1password(hub, people, home_client):
    _setup(hub)  # 1Password copy: p1234567 / hunter2
    hub.vault.set("vpn.pia.admin_username", "p7654321")
    hub.vault.set("vpn.pia.admin_password", "typed-in")
    sent = []

    def token(req):
        sent.append(req.content.decode())
        return httpx.Response(200, json={"token": "tok-admin"})

    respx.post(TOKEN_URL).mock(side_effect=token)
    respx.get(hub_serverlist()).mock(return_value=httpx.Response(200, text=SERVERLIST))
    r = home_client.post("/v1/vpn/config", json={"exit": "private", "wg_public_key": wg_pub()},
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert "p7654321" in sent[0] and "typed-in" in sent[0] and "hunter2" not in sent[0]
    assert "admin page" in hub.pia.status()
    # Only half of the admin pair saved: fall back to 1Password.
    hub.vault.delete("vpn.pia.admin_password")
    assert hub.pia.login_source == "1Password"


def test_admin_page_alone_is_enough(hub, people, home_client):
    hub.vault.set("vpn.pia.admin_username", "p7654321")
    hub.vault.set("vpn.pia.admin_password", "typed-in")
    assert hub.pia.configured and hub.pia.login_source == "admin page"
