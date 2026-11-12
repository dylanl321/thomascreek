"""Firewalla MSP API for a child's internet time.

Only documented MSP endpoints are used: list rules, pause a rule, resume a rule
(MSP 2.7+). Dylan creates one "block internet" rule per child (scoped to that
child's devices or device group) in the Firewalla app once and lists its id in
config under firewalla.person_rules. Then:

  rule active (resumed) -> the child's internet is off
  rule paused           -> the child's internet is on

The hub never creates or deletes firewall rules; that stays in the Firewalla app.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from ..config import FirewallaConfig
from ..vault import Vault
from .base import AdapterError


class Firewalla:
    name = "firewalla"
    title = "Network"

    def __init__(self, cfg: FirewallaConfig, vault: Vault):
        self.cfg = cfg
        self.vault = vault
        self.last_error: str | None = None
        self.last_ok: float | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.msp_domain)

    def _c(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise AdapterError("Network control is not set up")
        if self._client is None:
            token = self.vault.get(self.cfg.token_secret)
            if not token:
                raise AdapterError("Firewalla token is not in the vault")
            self._client = httpx.AsyncClient(
                base_url=f"https://{self.cfg.msp_domain}/v2",
                headers={"Authorization": f"Token {token}"},
                timeout=self.cfg.timeout_s,
            )
        return self._client

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        import time

        try:
            resp = await self._c().request(method, path, **kw)
        except httpx.HTTPError as exc:
            self.last_error = "cannot reach Firewalla"
            raise AdapterError(self.last_error) from exc
        if resp.status_code >= 400:
            self.last_error = f"Firewalla answered {resp.status_code}"
            raise AdapterError(self.last_error)
        self.last_ok, self.last_error = time.time(), None
        return resp.json() if resp.content else None

    async def rules(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/rules")
        return data.get("results", data) if isinstance(data, dict) else data

    async def pause_rule(self, rule_id: str) -> None:
        await self._req("POST", f"/rules/{quote(rule_id)}/pause")

    async def resume_rule(self, rule_id: str) -> None:
        await self._req("POST", f"/rules/{quote(rule_id)}/resume")

    def rules_for(self, person_id: str) -> list[str]:
        return self.cfg.person_rules.get(person_id, [])

    async def internet_state(self, person_id: str) -> dict[str, Any]:
        ids = set(self.rules_for(person_id))
        if not ids:
            return {"managed": False, "paused": False}
        rows = [r for r in await self.rules() if str(r.get("id")) in ids]
        # "paused" here means the child's internet is paused (block rule active).
        blocking = any(r.get("status") != "paused" for r in rows)
        return {"managed": True, "paused": blocking}

    def health(self) -> dict[str, Any]:
        return {"id": self.name, "title": self.title, "enabled": self.enabled,
                "ok": self.enabled and self.last_error is None, "last_ok": self.last_ok, "error": self.last_error}

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
