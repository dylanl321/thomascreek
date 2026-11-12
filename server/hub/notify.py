"""The attention inbox and push: the single place alerts leave from.

Each notice lands in the recipient's inbox (four levels: act_now, decision,
maintenance, fyi) and, if that device has the category switched on, goes out
as a push.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Iterable

from .db import new_id
from .push import alert_payload

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.notify")

CATEGORIES = [
    {"id": "doors", "title": "Doors and garage", "detail": "Opened, closed, or left open", "default": True},
    {"id": "cameras", "title": "Cameras", "detail": "Doorbell and the cameras you pick under Alerts", "default": True},
    {"id": "requests", "title": "Requests", "detail": "Asks from the kids and their answers", "default": True},
    {"id": "media", "title": "Movies and shows", "detail": "When something you asked for is ready", "default": True},
    {"id": "chores", "title": "Chores and rewards", "detail": "Reminders and approvals", "default": True},
    {"id": "holiday", "title": "Holiday lights", "detail": "Show start, look changes", "default": False},
    {"id": "kitchen", "title": "Kitchen", "detail": "Oven preheated, timers, oven left on", "default": True},
    {"id": "house", "title": "House alerts", "detail": "Freezer, leaks, things left on", "default": True},
    {"id": "systems", "title": "Servers and network", "detail": "NAS, apps, internet (admin)", "default": True},
    {"id": "devices", "title": "New devices", "detail": "A phone or tablet joined (admin)", "default": True},
]
DEFAULT_PREFS = {c["id"]: c["default"] for c in CATEGORIES}
LEVELS = ("act_now", "decision", "maintenance", "fyi")


class Notifier:
    def __init__(self, hub: "Hub"):
        self.hub = hub

    def _recipients(self, person_ids: Iterable[str] | None, roles: Iterable[str] | None) -> list[dict[str, Any]]:
        db = self.hub.db
        out: dict[str, dict[str, Any]] = {}
        for pid in person_ids or []:
            p = db.person(pid)
            if p:
                out[p["id"]] = p
        if roles:
            for p in db.people_with_role(*roles):
                out[p["id"]] = p
        return list(out.values())

    async def send(
        self,
        *,
        title: str,
        body: str | None,
        cat: str,
        level: str = "fyi",
        person_ids: Iterable[str] | None = None,
        roles: Iterable[str] | None = None,
        actions: list[dict[str, Any]] | None = None,
        ref: str | None = None,
        group_key: str | None = None,
        exclude_person: str | None = None,
        push_category: str | None = None,
        silent: bool = False,
        image: str | None = None,
        interruption: str | None = None,
    ) -> list[str]:
        assert level in LEVELS
        db = self.hub.db
        ids = []
        for p in self._recipients(person_ids, roles):
            if p["id"] == exclude_person:
                continue
            iid = new_id("inb")
            db.insert("inbox", {
                "id": iid, "ts": time.time(), "person_id": p["id"], "level": level, "cat": cat, "title": title,
                "body": body, "actions": actions or [], "ref": ref, "group_key": group_key,
            })
            ids.append(iid)
            self.hub.bus.publish({"type": "inbox", "data": self.item(db.one("SELECT * FROM inbox WHERE id=?", [iid]))},
                                 person_id=p["id"])
            await self._push_person(p["id"], title, body, cat=cat, level=level, inbox_id=iid, ref=ref,
                                    category=push_category, silent=silent, image=image,
                                    interruption=interruption)
        return ids

    async def _push_person(self, person_id: str, title: str, body: str | None, **kw: Any) -> None:
        apns = self.hub.apns
        if not apns.enabled:
            return
        for d in self.hub.db.all(
            "SELECT * FROM devices WHERE person_id=? AND status='active' AND apns_token IS NOT NULL", [person_id]
        ):
            prefs = {**DEFAULT_PREFS, **(d.get("prefs") or {})}
            if not prefs.get(kw["cat"], True):
                continue
            payload = alert_payload(title, body, cat=kw["cat"], inbox_id=kw.get("inbox_id"), ref=kw.get("ref"),
                                    category=kw.get("category"), level=kw.get("level", "fyi"), thread=kw["cat"],
                                    silent=kw.get("silent", False), image=kw.get("image"),
                                    interruption=kw.get("interruption"))
            res = await apns.send(d["apns_token"], payload, env=d.get("apns_env") or "production",
                                  collapse_id=kw.get("inbox_id"))
            if res.token_dead:
                self.hub.db.update("devices", "id", d["id"], {"apns_token": None})

    async def push_admins_raw(self, payload: dict[str, Any]) -> None:
        for p in self.hub.db.people_with_role("admin"):
            for d in self.hub.db.devices(p["id"]):
                if d["status"] == "active" and d.get("apns_token"):
                    await self.hub.apns.send(d["apns_token"], payload, env=d.get("apns_env") or "production")

    @staticmethod
    def item(row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {}
        from .home import iso

        out = {"id": row["id"], "t": iso(row["ts"]), "level": row["level"], "cat": row["cat"],
               "title": row["title"], "body": row.get("body")}
        if row.get("actions"):
            out["actions"] = row["actions"]
        if row.get("ref"):
            out["ref"] = row["ref"]
        return out
