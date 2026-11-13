"""Thomas Creek TV: fetching verified builds from GitHub and installing them on the Android boxes over ADB."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from hub.config import TvAppConfig
from hub.tvapp import KV_SIGNER, TOKEN_SECRET, Adb, AdbResult, TvApp, TvAppError, signer_certificates

REPO = "dylanl321/thomascreek-tv"
API = "https://api.github.com"


def _lp(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def make_apk(cert: bytes, *, block_id: int = 0x7109871A, signed: bool = True) -> bytes:
    """A tiny ZIP with an APK Signing Block holding one signer whose first certificate is `cert`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00fake")
        z.writestr("classes.dex", b"dex\n035\x00" + bytes(64))
    raw = buf.getvalue()
    if not signed:
        return raw
    eocd = raw.rfind(b"PK\x05\x06")
    cd_off = struct.unpack_from("<I", raw, eocd + 16)[0]
    signed_data = _lp(_lp(b"")) + _lp(_lp(cert)) + _lp(b"")  # digests, certificates, attributes
    signer = _lp(signed_data) + _lp(b"") + _lp(b"pubkey")   # signed data, signatures, public key
    value = _lp(_lp(signer))
    pair = struct.pack("<Q", 4 + len(value)) + struct.pack("<I", block_id) + value
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    head, cd, tail = raw[:cd_off], raw[cd_off:eocd], bytearray(raw[eocd:])
    struct.pack_into("<I", tail, 16, cd_off + len(block))
    return head + block + cd + bytes(tail)


CERT_A = b"0\x82certificate-A"
CERT_B = b"0\x82certificate-B"


def test_signer_certificate_is_read_from_v2_and_v3_blocks():
    assert signer_certificates(make_apk(CERT_A)) == [hashlib.sha256(CERT_A).hexdigest()]
    assert signer_certificates(make_apk(CERT_B, block_id=0xF05368C0)) == [hashlib.sha256(CERT_B).hexdigest()]


def test_unsigned_apk_is_rejected():
    with pytest.raises(ValueError):
        signer_certificates(make_apk(CERT_A, signed=False))
    with pytest.raises(ValueError):
        signer_certificates(b"not a zip")


class FakeAdb(Adb):
    def __init__(self, home: Path, replies: dict[str, str]):
        super().__init__(home, runner=self._fake)
        self.replies = replies
        self.calls: list[list[str]] = []

    @property
    def available(self) -> bool:
        return True

    async def _fake(self, args: list[str], timeout: float) -> AdbResult:
        self.calls.append(args)
        key = args[0] if args[0] != "-s" else args[2] + (" " + args[3] if len(args) > 3 and args[2] == "shell" else "")
        out = self.replies.get(key, "")
        return AdbResult(1 if out.startswith("ERR") else 0, out)


class FakeVault:
    def __init__(self, token: str | None = "github_pat_x"):
        self.token = token

    def has(self, name: str) -> bool:
        return name == TOKEN_SECRET and self.token is not None

    def get(self, name: str):
        return self.token if name == TOKEN_SECRET else None


class FakeDB:
    def __init__(self):
        self.kv: dict = {}
        self.logs: list = []

    def kv_get(self, k):
        return self.kv.get(k)

    def kv_set(self, k, v):
        self.kv[k] = json.loads(json.dumps(v))

    def log(self, action, outcome, **kw):
        self.logs.append((action, outcome, kw))


def _release(mock: respx.MockRouter, apk: bytes, code: int = 601, sha: str | None = None) -> None:
    info = {"versionCode": code, "versionName": f"1.4.1-tc.{code - 600}", "apk": "ThomasCreekTV.apk",
            "sha256": sha or hashlib.sha256(apk).hexdigest(), "notes": "tc-1"}
    mock.get(f"{API}/repos/{REPO}/releases/latest").mock(return_value=httpx.Response(200, json={
        "tag_name": f"tc-{code - 600}",
        "assets": [{"id": 1, "name": "update.json", "size": 100}, {"id": 2, "name": "ThomasCreekTV.apk", "size": len(apk)}]}))
    mock.get(f"{API}/repos/{REPO}/releases/assets/1").mock(return_value=httpx.Response(200, content=json.dumps(info).encode()))
    mock.get(f"{API}/repos/{REPO}/releases/assets/2").mock(return_value=httpx.Response(200, content=apk))


def _tv(tmp_path, adb=None, vault=None, notes=None) -> TvApp:
    async def notify(title, body):
        if notes is not None:
            notes.append((title, body))

    return TvApp(TvAppConfig(), vault or FakeVault(), FakeDB(), tmp_path, adb=adb or FakeAdb(tmp_path / "adb", {}),
                 notify=notify)


@respx.mock
async def test_check_keeps_verified_build_and_pins_signer(tmp_path):
    tv = _tv(tmp_path)
    apk = make_apk(CERT_A)
    _release(respx.mock, apk)
    latest = await tv.check()
    assert latest["versionCode"] == 601
    assert tv.apk_path(601).read_bytes() == apk
    assert tv.db.kv_get(KV_SIGNER) == hashlib.sha256(CERT_A).hexdigest()


@respx.mock
async def test_build_with_another_signer_is_refused(tmp_path):
    notes: list = []
    tv = _tv(tmp_path, notes=notes)
    tv.db.kv_set(KV_SIGNER, hashlib.sha256(CERT_A).hexdigest())
    _release(respx.mock, make_apk(CERT_B), code=602)
    with pytest.raises(TvAppError) as exc:
        await tv.check()
    assert exc.value.code == "signer_changed"
    assert not tv.apk_path(602).exists()
    assert notes and "refused" in notes[0][0]


@respx.mock
async def test_hash_mismatch_is_refused(tmp_path):
    tv = _tv(tmp_path)
    _release(respx.mock, make_apk(CERT_A), sha="0" * 64)
    with pytest.raises(TvAppError) as exc:
        await tv.check()
    assert exc.value.code == "bad_hash"


async def test_check_without_token_says_so(tmp_path):
    tv = _tv(tmp_path, vault=FakeVault(None))
    with pytest.raises(TvAppError) as exc:
        await tv.check()
    assert exc.value.code == "no_token"


def _ready(tmp_path, adb: FakeAdb) -> TvApp:
    tv = _tv(tmp_path, adb=adb)
    tv.dir.mkdir(parents=True, exist_ok=True)
    tv.apk_path(602).write_bytes(b"apk")
    tv.db.kv_set("tvapp.latest", {"versionCode": 602, "versionName": "1.4.1-tc.2"})
    return tv


BOX = "10.0.0.86"
SERIAL = f"{BOX}:5555"


async def test_install_updates_an_allowed_box(tmp_path):
    adb = FakeAdb(tmp_path / "adb", {
        "connect": f"connected to {SERIAL}", "get-state": "device",
        "shell dumpsys": "Packages:\n  versionCode=601 minSdk=24", "install": "Performing Streamed Install\nSuccess"})
    tv = _ready(tmp_path, adb)
    await tv.add_box(BOX, "Living room Shield")
    box = await tv.install(BOX)
    assert box["installed"] == 602 and box["last_result"] == "installed"
    assert ["-s", SERIAL, "install", "-r", str(tv.apk_path(602))] in adb.calls


async def test_box_that_has_not_allowed_the_hub_is_reported(tmp_path):
    adb = FakeAdb(tmp_path / "adb", {"connect": f"failed to authenticate to {SERIAL}"})
    tv = _ready(tmp_path, adb)
    box = await tv.add_box(BOX, None)
    assert box["state"] == "needs_allow"
    with pytest.raises(TvAppError) as exc:
        await tv.install(BOX)
    assert exc.value.code == "needs_allow"
    assert not any("install" in c for c in adb.calls)


async def test_auto_update_waits_while_the_app_is_on_screen(tmp_path):
    adb = FakeAdb(tmp_path / "adb", {
        "connect": f"connected to {SERIAL}", "get-state": "device",
        "shell dumpsys": "versionCode=601\n  mResumedActivity: ActivityRecord{1 u0 me.dlewis.thomascreek.tv/com.ipvio.stream.MainActivity t9}"})
    tv = _ready(tmp_path, adb)
    await tv.add_box(BOX, None)
    await tv.update_all()
    assert not any("install" in c for c in adb.calls)
    assert tv.boxes()[BOX]["last_result"].startswith("postponed")


async def test_boxes_must_be_on_the_home_network(tmp_path):
    tv = _tv(tmp_path)
    with pytest.raises(TvAppError):
        await tv.add_box("8.8.8.8", None)
    with pytest.raises(TvAppError):
        await tv.add_box("not-an-ip", None)


def test_tv_app_routes_are_admin_only(people):
    dylan, avery = people["phones"]["Dylan"], people["phones"]["Avery"]
    r = dylan.client.get("/v1/tvapp", headers=dylan.h)
    assert r.status_code == 200
    body = r.json()
    assert body["package"] == "me.dlewis.thomascreek.tv" and body["boxes"] == [] and body["token_set"] is False
    assert avery.client.get("/v1/tvapp", headers=avery.h).status_code == 403


# -- TVs asking for their source ---------------------------------------------------------------------------------------

import base64  # noqa: E402
import time  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


class TvKey:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def sign(self, install_id: str, ts: int) -> str:
        return base64.b64encode(self.key.sign(f"tvconfig:{install_id}:{ts}".encode(), ec.ECDSA(hashes.SHA256()))).decode()


TV_ID = "tv_0123456789abcdef"


def _config(client, key: TvKey, ts: int | None = None, install_id: str = TV_ID):
    ts = int(time.time()) if ts is None else ts
    return client.post("/v1/tv/config", json={"install_id": install_id, "ts": ts, "signature": key.sign(install_id, ts)})


def test_tv_waits_for_approval_then_gets_its_source(hub, home_client, people):
    hub.vault.set("iptv.xtream_server", "http://iptv.example:8080")
    hub.vault.set("iptv.xtream_username", "household")
    hub.vault.set("iptv.xtream_password", "s3cret")
    key = TvKey()
    r = home_client.post("/v1/tv/enroll", json={"install_id": TV_ID, "name": "Office TV", "model": "onn 4K",
                                                "public_key": key.pub})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "pending" and body["profile"] == "adult"
    assert len(body["code"]) == 6 and body["approve_url"].endswith("/" + body["code"])
    assert _config(home_client, key).json() == {"status": "pending"}

    dylan = people["phones"]["Dylan"]
    tvs = dylan.client.get("/v1/tvapp", headers=dylan.h).json()["tvs"]
    assert [t["id"] for t in tvs] == [TV_ID] and "public_key" in tvs[0]
    assert dylan.client.patch(f"/v1/tvapp/tvs/{TV_ID}", json={"status": "approved"}, headers=dylan.h).status_code == 200

    cfg = _config(home_client, key).json()
    assert cfg["status"] == "approved" and cfg["profile"] == "adult"
    assert cfg["playlists"] == [{"type": "xtream", "name": "Thomas Creek", "server": "http://iptv.example:8080",
                                 "username": "household", "password": "s3cret"}]


def test_tv_on_an_added_box_is_approved_with_the_box_profile(hub, home_client):
    hub.tv_app.db.kv_set("tvapp.boxes", {"192.168.1.50": {"name": "Theo's TV", "auto": True, "profile": "kids"}})
    hub.tv_app.set_kids_categories(["Kids", "Cartoons", "Kids"])
    hub.vault.set("iptv.m3u_url", "http://iptv.example/list.m3u")
    key = TvKey()
    r = home_client.post("/v1/tv/enroll", json={"install_id": TV_ID, "name": "Theo's TV", "public_key": key.pub})
    assert r.json() == {"status": "approved", "profile": "kids", "mode": "adb"}
    cfg = _config(home_client, key).json()
    assert cfg["profile"] == "kids" and cfg["kids_categories"] == ["Kids", "Cartoons"]
    assert cfg["playlists"][0]["type"] == "m3u"


def test_config_needs_the_tvs_own_fresh_signature(hub, home_client):
    key, other = TvKey(), TvKey()
    home_client.post("/v1/tv/enroll", json={"install_id": TV_ID, "name": "TV", "public_key": key.pub})
    hub.tv_app.set_tv(TV_ID, status="approved")
    assert _config(home_client, other).status_code == 401
    assert _config(home_client, key, ts=int(time.time()) - 3600).status_code == 400
    # The same id can't be taken over with another key.
    r = home_client.post("/v1/tv/enroll", json={"install_id": TV_ID, "name": "TV", "public_key": other.pub})
    assert r.status_code == 409
    hub.tv_app.set_tv(TV_ID, status="revoked")
    assert _config(home_client, key).status_code == 403


def test_tv_endpoints_are_home_network_only(away_client):
    key = TvKey()
    r = away_client.post("/v1/tv/enroll", json={"install_id": TV_ID, "name": "TV", "public_key": key.pub})
    assert r.status_code == 403
    assert _config(away_client, key).status_code == 403


def test_overview_never_shows_iptv_secrets(hub, people):
    hub.vault.set("iptv.xtream_server", "http://iptv.example:8080")
    hub.vault.set("iptv.xtream_username", "household")
    hub.vault.set("iptv.xtream_password", "s3cret")
    dylan = people["phones"]["Dylan"]
    body = dylan.client.get("/v1/tvapp", headers=dylan.h)
    assert body.json()["source"] == {"kind": "xtream", "epg": False}
    assert "s3cret" not in body.text and "household" not in body.text
