"""Who is watching a TV: the hub's guess, the TV picker, "I'm watching" on the phone, and the phone's own
Continue watching (resume and progress)."""

from __future__ import annotations

import time

from hub import tvwho
from hub.routes.home import shape_event
from hub.upnext import shelf, who_key

from tests.test_tv_api import TV_ID, Clock, Key, _auth, _enroll, _library, _token
from tests.test_tv_phone import _login


def _tv(hub, home_client, profile: str = "adult", person_id: str | None = None):
    key = Key()
    _enroll(home_client, key, TV_ID, "Living room")
    hub.tv_app.set_tv(TV_ID, status="approved", room="living_room", profile=profile)
    token = _token(home_client, key, Clock(), person_id=person_id)["access_token"]
    return _auth(token)


def _home(hub, person, home: bool = True):
    hub.db.kv_set(f"presence.{person['id']}", {"home": home, "at": time.time()})


def _continue(hub, who, profile="adult"):
    rows = shelf(hub, profile, who=who)["rows"]
    row = next((r for r in rows if r["id"] == "continue"), {"items": []})
    return row["items"]


def test_nobody_is_guessed_without_a_reason_and_the_tv_keeps_its_own_history(hub, home_client, people):
    headers = _tv(hub, home_client)
    got = home_client.get("/v1/tv/watching", headers=headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["watching"]["person_id"] is None
    # The picker offers the household, grown-ups first; nobody's phone said they're home.
    assert [p["name"] for p in body["choices"]] == ["Avery", "Dylan", "Theo"]
    # Nobody named: the play goes to the TV's own bucket and no person's.
    _library(hub)
    home_client.post("/v1/tv/state", headers=headers, json={
        "playing": {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "kind": "movie", "position_s": 600}})
    assert [i["movie_id"] for i in _continue(hub, who_key(tv=TV_ID))] == ["vod:thomas-creek:100"]
    assert not hub.db.watch_buckets().get(who_key(person=people["dylan"]["id"]))


def test_the_only_grown_up_home_is_the_guess(hub, home_client, people):
    headers = _tv(hub, home_client)
    _home(hub, people["dylan"])
    _home(hub, people["avery"], home=False)
    _home(hub, people["theo"])  # a child at home never makes the guess for a grown-up TV
    watching = home_client.get("/v1/tv/watching", headers=headers).json()["watching"]
    assert watching["person_id"] == people["dylan"]["id"] and watching["reason"] == "home"
    assert watching["line"] == "Dylan is the only one home"
    _home(hub, people["avery"])
    assert home_client.get("/v1/tv/watching", headers=headers).json()["watching"]["person_id"] is None


def test_a_guess_from_who_is_home_never_writes_anyones_history(hub, home_client, people):
    """Only a claim made on purpose moves history; a presence guess is just the picker's first choice."""
    _library(hub)
    headers = _tv(hub, home_client)
    _home(hub, people["dylan"])
    home_client.post("/v1/tv/state", headers=headers, json={
        "playing": {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "kind": "movie", "position_s": 600}})
    assert not hub.db.watch_buckets().get(who_key(person=people["dylan"]["id"]))


def test_im_watching_on_the_phone_names_the_person_and_tells_the_tv(hub, home_client, people):
    _library(hub)
    headers = _tv(hub, home_client)
    subject = hub.tv_app.subject_for(hub.tv_app.tvs()[TV_ID], TV_ID, None)
    tv_events = hub.bus.subscribe(lambda ev: shape_event(hub, subject, ev), None, device_id=f"tv:{TV_ID}", tv=True).queue
    avery = people["phones"]["Avery"]
    claimed = avery.client.post(f"/v1/tvs/{TV_ID}/watching", headers=avery.h, json={})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["person_id"] == people["avery"]["id"] and claimed.json()["reason"] == "claimed"
    seen = []
    while not tv_events.empty():
        seen.append(tv_events.get_nowait())
    assert any(ev["type"] == "tv_watching" and ev["data"]["person_id"] == people["avery"]["id"] for ev in seen)
    # The phone's TV list shows who's watching.
    row = avery.client.get("/v1/tvs", headers=avery.h).json()["tvs"][0]
    assert row["watching"]["name"] == "Avery"
    # An older TV build sends no person: the claim still puts the play in Avery's Continue watching.
    home_client.post("/v1/tv/state", headers=headers, json={
        "playing": {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "kind": "movie", "position_s": 900}})
    assert [i["movie_id"] for i in _continue(hub, who_key(person=people["avery"]["id"]))] == ["vod:thomas-creek:100"]
    # null is "nobody": back to the TV's own history, and the hub stops guessing.
    _home(hub, people["dylan"])
    cleared = avery.client.post(f"/v1/tvs/{TV_ID}/watching", headers=avery.h, json={"person_id": None})
    assert cleared.status_code == 200 and cleared.json()["person_id"] is None


def test_sending_a_show_from_the_phone_claims_the_tv(hub, home_client, people):
    _library(hub)
    headers = _tv(hub, home_client)
    home_client.post("/v1/tv/state", headers=headers, json={"foreground": True})
    hub.config.hub.dry_run = False
    dylan = people["phones"]["Dylan"]
    sent = dylan.client.post("/v1/actions", headers=dylan.h, json={
        "action": "tv.play", "target": TV_ID, "params": {"kind": "movie", "movie_id": "vod:thomas-creek:100"}})
    assert sent.status_code == 200, sent.text
    watching = tvwho.current(hub, TV_ID)
    assert watching["person_id"] == people["dylan"]["id"] and watching["reason"] == "phone"


def test_the_tv_picker_names_someone_and_the_heartbeat_keeps_it(hub, home_client, people):
    _library(hub)
    headers = _tv(hub, home_client)
    picked = home_client.post("/v1/tv/watching", headers=headers, json={"person_id": people["dylan"]["id"]})
    assert picked.status_code == 200, picked.text
    assert picked.json()["watching"]["reason"] == "claimed"
    home_client.post("/v1/tv/state", headers=headers, json={
        "person_id": people["dylan"]["id"],
        "playing": {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "kind": "movie", "position_s": 1200}})
    assert _continue(hub, who_key(person=people["dylan"]["id"]))[0]["position_s"] == 1200
    # A heartbeat naming someone else moves the claim with it.
    home_client.post("/v1/tv/state", headers=headers, json={
        "person_id": people["avery"]["id"], "playing": {"title": "News", "kind": "live"}})
    assert tvwho.current(hub, TV_ID)["person_id"] == people["avery"]["id"]
    # A claim fades after hours of quiet.
    later = time.time() + tvwho.CLAIM_S + 60
    assert tvwho.current(hub, TV_ID, later)["person_id"] is None


def test_a_kids_tv_is_only_ever_a_childs(hub, home_client, people):
    _library(hub)
    headers = _tv(hub, home_client, profile="kids")
    _home(hub, people["dylan"])
    _home(hub, people["theo"])
    body = home_client.get("/v1/tv/watching", headers=headers).json()
    assert [p["name"] for p in body["choices"]] == ["Theo"]
    assert body["watching"]["person_id"] == people["theo"]["id"]
    # Nobody can name a grown-up here: not the picker, not a parent's phone, not a heartbeat.
    assert home_client.post("/v1/tv/watching", headers=headers,
                            json={"person_id": people["dylan"]["id"]}).status_code == 403
    dylan = people["phones"]["Dylan"]
    assert dylan.client.post(f"/v1/tvs/{TV_ID}/watching", headers=dylan.h, json={}).status_code == 403
    home_client.post("/v1/tv/state", headers=headers, json={
        "person_id": people["dylan"]["id"],
        "playing": {"movie_id": "vod:thomas-creek:101", "title": "Kids Movie", "kind": "movie", "position_s": 300}})
    assert not hub.db.watch_buckets().get(who_key(person=people["dylan"]["id"]))
    assert tvwho.current(hub, TV_ID)["person_id"] == people["theo"]["id"]


def test_a_child_names_only_themselves_and_never_takes_over_a_grown_ups_show(hub, home_client, people):
    headers = _tv(hub, home_client)
    theo = people["phones"]["Theo"]
    other = theo.client.post(f"/v1/tvs/{TV_ID}/watching", headers=theo.h, json={"person_id": people["dylan"]["id"]})
    assert other.status_code == 403
    # Dylan is watching something: Theo's phone can't claim the TV out from under him.
    dylan = people["phones"]["Dylan"]
    dylan.client.post(f"/v1/tvs/{TV_ID}/watching", headers=dylan.h, json={})
    home_client.post("/v1/tv/state", headers=headers, json={
        "person_id": people["dylan"]["id"], "playing": {"title": "News", "kind": "live"}})
    busy = theo.client.post(f"/v1/tvs/{TV_ID}/watching", headers=theo.h, json={})
    assert busy.status_code == 409 and busy.json()["error"] == "busy"
    # Once nothing is playing, Theo may say it's him; that only narrows the TV.
    home_client.post("/v1/tv/state", headers=headers, json={"playing": None})
    mine = theo.client.post(f"/v1/tvs/{TV_ID}/watching", headers=theo.h, json={})
    assert mine.status_code == 200 and mine.json()["person_id"] == people["theo"]["id"]


def test_a_launcher_pinned_to_one_person_is_that_person(hub, home_client, people):
    headers = _tv(hub, home_client)
    tvs = hub.tv_app.tvs()
    tvs[TV_ID]["launcher"] = {"on": True, "person_id": people["theo"]["id"], "apps": []}
    hub.tv_app._save_tvs(tvs)
    watching = home_client.get("/v1/tv/watching", headers=headers).json()["watching"]
    assert watching["person_id"] == people["theo"]["id"] and watching["reason"] == "pinned"


def test_older_tv_builds_keep_working(hub, home_client, people):
    """No picker, no person: state, upnext and config answer as before."""
    headers = _tv(hub, home_client)
    assert home_client.post("/v1/tv/state", headers=headers, json={"foreground": True}).status_code == 200
    assert home_client.get("/v1/tv/upnext", headers=headers).status_code == 200
    phones = people["phones"]
    assert phones["Dylan"].client.get(f"/v1/tvs/{TV_ID}", headers=phones["Dylan"].h).json()["watching"]["person_id"] is None


# -- the phone's player -----------------------------------------------------------------------------------------------

def test_the_phone_reports_progress_and_resumes_where_the_person_stopped(hub, home_client, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": "vod:thomas-creek:100"})
    assert first.status_code == 200 and first.json()["resume_s"] is None
    sent = dylan.client.post("/v1/tv/phone/progress", headers=dylan.h, json={
        "movie_id": "vod:thomas-creek:100", "position_s": 1500.4, "duration_s": 6000})
    assert sent.status_code == 200, sent.text
    again = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": "vod:thomas-creek:100"})
    assert again.json()["resume_s"] == 1500
    # Starting from the second a TV was at wins over the saved place.
    moved = dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                              json={"movie_id": "vod:thomas-creek:100", "start_s": 2222})
    assert moved.json()["resume_s"] == 2222
    # The phone's play is in Dylan's Continue watching, which every TV reads when Dylan is picked.
    items = _continue(hub, who_key(person=people["dylan"]["id"]))
    assert items[0]["movie_id"] == "vod:thomas-creek:100" and items[0]["position_s"] == 1500
    assert items[0]["progress"] == 0.25
    # Avery's phone has its own place (once Dylan's phone gives the one login back).
    assert dylan.client.post("/v1/tv/phone/hold", headers=dylan.h, json={"stop": True}).status_code == 200
    avery = people["phones"]["Avery"]
    assert avery.client.post("/v1/tv/phone/play", headers=avery.h,
                             json={"movie_id": "vod:thomas-creek:100"}).json()["resume_s"] is None


def test_a_film_started_on_a_tv_resumes_on_the_phone_and_back(hub, home_client, people):
    _library(hub)
    _login(hub)
    headers = _tv(hub, home_client)
    dylan = people["phones"]["Dylan"]
    home_client.post("/v1/tv/state", headers=headers, json={
        "person_id": people["dylan"]["id"],
        "playing": {"movie_id": "vod:thomas-creek:100", "title": "Drama Movie", "kind": "movie", "position_s": 700}})
    assert dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"movie_id": "vod:thomas-creek:100"}).json()["resume_s"] == 700
    dylan.client.post("/v1/tv/phone/progress", headers=dylan.h,
                      json={"movie_id": "vod:thomas-creek:100", "position_s": 1900})
    tv_resume = home_client.get("/v1/tv/phone/resume", headers=headers,
                                params={"movie_id": "vod:thomas-creek:100", "person": people["dylan"]["id"]})
    assert tv_resume.status_code == 200, tv_resume.text
    assert tv_resume.json()["position_s"] == 1900
    up = home_client.get("/v1/tv/upnext", headers=headers, params={"person": people["dylan"]["id"]}).json()
    cont = next(r for r in up["rows"] if r["id"] == "continue")
    assert cont["items"][0]["position_s"] == 1900


def test_phone_progress_follows_the_kids_filter(hub, home_client, people):
    _library(hub)
    theo = people["phones"]["Theo"]
    no = theo.client.post("/v1/tv/phone/progress", headers=theo.h,
                          json={"movie_id": "vod:thomas-creek:100", "position_s": 300})
    assert no.status_code == 404
    ok = theo.client.post("/v1/tv/phone/progress", headers=theo.h,
                          json={"movie_id": "vod:thomas-creek:101", "position_s": 300})
    assert ok.status_code == 200
    assert _continue(hub, who_key(person=people["theo"]["id"]), "kids")[0]["movie_id"] == "vod:thomas-creek:101"
    bad = theo.client.post("/v1/tv/phone/progress", headers=theo.h, json={"movie_id": "vod:thomas-creek:101"})
    assert bad.status_code == 400


def test_a_finished_film_has_nothing_to_resume(hub, home_client, people):
    _library(hub)
    _login(hub)
    dylan = people["phones"]["Dylan"]
    dylan.client.post("/v1/tv/phone/progress", headers=dylan.h,
                      json={"movie_id": "vod:thomas-creek:100", "position_s": 5990, "duration_s": 6000})
    assert dylan.client.post("/v1/tv/phone/play", headers=dylan.h,
                             json={"movie_id": "vod:thomas-creek:100"}).json()["resume_s"] is None
