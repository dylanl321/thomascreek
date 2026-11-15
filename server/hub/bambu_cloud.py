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

try:  # Chrome's TLS fingerprint; Dylan chose this (2026-11-15) after Bambu's Cloudflare answered 403.
    from curl_cffi.requests import AsyncSession as BrowserSession
except ImportError:  # pragma: no cover
    BrowserSession = None

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


def token_claims(token: str | None) -> dict[str, Any]:
    """A JWT's claims, read without checking the signature (Bambu checks it, not us). {} if not a JWT."""
    if not token or token.count(".") != 2:
        return {}
    part = token.split(".")[1]
    for decode in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            v = json.loads(decode(part + "=" * (-len(part) % 4)))
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            continue
    return {}


def token_expiry(token: str | None) -> float | None:
    try:
        exp = token_claims(token).get("exp")
        return float(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


async def _browser_get(url: str, token: str) -> tuple[int, dict[str, Any], str]:
    """GET through curl_cffi as Chrome (the fallback Home Assistant's Bambu integration uses)."""
    if BrowserSession is None:
        return 0, {}, "browser mode not installed"
    async with BrowserSession(impersonate="chrome", timeout=20) as s:
        r = await s.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body if isinstance(body, dict) else {}, (r.text or "")[:400]


async def authed_get(path: str, token: str) -> tuple[int, dict[str, Any], str]:
    """GET an account endpoint: plainly first, then as a browser if that gets 403.

    Returns (status, json body, how it went)."""
    url = f"{API}{path}"
    status, text = 0, ""
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS | {"Authorization": f"Bearer {token}"}) as c:
            r = await c.get(url)
        status, text = r.status_code, r.text[:400]
        if status != 403:
            return status, _json(r), f"HTTP {status}"
    except httpx.HTTPError as exc:
        text = exc.__class__.__name__
    try:
        b_status, body, b_text = await _browser_get(url, token)
    except Exception as exc:  # noqa: BLE001
        return status, {}, f"HTTP {status or text}; as browser: {exc.__class__.__name__}"
    if b_status and b_status != 403:
        return b_status, body, f"as browser HTTP {b_status}"
    who = "Cloudflare" if "cloudflare" in (text + b_text).lower() else "Bambu"
    return status or b_status, {}, f"HTTP {status or text}, as browser {b_status or b_text} ({who})"


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
        if not email:
            raise CloudError("Enter the Bambu account email")
        if not password:
            # Accounts made with Google or Apple have no Bambu password: Bambu emails a login code instead.
            return await self._email_code(email)
        r = await self._post(f"{API}/v1/user-service/user/login", {"account": email, "password": password, "apiError": ""})
        data = _json(r)
        if r.status_code >= 400 and not data.get("loginType"):
            raise CloudError(_message(data) or "Bambu didn't accept that email and password", 401)
        if data.get("accessToken"):
            await self._finish(data)
            return "done"
        kind = data.get("loginType")
        if kind == "verifyCode":
            return await self._email_code(email)
        if kind == "tfa":
            self._pending = {"email": email, "kind": "tfa", "tfa_key": data.get("tfaKey"), "at": time.time()}
            return "tfa"
        raise CloudError(_message(data) or "Bambu didn't accept that email and password", 401)

    async def _email_code(self, email: str) -> str:
        r = await self._post(f"{API}/v1/user-service/user/sendemail/code", {"email": email, "type": "codeLogin"})
        if r.status_code >= 400:
            raise CloudError(_message(_json(r)) or "Bambu didn't send the sign-in code. Try again in a minute.", 502)
        self._pending = {"email": email, "kind": "code", "at": time.time()}
        return "code"

    def cancel(self) -> None:
        self._pending = None

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
        # Keep the token first, so "Try again" can redo the account lookup without a new email code.
        self.hub.vault.set(TOKEN, token)
        if data.get("refreshToken"):
            self.hub.vault.set(REFRESH, str(data["refreshToken"]))
        self.hub.vault.delete(UID)
        self._pending = None
        uid, why = await self._uid(token)
        if not uid:
            self.last_error = f"Couldn't read which Bambu account it is ({why})."
            raise CloudError(f"Signed in, but couldn't read which Bambu account it is ({why}). "
                             "Press Try again under 3D printer, Bambu account; no new code is needed.", 502)
        self.hub.vault.set(UID, str(uid))
        self.last_error = None
        await self.load_devices(force=True)

    async def _uid(self, token: str) -> tuple[str | None, str]:
        """The account id behind the MQTT login u_<uid>.

        Bambu's tokens are usually JWTs carrying ``username`` ("u_<uid>"), so no request is needed
        (this is how Home Assistant's Bambu integration does it). Opaque tokens fall back to the
        account's preference or profile, which can be turned away by Bambu's Cloudflare.
        """
        claims = token_claims(token)
        for key in ("username", "uid", "user_id", "userId"):
            v = claims.get(key)
            if v not in (None, ""):
                v = str(v)
                return (v[2:] if v.startswith("u_") else v), "token"
        seen = []
        for path in ("/v1/design-user-service/my/preference", "/v1/design-user-service/my/profile",
                     "/v1/user-service/my/profile"):
            _, body, how = await authed_get(path, token)
            inner = body.get("data") if isinstance(body.get("data"), dict) else {}
            uid = body.get("uid") or body.get("uidStr") or inner.get("uid")
            if uid:
                return str(uid), path
            seen.append(f"{path.rsplit('/', 1)[-1]}: {how}")
        log.warning("bambu account id not found: %s", "; ".join(seen))
        return None, "; ".join(seen) or "no answer"

    @property
    def needs_account(self) -> bool:
        """Signed in (token kept) but the account lookup hasn't worked yet."""
        return bool(self.token) and not self.uid

    async def retry(self) -> None:
        """Look up the account again with the kept token, so no new email code is needed."""
        token = self.token
        if not token:
            raise CloudError("Sign in first")
        uid, why = await self._uid(token)
        if not uid:
            self.last_error = f"Still couldn't read which Bambu account it is ({why})."
            raise CloudError(self.last_error, 502)
        self.hub.vault.set(UID, str(uid))
        self.last_error = None
        await self.load_devices(force=True)

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
            status, body, how = await authed_get("/v1/iot-service/api/user/bind", str(self.token))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Couldn't reach Bambu: {exc}"
            return self.devices
        if status == 401:
            self.last_error = "The Bambu sign-in has expired. Sign in again on the hub's admin page."
        elif not status or status >= 400:
            self.last_error = f"Couldn't list the account's printers ({how})"
        else:
            self.devices = list(body.get("devices") or [])
            self.devices_at = time.time()
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
