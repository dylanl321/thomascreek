"""Thomas Creek TV: the household's IPTV app (a fork of IPVIO Stream) kept installed and current on the Android TV boxes.

Where builds come from
  CI in github.com/dylanl321/thomascreek-tv publishes a GitHub release for every `tc-*` tag with two files:
  ThomasCreekTV.apk and update.json ({"versionCode", "versionName", "apk", "sha256", "notes"}). The repo is
  private, so the hub reads it with a fine-grained GitHub token saved in the vault as `github.tv_app_token`
  (read-only "Contents" access to that one repo).

What the hub checks before keeping a build
  1. the APK's SHA-256 matches update.json (catches a damaged download);
  2. the APK's signing certificate (APK Signature Scheme v2/v3 block) matches the pinned one. The first verified
     build pins it (kv "tvapp.signer"); a later build with a different certificate is refused and Dylan is told.
     Android itself checks the signature on install and refuses an update signed by another key, so the pin is
     about not *offering* a foreign build, not about replacing Android's check.

How it reaches the TVs
  Over ADB on the network (port 5555). Each box is added once by an admin: turn on Developer options and
  Network/Wireless debugging on the box, add it here, then press "Allow" on the TV the first time the hub connects
  (tick "Always allow from this computer"). The hub's ADB key lives under the data dir (adb/.android/adbkey).
  A box with auto-update on gets a new build within one check interval, but never while the app is on screen.

How a TV gets its source (no typing on a remote)
  On first start the app makes a P-256 key in the Android keystore and calls POST /v1/tv/enroll from the home
  network with an install id, its name and the public key. A TV whose IP is a box an admin already added is
  approved at once with that box's profile; any other TV waits for an admin (the hub sends a notice). The app then
  calls POST /v1/tv/config with a signature over "tvconfig:<install id>:<unix time>" and gets the IPTV source
  (vault keys iptv.*), the guide address and its profile (adult or kids, with the kids' category list). Changing a
  source on the hub reaches every TV on its next config call. Revoking a TV stops that at once.

  POST /v1/tv/token is the same kind of signature ("tvtoken:<id>:<unix time>" or with ":<person id>") and returns
  the same short-lived bearer the phones use. That bearer is limited to the TV's room and profile: a kids' TV is a
  child, an adult TV is room-scoped everyday control, and neither can ever lock, open the garage or view a camera.
  The bearer then calls the existing home, action and event routes. Revoking the TV rejects the bearer on the
  next request.

Nothing is installed on a box that an admin hasn't added. Downgrades aren't possible without losing the app's data
(Android only allows them for debuggable apps), so there is no automatic rollback: the previous APK is kept so a
fixed build can be re-tagged quickly.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .crypto import verify_signature
from .db import new_id
from .home import iso
from .policy import ALL_CAPABILITIES, Subject
from .tvlineup import LineupError, normalize_channel, public_channel, valid_key
from .upnext import remember

log = logging.getLogger("hub.tvapp")

TOKEN_SECRET = "github.tv_app_token"
KV_SIGNER = "tvapp.signer"
KV_BOXES = "tvapp.boxes"
KV_LATEST = "tvapp.latest"
KV_TVS = "tvapp.tvs"
KV_KIDS = "tvapp.kids_categories"
KV_CONFIG_VERSION = "tvapp.config_version"
KV_LINEUP = "tvapp.lineup"
KV_COMMANDS = "tvapp.commands"
KV_INSTALL = "tvapp.install_links"
INSTALL_TTL_S = 2 * 3600
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PROFILES = ("adult", "kids")
MAX_TVS = 40
MAX_CHANNELS = 5000
MAX_COMMANDS = 20
COMMAND_TTL_S = 120
CONFIG_SKEW_S = 300
# What a TV token is allowed to keep. Everything else in a role is denied, so
# an admin narrowed onto a TV does not bring doors, cameras or the whole house.
TV_KEEP = frozenset({"lights.own_room", "climate.own_room", "media.own_tv", "family.view"})
_UNSET = object()
_PERSON_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ROOM = re.compile(r"^[a-z0-9_]{1,40}$")
_MEDIA = re.compile(r"^media_player\.[a-z0-9_]+$")
_URL = re.compile(r"https?://\S+", re.I)


def _favorite_channels(db: Any) -> list[str]:
    """Channel keys someone has starred. Pairing includes them so a new TV opens on the familiar ones."""
    found: list[str] = []
    try:
        rows = db.all("SELECT value FROM person_settings WHERE key=?", ["fav.media"])
    except Exception:  # noqa: BLE001
        return found
    for row in rows:
        value = row.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if not isinstance(value, list):
            continue
        for item in value:
            ident = item.get("id") if isinstance(item, dict) else None
            if isinstance(ident, str) and valid_key(ident) and ident not in found:
                found.append(ident)
    return found[:40]


def token_message(install_id: str, ts: int, person_id: str | None) -> bytes:
    """Bytes the TV signs for POST /v1/tv/token. person_id is omitted when it isn't narrowing."""
    if person_id:
        return f"tvtoken:{install_id}:{int(ts)}:{person_id}".encode()
    return f"tvtoken:{install_id}:{int(ts)}".encode()


def _position(value: object) -> int:
    """Seconds into the title. The TV sends this so another room can resume."""
    if isinstance(value, bool):
        raise TvAppError(400, "bad_request", "position_s is a number of seconds")
    try:
        seconds = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise TvAppError(400, "bad_request", "position_s is a number of seconds") from None
    if not 0 <= seconds <= 24 * 3600:
        raise TvAppError(400, "bad_request", "position_s is too far into the title")
    return seconds


def _play_id(value: object) -> str | None:
    text = _clip(value, 80)
    if not text or "://" in text or any(ch.isspace() for ch in text):
        return None
    return text


def _play_start(value: object) -> int | str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = _clip(value, 40)
    if text and "T" in text:
        return text
    return None


def _clip(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _command_result(body: dict[str, Any]) -> dict[str, Any] | None:
    """The TV reporting how a tv.play / tv.stop turned out. Absent when this heartbeat isn't a result."""
    if not body.get("command_id"):
        return None
    ok = body.get("command_ok")
    if not isinstance(ok, bool):
        raise TvAppError(400, "bad_request", "command_ok must be true or false with command_id")
    return {
        "id": str(body["command_id"])[:48],
        "ok": ok,
        "error": None if ok else _redact(body.get("command_error")),
    }


def _redact(value: object) -> str | None:
    text = _clip(value, 200)
    if not text:
        return None
    return _URL.sub("[url]", text)


_COMMAND_FIELDS = ("kind", "channel_key", "start", "movie_id", "series_id", "episode_id", "resume",
                    "name", "number", "title")


def _public_command(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row["id"], "install_id": row["install_id"], "action": row["action"], "at": row.get("at"),
    }
    for key in _COMMAND_FIELDS:
        if key in row and row[key] is not None:
            out[key] = row[key]
    return out
IPTV_SECRETS = ("iptv.xtream_server", "iptv.xtream_username", "iptv.xtream_password", "iptv.m3u_url", "iptv.epg_url")
APK_NAME = "ThomasCreekTV.apk"
GITHUB = "https://api.github.com"
MAX_APK = 200 * 1024 * 1024

# APK Signature Scheme block ids (https://source.android.com/docs/security/features/apksigning/v2)
_SIG_V2 = 0x7109871A
_SIG_V3 = 0xF05368C0
_SIG_V31 = 0x1B93AD61
_MAGIC = b"APK Sig Block 42"


class TvAppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# -- APK signer --------------------------------------------------------------------------------------------------------

def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _lp(b: bytes, o: int) -> tuple[bytes, int]:
    """A uint32 length-prefixed field at o -> (bytes, offset after it)."""
    n = _u32(b, o)
    if o + 4 + n > len(b):
        raise ValueError("length-prefixed field runs past its parent")
    return b[o + 4:o + 4 + n], o + 4 + n


def signer_certificates(apk: bytes) -> list[str]:
    """SHA-256 (hex) of each signer's first certificate in the APK's v3/v2 signature block.

    Reads the "APK Sig Block 42" that sits just before the ZIP central directory. Raises ValueError when the APK
    has no v2/v3 signature (a v1-only or unsigned APK), which the hub refuses anyway.
    """
    eocd = apk.rfind(b"PK\x05\x06", max(0, len(apk) - 65557))
    if eocd < 0 or eocd + 22 > len(apk):
        raise ValueError("not a ZIP file")
    cd_offset = _u32(apk, eocd + 16)
    if cd_offset < 32 or cd_offset > len(apk) or apk[cd_offset - 16:cd_offset] != _MAGIC:
        raise ValueError("no APK signing block (unsigned or v1-only APK)")
    size = struct.unpack_from("<Q", apk, cd_offset - 24)[0]
    start = cd_offset - size - 8
    if start < 0 or struct.unpack_from("<Q", apk, start)[0] != size:
        raise ValueError("damaged APK signing block")
    pairs: dict[int, bytes] = {}
    o, end = start + 8, cd_offset - 24
    while o + 12 <= end:
        n = struct.unpack_from("<Q", apk, o)[0]
        if n < 4 or o + 8 + n > end:
            raise ValueError("damaged APK signing block entry")
        pairs[_u32(apk, o + 8)] = apk[o + 12:o + 8 + n]
        o += 8 + n
    block = pairs.get(_SIG_V31) or pairs.get(_SIG_V3) or pairs.get(_SIG_V2)
    if block is None:
        raise ValueError("no v2/v3 signature in the APK signing block")
    out: list[str] = []
    signers, _ = _lp(block, 0)
    so = 0
    while so < len(signers):
        signer, so = _lp(signers, so)
        signed_data, _ = _lp(signer, 0)
        _digests, do = _lp(signed_data, 0)
        certs, _ = _lp(signed_data, do)
        if certs:
            first, _ = _lp(certs, 0)
            out.append(hashlib.sha256(first).hexdigest())
    if not out:
        raise ValueError("the APK signature has no certificate")
    return out


# -- ADB -------------------------------------------------------------------------------------------------------------------

@dataclass
class AdbResult:
    code: int
    out: str


Runner = Callable[[list[str], float], Awaitable[AdbResult]]


class Adb:
    """Thin wrapper around the `adb` binary. The hub's key lives under <data>/adb/.android (HOME is pointed there)."""

    def __init__(self, home: Path, binary: str = "adb", runner: Runner | None = None):
        self.home = home
        self.binary = binary
        self._run = runner or self._exec

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def _exec(self, args: list[str], timeout: float) -> AdbResult:
        self.home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, HOME=str(self.home), ANDROID_USER_HOME=str(self.home / ".android"))
        proc = await asyncio.create_subprocess_exec(self.binary, *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return AdbResult(124, "timed out")
        return AdbResult(proc.returncode or 0, out.decode(errors="replace").strip())

    async def run(self, args: list[str], timeout: float = 20) -> AdbResult:
        return await self._run(args, timeout)

    async def connect(self, serial: str) -> str:
        """-> "ok" | "needs_allow" (TV shows the Allow prompt) | "offline" (debugging off, wrong IP or box asleep)."""
        r = await self.run(["connect", serial], 15)
        text = r.out.lower()
        if "connected to" in text and "cannot" not in text and "failed" not in text:
            st = await self.run(["-s", serial, "get-state"], 10)
            s = st.out.lower()
            if "unauthorized" in s:
                return "needs_allow"
            if st.code == 0 and "device" in s:
                return "ok"
            return "needs_allow" if "authoriz" in s else "offline"
        if "authenticate" in text or "unauthorized" in text:
            return "needs_allow"
        return "offline"

    async def installed_version(self, serial: str, package: str) -> int | None:
        r = await self.run(["-s", serial, "shell", "dumpsys", "package", package], 20)
        m = re.search(r"versionCode=(\d+)", r.out)
        return int(m.group(1)) if m else None

    async def in_foreground(self, serial: str, package: str) -> bool:
        r = await self.run(["-s", serial, "shell", "dumpsys", "activity", "activities"], 20)
        for line in r.out.splitlines():
            if ("ResumedActivity" in line or "mFocusedApp" in line) and package + "/" in line:
                return True
        return False

    async def install(self, serial: str, apk: Path) -> AdbResult:
        return await self.run(["-s", serial, "install", "-r", str(apk)], 300)

    async def pair(self, host_port: str, code: str) -> AdbResult:
        """Android 11+ wireless debugging: `adb pair ip:port code`."""
        return await self.run(["pair", host_port, code], 30)

    async def props(self, serial: str) -> dict[str, str]:
        r = await self.run(["-s", serial, "shell", "getprop"], 15)
        out: dict[str, str] = {}
        for line in r.out.splitlines():
            m = re.match(r"\[([^\]]+)\]: \[([^\]]*)\]", line.strip())
            if m:
                out[m.group(1)] = m.group(2)
        return out

    async def grant_overlay(self, serial: str, package: str) -> AdbResult:
        return await self.run(["-s", serial, "shell", "appops", "set", package, "SYSTEM_ALERT_WINDOW", "allow"], 15)

    async def grant(self, serial: str, package: str, permission: str) -> AdbResult:
        return await self.run(["-s", serial, "shell", "pm", "grant", package, permission], 15)

    async def current_home(self, serial: str) -> str | None:
        r = await self.run(["-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief",
                            "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"], 15)
        for line in r.out.splitlines():
            line = line.strip()
            if "/" in line and " " not in line:
                return line
        return None

    async def set_home(self, serial: str, component: str) -> AdbResult:
        """Points Home at component. Does not disable the previous launcher."""
        return await self.run(["-s", serial, "shell", "cmd", "package", "set-home-activity", component], 20)

    async def mdns(self) -> str:
        r = await self.run(["mdns", "services"], 8)
        return r.out


# -- the manager -----------------------------------------------------------------------------------------------------------

def clean_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        raise TvAppError(400, "bad_ip", "That isn't an IP address") from None
    if not addr.is_private:
        raise TvAppError(400, "bad_ip", "Only boxes on the home network can be added")
    return str(addr)


def _update_reports(tv: TvApp) -> list[dict[str, Any]]:
    # tvupdate imports this module, so the admin list is loaded when the overview is built.
    from .tvupdate import listing

    return listing(tv)


class TvApp:
    def __init__(self, cfg: Any, vault: Any, db: Any, data: Path, *, adb: Adb | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 notify: Callable[[str, str], Awaitable[None]] | None = None):
        self.cfg = cfg
        self.vault = vault
        self.db = db
        self.dir = data / "tvapp"
        self.adb = adb or Adb(data / "adb")
        self._transport = transport
        self.notify = notify
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._waiters: dict[str, list[asyncio.Event]] = {}
        self.last_error: str | None = None
        self.last_check: float | None = None

    # -- state -------------------------------------------------------------------------------------------------------
    def boxes(self) -> dict[str, dict[str, Any]]:
        return dict(self.db.kv_get(KV_BOXES) or {})

    def _save_boxes(self, boxes: dict[str, dict[str, Any]]) -> None:
        self.db.kv_set(KV_BOXES, boxes)

    def latest(self) -> dict[str, Any] | None:
        return self.db.kv_get(KV_LATEST)

    def apk_path(self, code: int) -> Path:
        return self.dir / f"{int(code)}.apk"

    def overview(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.cfg.enabled),
            "repo": self.cfg.repo,
            "package": self.cfg.package,
            "token_set": self.vault.has(TOKEN_SECRET),
            "adb_available": self.adb.available,
            "latest": self.latest(),
            "signer": self.db.kv_get(KV_SIGNER),
            "last_check": self.last_check,
            "last_error": self.last_error,
            "boxes": [dict(v, ip=k) for k, v in sorted(self.boxes().items())],
            "tvs": [dict(v, id=k) for k, v in sorted(self.tvs().items(), key=lambda kv: kv[1].get("created_at", 0))],
            "source": self.source_summary(),
            "kids_categories": self.kids_categories(),
            "config_version": self.config_version(),
            "lineup_version": self.lineup_version(),
            "updates": _update_reports(self),
        }

    # -- releases ----------------------------------------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        tok = self.vault.get(TOKEN_SECRET)
        if not tok:
            raise TvAppError(409, "no_token", "Add the GitHub token for the TV app on the hub web page")
        return httpx.AsyncClient(base_url=GITHUB, timeout=60, follow_redirects=True, transport=self._transport,
                                 headers={"Authorization": f"Bearer {tok}", "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "thomascreek-hub"})

    async def check(self) -> dict[str, Any] | None:
        """Fetch the newest release; download, verify and keep it if it's new. -> the latest build or None."""
        async with self._lock:
            self.last_check = time.time()
            try:
                out = await self._check()
                self.last_error = None
                return out
            except TvAppError as exc:
                self.last_error = exc.message
                raise

    async def _check(self) -> dict[str, Any] | None:
        async with self._client() as c:
            r = await c.get(f"/repos/{self.cfg.repo}/releases/latest")
            if r.status_code == 404:
                return self.latest()  # no release yet
            if r.status_code in (401, 403):
                raise TvAppError(409, "bad_token", "GitHub refused the TV app token. Make a new one with read access to the repo.")
            r.raise_for_status()
            rel = r.json()
            assets = {a.get("name"): a for a in rel.get("assets") or []}
            if "update.json" not in assets or APK_NAME not in assets:
                raise TvAppError(502, "bad_release", f"Release {rel.get('tag_name')} is missing update.json or {APK_NAME}")
            info = json.loads((await self._asset(c, assets["update.json"])).decode())
            code = int(info["versionCode"])
            sha = str(info.get("sha256") or "").lower()
            cur = self.latest()
            if cur and int(cur.get("versionCode", 0)) >= code and self.apk_path(int(cur["versionCode"])).exists():
                return cur
            if int(assets[APK_NAME].get("size") or 0) > MAX_APK:
                raise TvAppError(502, "too_big", "The APK in the release is too large")
            apk = await self._asset(c, assets[APK_NAME])
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or hashlib.sha256(apk).hexdigest() != sha:
            raise TvAppError(502, "bad_hash", f"Build {info.get('versionName')} doesn't match its update.json; not kept")
        try:
            certs = signer_certificates(apk)
        except ValueError as exc:
            raise TvAppError(502, "unsigned", f"Build {info.get('versionName')} isn't properly signed ({exc})") from None
        pinned = self.db.kv_get(KV_SIGNER)
        if pinned is None:
            self.db.kv_set(KV_SIGNER, certs[0])
            pinned = certs[0]
            log.info("tvapp: pinned signer %s", pinned)
        if pinned not in certs:
            msg = f"Build {info.get('versionName')} is signed with a different key than earlier builds; it was not installed anywhere"
            if self.notify:
                await self.notify("TV app build refused", msg)
            raise TvAppError(502, "signer_changed", msg)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.apk_path(code).with_suffix(".part")
        tmp.write_bytes(apk)
        tmp.replace(self.apk_path(code))
        latest = {"versionCode": code, "versionName": str(info.get("versionName") or code), "sha256": sha,
                  "notes": str(info.get("notes") or "")[:500], "tag": rel.get("tag_name"), "fetched_at": time.time()}
        self.db.kv_set(KV_LATEST, latest)
        self._prune(keep=(code, int(cur["versionCode"])) if cur else (code,))
        return latest

    async def _asset(self, c: httpx.AsyncClient, asset: dict[str, Any]) -> bytes:
        r = await c.get(f"/repos/{self.cfg.repo}/releases/assets/{asset['id']}",
                        headers={"Accept": "application/octet-stream"})
        r.raise_for_status()
        return r.content

    def _prune(self, keep: tuple[int, ...]) -> None:
        for p in self.dir.glob("*.apk"):
            if p.stem.isdigit() and int(p.stem) not in keep:
                p.unlink(missing_ok=True)

    # -- boxes ---------------------------------------------------------------------------------------------------------
    def _serial(self, ip: str) -> str:
        return f"{ip}:{int(self.cfg.adb_port)}"

    async def add_box(self, ip: str, name: str | None, auto: bool = True, profile: str = "adult") -> dict[str, Any]:
        ip = clean_ip(ip)
        if profile not in PROFILES:
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        boxes = self.boxes()
        box = boxes.get(ip, {"added_at": time.time()})
        box.update(name=(name or box.get("name") or ip)[:80], auto=bool(auto), profile=profile)
        boxes[ip] = box
        self._save_boxes(boxes)
        return await self.refresh_box(ip)

    def remove_box(self, ip: str) -> None:
        boxes = self.boxes()
        if boxes.pop(clean_ip(ip), None) is None:
            raise TvAppError(404, "not_found", "That box isn't on the list")
        self._save_boxes(boxes)

    def set_auto(self, ip: str, auto: bool) -> dict[str, Any]:
        boxes = self.boxes()
        ip = clean_ip(ip)
        if ip not in boxes:
            raise TvAppError(404, "not_found", "That box isn't on the list")
        boxes[ip]["auto"] = bool(auto)
        self._save_boxes(boxes)
        return dict(boxes[ip], ip=ip)

    def _update_box(self, ip: str, **fields: Any) -> dict[str, Any]:
        boxes = self.boxes()
        if ip not in boxes:
            raise TvAppError(404, "not_found", "That box isn't on the list")
        boxes[ip].update(fields)
        self._save_boxes(boxes)
        return dict(boxes[ip], ip=ip)

    async def refresh_box(self, ip: str) -> dict[str, Any]:
        if not self.adb.available:
            return self._update_box(ip, state="no_adb", checked_at=time.time())
        serial = self._serial(ip)
        state = await self.adb.connect(serial)
        fields: dict[str, Any] = {"state": state, "checked_at": time.time()}
        if state == "ok":
            fields["installed"] = await self.adb.installed_version(serial, self.cfg.package)
        return self._update_box(ip, **fields)

    async def install(self, ip: str, *, force: bool = False) -> dict[str, Any]:
        """Install (or update to) the latest kept build on one box. force=True also installs while the app is open."""
        ip = clean_ip(ip)
        latest = self.latest()
        if not latest or not self.apk_path(int(latest["versionCode"])).exists():
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet")
        box = await self.refresh_box(ip)
        if box.get("state") == "no_adb":
            raise TvAppError(409, "no_adb", "The hub can't run adb (it isn't installed in the hub image)")
        if box.get("state") != "ok":
            raise TvAppError(409, box.get("state") or "offline",
                             "Press Allow on the TV, then try again" if box.get("state") == "needs_allow"
                             else "The box didn't answer. Is it on, with network debugging turned on?")
        serial = self._serial(ip)
        if not force and await self.adb.in_foreground(serial, self.cfg.package):
            return self._update_box(ip, last_result="postponed: the app is on screen")
        r = await self.adb.install(serial, self.apk_path(int(latest["versionCode"])))
        ok = r.code == 0 and "Success" in r.out
        result = "installed" if ok else f"failed: {r.out[-200:]}"
        self.db.log("tvapp.install", "ok" if ok else "error", target=ip,
                    params={"version": latest["versionName"]}, detail=None if ok else r.out[-200:], source="hub")
        fields: dict[str, Any] = {"last_result": result, "last_install_at": time.time()}
        if ok:
            fields["installed"] = int(latest["versionCode"])
        out = self._update_box(ip, **fields)
        if not ok:
            if self.notify:
                await self.notify("TV app update failed", f"{out.get('name') or ip}: {r.out[-160:]}")
            raise TvAppError(502, "install_failed", f"The box refused the install: {r.out[-200:]}")
        return out

    async def update_all(self) -> None:
        latest = self.latest()
        if not latest:
            return
        for ip, box in self.boxes().items():
            if not box.get("auto"):
                continue
            try:
                if box.get("overnight_only") and not self.in_update_window():
                    continue
                cur = await self.refresh_box(ip)
                if cur.get("state") == "ok" and (cur.get("installed") or 0) < int(latest["versionCode"]):
                    await self.install(ip)
            except TvAppError as exc:
                log.info("tvapp: %s: %s", ip, exc.message)
            except Exception as exc:  # noqa: BLE001
                log.warning("tvapp: %s: %s", ip, exc)

    def in_update_window(self, moment: Any | None = None) -> bool:
        """Automatic installs run from 1am to 6am in the house timezone. A manual install does not wait."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if moment is None:
            clock = getattr(self, "now", None)
            moment = clock() if clock else datetime.now(ZoneInfo("America/New_York"))
        return 1 <= moment.hour < 6

    # -- TVs and their source ------------------------------------------------------------------------------------------
    def tvs(self) -> dict[str, dict[str, Any]]:
        return dict(self.db.kv_get(KV_TVS) or {})

    def _save_tvs(self, tvs: dict[str, dict[str, Any]]) -> None:
        self.db.kv_set(KV_TVS, tvs)

    def kids_categories(self) -> list[str]:
        return list(self.db.kv_get(KV_KIDS) or [])

    def set_kids_categories(self, cats: list[str]) -> list[str]:
        clean = list(dict.fromkeys(str(c).strip()[:80] for c in cats if str(c).strip()))[:200]
        self.db.kv_set(KV_KIDS, clean)
        self.bump_config()
        return clean

    def source_summary(self) -> dict[str, Any]:
        """What kind of source is set, without any secret in it."""
        have = {n: self.vault.has(n) for n in IPTV_SECRETS}
        kind = "xtream" if all(have[n] for n in IPTV_SECRETS[:3]) else "m3u" if have["iptv.m3u_url"] else None
        return {"kind": kind, "epg": have["iptv.epg_url"]}

    def _source(self) -> dict[str, Any]:
        def v(n: str) -> str:
            return str(self.vault.get(n) or "").strip()

        playlists: list[dict[str, Any]] = []
        if v("iptv.xtream_server") and v("iptv.xtream_username") and v("iptv.xtream_password"):
            playlists.append({"type": "xtream", "name": "Thomas Creek", "server": v("iptv.xtream_server"),
                              "username": v("iptv.xtream_username"), "password": v("iptv.xtream_password")})
        if v("iptv.m3u_url"):
            playlists.append({"type": "m3u", "name": "Thomas Creek" if not playlists else "Thomas Creek M3U",
                              "url": v("iptv.m3u_url")})
        return {"playlists": playlists, "epg_url": v("iptv.epg_url") or None}

    def _short_code(self) -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))

    def install_links(self) -> dict[str, dict[str, Any]]:
        raw = self.db.kv_get(KV_INSTALL) or {}
        now = time.time()
        live = {k: v for k, v in raw.items() if float(v.get("expires_at") or 0) > now}
        if live != raw:
            self.db.kv_set(KV_INSTALL, live)
        return live

    def create_install_link(self, *, name: str | None, room: str | None, profile: str) -> dict[str, Any]:
        """A short-lived code that lets Downloader or a browser fetch the signed APK on the home network."""
        if profile not in PROFILES:
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        if room:
            self._room(room)
        latest = self.latest()
        if not latest or not self.apk_path(int(latest["versionCode"])).exists():
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet")
        links = self.install_links()
        code = self._short_code()
        while code in links:
            code = self._short_code()
        rec = {
            "code": code, "name": (name or "TV")[:80], "room": room, "profile": profile,
            "created_at": time.time(), "expires_at": time.time() + INSTALL_TTL_S, "mode": "non-adb",
        }
        links[code] = rec
        self.db.kv_set(KV_INSTALL, links)
        boxes = self.boxes()
        boxes[f"code:{code}"] = {
            "added_at": rec["created_at"], "name": rec["name"], "profile": profile, "room": room,
            "auto": False, "setup": True, "mode": "non-adb", "phase": "install",
            "message": "On the TV, open Downloader or a browser and open the address from the phone.",
            "you_do": "Install Thomas Creek TV from the address on the phone",
            "install_code": code, "home": {"wanted": False},
            "capabilities": {"overlay": False, "home": False, "adb": False},
        }
        self._save_boxes(boxes)
        return rec

    def install_link(self, code: str) -> dict[str, Any] | None:
        return self.install_links().get((code or "").strip().upper())

    def apk_file(self) -> Path:
        latest = self.latest()
        if not latest:
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet")
        path = self.apk_path(int(latest["versionCode"]))
        if not path.is_file():
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet")
        return path

    def update_manifest(self) -> dict[str, Any]:
        """What the installed app needs to download and check the next APK itself."""
        latest = self.latest() or {}
        if not latest or not self.apk_path(int(latest.get("versionCode") or 0)).is_file():
            raise TvAppError(409, "no_build", "The hub doesn't have a TV app build yet")
        return {
            "version_code": int(latest["versionCode"]),
            "version_name": latest.get("versionName"),
            "sha256": latest.get("sha256"),
            "signer_sha256": self.db.kv_get(KV_SIGNER),
            "apk_path": "/v1/tv/apk",
            "package": self.cfg.package,
            "notes": latest.get("notes") or None,
            "install": "Download the APK with this TV's bearer, check sha256 and signer_sha256, then hand the file to PackageInstaller. The person confirms on the TV.",
        }

    def approve_code(self, code: str, *, room: str | None = None, profile: str | None = None) -> dict[str, Any]:
        """Approve a TV that is showing this code. No credentials are typed on the TV."""
        want = (code or "").strip().upper()
        if not re.fullmatch(r"[A-Z2-9]{6}", want):
            raise TvAppError(400, "bad_code", "That code isn't one the TV would show")
        found = None
        for install_id, tv in self.tvs().items():
            if tv.get("approve_code") == want and tv.get("status") == "pending":
                found = install_id
                break
        if found is None:
            raise TvAppError(404, "not_found", "No TV is waiting with that code")
        if profile is not None and profile not in PROFILES:
            raise TvAppError(400, "bad_profile", "Profile is adult or kids")
        if room:
            self._room(room)
        return self.set_tv(found, status="approved", profile=profile, room=room if room else _UNSET)

    async def enroll(self, ip: str, install_id: str, name: str, model: str | None, app_version: str | None,
                     public_key: str, code: str | None = None) -> dict[str, Any]:
        from .crypto import CryptoError, normalize_public_key

        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", install_id or ""):
            raise TvAppError(400, "bad_install_id", "install_id must be 16-64 letters, digits, - or _")
        try:
            pk = normalize_public_key(public_key)
        except CryptoError as exc:
            raise TvAppError(400, "bad_key", str(exc)) from None
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv and tv.get("public_key") != pk:
            raise TvAppError(409, "key_mismatch", "This TV id is already used with another key. Clear the app's data and try again.")
        if tv is None:
            if len(tvs) >= MAX_TVS:
                raise TvAppError(429, "too_many", "Too many TVs are registered. Remove old ones on the hub.")
            tv = {"public_key": pk, "status": "pending", "profile": "adult", "created_at": time.time()}
        tv.update(name=(name or "TV")[:80], model=(model or "")[:80] or None, app_version=(app_version or "")[:40] or None,
                  ip=ip, last_seen_at=time.time())
        link = self.install_link(code) if code else None
        box = self.boxes().get(ip)
        newly_pending = False
        if tv["status"] == "pending" and link is not None:
            # The phone already created this install link, so the TV is approved without anyone typing on it.
            tv.update(status="approved", profile=link.get("profile") or "adult", approved_at=time.time(),
                      approved_by="code", mode="non-adb", room=link.get("room") or tv.get("room"))
            if link.get("name"):
                tv["name"] = link["name"]
            boxes = self.boxes()
            manual = boxes.get(f"code:{link['code']}")
            if manual is not None:
                manual.update(phase="ready", message="This TV is registered and has its source.", you_do=None, ip=ip)
                boxes[f"code:{link['code']}"] = manual
                self._save_boxes(boxes)
        elif tv["status"] == "pending" and box is not None:
            # An admin added this box for the TV app already: that is the approval.
            tv.update(status="approved", profile=box.get("profile") or "adult", approved_at=time.time(), approved_by="box",
                      mode=box.get("mode") or "adb")
            if box.get("room") and not tv.get("room"):
                tv["room"] = box["room"]
        elif tv["status"] == "pending" and install_id not in tvs:
            newly_pending = True
        if tv["status"] == "pending" and not tv.get("approve_code"):
            tv["approve_code"] = self._short_code()
            tv["mode"] = tv.get("mode") or "non-adb"
        tvs[install_id] = tv
        self._save_tvs(tvs)
        self.db.log("tvapp.enroll", "ok", target=install_id, params={"name": tv["name"], "ip": ip, "status": tv["status"]},
                    source="tv")
        if newly_pending and self.notify:
            await self.notify("A TV wants the TV app source",
                              f"{tv['name']} is showing code {tv.get('approve_code')}. Approve it in the app.")
        out = {"status": tv["status"], "profile": tv["profile"], "mode": tv.get("mode") or "adb"}
        if tv["status"] == "pending" and tv.get("approve_code"):
            out["code"] = tv["approve_code"]
            out["approve_url"] = f"thomascreek://tv/{tv['approve_code']}"
        return out

    def set_tv(self, install_id: str, *, status: str | None = None, profile: str | None = None,
               name: str | None = None, room: Any = _UNSET, media_entity: Any = _UNSET) -> dict[str, Any]:
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv is None:
            raise TvAppError(404, "not_found", "No such TV")
        if status is not None:
            if status not in ("approved", "revoked", "pending"):
                raise TvAppError(400, "bad_status", "Status is approved, revoked or pending")
            tv["status"] = status
            if status == "approved":
                tv["approved_at"] = time.time()
                tv["approved_by"] = "admin"
        if profile is not None:
            if profile not in PROFILES:
                raise TvAppError(400, "bad_profile", "Profile is adult or kids")
            tv["profile"] = profile
        if name:
            tv["name"] = name[:80]
        if room is not _UNSET:
            tv["room"] = self._room(room)
        if media_entity is not _UNSET:
            tv["media_entity"] = self._media_entity(media_entity)
        tvs[install_id] = tv
        self._save_tvs(tvs)
        self.bump_config()
        return dict(tv, id=install_id)

    @staticmethod
    def _room(value: object) -> str | None:
        if value is None or value == "":
            return None
        text = str(value)
        if not _ROOM.fullmatch(text):
            raise TvAppError(400, "bad_room", "room is a house area id, such as living_room")
        return text

    @staticmethod
    def _media_entity(value: object) -> str | None:
        if value is None or value == "":
            return None
        text = str(value)
        if not _MEDIA.fullmatch(text):
            raise TvAppError(400, "bad_target", "media_entity is a media_player id")
        return text

    def remove_tv(self, install_id: str) -> None:
        tvs = self.tvs()
        if tvs.pop(install_id, None) is None:
            raise TvAppError(404, "not_found", "No such TV")
        self._save_tvs(tvs)

    def _verified(self, install_id: str, ts: int, signature: str, message: bytes, now: float, *,
                  log_action: str) -> dict[str, Any]:
        tv = self.tvs().get(install_id or "")
        if tv is None:
            raise TvAppError(404, "unknown_tv", "This TV isn't registered. Open the app at home to register it.")
        if abs(now - int(ts)) > CONFIG_SKEW_S:
            raise TvAppError(400, "clock", "The TV's clock is off by more than five minutes")
        if not verify_signature(tv["public_key"], message, signature or ""):
            self.db.log(log_action, "denied", target=install_id, detail="bad signature", source="tv")
            raise TvAppError(401, "bad_signature", "Signature didn't match this TV")
        if tv.get("status") == "revoked":
            raise TvAppError(403, "revoked", "This TV was removed on the hub")
        return tv

    def config_for(self, install_id: str, ts: int, signature: str, ip: str | None, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        tv = self._verified(install_id, ts, signature, f"tvconfig:{install_id}:{int(ts)}".encode(), now,
                            log_action="tvapp.config")
        if tv["status"] != "approved":
            return {"status": "pending"}
        tvs = self.tvs()
        tvs[install_id].update(last_config_at=now, ip=ip or tv.get("ip"))
        self._save_tvs(tvs)
        src = self._source()
        out: dict[str, Any] = {
            "status": "approved",
            "version": self.config_version(),
            "name": tv.get("name"),
            "profile": tv.get("profile", "adult"),
            "room": tv.get("room"),
            "media_entity": tv.get("media_entity"),
            "playlists": src["playlists"],
            "epg_url": src["epg_url"],
            "lineup_version": self.lineup_version(),
            "theme": self.theme(),
        }
        if out["profile"] == "kids":
            out["kids_categories"] = self.kids_categories()
        return out

    def approved_config(self, install_id: str) -> dict[str, Any]:
        """The config body for a TV that is already approved. Pairing returns this with the token."""
        tv = self.tvs().get(install_id) or {}
        if tv.get("status") != "approved":
            return {"status": "pending"}
        src = self._source()
        out: dict[str, Any] = {
            "status": "approved",
            "version": self.config_version(),
            "name": tv.get("name"),
            "profile": tv.get("profile", "adult"),
            "room": tv.get("room"),
            "media_entity": tv.get("media_entity"),
            "playlists": src["playlists"],
            "epg_url": src["epg_url"],
            "lineup_version": self.lineup_version(),
            "theme": self.theme(),
            "favorites": _favorite_channels(self.db),
        }
        if out["profile"] == "kids":
            out["kids_categories"] = self.kids_categories()
        return out

    def config_version(self) -> int:
        return int(self.db.kv_get(KV_CONFIG_VERSION) or 1)

    def bump_config(self) -> int:
        version = self.config_version() + 1
        self.db.kv_set(KV_CONFIG_VERSION, version)
        return version

    def theme(self) -> dict[str, str] | None:
        raw = self.db.kv_get("tvapp.theme")
        if not isinstance(raw, dict):
            return None
        out: dict[str, str] = {}
        for key in ("accent", "background", "motion"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()[:40]
        return out or None

    # -- token, lineup, state, commands --------------------------------------------------------------------------------
    @staticmethod
    def key_fingerprint(tv: dict[str, Any]) -> str:
        return hashlib.sha256(str(tv.get("public_key") or "").encode()).hexdigest()[:16]

    def begin_token(self, install_id: str, ts: int, signature: str, person_id: str | None,
                    ip: str | None, now: float | None = None) -> tuple[Subject, dict[str, Any], str]:
        """Check the device-key signature and return (subject, device, key fingerprint).

        The same timestamp cannot be used twice, so a captured sign-in cannot mint
        another bearer. The caller still has to put the fingerprint in the token
        and re-check it on every later request.
        """
        now = time.time() if now is None else now
        person_id = person_id or None
        if person_id is not None and not _PERSON_ID.fullmatch(person_id):
            raise TvAppError(400, "bad_request", "person_id is not a person id")
        tv = self._verified(install_id, ts, signature, token_message(install_id, ts, person_id), now,
                            log_action="tvapp.token")
        if tv.get("status") != "approved":
            raise TvAppError(403, "pending", "This TV isn't approved yet")
        if int(ts) <= int(tv.get("last_token_ts") or 0):
            raise TvAppError(401, "replay", "This sign-in was already used. Ask again.")
        subject = self.subject_for(tv, install_id, person_id)
        device = self._device(tv, install_id)
        fingerprint = self.key_fingerprint(tv)
        tvs = self.tvs()
        tvs[install_id].update(last_token_ts=int(ts), last_seen_at=now, ip=ip or tv.get("ip"))
        self._save_tvs(tvs)
        self.db.log("tvapp.token", "ok", person_id=subject.person_id, device_id=device["id"], target=install_id,
                    params={"role": subject.role, "room": subject.areas[:1] or None}, source="tv")
        return subject, device, fingerprint

    def session(self, install_id: str, person_id: str | None, key_fp: str | None) -> tuple[Subject, dict[str, Any]]:
        """Rebuild the TV's subject from the current record. The token's role is not trusted."""
        tv = self.tvs().get(install_id or "")
        if tv is None:
            raise TvAppError(401, "unknown_tv", "This TV isn't registered. Open the app at home to register it.")
        if tv.get("status") == "revoked" or (key_fp and key_fp != self.key_fingerprint(tv)):
            raise TvAppError(401, "revoked", "This TV was removed on the hub")
        if tv.get("status") != "approved":
            raise TvAppError(403, "pending", "This TV isn't approved yet")
        if person_id is not None and not isinstance(person_id, str):
            person_id = str(person_id)
        subject = self.subject_for(tv, install_id, person_id or None)
        device = self._device(tv, install_id)
        tvs = self.tvs()
        if install_id in tvs:
            tvs[install_id]["last_seen_at"] = time.time()
            self._save_tvs(tvs)
        return subject, device

    def subject_for(self, tv: dict[str, Any], install_id: str, person_id: str | None) -> Subject:
        """The TV's rights: its profile and room, optionally narrowed to one person.

        A kids' profile is always a child. An adult profile starts as room-scoped
        everyday control (the tablet bundle, with whole-house caps removed).
        Naming a person can only narrow that. Grants on the person are not copied.
        Denies are. Doors, cameras and every whole-house capability are denied.
        """
        person = self._person(person_id) if person_id else None
        profile = tv.get("profile") or "adult"
        role = self._tv_role(profile, person)
        areas, shared = self._tv_areas(person, tv.get("room"), tv.get("media_entity"))
        denies = list(person.get("denies") or []) if person else []
        denies += [{"capability": cap} for cap in ALL_CAPABILITIES if cap not in TV_KEEP]
        return Subject(
            person_id=person["id"] if person else None,
            role=role,
            areas=areas,
            shared_areas=shared,
            grants=[{"capability": cap} for cap in ("lights.own_room", "climate.own_room", "media.own_tv")],
            denies=denies,
            tv_device=tv.get("media_entity"),
            at_home=True,
            device_id=f"tv:{install_id}",
            name=tv.get("name") or "TV",
            tv=True,
            tv_id=install_id,
        )

    def _person(self, person_id: str) -> dict[str, Any]:
        person = self.db.person(person_id)
        if person is None:
            raise TvAppError(404, "not_found", "No such person")
        if person.get("expires_at") and person["expires_at"] < time.time():
            raise TvAppError(403, "forbidden", "That person's access has ended")
        return person

    @staticmethod
    def _tv_role(profile: str, person: dict[str, Any] | None) -> str:
        if profile == "kids":
            if person is not None and person.get("role") != "child":
                raise TvAppError(403, "forbidden", "A kids' TV can't act as a grown-up")
            return "child"
        if person is None:
            return "tablet"
        role = person.get("role")
        if role == "child":
            return "child"
        if role == "guest":
            return "guest"
        if role in ("adult", "admin", "tablet"):
            # An admin or adult is narrowed to room-scoped everyday control.
            return "tablet"
        raise TvAppError(403, "forbidden", "That person can't use a TV")

    @staticmethod
    def _tv_areas(person: dict[str, Any] | None, room: str | None,
                  media_entity: str | None) -> tuple[list[str], list[str]]:
        if person is None:
            return ([room] if room else []), []
        role = person.get("role")
        if role in ("admin", "adult", "tablet"):
            return ([room] if room else []), []
        own = set(person.get("areas") or [])
        shared = set(person.get("shared_areas") or [])
        if room and room in own:
            return [room], []
        if room and room in shared:
            return [], [room]
        # No overlap with this room. Their own player still matches tv_device
        # when this TV is that player; otherwise the token can look but not act.
        if person.get("tv_device") and person.get("tv_device") == media_entity:
            return ([room] if room and room in own else []), []
        return [], []

    @staticmethod
    def _device(tv: dict[str, Any], install_id: str) -> dict[str, Any]:
        return {
            "id": f"tv:{install_id}",
            "name": tv.get("name") or "TV",
            "status": "active",
            "kind": "tv",
            "install_id": install_id,
        }

    def lineup_version(self) -> int:
        return int(self._lineup().get("version") or 0)

    def _lineup(self) -> dict[str, Any]:
        raw = self.db.kv_get(KV_LINEUP) or {}
        if not isinstance(raw, dict):
            return {"version": 0, "channels": []}
        channels = raw.get("channels")
        return {"version": int(raw.get("version") or 0),
                "channels": channels if isinstance(channels, list) else []}

    def set_lineup(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if len(rows) > MAX_CHANNELS:
            raise TvAppError(400, "too_many", "Too many channels")
        try:
            channels = [normalize_channel(row) for row in rows]
        except LineupError as exc:
            raise TvAppError(400, exc.code, exc.message) from None
        seen: set[str] = set()
        for row in channels:
            if row["key"] in seen:
                raise TvAppError(400, "bad_channel", f"Channel {row['key']} is listed twice")
            seen.add(row["key"])
        version = self.lineup_version() + 1
        self.db.kv_set(KV_LINEUP, {"version": version, "channels": channels})
        self.bump_config()
        shown = self.public_lineup(kids_only=False)
        shown["hidden"] = sum(1 for row in channels if row.get("hidden"))
        return shown

    def public_lineup(self, *, kids_only: bool) -> dict[str, Any]:
        doc = self._lineup()
        rows = []
        for row in doc["channels"]:
            if row.get("hidden"):
                continue
            if kids_only and not row.get("kids"):
                continue
            shown = public_channel(row)
            art = getattr(self, "artwork", None)
            shown["logo_url"] = art.public_url("logo", shown["key"]) if art is not None else None
            rows.append(shown)
        rows.sort(key=lambda row: (row["number"] is None, row["number"] or 0, row["name"].casefold()))
        return {"version": doc["version"], "channels": rows}

    def require_channel(self, key: str) -> dict[str, Any]:
        if not valid_key(key or ""):
            raise TvAppError(400, "bad_key", "channel_key must be an iptv-org, Xtream or tvg id")
        for row in self._lineup()["channels"]:
            if row.get("key") == key and not row.get("hidden"):
                return row
        raise TvAppError(404, "not_found", "That channel isn't in the lineup")

    def resolve(self, target: str) -> tuple[str, dict[str, Any]]:
        tvs = self.tvs()
        if target in tvs:
            return target, tvs[target]
        matches = [(iid, tv) for iid, tv in tvs.items()
                   if tv.get("media_entity") == target and tv.get("status") == "approved"]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TvAppError(409, "ambiguous", "That player is linked to more than one TV")
        raise TvAppError(404, "not_found", "That TV isn't registered")

    def record_state(self, install_id: str, body: dict[str, Any]) -> dict[str, Any]:
        playing = self._playing(body.get("playing"))
        health = self._health(body.get("health"))
        foreground = bool(body.get("foreground", True))
        now = time.time()
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv is None or tv.get("status") != "approved":
            raise TvAppError(401, "revoked", "This TV was removed on the hub")
        tv.update(playing=playing, foreground=foreground, health=health, state_at=now, last_seen_at=now)
        if health.get("app_version"):
            tv["app_version"] = health["app_version"]
        command = _command_result(body)
        if command is not None:
            tv["last_command"] = command
        tvs[install_id] = tv
        self._save_tvs(tvs)
        self.db.log("tvapp.state", "ok", device_id=f"tv:{install_id}", target=install_id,
                    params={"playing": (playing or {}).get("channel_key"), "foreground": foreground,
                            "command": None if command is None else command.get("id")}, source="tv")
        if playing:
            remember(self.db, "kids" if tv.get("profile") == "kids" else "adult", playing, now)
        event = self.watch_event(install_id)
        if command is not None:
            event["command"] = command
        return event

    def watch_event(self, install_id: str) -> dict[str, Any]:
        tv = self.tvs().get(install_id) or {}
        return {
            "id": install_id,
            "name": tv.get("name"),
            "room": tv.get("room"),
            "profile": tv.get("profile"),
            "media_entity": tv.get("media_entity"),
            "foreground": bool(tv.get("foreground", True)),
            "playing": tv.get("playing"),
            "at": iso(tv.get("state_at")),
        }

    def _playing(self, raw: object) -> dict[str, Any] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise TvAppError(400, "bad_request", "playing must be an object")
        key = raw.get("channel_key")
        if key:
            if not valid_key(str(key)):
                raise TvAppError(400, "bad_key", "channel_key must be an iptv-org, Xtream or tvg id")
            key = str(key)
        else:
            key = None
        number = raw.get("number")
        if number is not None:
            try:
                number = int(number)
            except (TypeError, ValueError):
                raise TvAppError(400, "bad_request", "number must be an integer") from None
            if not 0 <= number <= 99999:
                raise TvAppError(400, "bad_request", "number is out of range")
        title = _clip(raw.get("title"), 120)
        name = _clip(raw.get("name"), 80)
        position = _position(raw.get("position_s")) if "position_s" in raw and raw.get("position_s") is not None else None
        movie = _play_id(raw.get("movie_id"))
        series = _play_id(raw.get("series_id"))
        episode = _play_id(raw.get("episode_id"))
        if key is None and title is None and name is None and number is None and movie is None and episode is None:
            return None
        out: dict[str, Any] = {"channel_key": key, "title": title, "name": name, "number": number}
        if position is not None:
            out["position_s"] = position
        if movie:
            out["movie_id"] = movie
        if series:
            out["series_id"] = series
        if episode:
            out["episode_id"] = episode
        kind = raw.get("kind")
        if kind in ("live", "catchup", "movie", "episode"):
            out["kind"] = kind
        start = _play_start(raw.get("start"))
        if start is not None:
            out["start"] = start
        return out

    def _health(self, raw: object) -> dict[str, Any]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise TvAppError(400, "bad_request", "health must be an object")
        out: dict[str, Any] = {}
        version = _clip(raw.get("app_version"), 40)
        if version:
            out["app_version"] = version
        profile = raw.get("profile")
        if profile in PROFILES:
            out["profile"] = profile
        age = raw.get("epg_age_s")
        if age is not None:
            try:
                age_i = int(age)
            except (TypeError, ValueError):
                raise TvAppError(400, "bad_request", "epg_age_s must be a number of seconds") from None
            if age_i >= 0:
                out["epg_age_s"] = min(age_i, 8640000)
        error = _redact(raw.get("last_error"))
        if error:
            out["last_error"] = error
        return out

    def enqueue_command(self, install_id: str, command: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        stored = {"id": new_id("tvc"), "install_id": install_id, "action": command["action"], "at": iso(now), "ts": now}
        for key in _COMMAND_FIELDS:
            if command.get(key) is not None:
                stored[key] = command[key]
        all_commands = self.db.kv_get(KV_COMMANDS) or {}
        queued = [row for row in all_commands.get(install_id, []) if self._command_fresh(row, now)]
        queued.append(stored)
        all_commands[install_id] = queued[-MAX_COMMANDS:]
        self.db.kv_set(KV_COMMANDS, all_commands)
        if stored["action"] == "play":
            self._remember_play(install_id, stored)
        self.kick(install_id)
        return _public_command(stored)

    def _remember_play(self, install_id: str, stored: dict[str, Any]) -> None:
        """What the hub last asked this TV to play, so a title-only heartbeat can still move rooms."""
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv is None:
            return
        play = {key: stored[key] for key in _COMMAND_FIELDS if stored.get(key) is not None and key != "resume"}
        tv["last_play"] = play
        tvs[install_id] = tv
        self._save_tvs(tvs)

    def pending_commands(self, install_id: str) -> list[dict[str, Any]]:
        now = time.time()
        all_commands = self.db.kv_get(KV_COMMANDS) or {}
        queued = list(all_commands.get(install_id, []))
        fresh = [row for row in queued if self._command_fresh(row, now)]
        if len(fresh) != len(queued):
            all_commands[install_id] = fresh
            self.db.kv_set(KV_COMMANDS, all_commands)
        return [_public_command(row) for row in fresh]

    def ack_commands(self, install_id: str, ids: list[str]) -> int:
        want = {str(i) for i in ids}
        all_commands = self.db.kv_get(KV_COMMANDS) or {}
        queued = list(all_commands.get(install_id, []))
        kept = [row for row in queued if row.get("id") not in want]
        all_commands[install_id] = kept
        self.db.kv_set(KV_COMMANDS, all_commands)
        return len(queued) - len(kept)

    async def wait_commands(self, install_id: str, timeout: float) -> list[dict[str, Any]]:
        event = asyncio.Event()
        self._waiters.setdefault(install_id, []).append(event)
        try:
            found = self.pending_commands(install_id)
            if found or timeout <= 0:
                return found
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            waiters = self._waiters.get(install_id, [])
            if event in waiters:
                waiters.remove(event)
        return self.pending_commands(install_id)

    def kick(self, install_id: str) -> None:
        for event in self._waiters.get(install_id, []):
            event.set()

    @staticmethod
    def _command_fresh(row: dict[str, Any], now: float) -> bool:
        try:
            return now - float(row.get("ts") or 0) < COMMAND_TTL_S
        except (TypeError, ValueError):
            return False

    # -- background ----------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self.cfg.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                if self.vault.has(TOKEN_SECRET):
                    await self.check()
                await self.update_all()
                from .tvsetup import reconcile

                await reconcile(self)
            except asyncio.CancelledError:
                raise
            except TvAppError as exc:
                log.info("tvapp check: %s", exc.message)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)[:200]
                log.warning("tvapp loop: %s", exc)
            await asyncio.sleep(max(300, int(self.cfg.check_minutes) * 60))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
