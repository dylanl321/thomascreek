"""Nov 21 review follow-ups: home-only pages fail closed behind a reverse proxy (S5), a hub reading of every IPTV
provider (R4), a Now playing push tally (R3) and the timing tool's own signed sign-in (P7)."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
from pathlib import Path

import httpx
import respx
from starlette.testclient import TestClient

from hub import iptvaddr, iptvprobe, nowplaying_live
from hub.main import build_app
from tests.test_iptv_addresses import _ready_logins


# -- S5: the proxy -------------------------------------------------------------------------------------------------
def test_an_unlisted_proxy_never_makes_the_internet_home(hub):
    # NPM moved to a Docker network that isn't in trusted_proxies: its own address is private, so taking it as the
    # caller would make every stranger "home". A forwarded request from an unlisted local machine is away instead.
    hub._proxies = [ipaddress.ip_network("172.16.20.0/24")]
    assert hub.client_ip("192.168.1.77", "203.0.113.9") is None
    assert not hub.is_home(hub.client_ip("192.168.1.77", "192.168.1.50"))
    # A listed proxy that doesn't say who it's passing on is not home either.
    assert hub.client_ip("172.16.20.5", None) is None
    # Unchanged: direct callers at home, the listed proxy naming its caller, a public caller's own address.
    assert hub.is_home(hub.client_ip("192.168.1.50", None))
    assert hub.client_ip("172.16.20.5", "203.0.113.9") == "203.0.113.9"
    assert hub.client_ip("203.0.113.1", "192.168.1.5") == "203.0.113.1"


def test_home_only_pages_through_a_proxy_and_the_caller_tally(hub):
    hub._proxies = [ipaddress.ip_network("172.16.20.0/24")]
    app = build_app(hub, start_background=False)
    listed = TestClient(app, base_url="https://hub.test", client=("172.16.20.5", 40000))
    unlisted = TestClient(app, base_url="https://hub.test", client=("192.168.1.77", 40000))
    home = TestClient(app, base_url="https://hub.test", client=("192.168.1.50", 40000))
    mobile = {"X-Forwarded-For": "203.0.113.9"}
    for path in ("/v1/ping/timings", "/v1/ping/stalls", "/v1/ping/providers"):
        assert listed.get(path, headers=mobile).status_code == 403, path
        assert unlisted.get(path, headers=mobile).status_code == 403, path
        assert listed.get(path).status_code == 403, path  # the proxy naming nobody
    assert listed.get("/v1/ping/timings", headers={"X-Forwarded-For": "192.168.1.50"}).status_code == 200
    body = home.get("/v1/ping/timings").json()
    callers = body["callers"]
    assert callers["proxy_away"]["n"] == 3 and callers["proxy_away"]["refused"] == 3
    assert callers["forwarded_untrusted"]["refused"] == 3 and callers["proxy_unnamed"]["refused"] == 3
    assert callers["proxy_home"]["n"] == 1 and callers["proxy_home"]["refused"] == 0
    assert body["proxies"] == 1
    assert "203.0.113.9" not in json.dumps(body)


# -- R4: the provider reading --------------------------------------------------------------------------------------
def test_provider_reading_says_how_far_each_address_gets_without_addresses_or_logins(hub, home_client, monkeypatch):
    _ready_logins(hub)
    iptvaddr.set_backups(hub.db, "alpha", ["http://alpha-backup.test"], "http://alpha.test")
    iptvprobe._last.clear()

    async def tcp(url):
        if "beta" in url:
            return {"scheme": "http", "port": 80, "dns": "ok", "connect": "connection refused", "reached": "tcp"}
        return {"scheme": "http", "port": 80, "dns": "ok", "connect": "ok", "connect_ms": 3}

    monkeypatch.setattr(iptvprobe, "_tcp", tcp)

    def api(request):
        if request.url.host == "alpha-backup.test":
            return httpx.Response(403, headers={"server": "cloudflare"})
        if "VLC" not in request.headers.get("user-agent", ""):
            return httpx.Response(403)  # turns away the hub's own client name, not a player
        return httpx.Response(200, json={"user_info": {"auth": 1, "status": "Active", "active_cons": "1",
                                                       "max_connections": "1"}})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"http://.*/player_api\.php").mock(side_effect=api)
        r = home_client.get("/v1/ping/providers")
    assert r.status_code == 200, r.text
    body = r.json()
    text = json.dumps(body)
    for secret in ("alpha.test", "beta.test", "secret-house", "secret-spare", "username"):
        assert secret not in text, secret
    alpha = next(p for p in body["providers"] if p["id"] == "alpha")
    main, backup = alpha["addresses"]
    assert main["main"] and main["api"]["hub"]["answer"] == "refused" and main["api"]["player"]["answer"] == "ok"
    assert backup["api"]["player"]["answer"] == "refused" and backup["api"]["player"]["server"] == "cloudflare"
    assert alpha["login_checks"][0]["login"] == "house" and alpha["login_checks"][0]["streams"] == "1 of 1 in use"
    beta = next(p for p in body["providers"] if p["id"] == "beta")
    assert beta["addresses"][0]["connect"] == "connection refused" and "login_checks" not in beta
    # A second look within a minute is the same reading, not another round of calls to the providers.
    assert home_client.get("/v1/ping/providers").json()["cached"] is True


def test_failure_names_are_short_and_address_free():
    refused = ConnectionRefusedError(111, "Connect call failed ('198.51.100.7', 80)")
    assert iptvprobe._why(refused) == "connection refused"
    assert iptvprobe._why(asyncio.TimeoutError()) == "no answer (timed out)"
    assert iptvprobe._why(httpx.ConnectError("[Errno -2] Name or service not known")) == "name lookup failed"
    assert iptvprobe._why(ConnectionResetError()) == "connection reset"


# -- R3: the Now playing tally -------------------------------------------------------------------------------------
def test_now_playing_tally_is_on_the_timings_page(home_client):
    body = home_client.get("/v1/ping/timings").json()
    tally = body["now_playing"]
    assert set(tally) >= {"started", "ended", "people_alerted", "tvs_playing", "tvs_playing_for_nobody"}
    assert nowplaying_live.TALLY["people"] is not None


# -- P7: the timing tool signs in on its own -----------------------------------------------------------------------
def _timing_tool():
    path = Path(__file__).parents[1] / "tools" / "timing.py"
    spec = importlib.util.spec_from_file_location("timing_tool", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_timing_tool_enrolls_then_signs_in_like_a_phone(hub, app, tmp_path, monkeypatch, people):
    tool = _timing_tool()
    client = TestClient(app, base_url="https://hub.test", client=("192.168.1.60", 40000))
    monkeypatch.setattr(tool, "_client", lambda base, ctx: client)
    monkeypatch.setattr(client, "__exit__", lambda *a: None)
    device = tmp_path / "keys" / "timing.json"
    assert tool.enroll("https://hub.test", None, str(device), None) == 0
    assert (device.stat().st_mode & 0o777) == 0o600
    saved = json.loads(device.read_text())
    token, why = tool.sign_in("https://hub.test", None, str(device))
    assert token is None and "waiting" in why  # pending until an admin says whose it is
    # An admin gives it to a person: now it signs in, with that person's role, and the token works on a route.
    adult = next(p for p in hub.db.all("SELECT * FROM people") if p["role"] == "adult")
    hub.db.update("devices", "id", saved["device_id"], {"person_id": adult["id"], "status": "active"})
    token, why = tool.sign_in("https://hub.test", None, str(device))
    assert token and why.startswith("signed in, role adult") and adult["name"] not in why
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # A second enroll never overwrites the key.
    assert tool.enroll("https://hub.test", None, str(device), None) == 1
