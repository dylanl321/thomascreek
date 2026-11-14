"""Shared adapter plumbing.

Each adapter wraps one home system. It reads its address from config and its
credential from the vault. Errors are reduced to short, plain messages; raw
responses (which might echo a token back) are never logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import ServiceConfig
from ..vault import Vault

log = logging.getLogger("hub.adapters")


class AdapterError(RuntimeError):
    pass


class NotConfigured(AdapterError):
    pass


class Unreachable(AdapterError):
    """No connection was made, so nothing reached the service. Safe to send again."""


class NoAnswer(AdapterError):
    """The request went out but no answer came back in time. The service may still carry it out,
    so a non-idempotent call must not be sent again."""


class ServiceError(AdapterError):
    """The service answered with an error. `reason` is its own short message, if it gave one."""

    def __init__(self, message: str, status: int, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def _reason(resp: httpx.Response) -> str | None:
    """The service's own short reason (an error code or message, never a credential)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    why = body.get("message") or body.get("code") or body.get("error")
    return str(why)[:160] if isinstance(why, str) and why else None


def _why(resp: httpx.Response) -> str:
    why = _reason(resp)
    return f" ({why[:80]})" if why else ""


class Adapter:
    name = "adapter"
    title = "Service"

    def __init__(self, cfg: ServiceConfig, vault: Vault):
        self.cfg = cfg
        self.vault = vault
        self._client: httpx.AsyncClient | None = None
        self.last_ok: float | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.url)

    def secret(self, name: str | None) -> str | None:
        return self.vault.get(name) if name else None

    def auth_headers(self) -> dict[str, str]:
        return {}

    def client(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise NotConfigured(f"{self.title} is not set up")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.url.rstrip("/"),  # type: ignore[union-attr]
                timeout=self.cfg.timeout_s,
                verify=self.cfg.verify_tls,
                follow_redirects=False,
            )
        return self._client

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {**self.auth_headers(), **kw.pop("headers", {})}
        try:
            resp = await self.client().request(method, path, headers=headers, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            self._fail(f"cannot reach {self.title}: {type(exc).__name__}")
            raise Unreachable(self.last_error) from exc
        except httpx.HTTPError as exc:
            self._fail(f"no answer from {self.title}: {type(exc).__name__}")
            raise NoAnswer(self.last_error) from exc
        if resp.status_code in (401, 403):
            self._fail(f"{self.title} refused the hub's credential{_why(resp)}")
            raise ServiceError(self.last_error, resp.status_code, _reason(resp))  # type: ignore[arg-type]
        if resp.status_code >= 400:
            reason = _reason(resp)
            self._fail(f"{self.title} answered {resp.status_code} for {method} {path.split('?')[0]}"
                       + (f": {reason[:80]}" if reason else ""))
            raise ServiceError(self.last_error, resp.status_code, reason)  # type: ignore[arg-type]
        self.last_ok = time.time()
        self.last_error = None
        return resp

    async def get_json(self, path: str, **kw: Any) -> Any:
        return (await self.request("GET", path, **kw)).json()

    async def post_json(self, path: str, body: Any = None, **kw: Any) -> Any:
        resp = await self.request("POST", path, json=body, **kw)
        return resp.json() if resp.content else None

    def _fail(self, msg: str) -> None:
        self.last_error = msg
        log.warning("%s: %s", self.name, msg)

    # A light, authenticated read the hub uses to keep the status page current. None: no probe.
    ping_path: str | None = None

    async def ping(self) -> None:
        if self.ping_path:
            await self.request("GET", self.ping_path)

    def health(self) -> dict[str, Any]:
        return {
            "id": self.name,
            "title": self.title,
            "enabled": self.enabled,
            "ok": self.enabled and self.last_error is None and self.last_ok is not None,
            "last_ok": self.last_ok,
            "error": self.last_error,
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
