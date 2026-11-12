"""Fill the hub's vault from 1Password.

The hub still keeps every credential in its own encrypted vault, so it keeps
working if 1Password is unreachable. With a 1Password service account it copies
them in on start and then every `refresh_s`, so a token rotated in 1Password
reaches the hub by itself.

Two ways to name what to copy (both can be used):

* **Vault scan.** Every item in `onepassword.vault` whose title is a hub
  secret name (``home_assistant.token``, ``apns.auth_key_p8``) is copied. The
  value is the item's document (for the .p8 file), else its password /
  credential field, else its only concealed field.
* **References.** `onepassword.refs` maps a hub secret name to an
  ``op://vault/item/field`` reference, for items that live elsewhere.

Values are never logged or printed: only names and counts are reported.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .config import OnePasswordConfig
from .vault import Vault

log = logging.getLogger("hub.onepassword")

NAME_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
PREFERRED_FIELDS = ("password", "credential", "token", "api key", "api_key", "key", "secret", "value")


@dataclass
class SyncReport:
    stored: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{len(self.stored)} updated", f"{len(self.unchanged)} unchanged"]
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return ", ".join(parts)


def pick_field(item: Any) -> str | None:
    """The secret value of a 1Password item, by the rules in the module doc."""
    fields = list(getattr(item, "fields", None) or [])
    def kind(f: Any) -> str:
        t = getattr(f, "field_type", "")
        return str(getattr(t, "value", t)).upper()
    for wanted in PREFERRED_FIELDS:
        for f in fields:
            if (getattr(f, "title", "") or "").strip().lower() == wanted and getattr(f, "value", None):
                return f.value
    concealed = [f for f in fields if kind(f) == "CONCEALED" and getattr(f, "value", None)]
    if len(concealed) == 1:
        return concealed[0].value
    return None


class OnePasswordSync:
    def __init__(self, cfg: OnePasswordConfig, vault: Vault):
        self.cfg = cfg
        self.vault = vault
        self.last: SyncReport | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.vault.has(self.cfg.token_secret))

    async def _client(self) -> Any:
        from onepassword.client import Client  # imported lazily: optional at test time

        return await Client.authenticate(
            auth=str(self.vault.require(self.cfg.token_secret)),
            integration_name="Thomas Creek Hub",
            integration_version="v0.1.0",
        )

    def _store(self, name: str, value: str | bytes, report: SyncReport) -> None:
        if isinstance(value, bytes):
            value = value.decode()
        value = value.strip()
        if "\n" in value:
            value += "\n"  # keep multi-line values (a .p8 key) as files have them
        if not value:
            report.errors[name] = "empty"
            return
        if name == self.cfg.token_secret:
            report.errors[name] = "will not overwrite the 1Password token itself"
            return
        if self.vault.get(name) == value:
            report.unchanged.append(name)
            return
        self.vault.set(name, value)
        report.stored.append(name)

    async def sync(self) -> SyncReport:
        report = SyncReport()
        client = await self._client()
        if self.cfg.vault:
            vaults = await client.vaults.list()
            match = [v for v in vaults if v.title == self.cfg.vault]
            if not match:
                report.errors["(vault)"] = f"no 1Password vault named {self.cfg.vault!r} is shared with the service account"
            else:
                vid = match[0].id
                for ov in await client.items.list(vid):
                    name = (ov.title or "").strip()
                    if not NAME_RE.match(name) or name in self.cfg.refs:
                        continue
                    try:
                        item = await client.items.get(vid, ov.id)
                        doc = getattr(item, "document", None)
                        if doc is not None:
                            value: str | bytes | None = await client.items.files.read(vid, item.id, doc)
                        else:
                            value = pick_field(item)
                        if value is None:
                            report.errors[name] = "no password/credential field"
                            continue
                        self._store(name, value, report)
                    except Exception as exc:  # noqa: BLE001
                        report.errors[name] = type(exc).__name__
        for name, ref in self.cfg.refs.items():
            try:
                self._store(name, await client.secrets.resolve(ref), report)
            except Exception as exc:  # noqa: BLE001
                report.errors[name] = type(exc).__name__
        self.last = report
        log.info("1Password sync: %s%s", report.summary(),
                 f" (failed: {', '.join(sorted(report.errors))})" if report.errors else "")
        return report

    async def sync_safely(self) -> SyncReport | None:
        try:
            r = await asyncio.wait_for(self.sync(), timeout=60)
            self.last_error = None
            return r
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"1Password sync failed: {type(exc).__name__}"
            log.warning(self.last_error)
            return None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(300, self.cfg.refresh_s))
            await self.sync_safely()

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
