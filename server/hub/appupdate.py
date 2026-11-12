"""Which build of the iPhone app is current, so the app can offer (or require) an update.

The hub asks App Store Connect for the newest build that TestFlight is handing out,
using an App Store Connect API key saved in the vault:

  appstore.auth_key_p8   the AuthKey_XXXXXXXXXX.p8 file (the same kind of key ios/scripts/upload.sh uses)
  appstore.key_id        its 10-character Key ID (read from the file name when uploaded on the web page)
  appstore.issuer_id     the Issuer ID shown above the keys in App Store Connect

Without a key, Dylan can type the latest build on the hub web page. He can also set a
minimum build: phones older than that see a full-screen "Update required" until they update.
Those two settings live in data/app_update.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from .config import AppUpdateConfig, data_dir
from .vault import Vault

log = logging.getLogger("hub.appupdate")

ASC = "https://api.appstoreconnect.apple.com"
KEY_SECRET = "appstore.auth_key_p8"
KEY_ID_SECRET = "appstore.key_id"
ISSUER_SECRET = "appstore.issuer_id"
# Builds TestFlight is handing out to internal testers.
READY_STATES = {"IN_BETA_TESTING", "READY_FOR_BETA_TESTING"}


def _int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


class AppUpdates:
    def __init__(self, cfg: AppUpdateConfig, vault: Vault, path: Path | None = None):
        self.cfg = cfg
        self.vault = vault
        self.path = path or data_dir() / "app_update.json"
        self.apple_id: str | None = cfg.apple_id
        self.found: dict[str, Any] | None = None  # newest build from App Store Connect
        self.last_check: float | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._jwt: tuple[str, float] | None = None
        self.on_change: Any = None  # called with info() when TestFlight gets a newer build

    # -- settings Dylan sets on the web page ----------------------------------
    def settings(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self, **changes: Any) -> None:
        s = self.settings()
        for k, v in changes.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(self.path)

    @property
    def configured(self) -> bool:
        return all(self.vault.has(n) for n in (KEY_SECRET, KEY_ID_SECRET, ISSUER_SECRET))

    # -- what the app sees ---------------------------------------------------
    def info(self) -> dict[str, Any]:
        s = self.settings()
        manual = _int(s.get("latest_build"))
        if self.cfg.latest_build and (manual is None or self.cfg.latest_build > manual):
            manual = self.cfg.latest_build
        found = self.found or {}
        latest, version, released, source = None, None, None, None
        if found.get("build") is not None:
            latest, version, released, source = found["build"], found.get("version"), found.get("uploaded"), "testflight"
        if manual is not None and (latest is None or manual > latest):
            latest, version, released, source = manual, s.get("latest_version"), s.get("set_at"), "manual"
        minimum = max(_int(s.get("min_build")) or 0, self.cfg.min_build)
        if latest is not None:
            minimum = min(minimum, latest)  # never require a build nobody can get
        apple_id = self.apple_id
        return {
            "latest_build": latest,
            "latest_version": version,
            "released_at": released,
            "min_build": minimum or None,
            "notes": s.get("notes") or None,
            "source": source,
            "apple_id": apple_id,
            "testflight_url": f"itms-beta://beta.itunes.apple.com/v1/app/{apple_id}" if apple_id else "itms-beta://",
            "checked_at": self.last_check,
        }

    # -- App Store Connect ---------------------------------------------------
    def _token(self) -> str:
        if self._jwt and self._jwt[1] - time.time() > 60:
            return self._jwt[0]
        key = str(self.vault.require(KEY_SECRET))
        kid = str(self.vault.require(KEY_ID_SECRET)).strip()
        iss = str(self.vault.require(ISSUER_SECRET)).strip()
        now = int(time.time())
        exp = now + 15 * 60
        tok = jwt.encode({"iss": iss, "iat": now, "exp": exp, "aud": "appstoreconnect-v1"}, key,
                         algorithm="ES256", headers={"kid": kid, "typ": "JWT"})
        self._jwt = (tok, exp)
        return tok

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=ASC, timeout=20)
        r = await self._client.get(path, params=params, headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code == 401:
            self._jwt = None
            raise RuntimeError("App Store Connect turned the key down (check the Issuer ID and Key ID)")
        if r.status_code >= 400:
            try:
                detail = r.json()["errors"][0].get("detail") or r.json()["errors"][0].get("title")
            except Exception:  # noqa: BLE001
                detail = r.text[:120]
            raise RuntimeError(f"App Store Connect said {r.status_code}: {detail}")
        return r.json()

    async def check(self) -> dict[str, Any] | None:
        """Ask App Store Connect for the newest build testers can install. Returns it, or None."""
        if not self.configured:
            self.last_error = None
            return None
        try:
            if not self.apple_id:
                apps = await self._get("/v1/apps", {"filter[bundleId]": self.cfg.bundle_id, "fields[apps]": "bundleId"})
                if not apps.get("data"):
                    raise RuntimeError(f"No app with bundle id {self.cfg.bundle_id} under this key's team")
                self.apple_id = apps["data"][0]["id"]
            data = await self._get("/v1/builds", {
                "filter[app]": self.apple_id,
                "filter[expired]": "false",
                "filter[processingState]": "VALID",
                "sort": "-uploadedDate",
                "limit": "10",
                "fields[builds]": "version,uploadedDate,processingState,expired,preReleaseVersion,buildBetaDetail",
                "include": "preReleaseVersion,buildBetaDetail",
                "fields[preReleaseVersions]": "version",
                "fields[buildBetaDetails]": "internalBuildState",
            })
            before = (self.found or {}).get("build")
            self.found = self.pick(data)
            self.last_error = None
            if self.on_change and self.found and before is not None and self.found["build"] != before:
                self.on_change(self.info())
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc) or type(exc).__name__
            log.warning("app update check: %s", self.last_error)
        self.last_check = time.time()
        return self.found

    @staticmethod
    def pick(data: dict[str, Any]) -> dict[str, Any] | None:
        """The highest build number among valid builds that internal testers can install."""
        inc = {(i["type"], i["id"]): i.get("attributes") or {} for i in data.get("included") or []}
        best: dict[str, Any] | None = None
        for b in data.get("data") or []:
            a = b.get("attributes") or {}
            n = _int(a.get("version"))
            if n is None or a.get("expired") or a.get("processingState", "VALID") != "VALID":
                continue
            rel = b.get("relationships") or {}
            bd = (rel.get("buildBetaDetail") or {}).get("data")
            if bd:
                state = inc.get(("buildBetaDetails", bd["id"]), {}).get("internalBuildState")
                if state and state not in READY_STATES:
                    continue
            pv = (rel.get("preReleaseVersion") or {}).get("data")
            version = inc.get(("preReleaseVersions", pv["id"]), {}).get("version") if pv else None
            if best is None or n > best["build"]:
                best = {"build": n, "version": version, "uploaded": a.get("uploadedDate")}
        return best

    # -- background -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("app update loop: %s", exc)
            await asyncio.sleep(max(60, self.cfg.check_minutes * 60))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
