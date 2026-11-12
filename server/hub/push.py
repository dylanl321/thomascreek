"""Apple Push Notification service sender (token-based auth, HTTP/2).

The .p8 signing key lives in the vault. One provider JWT is reused for up to
50 minutes (Apple asks for between 20 and 60). A 410 from Apple means the
device token is dead, and the hub forgets it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
import jwt

from .config import ApnsConfig
from .vault import Vault

log = logging.getLogger("hub.push")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}


class PushResult:
    def __init__(self, ok: bool, status: int, reason: str | None = None):
        self.ok, self.status, self.reason = ok, status, reason

    @property
    def token_dead(self) -> bool:
        return self.status == 410 or self.reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")


def normalize_pem(text: str) -> str:
    """Rebuild a PEM key whose line breaks were lost (pasted into one field)."""
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S)
    if not m:
        return text
    body = re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {m.group(1)}-----\n" + "\n".join(lines) + f"\n-----END {m.group(1)}-----\n"


class APNs:
    def __init__(self, cfg: ApnsConfig, vault: Vault):
        self.cfg = cfg
        self.vault = vault
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._client: httpx.AsyncClient | None = None
        self.sent = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.team_id and self.cfg.key_id and self.vault.has(self.cfg.key_secret))

    def _provider_token(self) -> str:
        if self._jwt and time.time() - self._jwt_at < 50 * 60:
            return self._jwt
        key = normalize_pem(str(self.vault.require(self.cfg.key_secret)))
        self._jwt = jwt.encode(
            {"iss": self.cfg.team_id, "iat": int(time.time())},
            key,
            algorithm="ES256",
            headers={"kid": self.cfg.key_id},
        )
        self._jwt_at = time.time()
        return self._jwt

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=10)
        return self._client

    async def send(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        env: str = "production",
        push_type: str = "alert",
        priority: int = 10,
        collapse_id: str | None = None,
        topic_suffix: str = "",
        expiration: int | None = None,
    ) -> PushResult:
        if not self.enabled:
            return PushResult(False, 0, "apns disabled")
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self.cfg.topic + topic_suffix,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        if expiration is not None:
            headers["apns-expiration"] = str(expiration)
        url = f"{HOSTS.get(env, HOSTS['production'])}/3/device/{token}"
        try:
            resp = await self._c().post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            self.last_error = f"cannot reach Apple: {type(exc).__name__}"
            log.warning(self.last_error)
            return PushResult(False, 0, "network")
        if resp.status_code == 200:
            self.sent += 1
            self.last_error = None
            return PushResult(True, 200)
        reason = None
        try:
            reason = resp.json().get("reason")
        except Exception:  # noqa: BLE001
            pass
        if resp.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken"):
            self._jwt = None
        self.last_error = f"Apple refused a push: {resp.status_code} {reason}"
        log.warning(self.last_error)
        return PushResult(False, resp.status_code, reason)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


def alert_payload(title: str, body: str | None, *, cat: str, inbox_id: str | None = None,
                  ref: str | None = None, category: str | None = None, level: str = "fyi",
                  thread: str | None = None) -> dict[str, Any]:
    aps: dict[str, Any] = {"alert": {"title": title, "body": body or ""}, "sound": "default"}
    if category:
        aps["category"] = category
    if thread:
        aps["thread-id"] = thread
    aps["interruption-level"] = {"act_now": "time-sensitive", "decision": "active", "maintenance": "passive", "fyi": "passive"}.get(level, "active")
    out: dict[str, Any] = {"aps": aps, "cat": cat}
    if inbox_id:
        out["inbox_id"] = inbox_id
    if ref:
        out["ref"] = ref
    return out
