"""Thomas Creek TV builds and boxes: releases from GitHub, the signer pin, ADB installs and updates."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .tvapp_common import (
    TvAppParts,
    APK_NAME,
    GITHUB,
    KV_BOXES,
    KV_BUILDS,
    KV_LATEST,
    KV_SIGNER,
    MAX_APK,
    PROFILES,
    TOKEN_SECRET,
    TvAppError,
    _update_reports,
    clean_ip,
    log,
    signer_certificates,
)


class BuildsMixin(TvAppParts):
    """Part of TvApp (hub/tvapp.py); uses its attributes (cfg, vault, db, dir, adb, ...)."""

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
            "updates": _update_reports(self),  # type: ignore[arg-type]  # self is the whole TvApp
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
            cur = self.latest()
            if cur and int(cur.get("versionCode", 0)) >= code and self.apk_path(int(cur["versionCode"])).exists():
                return cur
            if int(assets[APK_NAME].get("size") or 0) > MAX_APK:
                raise TvAppError(502, "too_big", "The APK in the release is too large")
            apk = await self._asset(c, assets[APK_NAME])
        try:
            return self.store_build(apk, info, tag=rel.get("tag_name"), announce=True)
        except TvAppError as exc:
            if exc.code == "signer_changed" and self.notify:
                await self.notify("TV app build refused", exc.message)
            raise

    async def _asset(self, c: httpx.AsyncClient, asset: dict[str, Any]) -> bytes:
        r = await c.get(f"/repos/{self.cfg.repo}/releases/assets/{asset['id']}",
                        headers={"Accept": "application/octet-stream"})
        r.raise_for_status()
        return r.content

    def store_build(self, apk: bytes, info: dict[str, Any], *, tag: str | None = None, announce: bool = False) -> dict[str, Any]:
        """Keep a signed APK whose checksum matches update.json. The newest one is what TVs install."""
        try:
            code = int(info.get("versionCode"))
        except (TypeError, ValueError):
            raise TvAppError(400, "bad_release", "That update.json isn't the one from the TV build.") from None
        if code < 1 or isinstance(info.get("versionCode"), bool):
            raise TvAppError(400, "bad_release", "That update.json isn't the one from the TV build.")
        name = str(info.get("versionName") or code)
        sha = str(info.get("sha256") or "").lower()
        status = 502 if announce else 400
        if len(apk) > MAX_APK:
            raise TvAppError(status, "too_big", "That build is too large.")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or hashlib.sha256(apk).hexdigest() != sha:
            raise TvAppError(status, "bad_hash", f"Build {name} doesn't match its update.json; not kept")
        try:
            certs = signer_certificates(apk)
        except ValueError as exc:
            raise TvAppError(status, "unsigned", f"Build {name} isn't properly signed ({exc})") from None
        pinned = self.db.kv_get(KV_SIGNER)
        if pinned is None:
            self.db.kv_set(KV_SIGNER, certs[0])
            pinned = certs[0]
            log.info("tvapp: pinned signer %s", pinned)
        if pinned not in certs:
            raise TvAppError(status, "signer_changed",
                             f"Build {name} is signed with a different key than earlier builds; it was not installed anywhere")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.apk_path(code).with_suffix(".part")
        tmp.write_bytes(apk)
        tmp.replace(self.apk_path(code))
        self._remember_build(code, name, sha, str(info.get("notes") or "")[:500], tag)
        cur = self.latest() or {}
        if not cur or code >= int(cur.get("versionCode") or 0):
            cur = {"versionCode": code, "versionName": name, "sha256": sha,
                   "notes": str(info.get("notes") or "")[:500], "tag": tag, "fetched_at": time.time()}
            self.db.kv_set(KV_LATEST, cur)
        self._prune(self._kept_codes(code))
        return self.latest() or cur

    def _remember_build(self, code: int, name: str, sha: str, notes: str, tag: str | None) -> None:
        raw = self.db.kv_get(KV_BUILDS) or {}
        if not isinstance(raw, dict):
            raw = {}
        raw[str(code)] = {"versionCode": code, "versionName": name, "sha256": sha, "notes": notes or None,
                          "tag": tag, "fetched_at": time.time()}
        self.db.kv_set(KV_BUILDS, raw)

    def _kept_codes(self, newest: int) -> tuple[int, ...]:
        """The latest build and up to seven older ones, so the Downloads page can still offer them."""
        found = {newest}
        for path in self.dir.glob("*.apk"):
            if path.stem.isdigit():
                found.add(int(path.stem))
        return tuple(sorted(found, reverse=True)[:8])

    def _prune(self, keep: tuple[int, ...]) -> None:
        kept = set(keep)
        for path in self.dir.glob("*.apk"):
            if path.stem.isdigit() and int(path.stem) not in kept:
                path.unlink(missing_ok=True)
        raw = self.db.kv_get(KV_BUILDS) or {}
        if isinstance(raw, dict):
            left = {key: row for key, row in raw.items() if str(key).isdigit() and int(key) in kept}
            if left != raw:
                self.db.kv_set(KV_BUILDS, left)

    # -- boxes ---------------------------------------------------------------------------------------------------------
    def _serial(self, ip: str) -> str:
        """ip:port. Wireless debugging (Android 11+) listens on a random port the hub learned from mDNS."""
        port = (self.boxes().get(ip) or {}).get("connect_port") or self.cfg.adb_port
        return f"{ip}:{int(port)}"

    async def wireless_ports(self, ip: str) -> dict[str, Any]:
        """{connect_port?, pairing_port?} this TV advertises for Wireless debugging right now."""
        from .tvsetup import parse_mdns

        if not self.adb.available:
            return {}
        for row in parse_mdns(await self.adb.mdns()):
            if row.get("ip") == ip:
                return row
        return {}

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
        if state == "offline":
            # Wireless debugging picks a new port after a reboot or a toggle; look it up and try once more.
            found = await self.wireless_ports(ip)
            port = found.get("connect_port")
            if port and f"{ip}:{int(port)}" != serial:
                self._update_box(ip, connect_port=int(port), wireless=True)
                serial = self._serial(ip)
                state = await self.adb.connect(serial)
                fields["state"] = state
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
