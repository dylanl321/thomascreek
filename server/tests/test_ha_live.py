"""Lights feel instant: Home Assistant's own reply and event stream update phones without waiting for the poll,
calls with an unknown outcome are never sent twice, and failures read as plain sentences."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from hub.adapters.base import NoAnswer, ServiceError, Unreachable


def _light(home_client, person, target="light.living_room", **params):
    return home_client.post("/v1/actions", json={"action": "light.set", "target": target, "params": params},
                            headers=person.h)


def _events(hub):
    seen: list[dict] = []
    hub.bus.publish = lambda e, *a, **kw: seen.append(e)  # type: ignore[method-assign]
    return seen


def _wait(seen, n=1, timeout=2.0):
    end = time.time() + timeout
    while len(seen) < n and time.time() < end:
        time.sleep(0.02)


def test_the_reply_from_home_assistant_updates_phones_at_once(hub, people, home_client):
    hub.config.hub.dry_run = False
    fresh = {"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "Living room",
             "brightness": 255}, "last_updated": "2026-11-14T13:00:00+00:00"}

    async def call_service(domain, service, data):
        hub.calls.append((domain, service, data))
        return [fresh]
    hub.ha.call_service = call_service
    seen = _events(hub)
    assert _light(home_client, people["phones"]["Dylan"], on=True).status_code == 200
    assert hub.home.states["light.living_room"]["state"] == "on"
    _wait(seen)
    assert [e for e in seen if e.get("type") == "device"] == [{"type": "device", "entity_id": "light.living_room"}]


def test_a_device_that_reports_later_does_not_send_its_old_state(hub, people, home_client):
    """Before: the hub sent the light's stale state right after the call, and the app flipped the switch back."""
    hub.config.hub.dry_run = False

    async def call_service(domain, service, data):
        return []  # Home Assistant answered before the bulb reported back
    hub.ha.call_service = call_service
    seen = _events(hub)
    assert _light(home_client, people["phones"]["Dylan"], on=True).status_code == 200
    time.sleep(0.3)
    assert not [e for e in seen if e.get("type") == "device"]


async def test_event_stream_changes_are_gathered_and_published(hub):
    seen = _events(hub)
    hub.home.PUBLISH_DEBOUNCE_S = 0.05
    for b in (10, 20, 30):
        hub.home.publish_soon(hub.home.merge_states([{
            "entity_id": "light.kitchen", "state": "on", "attributes": {"brightness": b},
            "last_updated": f"2026-11-14T13:00:{b}+00:00"}]))
    # an entity the model doesn't know waits for the poll
    hub.home.publish_soon(hub.home.merge_states([{"entity_id": "light.new", "state": "on"}]))
    await asyncio.sleep(0.15)
    assert seen == [{"type": "device", "entity_id": "light.kitchen"}]
    assert hub.home.states["light.kitchen"]["attributes"] == {"brightness": 30}
    # the same state again (the poll, or the reply then the event) is not a change
    assert hub.home.merge_states([dict(hub.home.states["light.kitchen"])]) == []


def test_tv_power_is_explicit_on_or_off(hub, people, home_client):
    from tests.test_tvremote import _press, _tv

    st = _tv(hub)
    dylan = people["phones"]["Dylan"]
    assert _press(home_client, dylan, command="power", on=False).status_code == 200
    assert hub.calls[-1][:2] == ("media_player", "turn_off")
    assert _press(home_client, dylan, command="power", on=True).status_code == 200
    assert hub.calls[-1][:2] == ("media_player", "turn_on")
    # an older app sends no "on": the reported state decides
    st["state"] = "on"
    _press(home_client, dylan, command="power")
    assert ("media_player", "turn_off") in [c[:2] for c in hub.calls[-2:]]
    assert all(c[1] != "toggle" for c in hub.calls)


def test_a_call_with_no_answer_is_not_sent_again(hub, people, home_client):
    from tests.test_tvremote import _press, _tv

    _tv(hub)
    sent = []

    async def slow(domain, service, data):
        sent.append((domain, service))
        raise NoAnswer("no answer from Home Assistant: ReadTimeout")
    hub.ha.call_service = slow
    r = _press(home_client, people["phones"]["Dylan"], command="up")
    assert r.status_code == 504 and "slow to answer" in r.json()["message"]
    assert sent == [("remote", "send_command")]


@pytest.mark.parametrize("exc,status,words", [
    (ServiceError("x", 500, "Failed to call service light/turn_on. Zigbee timeout"), 502, "Zigbee timeout"),
    (ServiceError("x", 400, "Entity light.nope not found"), 502, "didn't accept"),
    (Unreachable("cannot reach"), 503, "can't reach Home Assistant"),
    (NoAnswer("no answer"), 504, "may still change"),
])
def test_failures_read_as_plain_sentences(hub, people, home_client, exc, status, words):
    hub.config.hub.dry_run = False

    async def broken(domain, service, data):
        raise exc
    hub.ha.call_service = broken
    r = _light(home_client, people["phones"]["Dylan"], on=True)
    assert r.status_code == status and words in r.json()["message"], r.text


def test_an_offline_light_says_so_without_calling(hub, people, home_client):
    hub.config.hub.dry_run = False
    # a copy: the fixture's state dicts are shared with every other test
    hub.home.states["light.living_room"] = {**hub.home.states["light.living_room"], "state": "unavailable"}
    r = _light(home_client, people["phones"]["Dylan"], on=True)
    assert r.status_code == 409 and "offline" in r.json()["message"]
    assert hub.calls == []


@respx.mock
async def test_adapter_tells_no_connection_from_no_answer(hub):
    hub.vault.set(hub.config.home_assistant.token_secret, "t")
    route = respx.post("http://ha.test/api/services/light/turn_on")
    from hub.adapters.homeassistant import HomeAssistant

    ha = HomeAssistant(hub.config.home_assistant, hub.vault)
    route.side_effect = httpx.ConnectError("refused")
    with pytest.raises(Unreachable):
        await ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    route.side_effect = httpx.ReadTimeout("slow")
    with pytest.raises(NoAnswer):
        await ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    route.side_effect = None
    route.return_value = httpx.Response(500, json={"message": "Bulb unreachable"})
    with pytest.raises(ServiceError) as err:
        await ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    assert err.value.status == 500 and err.value.reason == "Bulb unreachable"
    await ha.close()
