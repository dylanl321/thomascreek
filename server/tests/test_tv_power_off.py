"""Turning TVs and speakers off from a card: only what can really switch off is offered, a speaker that can't
power off is stopped, a Thomas Creek box goes through the hub's own HDMI-CEC path, and Home Assistant's own
error text never reaches the phone."""

from __future__ import annotations

from typing import Any

from hub.adapters.base import ServiceError
from hub.brain.jobs import Trigger
from hub.home import MP_PAUSE, MP_PLAY, MP_STOP, MP_TURN_OFF, MP_TURN_ON, MP_VOLUME_SET

from tests.test_brain import brain, iso_ago, smart_cards, word_all  # noqa: F401

SONOS = MP_PAUSE | MP_PLAY | MP_STOP | MP_VOLUME_SET  # no turn_on / turn_off, like a Sonos


def player(hub, eid: str, name: str, state: str, features: int | None, device_class: str | None = None,
           area: str = "living_room") -> None:
    attrs: dict[str, Any] = {"friendly_name": name}
    if features is not None:
        attrs["supported_features"] = features
    if device_class:
        attrs["device_class"] = device_class
    hub.home.states[eid] = {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": iso_ago(3600)}
    hub.home.entity_area[eid] = area


def record(hub, fail: dict[str, Exception] | None = None) -> list[tuple[str, str, Any]]:
    hub.config.hub.dry_run = False
    sent: list[tuple[str, str, Any]] = []

    async def call(domain, service, data):
        sent.append((domain, service, data.get("entity_id")))
        exc = (fail or {}).get(str(data.get("entity_id")))
        if exc:
            raise exc
        return []
    hub.ha.call_service = call
    return sent


def steps_off(*eids: str) -> dict[str, Any]:
    return {"action": "steps.run", "params": {"label": "Turn off", "steps": [
        {"action": "media.command", "target": e, "params": {"command": "power", "on": False}} for e in eids]}}


async def test_a_paused_speaker_is_not_left_on(hub, brain, home_client, people):  # noqa: F811
    brain.use(word_all("Basement Garage is still playing", ""))
    player(hub, "media_player.unnamed_room_13", "Basement Garage", "paused", SONOS, "speaker")
    player(hub, "media_player.office_sonos", "Basement Office", "paused", SONOS, "speaker")
    assert await brain.run("left_on", Trigger("hub", "last_left")) == "nothing"
    assert smart_cards(home_client, people["phones"]["Dylan"]) == []


async def test_a_playing_speaker_gets_a_stop_button_that_stops_it(hub, brain, home_client, people):  # noqa: F811
    fake = brain.use(word_all("Basement Garage is still playing", ""))
    player(hub, "media_player.unnamed_room_13", "Basement Garage", "playing", SONOS, "speaker")
    assert await brain.run("left_on", Trigger("hub", "last_left")) == "suggested"
    item = fake.items()[0]
    assert item["say"] == "Basement Garage is still playing"
    assert item["players"] == [{"name": "Basement Garage", "type": "speaker"}]
    c = smart_cards(home_client, people["phones"]["Dylan"])[0]
    button = next(b for b in c["buttons"] if b.get("action") == "steps.run")
    assert button["title"] == "Stop"
    sent = record(hub)
    r = home_client.post("/v1/actions", json={"action": "steps.run", "params": button["params"]},
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert sent == [("media_player", "media_stop", "media_player.unnamed_room_13")]


async def test_a_tv_the_hub_cannot_switch_off_gets_no_dead_button(hub, brain, home_client, people):  # noqa: F811
    brain.use(word_all("Office TV is still on", ""))
    player(hub, "media_player.office_tv", "Office TV", "on", MP_VOLUME_SET, "tv")
    assert await brain.run("left_on", Trigger("hub", "last_left")) == "suggested"
    c = smart_cards(home_client, people["phones"]["Dylan"])[0]
    assert not [b for b in c["buttons"] if b.get("action") == "steps.run"]
    assert [b["title"] for b in c["buttons"]] == ["Got it"]


async def test_a_tv_and_a_speaker_are_named_for_what_they_are(hub, brain, home_client, people):  # noqa: F811
    fake = brain.use(word_all("Two still on", ""))
    player(hub, "media_player.office_tv", "Office TV", "on", MP_TURN_ON | MP_TURN_OFF, "tv")
    player(hub, "media_player.garage_sonos", "Garage", "playing", SONOS, "speaker")
    assert await brain.run("left_on", Trigger("hub", "last_left")) == "suggested"
    assert fake.items()[0]["say"] == "2 TVs and speakers still on"
    c = smart_cards(home_client, people["phones"]["Dylan"])[0]
    button = next(b for b in c["buttons"] if b.get("action") == "steps.run")
    assert button["title"] == "Turn off"
    assert [s["target"] for s in button["params"]["steps"]] == ["media_player.garage_sonos", "media_player.office_tv"]


def test_turn_off_on_a_player_without_power_never_sends_turn_off(hub, home_client, people):
    player(hub, "media_player.unnamed_room_13", "Basement Garage", "playing", SONOS, "speaker")
    player(hub, "media_player.office_sonos", "Basement Office", "paused", SONOS, "speaker")
    sent = record(hub)
    r = home_client.post("/v1/actions", json=steps_off("media_player.unnamed_room_13", "media_player.office_sonos"),
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert sent == [("media_player", "media_stop", "media_player.unnamed_room_13")]  # the paused one is quiet already


def test_a_tv_with_no_way_off_says_so_plainly(hub, home_client, people):
    player(hub, "media_player.office_tv", "Office TV", "on", MP_VOLUME_SET, "tv")
    sent = record(hub)
    r = home_client.post("/v1/actions", json={"action": "media.command", "target": "media_player.office_tv",
                                              "params": {"command": "power", "on": False}},
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 400 and sent == []
    assert r.json()["message"] == "Office TV can't be turned off from the app. Use its own remote or app."


def test_a_thomas_creek_box_turns_off_over_the_hubs_own_path(hub, home_client, people, monkeypatch):
    from hub import tvs

    player(hub, "media_player.office_tv", "Office TV", "on", MP_VOLUME_SET, "tv")
    monkeypatch.setattr(hub.actions, "tv_box", lambda eid: "tv-1" if eid == "media_player.office_tv" else None)
    asked: list[tuple[str, dict[str, Any]]] = []

    async def act(h, install_id, body, who, now=None):
        asked.append((install_id, body))
        return {"ok": True}
    monkeypatch.setattr(tvs, "act", act)
    sent = record(hub)
    r = home_client.post("/v1/actions", json=steps_off("media_player.office_tv"), headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert sent == [] and asked == [("tv-1", {"action": "tv_power", "power": "off"})]


def test_home_assistant_refusals_read_as_words_without_entity_ids(hub, home_client, people):
    player(hub, "media_player.unnamed_room_13", "Basement Garage", "on", None, "tv")
    record(hub, {"media_player.unnamed_room_13": ServiceError("x", 400, (
        "Validation error: Entity media_player.unnamed_room_13 does not support action media_player.turn_off"))})
    r = home_client.post("/v1/actions", json=steps_off("media_player.unnamed_room_13"),
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 502
    message = r.json()["message"]
    assert message == "Basement Garage can't be turned off from the app. Use its own remote or app."
    assert "media_player." not in message and "Validation" not in message


def test_one_player_failing_does_not_stop_the_rest(hub, home_client, people):
    player(hub, "media_player.garage_tv", "Garage TV", "on", MP_TURN_OFF, "tv")
    player(hub, "media_player.office_tv", "Office TV", "on", MP_TURN_OFF, "tv")
    sent = record(hub, {"media_player.garage_tv": ServiceError("x", 500, "timeout")})
    r = home_client.post("/v1/actions", json=steps_off("media_player.garage_tv", "media_player.office_tv"),
                         headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    assert ("media_player", "turn_off", "media_player.office_tv") in sent
    assert r.json()["message"] == "Done, but Garage TV didn't change."
