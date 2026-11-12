"""Live checks against real services on the home network. Skipped unless LIVE_FRIGATE_URL is set.

Run on a machine inside the LAN:  LIVE_FRIGATE_URL=http://<frigate>:5000 python -m pytest -q tests/test_live.py -s
"""

import os

import pytest

FRIGATE = os.environ.get("LIVE_FRIGATE_URL")
pytestmark = pytest.mark.skipif(not FRIGATE, reason="set LIVE_FRIGATE_URL to run live checks")


@pytest.fixture
def config(config):  # noqa: F811
    config.frigate.enabled = True
    config.frigate.url = FRIGATE
    return config


def test_live_camera_events_end_to_end(hub, people, home_client):
    dylan = people["phones"]["Dylan"]
    r = home_client.get("/v1/cameras/events", params={"limit": 20}, headers=dylan.h)
    assert r.status_code == 200, r.text
    page = r.json()
    print("events:", len(page["items"]), "next_before:", repr(page["next_before"]))
    assert page["items"], "Frigate returned no events"
    assert page["next_before"] is None or isinstance(page["next_before"], str)
    for it in page["items"]:
        assert isinstance(it["id"], str) and isinstance(it["label"], str) and isinstance(it["start"], str)
    first = page["items"][0]
    t = home_client.get(first["thumb_url"], headers=dylan.h)
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg" and len(t.content) > 500
    one = home_client.get(f"/v1/cameras/events/{first['id']}", headers=dylan.h)
    assert one.status_code == 200 and one.json()["id"] == first["id"]
    if page["next_before"]:
        r2 = home_client.get("/v1/cameras/events", params={"limit": 20, "before": page["next_before"]},
                             headers=dylan.h)
        assert r2.status_code == 200 and r2.json()["items"][0]["id"] != first["id"]
    cams = home_client.get("/v1/cameras", headers=dylan.h).json()["cameras"]
    print("cameras:", [(c["id"], c["width"], c["height"], c["online"]) for c in cams])
    assert cams and all(c["width"] for c in cams)
    for cam in cams:
        snap = home_client.get(cam["snapshot_url"] + "?m=1", headers=dylan.h)
        print("snapshot", cam["id"], snap.status_code, snap.text[:120] if snap.status_code != 200 else len(snap.content))
    per = home_client.get(f"/v1/cameras/{first['camera']}/events", headers=dylan.h)
    assert per.status_code == 200, per.text
    kid = people["phones"]["Theo"]
    print("child sees events:", home_client.get("/v1/cameras/events", headers=kid.h).status_code)
