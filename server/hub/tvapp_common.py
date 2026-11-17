"""Shared pieces of Thomas Creek TV management (see hub/tvapp.py): errors, ADB, APK signatures, helpers."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable


from .tvlineup import valid_key

if TYPE_CHECKING:
    from .tvapp import TvApp

log = logging.getLogger("hub.tvapp")

TOKEN_SECRET = "github.tv_app_token"
# Config field -> vault name. Only lookup keys, never logins or tokens.
METADATA_KEYS = {
    "tmdb": "tmdb.api_key",
    "omdb": "omdb.api_key",
    "opensubtitles": "opensubtitles.api_key",
    "subdl": "subdl.api_key",
}
KV_SIGNER = "tvapp.signer"
KV_BOXES = "tvapp.boxes"
KV_LATEST = "tvapp.latest"
KV_BUILDS = "tv.builds"
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
WATCHING_CLAIM_S = 4 * 3600  # hub/tvwho.CLAIM_S
_PERSON_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ROOM = re.compile(r"^[a-z0-9_]{1,40}$")
_MEDIA = re.compile(r"^media_player\.[a-z0-9_]+$")
_URL = re.compile(r"https?://\S+", re.I)


def _household(db: Any) -> list[dict[str, str]]:
    """First names and roles of the people who live here (no guests, tablets or pending), for TV profiles."""
    out: list[dict[str, str]] = []
    try:
        people = db.people()
    except Exception:  # noqa: BLE001 - a TV config must not fail on the people table
        return out
    for p in people:
        role = p.get("role")
        if role not in ("admin", "adult", "child"):
            continue
        first = str(p.get("name") or "").strip().split(" ")[0][:40]
        if first:
            out.append({"id": str(p["id"]), "name": first, "role": role})
    return out


def _person_layers(db: Any) -> dict[str, dict[str, Any]]:
    from .tvs import person_layers  # tvs imports this module
    return person_layers(db)


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


SHUFFLE_MAX = 2000


def _play_shuffle(value: object) -> dict[str, Any] | None:
    """A TV's shuffle of a show: the episode order it plays and the seasons it picked. Bad input is dropped."""
    if not isinstance(value, dict):
        return None
    order = value.get("order")
    if not isinstance(order, list) or not order:
        return None
    ids: list[str] = []
    for item in order[:SHUFFLE_MAX]:
        clean = _play_id(item)
        if clean and clean not in ids:
            ids.append(clean)
    if not ids:
        return None
    out: dict[str, Any] = {"order": ids}
    seasons = value.get("seasons")
    if isinstance(seasons, list):
        picked = sorted({int(n) for n in seasons if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 1000})
        if picked:
            out["seasons"] = picked
    return out


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
                    "name", "number", "title", "ambience_id")


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
        # Tests replace this to fake binary output (screencap).
        self.raw_runner: Callable[[list[str], float], Awaitable[bytes]] | None = None

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

    async def screencap(self, serial: str, timeout: float = 20) -> bytes | None:
        """A PNG of what the box shows now (`adb exec-out screencap -p`), or None."""
        if self.raw_runner is not None:
            data = await self.raw_runner(["-s", serial, "exec-out", "screencap", "-p"], timeout)
        else:
            self.home.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, HOME=str(self.home), ANDROID_USER_HOME=str(self.home / ".android"))
            proc = await asyncio.create_subprocess_exec(self.binary, "-s", serial, "exec-out", "screencap", "-p",
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL, env=env)
            try:
                data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return None
        return data if data and data.startswith(b"\x89PNG") else None

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

    async def launch(self, serial: str, package: str) -> AdbResult:
        """Open the app once so it registers with the hub."""
        return await self.run(["-s", serial, "shell", "monkey", "-p", package,
                               "-c", "android.intent.category.LEANBACK_LAUNCHER", "1"], 20)

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


class TvAppParts:
    """What the TvApp mixins (tvapp_builds, tvapp_enrol, tvapp_config) share; TvApp.__init__ sets it all."""

    cfg: Any
    vault: Any
    db: Any
    dir: Path
    adb: Adb
    notify: Callable[[str, str], Awaitable[None]] | None
    hub_config: Any
    on_config: Callable[[str | None, int], None] | None
    last_error: str | None
    last_check: float | None
    _transport: Any
    _lock: asyncio.Lock

    if TYPE_CHECKING:
        # A mixin calls methods of the others (self.tvs(), self.bump_config(), ...): all one TvApp at run time.
        def __getattr__(self, name: str) -> Any: ...

