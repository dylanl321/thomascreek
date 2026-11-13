"""Bambu Lab account sign-in, so the hub can watch a printer that isn't in LAN Only mode.

With LAN Only off, the printer still talks to Bambu's cloud. The hub signs in to the
household's Bambu account once (email and password, then the 6-digit code Bambu emails,
or the authenticator code when two-step sign-in is on) and keeps only the resulting
tokens in the vault. The password is used for that one request and never stored.

What the account gives the hub:

* an MQTT login for ``us.mqtt.bambulab.com`` (user ``u_<uid>``, password = token), which
  carries the same status reports the printer sends on the LAN;
* the list of printers on the account, with each one's serial number and access code,
  so the camera and a LAN connection need no extra typing.

Bambu refuses commands from other software that arrive this way (only status and the
light are allowed), so a printer watched through the account is watch-only. See
``bambu.py`` for how the hub switches to full control once LAN Only and Developer Mode
are on.

Vault keys: ``bambu.token``, ``bambu.refresh_token``, ``bambu.uid``.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.bambu_cloud")

API = "https://api.bambulab.com"
WEB = "https://bambulab.com"
MQTT_HOST = "us.mqtt.bambulab.com"
TOKEN, REFRESH, UID = "bambu.token", "bambu.refresh_token", "bambu.uid"
REFRESH_BEFORE_S = 7 * 86400
# Bambu's API sits behind Cloudflare, which turns away clients without a browser-like agent.
HEADERS = {"User-Agent": "bambu_network_agent/01.09.05.01", "X-BBL-Client-Name": "OrcaSlicer",
           "X-BBL-Client-Type": "slicer", "X-BBL-Client-Version": "01.09.05.51", "Accept": "application/json"}


class CloudError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def token_expiry(token: str | None) -> float | None:
    """The exp of a JWT, read without checking the signature (Bambu checks it, not us)."""
    if not token or token.count(".") != 2:
        return None
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(claims["exp"]) if "exp" in claims else None
    except (ValueError, KeyError, TypeError):
        return None


class BambuCloud:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._pending: dict[str, Any] | None = None  # {"email", "kind": "code"|"tfa", "tfa_key", "at"}
        self.devices: list[dict[str, Any]] = []
        self.devices_at = 0.0
        self.last_error: str | None = None

    # -- state ------------------------------------------------------------------
    def _get(self, name: str) -> str | None:
        v = self.hub.vault.get(name)
        return str(v) if v else None

    @property
    def token(self) -> str | None:
        return self._get(TOKEN)

    @property
    def uid(self) -> str | None:
        return self._get(UID)

    @property
    def signed_in(self) -> bool:
        if not (self.token and self.uid):
            return False
        exp = token_expiry(self.token)
        return exp is None or exp > time.time()

    @property
    def mqtt_user(self) -> str | None:
        return f"u_{self.uid}" if self.uid else None

    @property
    def waiting_for(self) -> str | None:
        """"code" or "tfa" while a sign-in waits for its code (for 15 minutes)."""
        if self._pending and time.time() - self._pending["at"] < 900:
            return self._pending["kind"]
        return None

    def info(self) -> dict[str, Any]:
        exp = token_expiry(self.token)
        return {"signed_in": self.signed_in, "waiting_for": self.waiting_for, "expires_at": exp,
                "printers": [{"serial": d.get("dev_id"), "name": d.get("name"), "model": d.get("dev_product_name"),
                              "online": d.get("online")} for d in self.devices],
                "error": self.last_error}

    # -- sign in ----------------------------------------------------------------
    async def _post(self, url: str, body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS) as c:
            return await c.post(url, json=body)

    async def start(self, email: str, password: str) -> str:
        """Returns "done", or "code" (Bambu emailed a code) or "tfa" (authenticator code needed)."""
        email = email.strip()
        if not email or not password:
            raise CloudError("Enter the Bambu account email and password")
        r = await self._post(f"{API}/v1/user-service/user/login", {"account": email, "password": password, "apiError": ""})
        data = _json(r)
        if r.status_code >= 400 and not data.get("loginType"):
            raise CloudError(_message(data) or "Bambu didn't accept that email and password", 401)
        if data.get("accessToken"):
            await self._finish(data)
            return "done"
        kind = data.get("loginType")
        if kind == "verifyCode":
            r2 = await self._post(f"{API}/v1/user-service/user/sendemail/code", {"email": email, "type": "codeLogin"})
            if r2.status_code >= 400:
                raise CloudError("Bambu didn't send the sign-in code. Try again in a minute.", 502)
            self._pending = {"email": email, "kind": "code", "at": time.time()}
            return "code"
        if kind == "tfa":
            self._pending = {"email": email, "kind": "tfa", "tfa_key": data.get("tfaKey"), "at": time.time()}
            return "tfa"
        raise CloudError(_message(data) or "Bambu didn't accept that email and password", 401)

    async def finish(self, code: str) -> None:
        code = "".join(ch for ch in code if ch.isdigit())
        kind = self.waiting_for
        if not kind or not self._pending:
            raise CloudError("Start again: enter the email and password first")
        if len(code) != 6:
            raise CloudError("The code is 6 digits")
        if kind == "code":
            r = await self._post(f"{API}/v1/user-service/user/login", {"account": self._pending["email"], "code": code})
            data = _json(r)
        else:
            r = await self._post(f"{WEB}/api/sign-in/tfa", {"tfaKey": self._pending["tfa_key"], "tfaCode": code})
            data = _json(r)
            if not data.get("accessToken") and r.cookies.get("token"):
                data = {"accessToken": r.cookies.get("token"), "refreshToken": r.cookies.get("refreshToken")}
        if not data.get("accessToken"):
            raise CloudError(_message(data) or "That code didn't work. Check it and try again.", 401)
        await self._finish(data)

    async def _finish(self, data: dict[str, Any]) -> None:
        token = str(data["accessToken"])
        uid = await self._uid(token)
        if not uid:
            raise CloudError("Signed in, but Bambu didn't say which account it is. Try again.", 502)
        self.hub.vault.set(TOKEN, token)
        if data.get("refreshToken"):
            self.hub.vault.set(REFRESH, str(data["refreshToken"]))
        self.hub.vault.set(UID, str(uid))
        self._pending = None
        self.last_error = None
        await self.load_devices(force=True)

    async def _uid(self, token: str) -> str | None:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS | {"Authorization": f"Bearer {token}"}) as c:
            for path in ("/v1/design-user-service/my/preference", "/v1/user-service/my/profile"):
                try:
                    r = await c.get(f"{API}{path}")
                except httpx.HTTPError:
                    continue
                uid = _json(r).get("uid")
                if uid:
                    return str(uid)
        return None

    def sign_out(self) -> None:
        for k in (TOKEN, REFRESH, UID):
            self.hub.vault.delete(k)
        self.devices, self._pending = [], None

    # -- keeping it working --------------------------------------------------------
    async def refresh_if_needed(self) -> None:
        exp = token_expiry(self.token)
        refresh = self._get(REFRESH)
        if exp is None or exp - time.time() > REFRESH_BEFORE_S or not refresh:
            return
        try:
            r = await self._post(f"{API}/v1/user-service/user/refreshtoken", {"refreshToken": refresh})
            data = _json(r)
        except httpx.HTTPError as exc:
            self.last_error = f"Couldn't renew the Bambu sign-in: {exc}"
            return
        if data.get("accessToken"):
            self.hub.vault.set(TOKEN, str(data["accessToken"]))
            if data.get("refreshToken"):
                self.hub.vault.set(REFRESH, str(data["refreshToken"]))
            self.last_error = None
        else:
            self.last_error = "The Bambu sign-in needs renewing on the hub's admin page"

    async def load_devices(self, force: bool = False) -> list[dict[str, Any]]:
        if not self.signed_in or (not force and time.time() - self.devices_at < 600):
            return self.devices
        try:
            async with httpx.AsyncClient(timeout=20, headers=HEADERS | {"Authorization": f"Bearer {self.token}"}) as c:
                r = await c.get(f"{API}/v1/iot-service/api/user/bind")
            if r.status_code == 401:
                self.last_error = "The Bambu sign-in has expired. Sign in again on the hub's admin page."
                return self.devices
            self.devices = list(_json(r).get("devices") or [])
            self.devices_at = time.time()
        except httpx.HTTPError as exc:
            self.last_error = f"Couldn't reach Bambu: {exc}"
        return self.devices

    def device(self, serial: str | None, model: str | None = None) -> dict[str, Any] | None:
        """The account's printer with this serial, or the only one (or the only one of this model)."""
        if serial:
            return next((d for d in self.devices if d.get("dev_id") == serial), None)
        if len(self.devices) == 1:
            return self.devices[0]
        if model:
            same = [d for d in self.devices if model.lower().replace(" ", "") in
                    str(d.get("dev_product_name") or "").lower().replace(" ", "")]
            if len(same) == 1:
                return same[0]
        return None


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        v = r.json()
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _message(data: dict[str, Any]) -> str | None:
    for k in ("message", "error", "msg"):
        if isinstance(data.get(k), str) and data[k]:
            return data[k][:200]
    return None
