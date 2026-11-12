"""Durable one-shot timers (turn the kid's internet back off at 8:30, close a
valve after 20 minutes, expire a guest). Stored in SQLite so a restart does not
lose them."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .db import new_id

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.timers")


class Timers:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._task: asyncio.Task | None = None

    def add(self, due_at: float, kind: str, payload: dict[str, Any]) -> str:
        tid = new_id("tmr")
        # One pending restore per person: a newer grant replaces the older timer.
        if kind == "network.restore":
            self.hub.db.execute("DELETE FROM timers WHERE kind=? AND json_extract(payload,'$.person_id')=?",
                                [kind, payload.get("person_id")])
        self.hub.db.insert("timers", {"id": tid, "due_at": due_at, "kind": kind, "payload": payload})
        return tid

    def pending(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self.hub.db.all("SELECT * FROM timers WHERE kind=? ORDER BY due_at", [kind])
        return self.hub.db.all("SELECT * FROM timers ORDER BY due_at")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("timer tick failed: %s", exc)
            await asyncio.sleep(10)

    async def tick(self, now: float | None = None) -> int:
        now = now or time.time()
        due = self.hub.db.all("SELECT * FROM timers WHERE due_at <= ? ORDER BY due_at", [now])
        for t in due:
            self.hub.db.execute("DELETE FROM timers WHERE id=?", [t["id"]])
            try:
                await self._fire(t["kind"], t["payload"])
            except Exception as exc:  # noqa: BLE001
                log.warning("timer %s failed: %s", t["kind"], exc)
                self.hub.db.log(f"timer.{t['kind']}", "error", detail=str(exc)[:300], source="schedule")
        # Guests whose access ran out are cut off.
        for p in self.hub.db.all("SELECT * FROM people WHERE role='guest' AND expires_at IS NOT NULL AND expires_at <= ?", [now]):
            for d in self.hub.db.devices(p["id"]):
                if d["status"] == "active":
                    self.hub.revoke_device(d, reason="guest access ended")
        return len(due)

    async def _fire(self, kind: str, payload: dict[str, Any]) -> None:
        runner = self.hub.actions
        if kind == "ha_call":
            if runner.is_live("valve.run"):
                await self.hub.ha.call_service(payload["domain"], payload["service"], payload["data"])
            self.hub.db.log("timer.ha_call", "ok" if runner.is_live("valve.run") else "dry_run",
                            target=payload["data"].get("entity_id"), source="schedule")
        elif kind == "oven.timer":
            await self.hub.kitchen.timer_done(payload["entity_id"])
        elif kind == "network.restore":
            fw = self.hub.firewalla
            pid = payload["person_id"]
            live = runner.is_live("network.grant")
            if live:
                for r in fw.rules_for(pid):
                    if payload["to"] == "off":
                        await fw.resume_rule(r)
                    else:
                        await fw.pause_rule(r)
            self.hub.db.log("timer.network.restore", "ok" if live else "dry_run", target=pid,
                            params={"to": payload["to"]}, source="schedule")
