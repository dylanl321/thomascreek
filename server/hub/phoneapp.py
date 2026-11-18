"""The Android travel phone app, handed out by the hub so a phone away from home can install it.

Builds come from the TV repository's GitHub releases tagged `phone-*` (pre-releases, so the TV update check,
which reads `releases/latest`, never sees them), with one asset, ThomasCreekPhone.apk. The hub reads them with
the TV app's GitHub token, keeps the newest one under `<data>/phoneapp/`, and serves it only to someone holding a
live join code from an admin's iPhone (`/phone?code=…`, routes/downloads.py). The repository is private, so a
GitHub link would need a GitHub sign-in on the phone; this one needs only the code.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from .tvapp_common import MAX_APK, TvAppError, signer_certificates

log = logging.getLogger("hub.phoneapp")

APK = "ThomasCreekPhone.apk"
TAG_PREFIX = "phone-"
KV = "phoneapp.latest"
CHECK_EVERY_S = 10 * 60


class PhoneApp:
    def __init__(self, tv_app: Any, data: Path):
        self.tv = tv_app
        self.dir = data / "phoneapp"
        self.last_check = 0.0
        self.last_error: str | None = None

    @property
    def path(self) -> Path:
        return self.dir / APK

    def latest(self) -> dict[str, Any] | None:
        row = self.tv.db.kv_get(KV)
        return row if isinstance(row, dict) and self.path.is_file() else None

    async def ensure(self, now: float | None = None) -> dict[str, Any] | None:
        """The build to hand out, checking GitHub at most every 10 minutes. A failed check keeps the kept build."""
        now = time.time() if now is None else now
        if now - self.last_check >= CHECK_EVERY_S or self.latest() is None:
            self.last_check = now
            try:
                await self._check()
                self.last_error = None
            except TvAppError as exc:
                self.last_error = exc.message
                log.warning("phoneapp: %s", exc.message)
            except Exception as exc:  # noqa: BLE001  the kept build still works; the cause goes to the log
                self.last_error = "GitHub didn't answer"
                log.warning("phoneapp: check failed (%s)", type(exc).__name__)
        return self.latest()

    async def _check(self) -> None:
        async with self.tv._client() as c:
            r = await c.get(f"/repos/{self.tv.cfg.repo}/releases", params={"per_page": 30})
            if r.status_code in (401, 403):
                raise TvAppError(409, "bad_token", "GitHub refused the TV app token")
            r.raise_for_status()
            rel = next((x for x in r.json() if str(x.get("tag_name") or "").startswith(TAG_PREFIX)
                        and not x.get("draft") and any(a.get("name") == APK for a in x.get("assets") or [])), None)
            if rel is None:
                return
            cur = self.latest()
            if cur and cur.get("tag") == rel["tag_name"]:
                return
            asset = next(a for a in rel["assets"] if a.get("name") == APK)
            if int(asset.get("size") or 0) > MAX_APK:
                raise TvAppError(502, "too_big", "The phone app in the release is too large")
            apk = await self.tv._asset(c, asset)
        self.store(apk, str(rel["tag_name"]))

    def store(self, apk: bytes, tag: str) -> dict[str, Any]:
        """Keep a signed APK (Android checks the signature itself on install; an unsigned file is refused here)."""
        try:
            signer_certificates(apk)
        except ValueError as exc:
            raise TvAppError(502, "unsigned", f"Phone app {tag} isn't properly signed ({exc})") from None
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".part")
        tmp.write_bytes(apk)
        tmp.replace(self.path)
        row = {"tag": tag, "size": len(apk), "sha256": hashlib.sha256(apk).hexdigest(), "fetched_at": time.time()}
        self.tv.db.kv_set(KV, row)
        log.info("phoneapp: kept %s (%d bytes)", tag, len(apk))
        return row
