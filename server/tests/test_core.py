from __future__ import annotations

import json
import secrets
from datetime import datetime

import pytest

from hub.policy import Policy, Subject, Target
from hub.vault import Vault, VaultError

from .conftest import Phone


# -- vault ---------------------------------------------------------------
def test_vault_roundtrip_and_no_plaintext(tmp_path):
    key = secrets.token_bytes(32)
    v = Vault(tmp_path / "v.json", key)
    v.set("home_assistant.token", "super-secret-value")
    assert "super-secret-value" not in (tmp_path / "v.json").read_text()
    assert Vault(tmp_path / "v.json", key).get("home_assistant.token") == "super-secret-value"
    assert repr(v.get("home_assistant.token")) == "Secret(****)"


def test_vault_wrong_key_fails(tmp_path):
    Vault(tmp_path / "v.json", secrets.token_bytes(32)).set("a", "b")
    with pytest.raises(VaultError):
        Vault(tmp_path / "v.json", secrets.token_bytes(32))


def test_vault_value_cannot_be_moved_to_another_name(tmp_path):
    key = secrets.token_bytes(32)
    v = Vault(tmp_path / "v.json", key)
    v.set("plex.token", "x")
    doc = json.loads((tmp_path / "v.json").read_text())
    doc["secrets"]["other"] = doc["secrets"]["plex.token"]
    (tmp_path / "v.json").write_text(json.dumps(doc))
    with pytest.raises(VaultError):
        Vault(tmp_path / "v.json", key).get("other")


# -- enrollment and sign-in ------------------------------------------------
def test_enroll_only_from_home(away_client):
    ph = Phone(away_client)
    r = away_client.post("/v1/enroll", json={"device_name": "x", "public_key": ph.pub(ph.key)})
    assert r.status_code == 403
    assert r.json()["error"] == "not_home"


def test_enroll_pending_notifies_admin(hub, home_client):
    dylan = hub.db.add_person("Dylan", "admin")
    ph = Phone(home_client, "New iPhone")
    assert ph.enroll()["status"] == "pending"
    rows = hub.db.all("SELECT * FROM inbox WHERE person_id=?", [dylan["id"]])
    assert len(rows) == 1 and rows[0]["cat"] == "devices" and rows[0]["level"] == "decision"
    # Re-enrolling the same key is idempotent.
    assert ph.enroll()["device_id"] == ph.device_id
    me = home_client.get("/v1/me", headers=(ph.sign_in() and ph.h)).json()
    assert me["role"] == "pending"
    assert "lights.all" not in me["capabilities"]


def test_token_needs_valid_signature_and_single_use_nonce(home_client):
    ph = Phone(home_client)
    ph.enroll()
    nonce = home_client.post("/v1/auth/challenge", json={"device_id": ph.device_id}).json()["nonce"]
    other = Phone(home_client)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    import base64

    bad = base64.b64encode(other.key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))).decode()
    r = home_client.post("/v1/auth/token", json={"device_id": ph.device_id, "nonce": nonce, "signature": bad})
    assert r.status_code == 401
    good = base64.b64encode(ph.key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))).decode()
    r = home_client.post("/v1/auth/token", json={"device_id": ph.device_id, "nonce": nonce, "signature": good})
    assert r.status_code == 401, "nonce was consumed by the failed attempt"


def test_revoked_device_is_cut_off(hub, people, home_client):
    theo = people["phones"]["Theo"]
    assert home_client.get("/v1/me", headers=theo.h).status_code == 200
    hub.revoke_device(hub.db.device(theo.device_id))
    assert home_client.get("/v1/me", headers=theo.h).status_code == 401
    assert home_client.post("/v1/auth/challenge", json={"device_id": theo.device_id}).status_code == 401


def test_me_capabilities_by_role(people, home_client):
    caps = {n: set(home_client.get("/v1/me", headers=p.h).json()["capabilities"]) for n, p in people["phones"].items()}
    assert "people" in caps["Dylan"] and "systems" in caps["Dylan"]
    assert "doors" in caps["Avery"] and "systems" not in caps["Avery"]
    assert caps["Theo"] >= {"lights.own_room", "chores.do", "media.request"}
    assert not caps["Theo"] & {"doors", "cameras", "appliances", "lights.all"}
    me = home_client.get("/v1/me", headers=people["phones"]["Theo"].h).json()
    assert me["person"]["ui_style"] == "kid"
    assert me["child"]["room_id"] == "theo_room"


# -- policy ------------------------------------------------------------------
def _post(client, ph, action, target, params=None):
    return client.post("/v1/actions", json={"action": action, "target": target, "params": params or {}}, headers=ph.h)


def test_child_own_room_yes_other_room_ask_parent(hub, people, home_client):
    theo = people["phones"]["Theo"]
    r = _post(home_client, theo, "light.set", "light.theo_room", {"on": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    r = _post(home_client, theo, "light.set", "light.kitchen", {"on": False})
    assert r.status_code == 403 and r.json()["ask_parent"] is True
    assert hub.calls == [], "dry run never calls Home Assistant"


def test_child_cannot_touch_doors(people, home_client):
    theo = people["phones"]["Theo"]
    assert _post(home_client, theo, "garage.set", "cover.large_garage", {"open": True}).status_code == 403
    assert _post(home_client, theo, "lock.set", "lock.front_door", {"locked": False}).status_code == 403


def test_action_cannot_aim_at_wrong_kind(people, home_client):
    theo = people["phones"]["Theo"]
    r = _post(home_client, theo, "light.set", "lock.front_door", {"on": True})
    assert r.status_code == 400 and r.json()["error"] == "wrong_target"


def test_secure_tier_needs_biometric_signature(hub, people, home_client):
    avery = people["phones"]["Avery"]
    r = _post(home_client, avery, "garage.set", "cover.large_garage", {"open": True})
    assert r.status_code == 428 and r.json()["error"] == "confirm_required"
    r = avery.secure_post("/v1/actions", {"action": "garage.set", "target": "cover.large_garage", "params": {"open": True}})
    assert r.status_code == 200, r.text
    # A signature from the ordinary (non-biometric) key is refused.
    raw = json.dumps({"action": "garage.set", "target": "cover.large_garage", "params": {"open": True}}).encode()
    nonce = home_client.post("/v1/actions", content=raw, headers={**avery.h, "Content-Type": "application/json"}).json()["nonce"]
    from hub.crypto import confirm_message
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    import base64

    sig = base64.b64encode(avery.key.sign(confirm_message(nonce, raw), ec.ECDSA(hashes.SHA256()))).decode()
    r = home_client.post("/v1/actions", content=raw, headers={**avery.h, "Content-Type": "application/json",
                                                              "X-TC-Confirm": f"{nonce}.{sig}"})
    assert r.status_code == 428
    # Closing the garage is everyday: no Face ID.
    assert _post(home_client, avery, "garage.set", "cover.large_garage", {"open": False}).status_code == 200


def test_live_actions_reach_home_assistant(hub, people, home_client):
    hub.config.hub.live_actions = ["light.set"]
    dylan = people["phones"]["Dylan"]
    r = _post(home_client, dylan, "light.set", "light.kitchen", {"on": True, "brightness": 40, "color": "#ff8800"})
    assert r.status_code == 200 and r.json()["dry_run"] is False
    assert hub.calls == [("light", "turn_on", {"entity_id": "light.kitchen", "brightness_pct": 40, "rgb_color": [255, 136, 0]})]
    r = _post(home_client, dylan, "climate.set", "climate.theo_room", {"target_temp": 72})
    assert r.json()["dry_run"] is True and len(hub.calls) == 1


def test_child_thermostat_is_clamped(hub, people, home_client):
    hub.config.hub.dry_run = False
    theo = people["phones"]["Theo"]
    assert _post(home_client, theo, "climate.set", "climate.theo_room", {"target_temp": 85}).status_code == 200
    assert hub.calls[-1] == ("climate", "set_temperature", {"entity_id": "climate.theo_room", "temperature": 74.0})


def test_bedtime_quiets_shared_rooms_for_children():
    p = Policy(timezone="UTC", bedtime="20:30", climate_min=66, climate_max=74)
    kid = Subject(person_id="k", role="child", areas=["theo_room"], shared_areas=["living_room"])
    t = Target(entity_id="light.living_room", area="living_room", kind="light")
    assert p.decide(kid, "light.set", t, {}, now=datetime(2026, 11, 12, 19, 0)).allowed
    assert not p.decide(kid, "light.set", t, {}, now=datetime(2026, 11, 12, 21, 0)).allowed
    own = Target(entity_id="light.theo_room", area="theo_room", kind="light")
    assert p.decide(kid, "light.set", own, {}, now=datetime(2026, 11, 12, 21, 0)).allowed


def test_personal_deny_and_grant():
    p = Policy(timezone="UTC", bedtime="20:30", climate_min=66, climate_max=74)
    kid = Subject(person_id="k", role="child", areas=["theo_room"],
                  grants=[{"action": "light.set", "areas": ["kitchen"], "until": "21:00"}],
                  denies=[{"action": "media.*"}])
    assert p.decide(kid, "light.set", Target(area="kitchen"), {}, now=datetime(2026, 1, 1, 18, 0)).allowed
    assert not p.decide(kid, "light.set", Target(area="kitchen"), {}, now=datetime(2026, 1, 1, 22, 0)).allowed
    assert not p.decide(kid, "media.command", Target(entity_id="x", area="theo_room"), {}).allowed
    # A grant can never lift a child into the secure tier.
    kid.grants.append({"action": "garage.set"})
    assert not p.decide(kid, "garage.set", Target(area="garage"), {"open": True}).allowed


def test_camera_view_is_secure_only_when_away():
    p = Policy(timezone="UTC", bedtime="20:30", climate_min=66, climate_max=74)
    adult = Subject(person_id="a", role="adult", at_home=True)
    assert p.decide(adult, "camera.view", Target(), {}).tier == "look"
    adult.at_home = False
    assert p.decide(adult, "camera.view", Target(), {}).tier == "secure"


# -- snapshot ------------------------------------------------------------------
def test_snapshot_is_filtered_by_role(people, home_client):
    kid = home_client.get("/v1/home", headers=people["phones"]["Theo"].h).json()
    kinds = {d["kind"] for d in kid["devices"]}
    ids = {d["id"] for d in kid["devices"]}
    assert "garage" not in kinds and "lock" not in kinds and kid["cameras"] == []
    assert ids <= {"light.theo_room", "climate.theo_room", "media_player.theo_tv", "light.living_room"}
    assert "systems" not in kid
    admin = home_client.get("/v1/home", headers=people["phones"]["Dylan"].h).json()
    assert {"garage", "lock"} <= {d["kind"] for d in admin["devices"]}
    assert admin["cameras"] and "systems" in admin
    garage = next(d for d in admin["devices"] if d["kind"] == "garage")
    assert garage["actions"] == ["garage.set"] and garage["state"]["open"] is False


def test_activity_log_admin_only(people, home_client):
    _post(home_client, people["phones"]["Theo"], "light.set", "light.kitchen", {"on": True})
    assert home_client.get("/v1/activity", headers=people["phones"]["Avery"].h).status_code == 403
    items = home_client.get("/v1/activity", headers=people["phones"]["Dylan"].h).json()["items"]
    denied = [i for i in items if i["outcome"] == "denied"]
    assert denied and denied[0]["who"] == "Theo" and denied[0]["target"] == "light.kitchen"


# -- requests and approvals -----------------------------------------------------
def test_child_asks_for_internet_adult_approves_with_face_id(hub, people, home_client):
    hub.config.firewalla.person_rules = {people["theo"]["id"]: ["rule1"]}
    theo, avery = people["phones"]["Theo"], people["phones"]["Avery"]
    r = home_client.post("/v1/requests", json={"kind": "internet", "minutes": 30, "text": "homework"}, headers=theo.h)
    assert r.status_code == 200 and r.json()["status"] == "waiting"
    req_id = r.json()["id"]
    inbox = home_client.get("/v1/inbox", headers=avery.h).json()["items"]
    assert any(i["cat"] == "requests" and i["level"] == "decision" for i in inbox)
    assert home_client.post(f"/v1/requests/{req_id}/approve", headers=theo.h).status_code == 403
    r = home_client.post(f"/v1/requests/{req_id}/approve", headers=avery.h)
    assert r.status_code == 428
    r = avery.secure_post(f"/v1/requests/{req_id}/approve", {})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    # The decision cleared from Dylan's inbox too.
    dylan_inbox = home_client.get("/v1/inbox", headers=people["phones"]["Dylan"].h).json()["items"]
    assert not any(i.get("ref", "").endswith(req_id) for i in dylan_inbox)
    # Theo heard back.
    assert any(i["title"].startswith("Yes!") for i in home_client.get("/v1/inbox", headers=theo.h).json()["items"])


# -- admin ---------------------------------------------------------------------
def test_assign_device_needs_admin_and_face_id(hub, people, home_client):
    newbie = Phone(home_client, "Juni's iPad")
    newbie.enroll()
    juni = hub.db.add_person("Juni", "child", ui_style="little")
    avery = people["phones"]["Avery"]
    assert home_client.post(f"/v1/devices/{newbie.device_id}/assign", json={"person_id": juni["id"]},
                            headers=avery.h).status_code == 403
    r = people["phones"]["Dylan"].secure_post(f"/v1/devices/{newbie.device_id}/assign", {"person_id": juni["id"]})
    assert r.status_code == 200 and r.json()["status"] == "active"
    newbie.sign_in()
    me = home_client.get("/v1/me", headers=newbie.h).json()
    assert me["role"] == "child" and me["person"]["ui_style"] == "little"


def test_hello_reports_pin(home_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    r = home_client.get("/v1/hello")
    assert r.status_code == 200
    body = r.json()
    assert body["api"] == "v1" and len(body["pin"]) == 64
    assert all(c in "0123456789abcdef" for c in body["pin"])


def test_bootstrap_admin_from_config(hub):
    import time as _t

    now = _t.time()
    hub.db.insert("devices", {"id": "dev_old", "name": "Old phone", "public_key": "k1", "status": "pending", "created_at": now - 3600})
    hub.config.hub.bootstrap_admin = {"name": "Dylan", "enrolled_after": now - 60}
    assert hub.bootstrap_admin() is None  # no phone enrolled since the cut-off
    hub.db.insert("devices", {"id": "dev_new", "name": "iPhone", "public_key": "k2", "status": "pending", "created_at": now})
    assert hub.bootstrap_admin() == "dev_new"
    assert hub.db.device("dev_new")["status"] == "active"
    assert [p["name"] for p in hub.db.people_with_role("admin")] == ["Dylan"]
    assert hub.bootstrap_admin() is None  # only once


def test_seed_people_from_config(hub):
    hub.db.add_person("Dylan", "admin")
    hub.config.hub.seed_people = [
        {"name": "Lauren", "role": "adult"},
        {"name": "Theo", "role": "child", "ui_style": "kid"},
        {"name": "dylan", "role": "adult"},     # already there (any case): left alone
        {"name": "Mallory", "role": "admin"},   # never seeds an admin
        {"name": "Zed", "role": "wizard"},      # unknown role
    ]
    made = hub.seed_people()
    assert len(made) == 2
    by_name = {p["name"]: p for p in hub.db.people()}
    assert set(by_name) == {"Dylan", "Lauren", "Theo"}
    assert by_name["Dylan"]["role"] == "admin"
    assert by_name["Theo"]["role"] == "child" and by_name["Theo"]["ui_style"] == "kid"
    assert hub.seed_people() == []  # only once


def test_drop_folder_moves_secrets_into_vault(tmp_path):
    from hub.dropbox import import_dropped
    from hub.vault import Vault, generate_master_key, load_master_key

    kf = tmp_path / "k"
    kf.write_text(generate_master_key())
    v = Vault(tmp_path / "vault.json", load_master_key(kf))
    d = tmp_path / "import"
    d.mkdir()
    (d / "plex.token").write_text("abc\n")
    (d / "apns.auth_key_p8.txt").write_text("-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n")
    (d / "Not A Name").write_text("x")
    assert sorted(import_dropped(v, d)) == ["apns.auth_key_p8", "plex.token"]
    assert v.get("plex.token") == "abc"
    assert v.get("apns.auth_key_p8").endswith("-----END PRIVATE KEY-----\n")
    assert list(d.iterdir()) == []


def test_switch_wired_to_a_garage_door_is_secure():
    p = Policy(timezone="UTC", bedtime="20:30", climate_min=66, climate_max=74)
    adult = Subject(person_id="a", role="adult", at_home=True)
    door = Target(entity_id="switch.small_garage_door_toggle_only", kind="switch")
    d = p.decide(adult, "switch.set", door, {"on": True})
    assert d.allowed and d.tier == "secure"
    # Ordinary switches stay everyday, including ones in the garage.
    assert p.decide(adult, "switch.set", Target(entity_id="switch.garage_fan", kind="switch"), {"on": True}).tier == "everyday"
    kid = Subject(person_id="k", role="child", areas=["garage"], grants=[{"action": "switch.set"}])
    assert not p.decide(kid, "switch.set", door, {"on": True}).allowed
