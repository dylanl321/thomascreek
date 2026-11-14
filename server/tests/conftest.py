from __future__ import annotations

import base64
import copy
import secrets
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from hub.config import Config
from hub.crypto import confirm_message
from hub.db import DB
from hub.main import build_app
from hub.state import Hub
from hub.vault import Vault

HOME_IP = ("192.168.1.50", 50000)
AWAY_IP = ("203.0.113.9", 50000)

STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen", "brightness": 128},
     "last_updated": "2026-11-12T01:00:00+00:00", "last_changed": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "light.theo_room", "state": "off", "attributes": {"friendly_name": "Theo's lamp"},
     "last_updated": "2026-11-12T01:00:00+00:00", "last_changed": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "light.living_room", "state": "off", "attributes": {"friendly_name": "Living room"},
     "last_updated": "2026-11-12T01:00:00+00:00", "last_changed": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "climate.theo_room", "state": "heat", "attributes": {"friendly_name": "Theo's room",
     "current_temperature": 70, "temperature": 70}, "last_updated": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "cover.large_garage", "state": "closed", "attributes": {"friendly_name": "Large garage door",
     "device_class": "garage"}, "last_updated": "2026-11-12T01:00:00+00:00", "last_changed": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"},
     "last_updated": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "camera.driveway", "state": "idle", "attributes": {"friendly_name": "Driveway"},
     "last_updated": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "media_player.theo_tv", "state": "off", "attributes": {"friendly_name": "Theo's TV"},
     "last_updated": "2026-11-12T01:00:00+00:00"},
    {"entity_id": "scene.movie_night", "state": "scening", "attributes": {"friendly_name": "Movie night"},
     "last_updated": "2026-11-12T01:00:00+00:00"},
]
AREAS = {
    "kitchen": {"id": "kitchen", "name": "Kitchen", "floor_id": "main", "symbol": "fork.knife", "shared": True},
    "theo_room": {"id": "theo_room", "name": "Theo's room", "floor_id": "up", "symbol": "bed.double", "shared": False},
    "living_room": {"id": "living_room", "name": "Living Room", "floor_id": "main", "symbol": "sofa", "shared": True},
    "garage": {"id": "garage", "name": "Garage", "floor_id": "main", "symbol": "car", "shared": False},
    "front": {"id": "front", "name": "Front", "floor_id": "main", "symbol": "door", "shared": False},
}
ENTITY_AREA = {
    "light.kitchen": "kitchen", "light.theo_room": "theo_room", "light.living_room": "living_room",
    "climate.theo_room": "theo_room", "cover.large_garage": "garage", "lock.front_door": "front",
    "camera.driveway": "front", "media_player.theo_tv": "theo_room", "scene.movie_night": "living_room",
}


class Phone:
    """A fake iPhone: a Secure Enclave-style key and a biometric key."""

    def __init__(self, client: TestClient, name: str = "iPhone"):
        self.client = client
        self.name = name
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.secure = ec.generate_private_key(ec.SECP256R1())
        self.device_id: str | None = None
        self.token: str | None = None

    @staticmethod
    def pub(k: ec.EllipticCurvePrivateKey) -> str:
        raw = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.b64encode(raw).decode()

    def enroll(self) -> dict[str, Any]:
        r = self.client.post("/v1/enroll", json={
            "device_name": self.name, "model": "iPhone", "platform": "iOS", "app_version": "1.0",
            "public_key": self.pub(self.key), "secure_public_key": self.pub(self.secure),
        })
        assert r.status_code == 200, r.text
        self.device_id = r.json()["device_id"]
        return r.json()

    def sign_in(self) -> dict[str, Any]:
        nonce = self.client.post("/v1/auth/challenge", json={"device_id": self.device_id}).json()["nonce"]
        sig = self.key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))
        r = self.client.post("/v1/auth/token", json={"device_id": self.device_id, "nonce": nonce,
                                                      "signature": base64.b64encode(sig).decode()})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]
        return r.json()

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def confirm(self, nonce: str, body: bytes) -> str:
        sig = self.secure.sign(confirm_message(nonce, body), ec.ECDSA(hashes.SHA256()))
        return f"{nonce}.{base64.b64encode(sig).decode()}"

    def secure_post(self, path: str, body: dict[str, Any]) -> Any:
        import json as _json

        raw = _json.dumps(body).encode()
        r = self.client.post(path, content=raw, headers={**self.h, "Content-Type": "application/json"})
        if r.status_code != 428:
            return r
        nonce = r.json()["nonce"]
        return self.client.post(path, content=raw, headers={**self.h, "Content-Type": "application/json",
                                                            "X-TC-Confirm": self.confirm(nonce, raw)})


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.hub.home_networks = ["192.168.0.0/16"]
    cfg.hub.bonjour = False
    cfg.home_assistant.url = "http://ha.test"
    return cfg


@pytest.fixture
def hub(tmp_path, config) -> Hub:
    vault = Vault(tmp_path / "vault.json", secrets.token_bytes(32))
    h = Hub(config, vault, DB(tmp_path / "hub.db"))
    h.home.states = {s["entity_id"]: copy.deepcopy(s) for s in STATES}
    h.home.areas = copy.deepcopy(AREAS)
    h.home.entity_area = dict(ENTITY_AREA)
    h.home.floors = [{"id": "main", "name": "Main Floor"}, {"id": "up", "name": "Upstairs"}]
    h.home.ha_ok_at = 1.0
    h.ha.last_ok = 1.0
    h.calls = []  # type: ignore[attr-defined]

    async def call_service(domain, service, data):
        h.calls.append((domain, service, data))  # type: ignore[attr-defined]

    h.ha.call_service = call_service  # type: ignore[method-assign]
    return h


@pytest.fixture
def app(hub):
    return build_app(hub, start_background=False)


@pytest.fixture
def home_client(app):
    with TestClient(app, client=HOME_IP, base_url="https://hub.test") as c:
        yield c


@pytest.fixture
def away_client(app):
    with TestClient(app, client=AWAY_IP, base_url="https://hub.test") as c:
        yield c


@pytest.fixture
def people(hub, home_client):
    """Dylan (admin), an adult, and Theo (child) with enrolled, assigned phones."""
    db = hub.db
    dylan = db.add_person("Dylan", "admin")
    avery = db.add_person("Avery", "adult")
    theo = db.add_person("Theo", "child", areas=["theo_room"], shared_areas=["living_room"],
                         tv_device="media_player.theo_tv", ui_style="kid")
    phones = {}
    for person in (dylan, avery, theo):
        ph = Phone(home_client, f"{person['name']}'s iPhone")
        ph.enroll()
        db.update("devices", "id", ph.device_id, {"person_id": person["id"], "status": "active"})
        ph.sign_in()
        phones[person["name"]] = ph
    return {"dylan": dylan, "avery": avery, "theo": theo, "phones": phones}
