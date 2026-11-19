"""Each child has a rating level a grown-up sets in the apps; rows, search and play follow it (Dylan, Nov 19 2026)."""

from __future__ import annotations

from hub import kidratings, tvexplore

from tests.test_tv_api import _library
from tests.test_tv_phone import _login


def test_levels_build_on_each_other_and_defaults_follow_the_childs_screens(hub, people):
    assert kidratings.allowed_at("little") == {"G", "TV-Y", "TV-G"}
    assert kidratings.allowed_at("kid") == {"G", "TV-Y", "TV-G", "TV-Y7", "TV-Y7-FV", "PG"}
    assert "TV-PG" in kidratings.allowed_at("older") and "PG-13" not in kidratings.allowed_at("older")
    assert {"PG-13", "TV-14"} <= kidratings.allowed_at("teen") and "R" not in kidratings.allowed_at("teen")
    theo = people["Theo"] if "Theo" in people else hub.db.people_with_role("child")[0]
    assert kidratings.level_of(hub, theo) == "kid"  # a "kid" screen: no TV-PG sitcoms by default
    assert kidratings.level_of(hub, {"id": "p_x", "ui_style": "little"}) == "little"


def test_a_grown_up_changes_a_childs_level_and_a_child_cannot(hub, people):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    body = dylan.client.get("/v1/kids/ratings", headers=dylan.h).json()
    kid = next(k for k in body["kids"] if k["name"] == "Theo")
    assert kid["level"] == "kid" and body["can_change"] is True
    assert [level["id"] for level in body["levels"]] == ["little", "kid", "older", "teen"]
    put = dylan.client.put(f"/v1/people/{kid['person']}/ratings", headers=dylan.h, json={"level": "older"})
    assert put.status_code == 200 and "TV-PG" in put.json()["ratings"]
    assert theo.client.put(f"/v1/people/{kid['person']}/ratings", headers=theo.h,
                           json={"level": "teen"}).status_code == 403
    own = theo.client.get("/v1/kids/ratings", headers=theo.h).json()
    assert [k["level"] for k in own["kids"]] == ["older"] and own["can_change"] is False
    assert dylan.client.put(f"/v1/people/{kid['person']}/ratings", headers=dylan.h,
                            json={"level": "adult"}).status_code == 422
    grown = hub.db.people_with_role("admin")[0]
    assert dylan.client.put(f"/v1/people/{grown['id']}/ratings", headers=dylan.h,
                            json={"level": "kid"}).status_code == 404


def test_narrow_drops_what_the_level_leaves_out_and_keeps_unrated_kids_titles():
    allowed = kidratings.allowed_at("kid")
    feed = {"hero": {"title": "Sitcom", "rating": "TV-PG"}, "rows": [
        {"id": "rewatch", "items": [{"title": "Friends", "rating": "TV-PG"}, {"title": "Bluey", "rating": "TV-Y"}]},
        {"id": "acclaimed", "items": [{"title": "Teen film", "rating": "PG-13"}]},
        {"id": "library", "items": [{"title": "Cartoon Show", "rating": None}, {"title": "Scored", "rating": "7.4"}]},
    ]}
    out = kidratings.narrow(feed, allowed)
    assert out["hero"] is None
    assert [r["id"] for r in out["rows"]] == ["rewatch", "library"]  # an emptied row goes
    assert [c["title"] for c in out["rows"][0]["items"]] == ["Bluey"]
    assert [c["title"] for c in out["rows"][1]["items"]] == ["Cartoon Show", "Scored"]


def test_explore_for_a_child_follows_that_childs_level(hub, people):
    raw = {"kids": {"hero": None, "rows": [{"id": "acclaimed", "title": "Must watch", "items": [
        {"title": "Little film", "kind": "movie", "rating": "G"},
        {"title": "Family sitcom", "kind": "series", "rating": "TV-PG"}]}]}}
    hub.db.kv_set(tvexplore.KV, raw)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    titles = lambda body: [c["title"] for r in body["rows"] for c in r.get("items") or []]  # noqa: E731
    assert "Family sitcom" not in titles(theo.client.get("/v1/tv/explore", headers=theo.h).json())
    kid = next(k for k in dylan.client.get("/v1/kids/ratings", headers=dylan.h).json()["kids"] if k["name"] == "Theo")
    # A grown-up's phone watching as Theo gets Theo's level too.
    as_theo = dylan.client.get("/v1/tv/explore", headers=dylan.h, params={"person": kid["person"]}).json()
    assert "Family sitcom" not in titles(as_theo) and "Little film" in titles(as_theo)
    dylan.client.put(f"/v1/people/{kid['person']}/ratings", headers=dylan.h, json={"level": "older"})
    assert "Family sitcom" in titles(theo.client.get("/v1/tv/explore", headers=theo.h).json())


def test_a_kids_title_above_the_childs_level_never_plays(hub, people):
    _library(hub)
    _login(hub)
    # A kids' category film the provider rated PG-13.
    hub.tv_catalog.ingest(
        vod=[{"stream_id": 101, "name": "Kids Movie", "category_id": "4", "rating": "PG-13"}],
        vod_categories=[{"category_id": "4", "category_name": "Kids"}],
    )
    theo = people["phones"]["Theo"]
    ask = {"movie_id": "vod:thomas-creek:101"}
    assert theo.client.post("/v1/tv/phone/play", headers=theo.h, json=ask).status_code == 404
    assert theo.client.get("/v1/tv/phone/available", headers=theo.h, params=ask).status_code == 404
    theo_id = next(p["id"] for p in hub.db.people_with_role("child") if p["name"] == "Theo")
    kidratings.set_level(hub, theo_id, "teen")
    assert theo.client.get("/v1/tv/phone/available", headers=theo.h, params=ask).status_code == 200


def test_the_admin_people_page_changes_a_childs_level(hub, people, home_client):
    from tests.test_admin_page import _sign_in

    csrf = _sign_in(people, home_client)
    page = home_client.get("/admin/people").text
    assert "/admin/people/ratings" in page and "Kids: adds TV-Y7 and PG films" in page
    theo = people["theo"]
    r = home_client.post("/admin/people/ratings", data={"csrf": csrf, "person": theo["id"], "level": "older"})
    assert "now watches at older kids" in r.text
    assert kidratings.level_of(hub, hub.db.person(theo["id"])) == "older"
    grown = {"csrf": csrf, "person": people["avery"]["id"], "level": "kid"}
    bad = home_client.post("/admin/people/ratings", data=grown)
    assert "children only" in bad.text
    assert home_client.post("/admin/people/ratings", data={"person": theo["id"], "level": "teen"}).status_code == 403
