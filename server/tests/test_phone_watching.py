"""The Android phone app 0.4: who's watching (a grown-up's phone picks a child), Ask in plain words, Pick for me,
and a shuffled show kept across screens."""

from __future__ import annotations

from tests.test_tv_api import _library
from tests.test_tv_phone import _login

ADULT_MOVIE = "vod:thomas-creek:100"
KIDS_MOVIE = "vod:thomas-creek:101"


def _titles(body):
    return {card["title"] for card in body["items"]}


def test_a_grown_ups_phone_can_watch_as_a_child_and_only_narrows(hub, people):
    _library(hub)
    _login(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    child, adult = people["theo"]["id"], people["avery"]["id"]

    feed = dylan.client.get("/v1/tv/explore", headers=dylan.h, params={"person": child})
    assert feed.status_code == 200, feed.text
    shown = {c.get("title") for row in feed.json()["rows"] for c in row.get("items") or []}
    assert "Drama Movie" not in shown and "Drama Show" not in shown

    no = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": ADULT_MOVIE, "person": child})
    assert no.status_code == 404
    ok = dylan.client.post("/v1/tv/phone/play", headers=dylan.h, json={"movie_id": KIDS_MOVIE, "person": child})
    assert ok.status_code == 200, ok.text

    # The place is kept for the child, not for the phone's owner.
    saved = dylan.client.post("/v1/tv/phone/progress", headers=dylan.h, json={
        "movie_id": KIDS_MOVIE, "position_s": 600, "duration_s": 5000, "person": child})
    assert saved.status_code == 200, saved.text
    as_child = dylan.client.get("/v1/tv/phone/resume", headers=dylan.h,
                                params={"movie_id": KIDS_MOVIE, "person": child})
    assert as_child.json()["position_s"] == 600
    as_self = dylan.client.get("/v1/tv/phone/resume", headers=dylan.h, params={"movie_id": KIDS_MOVIE})
    assert as_self.json()["position_s"] in (0, None)
    theirs = theo.client.get("/v1/tv/phone/resume", headers=theo.h, params={"movie_id": KIDS_MOVIE})
    assert theirs.json()["position_s"] == 600

    # Never wider: nobody watches as a grown-up, and a child's phone can't pick anyone.
    assert dylan.client.get("/v1/tv/explore", headers=dylan.h, params={"person": adult}).status_code == 403
    up = theo.client.get("/v1/tv/explore", headers=theo.h, params={"person": people["dylan"]["id"]})
    assert up.status_code == 403
    assert dylan.client.get("/v1/tv/upnext", headers=dylan.h, params={"person": "nobody"}).status_code == 404
    assert dylan.client.get("/v1/tv/upnext", headers=dylan.h, params={"person": child}).status_code == 200


def test_ask_in_plain_words_without_the_brain(hub, people):
    _library(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    r = dylan.client.post("/v1/tv/ask", headers=dylan.h, json={"text": "a drama film"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["by"] == "rules" and _titles(body) == {"Drama Movie"}
    assert "fit" in body["reply"]
    shows = dylan.client.post("/v1/tv/ask", headers=dylan.h, json={"text": "drama shows"}).json()
    assert _titles(shows) == {"Drama Show"}
    # A child's words never reach past the kids' library.
    kid = theo.client.post("/v1/tv/ask", headers=theo.h, json={"text": "drama"}).json()
    assert kid["items"] == [] and "Nothing" in kid["reply"]
    as_child = dylan.client.post("/v1/tv/ask", headers=dylan.h,
                                 json={"text": "cartoon", "person": people["theo"]["id"]}).json()
    assert "Drama Movie" not in _titles(as_child) and "Cartoon Show" in _titles(as_child)
    assert dylan.client.post("/v1/tv/ask", headers=dylan.h, json={"text": ""}).status_code == 422


def test_ask_uses_the_brain_and_only_its_candidates(hub, people, monkeypatch):
    from hub import tvask

    _library(hub)
    dylan = people["phones"]["Dylan"]

    class Thought:
        data = {"picks": [0, 99, 0], "reply": "Two quiet ones for tonight"}
        model, tier, tokens_in, tokens_out, cached, cost_usd, latency_ms, error = "m", "cheap", 1, 1, 0, 0.0, 1, None

    seen = {}

    class Router:
        async def think(self, **kw):
            seen.update(kw)
            return Thought()

    class Brain:
        on, router = True, Router()

        def has_key(self):
            return True

        def capped(self):
            return False

        def log_run(self, **kw):
            seen["logged"] = kw["job"]

    monkeypatch.setattr(hub, "brain", Brain(), raising=False)
    body = dylan.client.post("/v1/tv/ask", headers=dylan.h, json={"text": "something calm"}).json()
    assert body["by"] == "ai" and body["reply"] == "Two quiet ones for tonight"
    assert len(body["items"]) == 1  # the made-up number 99 and the repeat are dropped
    assert seen["logged"] == tvask.JOB and seen["payload"]["asked"] == "something calm"
    assert all("movie_id" not in c and "url" not in str(c) for c in seen["payload"]["candidates"])

    # A child is never sent to the model.
    seen.clear()
    theo = people["phones"]["Theo"]
    kid = theo.client.post("/v1/tv/ask", headers=theo.h, json={"text": "cartoon"}).json()
    assert kid["by"] == "rules" and "payload" not in seen


def test_pick_for_me_steps_on(hub, people):
    _library(hub)
    dylan = people["phones"]["Dylan"]
    first = dylan.client.get("/v1/tv/pick", headers=dylan.h)
    assert first.status_code == 200, first.text
    one = first.json()
    if one["item"] is None:  # a library too small for any row still answers with a reason
        assert one["reason"]
        return
    assert one["reason"] and one["key"]
    second = dylan.client.get("/v1/tv/pick", headers=dylan.h, params={"skip": one["key"]}).json()
    if one["more"]:
        assert second["key"] != one["key"]
    kid = people["phones"]["Theo"].client.get("/v1/tv/pick", headers=people["phones"]["Theo"].h).json()
    assert kid["item"] is None or kid["item"].get("title") not in ("Drama Movie", "Drama Show")


def test_a_shuffled_show_on_the_phone_keeps_its_order(hub, people):
    _library(hub)
    dylan = people["phones"]["Dylan"]
    r = dylan.client.post("/v1/tv/phone/progress", headers=dylan.h, json={
        "episode_id": "ep:thomas-creek:900", "position_s": 300, "duration_s": 1200,
        "shuffle": {"order": ["ep:thomas-creek:900", "ep:thomas-creek:901"], "seasons": [1]}})
    assert r.status_code == 200, r.text
    from hub.upnext import _history, who_key

    bucket = _history(hub, who_key(person=people["dylan"]["id"]))
    kept = [row for row in (bucket.get("series") or {}).values() if isinstance(row, dict) and row.get("shuffle")]
    assert kept and kept[0]["shuffle"]["order"][0] == "ep:thomas-creek:900"


def test_who_a_phone_may_watch_as(hub, people):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    grown = dylan.client.get("/v1/tv/watchers", headers=dylan.h).json()["watchers"]
    assert grown[0] == {"id": people["dylan"]["id"], "name": "Dylan", "role": "admin", "self": True}
    assert [w["name"] for w in grown[1:]] == ["Theo"]
    kid = theo.client.get("/v1/tv/watchers", headers=theo.h).json()["watchers"]
    assert [w["name"] for w in kid] == ["Theo"]
