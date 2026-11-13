"""Finding and adding Android TVs and Apple TVs through Home Assistant's setup flows."""

from __future__ import annotations

import json

import httpx
import respx

from hub.remotes import Found, clean_pin, devices_from_answers, model_name

HA = "http://ha.test"

ENTRIES_APPLE = [{"entry_id": "e-base", "domain": "apple_tv", "title": "Basement", "state": "loaded", "disabled_by": None}]
ENTRIES_ANDROID = [{"entry_id": "e-office", "domain": "androidtv_remote", "title": "Basement Office TV",
                    "state": "setup_retry", "disabled_by": None}]
FLOWS = [
    {"flow_id": "f1shield0000", "handler": "androidtv_remote", "step_id": "zeroconf_confirm",
     "context": {"source": "zeroconf", "title_placeholders": {"name": "SHIELD"}}},
    {"flow_id": "f2ignored000", "handler": "apple_tv", "step_id": "confirm",
     "context": {"source": "ignore", "title_placeholders": {"name": "Dylan's office"}}},
    {"flow_id": "f3hue0000000", "handler": "hue", "step_id": "link", "context": {"source": "zeroconf"}},
]
ENTITIES = [{"entity_id": "media_player.basement_apple_tv", "entry_id": "e-base", "domain": "apple_tv"},
            {"entity_id": "media_player.office_tv", "entry_id": "e-office", "domain": "androidtv_remote"}]
SCAN = [Found("apple", "10.0.3.143", "Living Room", "AppleTV14,1"), Found("apple", "10.0.0.204", "Basement", "AppleTV14,1"),
        Found("android", "10.0.1.21", "SHIELD", "SHIELD Android TV")]


def _template(request: httpx.Request) -> httpx.Response:
    tpl = json.loads(request.content)["template"]
    if "integration_entities" in tpl:
        return httpx.Response(200, text=json.dumps(ENTITIES))
    return httpx.Response(200, text="[]")


def _mock_lists(mock: respx.MockRouter) -> None:
    mock.get(f"{HA}/api/config/config_entries/entry", params={"domain": "apple_tv"}).mock(
        return_value=httpx.Response(200, json=ENTRIES_APPLE))
    mock.get(f"{HA}/api/config/config_entries/entry", params={"domain": "androidtv_remote"}).mock(
        return_value=httpx.Response(200, json=ENTRIES_ANDROID))
    mock.get(f"{HA}/api/config/config_entries/flow").mock(return_value=httpx.Response(200, json=FLOWS))
    mock.post(f"{HA}/api/template").mock(side_effect=_template)
    mock.get(f"{HA}/api/states").mock(return_value=httpx.Response(200, json=[]))


def test_mdns_answers_become_devices():
    answers = [
        ("10.0.1.21", [("PTR", "_androidtvremote2._tcp.local", "SHIELD._androidtvremote2._tcp.local"),
                       ("PTR", "_googlecast._tcp.local", "SHIELD-abc._googlecast._tcp.local"),
                       ("TXT", "SHIELD-abc._googlecast._tcp.local", {"md": "SHIELD Android TV", "fn": "living room Shield"})]),
        ("10.0.3.143", [("PTR", "_companion-link._tcp.local", "Living Room._companion-link._tcp.local"),
                        ("TXT", "Living Room._companion-link._tcp.local", {"rpMd": "AppleTV14,1"})]),
        # A Mac answers companion-link too; it isn't a TV.
        ("10.0.2.233", [("PTR", "_companion-link._tcp.local", "Dylan's MacBook Pro._companion-link._tcp.local"),
                        ("TXT", "Dylan's MacBook Pro._airplay._tcp.local", {"model": "Mac16,1"})]),
        # onn. boxes only advertise Cast but answer on the remote port; a Chromecast speaker doesn't.
        ("10.0.0.11", [("PTR", "_googlecast._tcp.local", "onn-1._googlecast._tcp.local"),
                       ("TXT", "onn-1._googlecast._tcp.local", {"md": "onn. 4K Streaming Box", "fn": "Master Bedroom TV"})]),
        ("10.0.1.50", [("PTR", "_googlecast._tcp.local", "speaker._googlecast._tcp.local"),
                       ("TXT", "speaker._googlecast._tcp.local", {"md": "Google Nest Mini", "fn": "Kitchen"})]),
    ]
    found = devices_from_answers(answers, has_remote=lambda ip: ip == "10.0.0.11")
    assert [(f.kind, f.host, f.name) for f in found] == [
        ("android", "10.0.1.21", "living room Shield"), ("android", "10.0.0.11", "Master Bedroom TV"),
        ("apple", "10.0.3.143", "Living Room")]
    assert model_name("apple", "AppleTV11,1") == "Apple TV 4K (2nd gen)"


def test_pin_cleanup():
    assert clean_pin("androidtv_remote", " ab 12-cd ") == "AB12CD"
    assert clean_pin("apple_tv", "1 2 3 4") == "1234"


def test_overview_merges_home_assistant_and_network(hub, people, home_client):
    hub.remotes.scanner = lambda: SCAN
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        _mock_lists(mock)
        r = home_client.get("/v1/remotes", headers=dylan.h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [(a["name"], a["kind"], a["entity_id"], a["working"]) for a in body["added"]] == [
            ("Basement Office TV", "android", "media_player.office_tv", False),
            ("Basement", "apple", "media_player.basement_apple_tv", True)]
        # SHIELD comes from Home Assistant's discovery (with the address from the scan); Basement is already
        # added; the ignored Apple TV flow and the Hue flow are left out; Living Room was only on the network.
        assert [(f["id"], f["name"], f["host"]) for f in body["found"]] == [
            ("flow:f1shield0000", "SHIELD", "10.0.1.21"), ("net:apple:10.0.3.143", "Living Room", "10.0.3.143")]
        assert body["found"][1]["model"] == "Apple TV 4K (3rd gen)"
        assert home_client.get("/v1/remotes", headers=theo.h).status_code == 403


def test_add_an_android_tv_with_the_code_on_screen(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    pair_form = {"type": "form", "flow_id": "abc123def456", "handler": "androidtv_remote", "step_id": "pair",
                 "data_schema": [{"name": "pin", "type": "string"}], "errors": {},
                 "description_placeholders": {"name": "living room Shield"}}
    sent: list[dict] = []

    def flow_step(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        sent.append(data)
        if "host" in data:
            return httpx.Response(200, json=pair_form)
        if data.get("pin") == "AB12CD":
            return httpx.Response(200, json={"type": "create_entry", "flow_id": "abc123def456", "handler": "androidtv_remote",
                                             "title": "living room Shield", "result": {"entry_id": "e-new"}})
        return httpx.Response(200, json={**pair_form, "errors": {"base": "invalid_auth"}})

    events = []
    hub.bus.publish = lambda e: events.append(e)  # type: ignore[method-assign]
    with respx.mock(assert_all_called=False) as mock:
        _mock_lists(mock)
        start = mock.post(f"{HA}/api/config/config_entries/flow").mock(return_value=httpx.Response(200, json={
            "type": "form", "flow_id": "abc123def456", "handler": "androidtv_remote", "step_id": "user",
            "data_schema": [{"name": "host"}], "errors": {}}))
        mock.get(f"{HA}/api/config/config_entries/flow/abc123def456").mock(return_value=httpx.Response(200, json=pair_form))
        mock.post(f"{HA}/api/config/config_entries/flow/abc123def456").mock(side_effect=flow_step)

        r = home_client.post("/v1/remotes/pair", json={"id": "net:android:10.0.0.86"}, headers=dylan.h).json()
        assert json.loads(start.calls[0].request.content)["handler"] == "androidtv_remote"
        assert sent[0] == {"host": "10.0.0.86"}
        assert r["state"] == "form" and r["needs_pin"] and r["message"] == "Type the code showing on living room Shield."

        bad = home_client.post("/v1/remotes/pair/abc123def456", json={"pin": "123"}, headers=dylan.h)
        assert bad.status_code == 400  # never sent to Home Assistant
        wrong = home_client.post("/v1/remotes/pair/abc123def456", json={"pin": "000000"}, headers=dylan.h).json()
        assert wrong["state"] == "form" and wrong["error"].startswith("That code didn't match")

        ok = home_client.post("/v1/remotes/pair/abc123def456", json={"pin": "ab12cd"}, headers=dylan.h).json()
        assert ok["state"] == "done" and ok["entry_id"] == "e-new" and "Everyone's remote" in ok["message"]
        assert events and events[-1]["type"] == "remotes"
    acts = [r["action"] for r in hub.db.activity()]
    assert "remotes.add" in acts and "remotes.pair_start" in acts


def test_apple_tv_step_that_shows_a_code_to_type_on_the_tv(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    flow = {"type": "form", "flow_id": "f1shield0000", "handler": "apple_tv", "step_id": "pair_no_pin",
            "data_schema": [], "errors": {}, "description_placeholders": {"name": "Living Room", "pin": "4821",
                                                                          "protocol": "AirPlay"}}
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HA}/api/config/config_entries/flow/f1shield0000").mock(return_value=httpx.Response(200, json=flow))
        r = home_client.post("/v1/remotes/pair", json={"id": "flow:f1shield0000"}, headers=dylan.h).json()
    assert r["state"] == "form" and not r["needs_pin"] and r["show_code"] == "4821"
    assert r["message"] == "On Living Room, enter 4821, then tap Continue."


def test_home_assistant_token_without_admin_rights(hub, people, home_client):
    hub.remotes.scanner = lambda: []
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{HA}/api/config/.*").mock(return_value=httpx.Response(401, json={"message": "Unauthorized"}))
        mock.post(f"{HA}/api/template").mock(side_effect=_template)
        r = home_client.get("/v1/remotes", headers=dylan.h)
    assert r.status_code == 409 and r.json()["error"] == "ha_admin_needed"


def test_other_integrations_flows_cannot_be_driven(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HA}/api/config/config_entries/flow/f3hue0000000").mock(return_value=httpx.Response(200, json={
            "type": "form", "flow_id": "f3hue0000000", "handler": "hue", "step_id": "link", "data_schema": []}))
        post = mock.post(f"{HA}/api/config/config_entries/flow/f3hue0000000")
        r = home_client.post("/v1/remotes/pair/f3hue0000000", json={}, headers=dylan.h)
    assert r.status_code == 404 and not post.called
