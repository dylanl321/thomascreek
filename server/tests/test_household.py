from __future__ import annotations

import asyncio
import base64
import time

from hub.config import GuestLockCode


def test_today_and_presence(hub, home_client, people):
    hub.home.states["person.avery"] = {"entity_id": "person.avery", "state": "not_home", "attributes": {},
                                        "last_changed": "2026-11-12T10:00:00+00:00"}
    hub.home.states["person.theo"] = {"entity_id": "person.theo", "state": "home", "attributes": {},
                                       "last_changed": "2026-11-12T10:00:00+00:00"}
    ph = people["phones"]["Dylan"]
    r = home_client.get("/v1/today", headers=ph.h)
    assert r.status_code == 200, r.text
    t = r.json()
    fam = {p["first"]: p for p in t["family"]}
    assert fam["Avery"]["home"] is False and fam["Theo"]["home"] is True and fam["Dylan"]["home"] is None
    assert fam["Dylan"]["me"] is True
    assert "kids_bedtimes" in t and t["kids_bedtimes"][0]["name"] == "Theo"
    # /v1/home people pick up the same presence
    home = home_client.get("/v1/home", headers=ph.h).json()
    assert {p["name"]: p.get("home") for p in home["people"]}["Theo"] is True


def test_phone_reports_and_last_one_out(hub, home_client, people):
    hub.config.hub.dry_run = False
    hub.home.states["cover.large_garage"] = {**hub.home.states["cover.large_garage"], "state": "open"}
    ph = people["phones"]["Avery"]
    # Nobody else is known to be home, so Avery is the last one out.
    r = home_client.post("/v1/presence", json={"event": "leave"}, headers=ph.h)
    assert r.status_code == 200, r.text
    dep = r.json()["departure"]
    assert dep["last_one_out"] is True
    kinds = {i["kind"] for i in dep["open"]}
    assert {"garage", "lights"} <= kinds
    inbox = hub.db.all("SELECT * FROM inbox WHERE person_id=?", [people["avery"]["id"]])
    assert any(i["title"] == "You're the last one out" for i in inbox)
    # Someone else home: no nudge.
    home_client.post("/v1/presence", json={"event": "arrive"}, headers=people["phones"]["Dylan"].h)
    d = home_client.get("/v1/departure", headers=ph.h).json()
    assert d["last_one_out"] is False and d["others_home"] == ["Dylan"]
    # Children can't see the house check.
    assert home_client.get("/v1/departure", headers=people["phones"]["Theo"].h).status_code == 403


def test_ha_presence_tick(hub, people):
    hub.home.states["person.avery"] = {"entity_id": "person.avery", "state": "home", "attributes": {}}
    asyncio.run(hub.household.tick())
    hub.home.states["person.avery"]["state"] = "not_home"
    left = asyncio.run(hub.household.tick())
    assert left == [people["avery"]["id"]]


def test_bedtime(hub, home_client, people):
    theo = people["theo"]["id"]
    kid = people["phones"]["Theo"]
    r = home_client.get(f"/v1/people/{theo}/bedtime", headers=kid.h)
    assert r.status_code == 200 and r.json()["time"] == "20:30"
    assert home_client.put(f"/v1/people/{theo}/bedtime", json={"time": "20:00"}, headers=kid.h).status_code == 403
    r = home_client.put(f"/v1/people/{theo}/bedtime", json={"time": "19:45", "wake": "06:45"},
                        headers=people["phones"]["Avery"].h)
    assert r.json()["time"] == "19:45" and r.json()["wake"] == "06:45"
    r = home_client.post(f"/v1/people/{theo}/bedtime/extend", json={"minutes": 30}, headers=people["phones"]["Avery"].h)
    assert r.json()["active"] is False and r.json()["extended_until"]


def test_intercom_and_ask_parent(hub, home_client, people):
    hub.home.states["media_player.kitchen_sonos"] = {"entity_id": "media_player.kitchen_sonos", "state": "idle",
                                                     "attributes": {"friendly_name": "Kitchen", "group_members": []}}
    hub.home.entity_area["media_player.kitchen_sonos"] = "kitchen"
    r = home_client.get("/v1/intercom", headers=people["phones"]["Avery"].h)
    assert r.status_code == 200
    assert r.json()["rooms"] == [{"id": "kitchen", "name": "Kitchen", "speakers": 1}]
    assert home_client.get("/v1/intercom", headers=people["phones"]["Theo"].h).status_code == 403
    r = home_client.post("/v1/ask-parent", json={"text": "I'm hungry", "emoji": "🍎"}, headers=people["phones"]["Theo"].h)
    assert r.status_code == 200 and set(r.json()["sent_to"]) == {"Dylan", "Avery"}


def test_guest_pass(hub, home_client, away_client, people):
    hub.config.guest.wifi_ssid = "Creek Guest"
    hub.vault.set("guest_wifi.password", "s3cret-wifi")
    hub.config.guest.lock_code = GuestLockCode(entity="lock.front_door")
    hub.config.hub.dry_run = False
    ph = people["phones"]["Avery"]
    assert home_client.post("/v1/guest-passes", json={"name": "Morgan"}, headers=people["phones"]["Theo"].h).status_code == 403
    r = ph.secure_post("/v1/guest-passes", {"name": "Morgan", "days": 3, "door_code": "4821", "note": "Back door sticks"})
    assert r.status_code == 200, r.text
    gp = r.json()
    assert gp["state"] == "active" and gp["has_door_code"] and gp["lock_set"]
    assert ("zwave_js", "set_lock_usercode", {"entity_id": "lock.front_door", "code_slot": 20, "usercode": "4821"}) in hub.calls
    token = gp["url"].rsplit("/", 1)[1]
    page = away_client.get(f"/g/{token}")
    assert page.status_code == 200 and "4821" in page.text and "s3cret-wifi" in page.text and "Morgan" in page.text
    js = away_client.get(f"/g/{token}.json").json()
    assert js["state"] == "active" and js["wifi"]["ssid"] == "Creek Guest"
    assert away_client.get("/g/nope").status_code == 404
    # Revoke clears the code and ends the page.
    assert home_client.delete(f"/v1/guest-passes/{gp['id']}", headers=ph.h).status_code == 200
    assert ("zwave_js", "clear_lock_usercode", {"entity_id": "lock.front_door", "code_slot": 20}) in hub.calls
    assert "has ended" in away_client.get(f"/g/{token}").text
    assert hub.vault.get(f"guest_pass.{gp['id']}.door_code") is None


def test_guest_pass_expires_by_timer(hub, home_client, people):
    ph = people["phones"]["Dylan"]
    gp = home_client.post("/v1/guest-passes", json={"name": "Sam", "days": 1}, headers=ph.h).json()
    asyncio.run(hub.timers.tick(time.time() + 2 * 86400))
    assert hub.db.one("SELECT * FROM activity WHERE action='guest_pass.end' AND target=?", [gp["id"]])


def test_feedback(hub, home_client, people):
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()
    r = home_client.post("/v1/feedback", json={"text": "The garage button is too small", "screen": "House › doors",
                                               "app_build": "23", "screenshot_jpeg_b64": jpeg},
                         headers=people["phones"]["Theo"].h)
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    inbox = hub.db.all("SELECT * FROM inbox WHERE person_id=? AND cat='feedback'", [people["dylan"]["id"]])
    assert inbox and "garage button" in inbox[0]["body"]
    lst = home_client.get("/v1/feedback", headers=people["phones"]["Dylan"].h).json()["items"]
    assert lst[0]["who"] == "Theo" and lst[0]["shot"]
    assert home_client.get(lst[0]["shot"], headers=people["phones"]["Dylan"].h).content.startswith(b"\xff\xd8")
    assert home_client.get("/v1/feedback", headers=people["phones"]["Avery"].h).status_code == 403
    assert home_client.post(f"/v1/feedback/{fid}/done", headers=people["phones"]["Dylan"].h).json()["ok"]


def test_live_activity_token(hub, home_client, people):
    ph = people["phones"]["Dylan"]
    r = home_client.post("/v1/devices/live-activity", json={"push_to_start_token": "abc123", "env": "sandbox"}, headers=ph.h)
    assert r.status_code == 200
    assert hub.db.kv_get(f"la_start.{ph.device_id}") == {"token": "abc123", "env": "sandbox"}

    sent = []

    class FakeApns:
        enabled = True

        async def close(self):
            pass

        async def send(self, token, payload, **kw):
            sent.append((token, payload, kw))

            class R:
                ok, token_dead = True, False
            return R()

    hub.apns = FakeApns()  # type: ignore[assignment]
    n = asyncio.run(hub.household.doorbell_live([people["dylan"]["id"]], "140_breezeway", "Breezeway"))
    assert n == 1
    tok, payload, kw = sent[0]
    assert kw["push_type"] == "liveactivity" and kw["topic_suffix"] == ".push-type.liveactivity"
    assert payload["aps"]["attributes-type"] == "DoorbellAttributes"


def test_welcome_mode_on_arrival(hub, home_client, people):
    from hub.config import ModeConfig

    hub.config.hub.dry_run = False
    hub.config.modes = [ModeConfig(id="welcome", name="Welcome home", steps=[
        {"action": "light.set", "target": "light.living_room", "params": {"on": True}},
        {"action": "lock.set", "target": "lock.front_door", "params": {"locked": False}},
    ])]
    ph = people["phones"]["Avery"]
    assert home_client.put("/v1/me/welcome", json={"mode": "welcome"}, headers=ph.h).status_code == 200
    r = home_client.post("/v1/presence", json={"event": "arrive"}, headers=ph.h).json()
    assert r["welcome"]["name"] == "Welcome home"
    # Only the light step ran; arriving never unlocks a door.
    assert ("light", "turn_on", {"entity_id": "light.living_room"}) in [(d, s, {"entity_id": x.get("entity_id")}) for d, s, x in hub.calls]
    assert not any(d == "lock" for d, _, _ in hub.calls)


def test_quick_garage(hub, home_client, people):
    hub.config.hub.dry_run = False
    ph = people["phones"]["Avery"]
    body = {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True}, "quick": True}
    # Without the quick flag (the app itself) it still asks for Face ID.
    plain = {k: v for k, v in body.items() if k != "quick"}
    assert home_client.post("/v1/actions", json=plain, headers=ph.h).status_code == 428
    r = home_client.post("/v1/actions", json=body, headers=ph.h)
    assert r.status_code == 200, r.text
    assert hub.db.one("SELECT source FROM activity WHERE action='garage.set'")["source"] == "quick"
    # Never for children, and never a door unlock.
    kid = home_client.post("/v1/actions", json=body, headers=people["phones"]["Theo"].h)
    assert kid.status_code == 403
    unlock = {"action": "lock.set", "target": "lock.front_door", "params": {"locked": False}, "quick": True}
    assert home_client.post("/v1/actions", json=unlock, headers=ph.h).status_code == 428
