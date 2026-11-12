from __future__ import annotations

import base64
import json
import subprocess

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from hub.push import APNs, alert_payload


def wg_pub() -> str:
    raw = X25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def test_vpn_config_adds_peer_and_returns_settings(hub, people, home_client):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    hub.vpn._run = runner
    hub.config.vpn.provider = "wg-cli"
    hub.config.vpn.endpoint = "vpn.example.invalid:51820"
    hub.config.vpn.home_ssids = ["HomeNet"]
    hub.config.vpn.allowed_ips = ["192.168.1.0/24"]
    ph = people["phones"]["Avery"]
    key = wg_pub()
    r = home_client.post("/v1/vpn/config", json={"wg_public_key": key}, headers=ph.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interface"]["addresses"] == ["10.77.0.2/32"]
    assert body["peer"]["endpoint"] == "vpn.example.invalid:51820" and body["home_ssids"] == ["HomeNet"]
    assert ["wg", "set", "wg0", "peer", key, "allowed-ips", "10.77.0.2/32"] in calls
    # The server private key exists only in the vault; the reply carries the public key.
    assert hub.vault.has("wireguard.server_private_key")
    assert body["peer"]["public_key"] != str(hub.vault.get("wireguard.server_private_key"))
    # Revoking removes the peer.
    hub.revoke_device(hub.db.device(ph.device_id))
    assert ["wg", "set", "wg0", "peer", key, "remove"] in calls


def test_vpn_off_answers_503(people, home_client):
    r = home_client.post("/v1/vpn/config", json={"wg_public_key": wg_pub()}, headers=people["phones"]["Avery"].h)
    assert r.status_code == 503


def test_vpn_rejects_bad_key(hub, people, home_client):
    hub.config.vpn.provider = "wg-cli"
    hub.config.vpn.endpoint = "x.invalid:51820"
    hub.vpn._run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    r = home_client.post("/v1/vpn/config", json={"wg_public_key": "not-a-key"}, headers=people["phones"]["Avery"].h)
    assert r.status_code == 400


def test_vpn_traffic_counts_as_away(hub):
    assert hub.is_home("192.168.1.5")
    assert not hub.is_home("10.77.0.5")  # the hub's own tunnel
    assert not hub.is_home("8.8.8.8")


def test_forwarded_for_only_trusted_from_listed_proxy(hub):
    assert hub.client_ip("203.0.113.1", "192.168.1.5") == "203.0.113.1"
    hub._proxies = [__import__("ipaddress").ip_network("172.30.0.0/16")]
    assert hub.client_ip("172.30.0.2", "192.168.1.5, 10.0.0.1") == "192.168.1.5"


def test_lights_passthrough_roles_and_dry_run(hub, people, home_client):
    hub.config.show_player.enabled = True
    hub.config.show_player.url = "http://show.test"
    hub.vault.set("show_player.app_token", "app-token-value")
    theo, avery = people["phones"]["Theo"], people["phones"]["Avery"]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get("http://show.test/status").mock(return_value=httpx.Response(200, json={"playing": True}))
        r = home_client.get("/v1/lights/status", headers=theo.h)
        assert r.status_code == 200 and r.json() == {"playing": True}
        assert route.calls[0].request.headers["X-App-Token"] == "app-token-value"
        # Kids can't stop the show; adults can, but only in practice mode until enabled.
        assert home_client.get("/v1/lights/stop", headers=theo.h).status_code == 403
        r = home_client.get("/v1/lights/stop", headers=avery.h)
        assert r.json()["dry_run"] is True
        stop = mock.get("http://show.test/stop").mock(return_value=httpx.Response(200, json={"ok": True}))
        hub.config.hub.live_actions = ["lights.control"]
        assert home_client.get("/v1/lights/stop", headers=avery.h).json() == {"ok": True}
        assert stop.called


@pytest.mark.asyncio
async def test_apns_request_shape(hub):
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    hub.vault.set("apns.auth_key_p8", pem.decode())
    hub.config.apns.enabled, hub.config.apns.team_id, hub.config.apns.key_id = True, "TEAM123456", "KEY1234567"
    apns = APNs(hub.config.apns, hub.vault)
    with respx.mock() as mock:
        route = mock.post("https://api.sandbox.push.apple.com/3/device/abcd").mock(return_value=httpx.Response(200))
        dead = mock.post("https://api.push.apple.com/3/device/dead").mock(
            return_value=httpx.Response(410, json={"reason": "Unregistered"}))
        res = await apns.send("abcd", alert_payload("Hi", "there", cat="doors"), env="sandbox")
        assert res.ok
        req = route.calls[0].request
        assert req.headers["apns-topic"] == "me.dlewis.christmaslights"
        assert req.headers["authorization"].startswith("bearer ")
        assert json.loads(req.content)["cat"] == "doors"
        assert (await apns.send("dead", {"aps": {}})).token_dead
        assert dead.called
    await apns.close()


@pytest.mark.asyncio
async def test_network_timer_restores(hub):
    hub.config.firewalla.person_rules = {"per_x": ["r1"]}
    hub.config.hub.dry_run = False
    seen = []

    async def resume(rule):
        seen.append(("resume", rule))

    async def pause(rule):
        seen.append(("pause", rule))

    hub.firewalla.resume_rule, hub.firewalla.pause_rule = resume, pause  # type: ignore[method-assign]
    hub.timers.add(0, "network.restore", {"person_id": "per_x", "to": "off"})
    assert await hub.timers.tick() == 1
    assert seen == [("resume", "r1")]


def test_assets_and_etag(hub, people, home_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "house-model-web.json").write_text('{"mats":{},"groups":[],"lines":[]}')
    ph = people["phones"]["Avery"]
    r = home_client.get("/v1/assets/house-model", headers=ph.h)
    assert r.status_code == 200 and r.json()["groups"] == []
    etag = r.headers["etag"]
    assert home_client.get("/v1/assets/house-model", headers={**ph.h, "If-None-Match": etag}).status_code == 304
    assert home_client.get("/v1/assets/..%2Fvault.json", headers=ph.h).status_code == 404


def test_camera_pass_and_player(hub, people, home_client, away_client):
    avery = people["phones"]["Avery"]
    r = home_client.post("/v1/cameras/driveway/pass", headers=avery.h)
    assert r.status_code == 200
    url = r.json()["player_url"]
    page = home_client.get(url)
    assert page.status_code == 200 and "RTCPeerConnection" in page.text
    assert home_client.get("/v1/cameras/front_door/player?" + url.split("?")[1]).status_code == 403
    assert home_client.post("/v1/cameras/driveway/pass", headers=people["phones"]["Theo"].h).status_code == 403
    # Away from home, the same request needs Face ID.
    assert away_client.post("/v1/cameras/driveway/pass", headers=avery.h).status_code == 428


@pytest.mark.asyncio
async def test_refresh_reads_home_assistant(tmp_path, config):
    import secrets as _s

    from hub.db import DB
    from hub.state import Hub
    from hub.vault import Vault

    h = Hub(config, Vault(tmp_path / "v.json", _s.token_bytes(32)), DB(tmp_path / "h.db"))
    h.vault.set("home_assistant.token", "ha-token")
    areas = [{"id": "kitchen", "name": "Kitchen", "floor": "main", "entities": ["light.kitchen"]}]
    with respx.mock() as mock:
        tmpl = mock.post("http://ha.test/api/template")
        tmpl.side_effect = [httpx.Response(200, text=json.dumps(areas)),
                            httpx.Response(200, text=json.dumps([{"id": "main", "name": "Main Floor"}]))]
        states = mock.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"},
             "last_updated": "2026-11-12T01:00:00+00:00"}]))
        await h.home.refresh()
        assert states.calls[0].request.headers["authorization"] == "Bearer ha-token"
    assert h.home.entity_area == {"light.kitchen": "kitchen"}
    from hub.policy import Subject

    snap = h.home.snapshot(Subject(person_id="p", role="adult"))
    assert snap["rooms"][0]["summary"] == "1 light on"
    assert snap["floors"] == [{"id": "main", "name": "Main Floor", "room_ids": ["kitchen"]}]
    assert snap["status"]["level"] == "ok"
    await h.stop()
