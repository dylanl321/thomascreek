"""Camera media follows a person's role on every path (#211).

A child gets no camera picture, stream or camera link from any endpoint unless an adult switched door alerts on
for kids (`kids` in /v1/tv/alerts/settings). Words with no picture (a hazard, an automation) may still reach them.
"""

from __future__ import annotations

import time

from hub.routes.home import shape_event

from tests.test_alerts import ev, sent, who  # noqa: F401  (sent is a fixture)
from tests.test_home_alerts import _door
from tests.test_tv_api import KID_TV, TV_ID, _auth
from tests.test_tv_cameras import _tv


def _pictures(hub):
    async def jpg(_cam, height=360):
        return b"\xff\xd8frame"

    async def proxy(_entity):
        return b"\xff\xd8frame"

    class Upstream:
        headers = {"content-type": "multipart/x-mixed-replace; boundary=frame"}

        async def aiter_raw(self):
            yield b"--frame"

        async def aclose(self):
            return None

    async def stream(_entity):
        return Upstream()

    async def go2rtc(_src, _params=None):
        return Upstream()

    hub.frigate.latest_jpg = jpg
    hub.ha.camera_proxy = proxy
    hub.ha.camera_stream = stream
    hub.frigate.open_go2rtc = go2rtc


def _alert_paths(alert):
    return [alert["snapshot_url"], alert["stream_url"], f"/v1/tv/alerts/{alert['id']}/media?src=api%2Fhls%2Fs.ts"]


async def test_phone_pushes_and_the_doorbell_live_activity_go_to_grown_ups_only(hub, people, sent):
    # Frigate camera alert: adults only, with the picture.
    assert await hub.alerts.on_frigate(ev(eid="r1")) == 2
    assert who(hub, sent) == ["Avery", "Dylan"] and all(r["image"] for r in sent)
    sent.clear()

    # Doorbell: push and Live Activity both to adults only.
    started = []

    async def live(person_ids, camera, name):
        started.append(list(person_ids))
        return len(person_ids)

    hub.household.doorbell_live = live
    hub.home.states["event.front_doorbell"] = {"entity_id": "event.front_doorbell", "state": "2026-11-12T10:00:00",
                                               "attributes": {"device_class": "doorbell", "friendly_name": "Doorbell"}}
    await hub.alerts.on_state("event.front_doorbell", {"state": "2026-11-12T10:00:00"},
                              {"state": "2026-11-12T10:05:00", "attributes": {"device_class": "doorbell"}})
    assert who(hub, sent) == ["Avery", "Dylan"]
    assert sorted(hub.db.person(pid)["name"] for pid in started[0]) == ["Avery", "Dylan"]


async def test_an_automation_with_a_camera_reaches_kids_without_the_picture(hub, people, sent):
    n = await hub.alerts.from_automation("Package at the door", "On the step", ["everyone"], camera="140_frontdoor")
    assert n == 3
    by = {hub.db.person(r["person_ids"][0])["name"]: r for r in sent}
    assert by["Dylan"]["image"] == "/v1/cameras/140_frontdoor/snapshot.jpg"
    assert by["Dylan"]["ref"] == "thomascreek://camera/140_frontdoor"
    assert by["Theo"]["image"] is None and by["Theo"]["ref"] is None


def test_history_never_hands_a_child_a_camera_picture(hub, people, home_client):
    theo = people["theo"]
    # An older line written before the split, with a picture and a camera link on it.
    hub.alerts.record("house", "Package at the door", None, [theo], ref="thomascreek://camera/140_frontdoor",
                      image="/v1/cameras/140_frontdoor/snapshot.jpg")
    items = home_client.get("/v1/alerts/history", headers=people["phones"]["Theo"].h).json()["items"]
    assert [i["title"] for i in items] == ["Package at the door"]
    assert items[0]["image"] is None and items[0]["ref"] is None


def test_tv_door_alerts_follow_the_kids_switch_on_every_route(hub, home_client, people):
    _door(hub)
    _pictures(hub)
    hub.frigate.cfg.go2rtc_url = "http://go2rtc.test:1984"

    async def no_playlist(_cam):
        return None

    hub.frigate.hls_playlist = no_playlist
    grown = _tv(hub, home_client, TV_ID, room="living_room")
    kid = _tv(hub, home_client, KID_TV, profile="kids", room="theo_room", media_entity="media_player.theo_tv")
    alert = hub.home_alerts.note_doorbell("binary_sensor.front_doorbell", now=time.time())
    theo = people["phones"]["Theo"]
    dylan = people["phones"]["Dylan"]
    theo_subject = hub.subject_for(hub.db.device(theo.device_id), True)
    event = {"type": "home_alert", "data": alert}

    # Off for kids (the default): no event, no picture, no stream, no segment, for a kids' TV or a child's phone.
    assert shape_event(hub, theo_subject, event) is None
    for path in _alert_paths(alert):
        assert home_client.get(path, headers=_auth(grown)).status_code == 200, path
        assert home_client.get(path, headers=_auth(kid)).status_code == 403, path
        assert home_client.get(path, headers=theo.h).status_code == 403, path

    # An adult switches door alerts on for kids: the same alert now reaches them.
    assert dylan.client.put("/v1/tv/alerts/settings", headers=dylan.h, json={"kids": True}).json()["kids"] is True
    assert shape_event(hub, theo_subject, event) == event
    for path in _alert_paths(alert):
        assert home_client.get(path, headers=_auth(kid)).status_code == 200, path
        assert home_client.get(path, headers=theo.h).status_code == 200, path

    # The Cameras screens stay grown-up whatever the switch says.
    assert home_client.get("/v1/tv/cameras", headers=_auth(kid)).status_code == 403
    assert home_client.get("/v1/tv/cameras/kitchen/stream", headers=_auth(kid)).status_code == 403
    for path in ("/v1/cameras", "/v1/cameras/events", "/v1/cameras/kitchen/snapshot.jpg",
                 "/v1/cameras/events/e1/snapshot.jpg", "/v1/cameras/events/e1/clip.mp4"):
        assert home_client.get(path, headers=theo.h).status_code == 403, path

    # Switching every TV alert off ends it for kids too.
    dylan.client.put("/v1/tv/alerts/settings", headers=dylan.h, json={"enabled": False})
    assert home_client.get(alert["snapshot_url"], headers=_auth(kid)).status_code == 403
