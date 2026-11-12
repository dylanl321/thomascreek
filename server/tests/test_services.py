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
        assert route.calls[0].request.headers["Authorization"] == "Bearer app-token-value"
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
        assert req.headers["apns-topic"] == "me.dlewis.thomascreek"
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


def test_lights_page_cookie_reaches_absolute_paths(hub, people, home_client):
    hub.config.show_player.enabled = True
    hub.config.show_player.url = "http://show.test"
    hub.vault.set("show_player.app_token", "app-token-value")
    theo = people["phones"]["Theo"]
    # Without the page cookie, root paths are not exposed at all.
    assert home_client.get("/layout.json").status_code == 404
    # The cookie never works as a bearer for the rest of the API.
    home_client.cookies.set("tc_page", theo.token)
    assert home_client.get("/v1/home").status_code == 401
    home_client.cookies.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://show.test/house").mock(return_value=httpx.Response(200, text="<html>"))
        layout = mock.get("http://show.test/layout.json").mock(return_value=httpx.Response(200, json={"m": 1}))
        r = home_client.get(f"/v1/lights/house?access_token={theo.token}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/v1/lights/house"
        assert "Path=/" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
        home_client.cookies.set("tc_page", theo.token)
        assert home_client.get("/layout.json").json() == {"m": 1}
        assert layout.calls[0].request.headers["Authorization"] == "Bearer app-token-value"
        # WLED control stays admin-only.
        assert home_client.get("/wled/api/info").status_code == 403


# -- 1Password sync ---------------------------------------------------------

class _F:
    def __init__(self, title, value, field_type="CONCEALED"):
        self.title, self.value, self.field_type = title, value, field_type


class _Item:
    def __init__(self, id, title, fields=(), document=None):
        self.id, self.title, self.fields, self.document = id, title, list(fields), document


class _FakeOP:
    def __init__(self, items, refs=None):
        self._items = {i.id: i for i in items}
        outer = self

        class Vaults:
            async def list(self):
                return [type("V", (), {"id": "v1", "title": "Thomas Creek Hub"})()]

        class Files:
            async def read(self, vid, iid, attr):
                return b"-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n"

        class Items:
            files = Files()

            async def list(self, vid):
                return list(outer._items.values())

            async def get(self, vid, iid):
                return outer._items[iid]

        class Secrets:
            async def resolve(self, ref):
                return (refs or {})[ref]

        self.vaults, self.items, self.secrets = Vaults(), Items(), Secrets()


async def test_onepassword_sync_copies_named_items(tmp_path):
    from hub.config import OnePasswordConfig
    from hub.onepassword import OnePasswordSync
    from hub.vault import Vault, generate_master_key, load_master_key

    kf = tmp_path / "k"
    kf.write_text(generate_master_key())
    v = Vault(tmp_path / "vault.json", load_master_key(kf))
    v.set("onepassword.service_account_token", "ops_x")
    v.set("plex.token", "same")
    cfg = OnePasswordConfig(enabled=True, refs={"mealie.token": "op://Other/Mealie/credential"})
    s = OnePasswordSync(cfg, v)
    fake = _FakeOP(
        [
            _Item("1", "home_assistant.token", [_F("username", "hub", "TEXT"), _F("credential", "  ha-secret \n")]),
            _Item("2", "plex.token", [_F("password", "same")]),
            _Item("3", "apns.auth_key_p8", document=object()),
            _Item("4", "Some Login", [_F("password", "nope")]),
            _Item("5", "frigate.password", [_F("notes", "x", "TEXT")]),
            _Item("6", "onepassword.service_account_token", [_F("credential", "evil")]),
        ],
        refs={"op://Other/Mealie/credential": "mealie-secret"},
    )

    async def fake_client():
        return fake

    s._client = fake_client
    r = await s.sync()
    assert v.get("home_assistant.token") == "ha-secret"
    assert v.get("apns.auth_key_p8").startswith("-----BEGIN PRIVATE KEY-----\nABC")
    assert v.get("mealie.token") == "mealie-secret"
    assert v.get("onepassword.service_account_token") == "ops_x"
    assert not v.has("Some Login")
    assert sorted(r.stored) == ["apns.auth_key_p8", "home_assistant.token", "mealie.token"]
    assert r.unchanged == ["plex.token"]
    assert set(r.errors) == {"frigate.password", "onepassword.service_account_token"}


def test_normalize_pem_rebuilds_lines():
    from hub.push import normalize_pem

    flat = "-----BEGIN PRIVATE KEY----- " + "A" * 100 + " -----END PRIVATE KEY-----"
    out = normalize_pem(flat)
    assert out.splitlines()[1] == "A" * 64 and out.splitlines()[2] == "A" * 36
    assert out.endswith("-----END PRIVATE KEY-----\n")


async def test_avahi_publish_sends_service(monkeypatch):
    import dbus_fast.aio as aio
    from dbus_fast import MessageType

    from hub import bonjour

    sent = []

    class Reply:
        def __init__(self, body):
            self.message_type, self.body = MessageType.METHOD_RETURN, body

    class FakeBus:
        def __init__(self, **kw):
            self.kw = kw

        async def connect(self):
            return self

        async def call(self, msg):
            sent.append((msg.member, msg.signature, list(msg.body)))
            return Reply(["/Client1/EntryGroup1"] if msg.member == "EntryGroupNew" else [])

        def disconnect(self):
            sent.append(("disconnect", "", []))

    monkeypatch.setattr(aio, "MessageBus", FakeBus)
    pub = await bonjour._Avahi.publish("Thomas Creek Hub", 8443, {"api": "v1", "pin": "ab"})
    await pub.async_close()
    members = [m for m, _, _ in sent]
    assert members == ["EntryGroupNew", "AddService", "Commit", "Free", "disconnect"]
    _, sig, body = sent[1]
    assert sig == "iiussssqaay"
    assert body[3:5] == ["Thomas Creek Hub", "_thomascreek._tcp"] and body[7] == 8443
    assert b"pin=ab" in body[8]


def test_camera_mjpeg_fallback_through_ha(hub, people, home_client):
    import respx
    from httpx import Response as R

    avery = people["phones"]["Avery"]
    url = home_client.post("/v1/cameras/driveway/pass", headers=avery.h).json()["player_url"]
    page = home_client.get(url)
    assert "if (!false) return mjpeg()" in page.text  # Frigate not set up: MJPEG straight away
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://ha.test/api/camera_proxy_stream/camera.driveway").mock(
            return_value=R(200, content=b"--frame\r\nJPEGDATA", headers={"content-type": "multipart/x-mixed-replace;boundary=frame"}))
        r = home_client.get(url.replace("/player?", "/mjpeg?"))
    assert r.status_code == 200 and r.content.startswith(b"--frame")
    assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert home_client.get("/v1/cameras/driveway/mjpeg?pass=bad").status_code == 401
