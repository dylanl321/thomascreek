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
    # The proxy appends the address it saw; anything left of it came from the caller.
    assert hub.client_ip("172.30.0.2", "203.0.113.7") == "203.0.113.7"
    assert hub.client_ip("172.30.0.2", "192.168.1.5, 203.0.113.7") == "203.0.113.7"
    assert hub.client_ip("172.30.0.2", "203.0.113.7, 172.30.0.9") == "203.0.113.7"
    assert hub.client_ip("172.30.0.2", "not-an-ip") is None


def test_forged_forwarded_for_cannot_claim_home(hub):
    import ipaddress
    from starlette.testclient import TestClient
    from hub.main import build_app

    hub._proxies = [ipaddress.ip_network("172.20.0.0/16")]
    proxied = TestClient(build_app(hub), base_url="https://testserver", client=("172.20.0.5", 40000))
    forged = {"X-Forwarded-For": "192.168.1.50, 203.0.113.9"}
    r = proxied.post("/v1/enroll", json={"public_key": "x", "device_name": "x", "model": "x"}, headers=forged)
    assert r.status_code == 403 and r.json()["error"] == "not_home"
    assert proxied.get("/admin/status.json", headers=forged).status_code == 403
    # A real home caller through the same proxy still counts as home.
    assert hub.is_home(hub.client_ip("172.20.0.5", "192.168.1.50"))


def test_away_sign_in_is_rate_limited(hub):
    import ipaddress
    from starlette.testclient import TestClient
    from hub.main import build_app

    hub._proxies = [ipaddress.ip_network("172.20.0.0/16")]
    hub.config.hub.away_auth_per_minute = 5
    proxied = TestClient(build_app(hub), base_url="https://testserver", client=("172.20.0.5", 40000))
    codes = [proxied.get("/v1/ping", headers={"X-Forwarded-For": "203.0.113.9"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    # Another caller, and anyone at home, is unaffected.
    assert proxied.get("/v1/ping", headers={"X-Forwarded-For": "198.51.100.4"}).status_code == 200
    assert all(proxied.get("/v1/ping", headers={"X-Forwarded-For": "192.168.1.50"}).status_code == 200 for _ in range(8))


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


@pytest.mark.asyncio
async def test_frigate_has_camera_uses_frigate_names():
    from hub.adapters.cameras import Frigate
    from hub.config import FrigateConfig

    cfg = FrigateConfig(enabled=True, url="http://frigate.test:5000")
    f = Frigate(cfg, None)
    with respx.mock() as mock:
        route = mock.get("http://frigate.test:5000/api/config").mock(
            return_value=httpx.Response(200, json={"cameras": {"driveway": {}, "creek": {}}}))
        assert await f.has_camera("driveway")
        assert not await f.has_camera("doorbell")  # a Home Assistant-only camera
        assert route.call_count == 1  # names are cached
    await f.close()
    off = Frigate(FrigateConfig(enabled=False, url="http://frigate.test:5000"), None)
    assert not await off.has_camera("driveway")


def _client_conf(n: int) -> str:
    priv = base64.b64encode(X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode()
    return (f"[Interface]\nPrivateKey = {priv}\nAddress = 10.11.12.{n + 1}/32\nDNS = 10.11.12.1\n\n"
            f"[Peer]\nPublicKey = {wg_pub()}\nAllowedIPs = 0.0.0.0/0\nEndpoint = home.example.invalid:51820\n"
            "PersistentKeepalive = 25\n")


def test_vpn_pool_hands_out_firewalla_clients(hub, people, home_client, tmp_path):
    from hub.dropbox import import_dropped

    hub.config.vpn.provider = "pool"
    hub.config.vpn.allowed_ips = ["10.0.0.0/22"]
    r = home_client.post("/v1/vpn/config", json={}, headers=people["phones"]["Avery"].h)
    assert r.status_code == 503  # nothing in the pool yet
    drop = tmp_path / "import"
    drop.mkdir()
    for n in (1, 2):
        (drop / f"vpn.pool.{n}.conf").write_text(_client_conf(n))
    assert import_dropped(hub.vault, drop) == ["vpn.pool.1", "vpn.pool.2"]

    avery, theo = people["phones"]["Avery"], people["phones"]["Theo"]
    a = home_client.post("/v1/vpn/config", json={}, headers=avery.h).json()
    assert a["interface"]["addresses"] == ["10.11.12.2/32"] and a["interface"]["private_key"]
    assert a["peer"]["endpoint"] == "home.example.invalid:51820"
    assert a["peer"]["allowed_ips"] == ["10.0.0.0/22", "10.11.12.1/32"]
    # Asking again returns the same client; a second phone gets the other one.
    assert home_client.post("/v1/vpn/config", json={}, headers=avery.h).json() == a
    t = home_client.post("/v1/vpn/config", json={}, headers=theo.h).json()
    assert t["interface"]["addresses"] == ["10.11.12.3/32"]
    assert not hub.is_home("10.11.12.2")
    # Pool used up.
    others = [p for name, p in people["phones"].items() if name not in ("Avery", "Theo")]
    if others:
        assert home_client.post("/v1/vpn/config", json={}, headers=others[0].h).status_code == 400
    # Removing a phone retires its client: the old phone still has that key.
    hub.revoke_device(hub.db.device(avery.device_id))
    assert not hub.vault.has("vpn.pool.1") and hub.vault.has("vpn.pool.2")
    status = {c["name"]: c["device_id"] for c in hub.vpn.pool_status()}
    assert status == {"vpn.pool.2": theo.device_id}


def test_vpn_pool_keeps_named_clients_for_their_person(hub, people, home_client):
    hub.config.vpn.provider = "pool"
    hub.vault.set("vpn.pool.theo", _client_conf(7))
    hub.vault.set("vpn.pool.3", _client_conf(3).replace("[Interface]\n", "[Interface]\nMTU = 1412\n"))
    a = home_client.post("/v1/vpn/config", json={}, headers=people["phones"]["Avery"].h).json()
    assert a["interface"]["addresses"] == ["10.11.12.4/32"] and a["interface"]["mtu"] == 1412
    t = home_client.post("/v1/vpn/config", json={}, headers=people["phones"]["Theo"].h).json()
    assert t["interface"]["addresses"] == ["10.11.12.8/32"]
    # Nobody else may take Theo's client.
    assert home_client.post("/v1/vpn/config", json={}, headers=people["phones"]["Dylan"].h).status_code == 400


def test_vpn_pool_admin_add_and_list(hub, people, home_client):
    hub.config.vpn.provider = "pool"
    dylan = people["phones"]["Dylan"]
    assert home_client.get("/v1/vpn/pool", headers=people["phones"]["Theo"].h).status_code == 403
    r = dylan.secure_post("/v1/vpn/pool", {"config": _client_conf(5)})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "vpn.pool.1"
    assert dylan.secure_post("/v1/vpn/pool", {"config": "[Interface]\n"}).status_code == 400
    listed = home_client.get("/v1/vpn/pool", headers=dylan.h).json()
    assert listed["clients"][0]["address"] == "10.11.12.6/32" and "private" not in str(listed)


def test_pipeline_sections_and_partial_failure(hub, people, home_client):
    for cfg, url in ((hub.config.sonarr, "http://sonarr.test"), (hub.config.radarr, "http://radarr.test"),
                     (hub.config.jellyseerr, "http://seerr.test")):
        cfg.enabled, cfg.url = True, url
    hub.vault.set("sonarr.api_key", "k")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://sonarr.test/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": [
            {"title": "Show S01E02", "size": 100, "sizeleft": 25, "timeleft": "00:10:00", "status": "downloading"}]}))
        mock.get("http://sonarr.test/api/v3/calendar").mock(return_value=httpx.Response(200, json=[
            {"seasonNumber": 1, "episodeNumber": 3, "title": "Next", "airDateUtc": "2026-11-14T01:00:00Z",
             "series": {"title": "Show"}}]))
        mock.get("http://sonarr.test/api/v3/history").mock(return_value=httpx.Response(200, json={"records": [
            {"date": "2026-11-11T10:00:00Z", "series": {"title": "Show"}, "episode": {"seasonNumber": 1, "episodeNumber": 1}}]}))
        mock.get(url__startswith="http://radarr.test").mock(return_value=httpx.Response(500))
        mock.get("http://seerr.test/api/v1/request").mock(return_value=httpx.Response(200, json={"results": [
            {"id": 7, "status": 1, "media": {"mediaType": "movie", "tmdbId": 11, "status": 2},
             "requestedBy": {"displayName": "Avery"}, "createdAt": "2026-11-11T09:00:00Z"}]}))
        mock.get("http://seerr.test/api/v1/movie/11").mock(return_value=httpx.Response(200, json={"title": "Film", "releaseDate": "2024-01-01"}))
        r = home_client.get("/v1/pipeline", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["downloads"][0]["progress"] == 0.75 and b["downloads"][0]["eta_s"] == 600
    assert b["upcoming"][0]["detail"].startswith("S01E03") and b["recent"][0]["title"] == "Show"
    assert b["requests"][0] == {**b["requests"][0], "title": "Film", "status": "pending", "requested_by": "Avery"}
    assert {f["source"] for f in b["failures"]} == {"radarr"}


def test_activity_merges_home_assistant_logbook(hub, people, home_client):
    import datetime as dt

    hub.vault.set("home_assistant.token", "t")
    now = dt.datetime.now(dt.timezone.utc)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="http://ha.test/api/logbook/").mock(return_value=httpx.Response(200, json=[
            {"when": (now - dt.timedelta(minutes=5)).isoformat(), "name": "Front door", "message": "was unlocked",
             "entity_id": "lock.front_door"},
            {"when": (now - dt.timedelta(minutes=4)).isoformat(), "name": "Kitchen", "message": "turned on",
             "entity_id": "light.kitchen"},
        ]))
        b = home_client.get("/v1/activity?category=doors", headers=people["phones"]["Dylan"].h).json()
    assert [i["detail"] for i in b["items"]] == ["Front door was unlocked"]
    item = b["items"][0]
    assert isinstance(item["id"], str) and isinstance(item["t"], float) and item["source"] == "home"


def test_drop_folder_takes_house_model(hub, tmp_path, monkeypatch):
    from hub.dropbox import import_dropped

    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    drop = tmp_path / "import"
    drop.mkdir()
    (drop / "house-model-web.json").write_text('{"rooms": []}')
    assert import_dropped(hub.vault, drop) == ["asset:house-model-web.json"]
    assert (tmp_path / "assets" / "house-model-web.json").read_text() == '{"rooms": []}'
    assert not hub.vault.has("house-model-web.json")


def test_tv_remote_commands_follow_home_assistant(hub, people, home_client):
    from hub.home import device_state

    st = hub.home.states["media_player.theo_tv"]
    st["state"] = "on"
    st["attributes"].update(supported_features=128 | 256 | 1 | 16384 | 1024 | 4 | 8 | 2048,
                            source_list=["HDMI 1", "Netflix"], source="Netflix", volume_level=0.3, is_volume_muted=False)
    hub.config.hub.dry_run = False
    hub.home.media_links = {"media_player.theo_tv": {"remote": "remote.theo_tv", "platform": "androidtv_remote"}}
    s = device_state("media", st, hub.home.media_links["media_player.theo_tv"])
    assert s["volume"] == 30 and s["muted"] is False and s["sources"] == ["HDMI 1", "Netflix"]
    assert {"power", "play", "pause", "vol_up", "volume", "mute", "source", "up", "select", "home"} <= set(s["commands"])
    assert "next" not in s["commands"]
    dylan = people["phones"]["Dylan"]
    for params in ({"command": "up"}, {"command": "source", "source": "HDMI 1"}, {"command": "volume", "volume": 55}):
        r = home_client.post("/v1/actions", json={"action": "media.command", "target": "media_player.theo_tv",
                                                  "params": params}, headers=dylan.h)
        assert r.status_code == 200, r.text
    assert ("remote", "send_command", {"entity_id": "remote.theo_tv", "command": "DPAD_UP"}) in hub.calls
    assert ("media_player", "select_source", {"entity_id": "media_player.theo_tv", "source": "HDMI 1"}) in hub.calls
    assert ("media_player", "volume_set", {"entity_id": "media_player.theo_tv", "volume_level": 0.55}) in hub.calls
    bad = home_client.post("/v1/actions", json={"action": "media.command", "target": "media_player.theo_tv",
                                                "params": {"command": "source", "source": "Nope"}}, headers=dylan.h)
    assert bad.status_code == 400


def test_frigate_cameras_events_and_home(hub, people, home_client):
    hub.config.frigate.enabled, hub.config.frigate.url = True, "http://frigate.test:5000"
    dylan = people["phones"]["Dylan"]
    ev = {"id": "1700.5-abc", "camera": "140_creek", "label": "person", "sub_label": None, "zones": ["yard"],
          "start_time": 1700000000.5, "end_time": None, "has_clip": True, "has_snapshot": True,
          "data": {"top_score": 0.81}}
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://frigate.test:5000/api/config").mock(return_value=httpx.Response(200, json={"cameras": {
            "140_creek": {"detect": {"width": 2560, "height": 720}},
            "140_garden": {"detect": {"width": 1280, "height": 720}}}}))
        mock.get("http://frigate.test:5000/api/stats").mock(return_value=httpx.Response(200, json={"cameras": {
            "140_creek": {"camera_fps": 5}, "140_garden": {"camera_fps": 0}}}))
        events = mock.get("http://frigate.test:5000/api/events").mock(return_value=httpx.Response(200, json=[ev]))
        mock.get("http://frigate.test:5000/api/events/1700.5-abc").mock(return_value=httpx.Response(200, json=ev))
        cams = home_client.get("/v1/cameras", headers=dylan.h).json()["cameras"]
        assert [(c["id"], c["width"], c["height"], c["online"]) for c in cams] == [
            ("140_creek", 2560, 720, True), ("140_garden", 1280, 720, False)]
        assert cams[0]["name"] == "Creek"
        feed = home_client.get("/v1/cameras/events?limit=1&label=person", headers=dylan.h).json()
        item = feed["items"][0]
        assert item["camera_name"] == "Creek" and item["score"] == 0.81 and item["end"] is None
        assert item["snapshot_url"].endswith("/snapshot.jpg") and feed["next_before"] == "1700000000.5"
        assert events.calls[-1].request.url.params["labels"] == "person"
        assert home_client.get("/v1/cameras/events/1700.5-abc", headers=dylan.h).json()["id"] == "1700.5-abc"
        # Only Frigate cameras exist now: the HA-only one is gone from Home and the API.
        assert home_client.get("/v1/cameras/driveway/snapshot.jpg", headers=dylan.h).status_code == 404
        home = home_client.get("/v1/home", headers=dylan.h).json()
        assert [c["id"] for c in home["cameras"]] == ["140_creek", "140_garden"]


def test_admin_web_page_code_sign_in_and_secrets(hub, people, home_client, away_client, monkeypatch, tmp_path):
    import re as _re

    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))

    dylan = people["phones"]["Dylan"]
    assert away_client.get("/admin").status_code == 403  # home network only
    page = home_client.get("/admin")
    assert page.status_code == 200 and "Hub web page" in page.text
    code = _re.search(r"class=code>([A-Z0-9-]+)<", page.text).group(1)
    pid = _re.search(r"const id='([^']+)'", page.text).group(1)
    assert home_client.get(f"/admin/login/poll?id={pid}").json() == {"ok": False}
    # Avery is not an admin.
    assert people["phones"]["Avery"].secure_post("/v1/admin/web-approve", {"code": code}).status_code == 403
    assert dylan.secure_post("/v1/admin/web-approve", {"code": code.lower()}).status_code == 200
    r = home_client.get(f"/admin/login/poll?id={pid}")
    assert r.json() == {"ok": True} and "tc_admin" in r.cookies
    status = home_client.get("/admin")
    assert "Upload several files" in status.text and "Long-lived access token" in status.text
    assert "Apple push key" in status.text and "Missing" in status.text
    csrf = _re.search(r"name=csrf value=([0-9a-f]+)", status.text).group(1)
    # Without the form token nothing is saved.
    assert home_client.post("/admin/secrets", data={"name": "plex.token", "value": "x"}).status_code == 403
    r = home_client.post("/admin/secrets", data={"csrf": csrf, "name": "apns.auth_key_p8"},
                         files={"file": ("AuthKey_ABCDE12345.p8", b"-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n")},
                         follow_redirects=False)
    assert r.status_code == 303
    assert str(hub.vault.get("apns.key_id")) == "ABCDE12345" and hub.vault.has("apns.auth_key_p8")
    page = home_client.get("/admin").text
    assert "BEGIN PRIVATE KEY" not in page and "ABCDE12345" not in page
    assert home_client.post("/admin/secrets/delete", data={"csrf": csrf, "name": "apns.key_id"},
                            follow_redirects=False).status_code == 303
    assert not hub.vault.has("apns.key_id")
    assert home_client.post("/admin/secrets", data={"csrf": csrf, "name": "wireguard.server_private_key", "value": "x"},
                            follow_redirects=False).status_code == 303
    assert not hub.vault.has("wireguard.server_private_key")

    # Bulk upload: VPN client files named from the file, a push key, the house model, a key-named text file.
    hub.config.vpn.provider = "pool"
    files = [("files", ("Client3.conf", _client_conf(3).encode())),
             ("files", ("AVERY.conf", _client_conf(4).encode())),
             ("files", ("Something.conf", _client_conf(5).encode())),
             ("files", ("AuthKey_ZYXWV98765.p8", b"-----BEGIN PRIVATE KEY-----\nBBB\n-----END PRIVATE KEY-----\n")),
             ("files", ("house-model-web.json", b'{"rooms": []}')),
             ("files", ("trek.api_key.txt", b"trek-secret\n")),
             ("files", ("notes.docx", b"hello"))]
    r = home_client.post("/admin/upload", data={"csrf": csrf}, files=files)
    assert r.status_code == 200 and "Not recognised: notes.docx" in r.text
    names = set(hub.vault.names())
    assert "vpn.pool.something" not in names and "notes.docx" not in names
    assert {"vpn.pool.3", "vpn.pool.avery", "vpn.pool.1", "trek.api_key", "apns.auth_key_p8"} <= names
    assert str(hub.vault.get("apns.key_id")) == "ZYXWV98765" and str(hub.vault.get("trek.api_key")) == "trek-secret"
    assert (tmp_path / "assets" / "house-model-web.json").read_text() == '{"rooms": []}'
    page = home_client.get("/admin").text
    assert "Firewalla VPN clients" in page and "3 saved" in page and "trek-secret" not in page
    # The VPN box alone, and a bad file.
    r = home_client.post("/admin/vpn", data={"csrf": csrf},
                         files=[("files", ("Client7.conf", _client_conf(7).encode())), ("files", ("x.conf", b"nope"))])
    assert "Added VPN clients: 7" in r.text and "x.conf" in r.text
    r = home_client.post("/admin/house-model", data={"csrf": csrf}, files={"file": ("m.json", b"not json")})
    assert "not JSON" in r.text


def test_admin_web_link_is_one_time(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    url = dylan.secure_post("/v1/admin/web-link", {}).json()["url"]
    r = home_client.get(url, follow_redirects=False)
    assert r.status_code == 303 and "tc_admin" in r.cookies
    home_client.cookies.clear()
    assert home_client.get(url, follow_redirects=False).status_code == 401


def test_drop_folder_takes_apple_push_key(hub, tmp_path):
    from hub.dropbox import import_dropped

    drop = tmp_path / "import"
    drop.mkdir()
    (drop / "AuthKey_ZYX9876543.p8").write_text("-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n")
    import_dropped(hub.vault, drop)
    assert str(hub.vault.get("apns.key_id")) == "ZYX9876543" and hub.vault.has("apns.auth_key_p8")
    hub.config.apns.team_id = "TEAM123456"
    assert hub.apns.enabled


@pytest.mark.asyncio
async def test_status_probe_checks_services_without_waiting_for_the_app(hub):
    hub.config.sonarr.enabled, hub.config.sonarr.url = True, "http://sonarr.test"
    hub.config.jellyseerr.enabled, hub.config.jellyseerr.url = True, "http://seerr.test"
    hub.config.truenas.enabled, hub.config.truenas.url = True, "https://nas.test"
    hub.vault.set("sonarr.api_key", "k")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://sonarr.test/api/v3/system/status").mock(return_value=httpx.Response(200, json={}))
        mock.get("http://seerr.test/api/v1/request").mock(return_value=httpx.Response(401))
        mock.route(host="ha.test").mock(return_value=httpx.Response(200, json={}))
        await hub.probe_once()
    health = {s["id"]: s for s in hub.health()}
    assert health["sonarr"]["ok"] and health["sonarr"]["last_ok"]
    assert not health["jellyseerr"]["ok"] and "refused" in health["jellyseerr"]["error"]
    assert health["truenas"]["error"] == "the NAS API key is not saved yet"


def test_refusal_reason_is_shown_and_status_json_is_home_only(hub, home_client, away_client):
    import asyncio

    hub.config.trek.enabled, hub.config.trek.url = True, "http://trek.test"
    hub.vault.set("trek.api_key", "trek_x")
    with respx.mock() as mock:
        mock.get("http://trek.test/api/v1/trips").mock(
            return_value=httpx.Response(401, json={"error": "Invalid API token", "code": "API_TOKEN_INVALID"}))
        with pytest.raises(Exception):
            asyncio.run(hub.trek.ping())
    assert hub.trek.last_error == "Trips refused the hub's credential (API_TOKEN_INVALID)"
    assert away_client.get("/admin/status.json").status_code == 403
    rows = {r["id"]: r for r in home_client.get("/admin/status.json").json()}
    assert rows["trek"]["error"].endswith("(API_TOKEN_INVALID)") and "trek_x" not in str(rows)
    detail = home_client.get("/admin/status.json?detail=1").json()
    assert "ffmpeg" in detail and detail["download_failures"] == [] and "trek_x" not in str(detail)


def test_trek_key_shape_is_explained(hub):
    import asyncio

    hub.config.trek.enabled, hub.config.trek.url = True, "http://trek.test"
    hub.vault.set("trek.api_key", "mcp_abc")
    with pytest.raises(Exception):
        asyncio.run(hub.trek.ping())
    assert "start with trek_" in hub.trek.last_error
    hub.vault.set("trek.api_key", "Bearer trek_abc ")
    assert hub.trek.auth_headers()["Authorization"] == "Bearer trek_abc"


def test_trek_block_without_token_secret_still_uses_the_saved_key():
    from hub.config import Config

    assert Config(**{"trek": {"enabled": True, "url": "http://trek.test"}}).trek.token_secret == "trek.api_key"


def test_climate_heat_cool_and_text_numbers():
    from hub.home import device_state

    st = device_state("climate", {"state": "heat_cool", "attributes": {
        "current_temperature": "71", "temperature": None, "target_temp_low": 68, "target_temp_high": 74,
        "humidity": 36.0, "temperature_unit": "°F"}})
    assert st["current_temp"] == 71.0 and st["target_temp"] == 71.0 and st["target_low"] == 68.0
    assert st["available"] and st["on"] and st["humidity"] == 36.0
    gone = device_state("climate", {"state": "unavailable", "attributes": {}})
    assert gone["available"] is False and gone["current_temp"] is None
