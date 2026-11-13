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
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("hub.tvapp")

TOKEN_SECRET = "github.tv_app_token"
KV_SIGNER = "tvapp.signer"
KV_BOXES = "tvapp.boxes"
KV_LATEST = "tvapp.latest"
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


# -- the manager -----------------------------------------------------------------------------------------------------------

def clean_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        raise TvAppError(400, "bad_ip", "That isn't an IP address") from None
    if not addr.is_private:
        raise TvAppError(400, "bad_ip", "Only boxes on the home network can be added")
    return str(addr)


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

    async def add_box(self, ip: str, name: str | None, auto: bool = True) -> dict[str, Any]:
        ip = clean_ip(ip)
        boxes = self.boxes()
        box = boxes.get(ip, {"added_at": time.time()})
        box.update(name=(name or box.get("name") or ip)[:80], auto=bool(auto))
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
                cur = await self.refresh_box(ip)
                if cur.get("state") == "ok" and (cur.get("installed") or 0) < int(latest["versionCode"]):
                    await self.install(ip)
            except TvAppError as exc:
                log.info("tvapp: %s: %s", ip, exc.message)
            except Exception as exc:  # noqa: BLE001
                log.warning("tvapp: %s: %s", ip, exc)

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
