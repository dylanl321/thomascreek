"""Firewalla MSP API, called directly by the hub, for a child's internet time.

Endpoints used (MSP API v2): GET /boxes, GET /devices, GET /rules,
POST /rules (MSP 2.10+), POST /rules/:id/pause, POST /rules/:id/resume.

When Dylan links a device to a child, the hub creates one rule for it:
block, target internet, scope that device's MAC, with a note naming the hub.
It pauses the rule straight away, so linking changes nothing for the child.

  rule active (resumed) -> that device's internet is off
  rule paused           -> that device's internet is on

The hub only creates these per-device internet rules and pauses or resumes
them. The MSP API has no delete, so unlinking pauses the rule and leaves it
for removal in the Firewalla app. Rule ids listed by hand under
firewalla.person_rules in config work the same way.
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

    def __init__(self, cfg: FirewallaConfig, vault: Vault, db=None):
        self.cfg = cfg
        self.vault = vault
        self.db = db
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

    async def alarms(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._req("GET", "/alarms", params={"limit": limit, "sortBy": "ts:desc"})
        return data.get("results", data) if isinstance(data, dict) else (data or [])

    async def ping(self) -> None:
        await self.boxes()

    async def boxes(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/boxes")
        return data.get("results", data) if isinstance(data, dict) else data

    async def devices(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/devices")
        rows = data.get("results", data) if isinstance(data, dict) else data
        out = []
        for d in rows or []:
            mac = (d.get("mac") or d.get("id") or "").upper()
            out.append({"mac": mac, "name": d.get("name") or d.get("macVendor") or mac, "ip": d.get("ip"),
                        "online": d.get("online"), "gid": d.get("gid"),
                        "group": (d.get("group") or {}).get("name") if isinstance(d.get("group"), dict) else None})
        return out

    async def create_block_rule(self, mac: str, gid: str, note: str) -> str:
        body = {
            "action": "block", "direction": "bidirection", "gid": gid, "notes": note,
            "target": {"type": "internet"}, "scope": {"type": "device", "value": mac},
        }
        rule = await self._req("POST", "/rules", json=body)
        rule_id = str((rule or {}).get("id") or "")
        if not rule_id:
            raise AdapterError("Firewalla didn't return a rule id")
        return rule_id

    def rules_for(self, person_id: str) -> list[str]:
        ids = list(self.cfg.person_rules.get(person_id, []))
        if self.db is not None:
            ids += [r["rule_id"] for r in self.db.all("SELECT rule_id FROM network_rules WHERE person_id=?", [person_id])
                    if r["rule_id"] not in ids]
        return ids

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
