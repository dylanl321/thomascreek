"""A garage opener's own lock (it only stops the remotes opening the door) is never an unlocked door."""

from __future__ import annotations

from hub.home import garage_lock_pairs, kind_for

from .test_smart import at, rules, subj

SMALL = {"entity_id": "cover.small_garage_door", "state": "closed",
         "attributes": {"friendly_name": "Small Garage Door Garage Door", "device_class": "garage"},
         "last_updated": "2026-11-21T01:00:00+00:00"}


def _opener_locks(hub, *, registry: bool = True):
    """Both openers' locks report unlocked (remote lock off), as ratgdo-style openers do."""
    h = hub.home
    h.states["cover.large_garage"]["attributes"]["friendly_name"] = "Large Garage Door Garage Door"
    h.states["cover.small_garage_door"] = dict(SMALL, attributes=dict(SMALL["attributes"]))
    for eid, name in (("lock.large_garage_door_lock", "Large Garage Door Lock"),
                      ("lock.small_garage_door_lock_remotes", "Small Garage Door Lock remotes")):
        h.states[eid] = {"entity_id": eid, "state": "unlocked", "attributes": {"friendly_name": name},
                         "last_updated": "2026-11-21T01:00:00+00:00"}
    for eid in ("cover.small_garage_door", "lock.large_garage_door_lock", "lock.small_garage_door_lock_remotes"):
        h.entity_area[eid] = "garage"
    if registry:
        h.entity_device.update({"cover.large_garage": "dev_large", "lock.large_garage_door_lock": "dev_large",
                                "cover.small_garage_door": "dev_small",
                                "lock.small_garage_door_lock_remotes": "dev_small"})
    h._tag_garage_locks()


def test_registry_pairs_lock_with_its_garage(hub):
    _opener_locks(hub)
    assert hub.home.garage_locks == {"lock.large_garage_door_lock": "cover.large_garage",
                                     "lock.small_garage_door_lock_remotes": "cover.small_garage_door"}
    assert kind_for(hub.home.states["lock.large_garage_door_lock"]) == "garage_lock"
    assert kind_for(hub.home.states["lock.front_door"]) == "lock"


def test_names_pair_lock_without_the_registry():
    states = {
        "cover.big": {"entity_id": "cover.big", "attributes": {"friendly_name": "Large Garage Door Garage Door",
                                                                "device_class": "garage"}},
        "lock.big": {"entity_id": "lock.big", "attributes": {"friendly_name": "Large Garage Door Lock"}},
        # A real lock on the door from the garage into the house stays a lock.
        "lock.entry": {"entity_id": "lock.entry", "attributes": {"friendly_name": "Garage Entry Door"}},
        "lock.front": {"entity_id": "lock.front", "attributes": {"friendly_name": "Front door"}},
    }
    assert garage_lock_pairs(states, {}, {}) == {"lock.big": "cover.big"}
    # A lock in another room never pairs by name.
    assert garage_lock_pairs(states, {}, {"lock.big": "porch", "cover.big": "garage"}) == {}


def test_garage_locks_raise_no_unlocked_warnings(hub, people):
    _opener_locks(hub, registry=False)
    hub.home.states["person.dylan"] = {"entity_id": "person.dylan", "state": "not_home", "attributes": {}}
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    dylan.at_home = False
    assert "away_unlocked" not in rules(hub.smart.cards(dylan, at(hub, h=11)))
    night = [c for c in hub.smart.cards(dylan, at(hub, h=22, mi=5)) if c["rule"] == "lockup"]
    assert not [i for c in night for i in c.get("items") or [] if "Lock" in i["title"]]
    assert all(st["action"] != "lock.set" for c in night for st in c["buttons"][0]["params"].get("steps", []))
    assert not [o for o in hub.household.open_things() if o["kind"] == "lock"]
    # A real door still warns.
    hub.home.states["lock.front_door"]["state"] = "unlocked"
    away = next(c for c in hub.smart.cards(dylan, at(hub, h=11)) if c["rule"] == "away_unlocked")
    assert away["title"] == "Front door is unlocked"


def test_garage_door_carries_its_remote_lock(hub, people, home_client):
    _opener_locks(hub)
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    assert hub.home.device("lock.large_garage_door_lock", dylan) is None
    big = hub.home.device("cover.large_garage", dylan)
    assert big["state"]["remote_lock"] == {"id": "lock.large_garage_door_lock", "name": "Large Garage Door Lock",
                                           "locked": False, "available": True, "can_set": True}
    assert hub.home.device("lock.front_door", dylan)["kind"] == "lock"
    kinds = {d["id"]: d["kind"] for d in hub.home.snapshot(dylan)["devices"]}
    assert "lock.large_garage_door_lock" not in kinds and kinds["cover.large_garage"] == "garage"
    # An opener lock change updates its garage door on screen.
    assert hub.home.event_id("lock.small_garage_door_lock_remotes") == "cover.small_garage_door"


def test_remote_lock_can_still_be_set(hub, people):
    _opener_locks(hub)
    dylan = subj(hub, people["dylan"], people["phones"]["Dylan"])
    plan = hub.actions.plan(dylan, "lock.set", "lock.large_garage_door_lock", {"locked": True})
    assert plan.target.kind == "garage_lock"


def test_tagging_survives_a_fresh_poll(hub):
    _opener_locks(hub)
    hub.home._apply_states([{k: v for k, v in s.items() if k != "tc_garage_of"} for s in hub.home.states.values()])
    assert kind_for(hub.home.states["lock.large_garage_door_lock"]) == "garage_lock"
