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
from .push import alert_payload, collapse_identifier

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.notify")

# Who each kind of notice is for. `group` is the audience:
#   household  something happening at the house (doorbell, doors, weather)
#   personal   about one person (their movie is ready, their print finished)
#   technical  keeping the systems running (NAS, apps, builds): admins only
# `roles` get it unless they switch it off. Technical ones stay off for everyone
# else, and only an admin can switch one on for someone (admin page).
CATEGORIES = [
    {"id": "doors", "group": "household", "title": "Doors and garage", "detail": "Opened, closed, or left open",
     "roles": ("admin", "adult")},
    {"id": "cameras", "group": "household", "title": "Cameras", "detail": "Doorbell and the cameras you pick under Alerts",
     "roles": ("admin", "adult")},
    {"id": "house", "group": "household", "title": "House alerts", "detail": "Freezer, leaks, things left on",
     "roles": ("admin", "adult")},
    {"id": "weather", "group": "household", "title": "Weather alerts", "detail": "Warnings from the weather service",
     "roles": ("admin", "adult", "child")},
    {"id": "kitchen", "group": "household", "title": "Kitchen", "detail": "Oven preheated, timers, oven left on",
     "roles": ("admin", "adult")},
    {"id": "kids", "group": "household", "title": "Kids' asks", "detail": "When a kid asks a grown-up for something",
     "roles": ("admin", "adult")},
    {"id": "holiday", "group": "household", "title": "Holiday lights", "detail": "Show start, look changes",
     "roles": ()},
    {"id": "media", "group": "personal", "title": "Movies and shows", "detail": "When something you asked for is ready",
     "roles": ("admin", "adult", "child")},
    {"id": "requests", "group": "personal", "title": "Requests", "detail": "Asks from the kids and their answers",
     "roles": ("admin", "adult", "child")},
    {"id": "printer", "group": "personal", "title": "3D prints", "detail": "Your print finished or needs you",
     "roles": ("admin", "adult", "child")},
    {"id": "chores", "group": "personal", "title": "Chores and rewards", "detail": "Reminders and approvals",
     "roles": ("admin", "adult", "child")},
    {"id": "systems", "group": "technical", "title": "Servers and network",
     "detail": "NAS, apps, Home Assistant device health, internet", "roles": ("admin",)},
    {"id": "devices", "group": "technical", "title": "New devices", "detail": "A phone or tablet joined",
     "roles": ("admin",)},
    {"id": "updates", "group": "technical", "title": "App updates", "detail": "A new build of Thomas Creek or the TV app",
     "roles": ("admin",)},
    {"id": "feedback", "group": "technical", "title": "Family feedback", "detail": "Notes sent with Tell Dylan",
     "roles": ("admin",)},
]
CATEGORY = {c["id"]: c for c in CATEGORIES}
GROUPS = {"household": "For the household", "personal": "Just for you", "technical": "Technical (admins)"}
# Kept for older callers: the admin's defaults.
DEFAULT_PREFS = {c["id"]: "admin" in c["roles"] for c in CATEGORIES}


def role_defaults(role: str | None) -> dict[str, bool]:
    return {c["id"]: (role or "") in c["roles"] for c in CATEGORIES}


def categories_for(role: str | None, prefs: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """The category list a person sees: technical ones only for admins, or when an admin switched one on."""
    out = []
    for c in CATEGORIES:
        if c["group"] == "technical" and role != "admin" and not (prefs or {}).get(c["id"]):
            continue
        out.append({"id": c["id"], "title": c["title"], "detail": c["detail"], "group": c["group"],
                    "default": (role or "") in c["roles"], "locked": c["group"] == "technical" and role != "admin"})
    return out


LEVELS = ("act_now", "decision", "maintenance", "fyi")


class Notifier:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        from .notify_sort import Sorter

        self.sorter = Sorter(hub)

    # -- who wants what ----------------------------------------------------
    def _overrides(self, person_id: str) -> dict[str, bool] | None:
        return self.hub.db.kv_get(f"notify.cats.{person_id}")

    def _seed(self, person: dict[str, Any]) -> dict[str, bool]:
        """First read for a person: carry over a category they switched off on every phone before prefs
        moved from each device to the person."""
        old_default = {"doors", "cameras", "requests", "media", "chores", "kitchen", "weather", "house",
                       "systems", "devices", "updates", "feedback"}
        devs = [d for d in self.hub.db.devices(person["id"]) if d["status"] == "active" and d.get("prefs")]
        seeded: dict[str, bool] = {}
        if devs:
            mine = role_defaults(person.get("role"))
            for cat in old_default:
                if mine.get(cat) and all((d.get("prefs") or {}).get(cat) is False for d in devs):
                    seeded[cat] = False
        self.hub.db.kv_set(f"notify.cats.{person['id']}", seeded)
        return seeded

    def prefs(self, person: dict[str, Any] | str) -> dict[str, bool]:
        """A person's categories: their role's defaults with their own (or an admin's) changes on top."""
        p = self.hub.db.person(person) if isinstance(person, str) else person
        if not p:
            return {}
        over = self._overrides(p["id"])
        if over is None:
            over = self._seed(p)
        out = role_defaults(p.get("role"))
        for k, v in over.items():
            if k in out and isinstance(v, bool):
                out[k] = v
        return out

    def set_prefs(self, person_id: str, changes: dict[str, Any], *, by_admin: bool) -> dict[str, bool]:
        """Change some categories. Only an admin can switch a technical one on for someone who isn't one."""
        p = self.hub.db.person(person_id)
        if not p:
            return {}
        cur = self.prefs(p)
        over = dict(self._overrides(p["id"]) or {})
        base = role_defaults(p.get("role"))
        for k, v in changes.items():
            c = CATEGORY.get(k)
            if not c or not isinstance(v, bool):
                continue
            if v and c["group"] == "technical" and p.get("role") != "admin" and not by_admin and not cur.get(k):
                continue
            if v == base[k]:
                over.pop(k, None)
            else:
                over[k] = v
        self.hub.db.kv_set(f"notify.cats.{p['id']}", over)
        return self.prefs(p)

    def wants(self, person: dict[str, Any], cat: str, *, named: bool = False) -> bool:
        """`named`: the sender picked this person on purpose (an automation sent "to: kids"). That beats the
        role default for a household or personal kind, but not the person's own "off", and never a technical one."""
        c = CATEGORY.get(cat)
        if c is None:  # an unlisted kind: whoever the sender chose
            return True
        if named and c["group"] != "technical":
            return (self._overrides(person["id"]) or {}).get(cat, True) is not False
        return self.prefs(person).get(cat, False)

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
        extra: dict[str, Any] | None = None,
        named: bool = False,
        sort: bool = True,
    ) -> list[str]:
        assert level in LEVELS
        db = self.hub.db
        # Fixed categories (doorbell, doors, NAS ...) go straight out. Free text is sorted first,
        # but an urgent one never waits for the AI.
        urgent = level == "act_now" or interruption in ("time-sensitive", "critical")
        if sort:
            cat = await self.sorter.sort(cat=cat, title=title, body=body, urgent=urgent)
        ids = []
        for p in self._recipients(person_ids, roles):
            if p["id"] == exclude_person:
                continue
            if not self.wants(p, cat, named=named):
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
                                    interruption=interruption, extra=extra, group_key=group_key)
        notice = self.tv_notice(level, cat, title, body, group_key, ref)
        if notice and ids:
            # One copy for every TV. No snapshot and no actions: a TV can show it, not approve it.
            self.hub.bus.publish({"type": "inbox", "data": notice}, tvs=True)
        return ids

    @staticmethod
    def tv_notice(level: str, cat: str, title: str, body: str | None, group_key: str | None,
                  ref: str | None) -> dict[str, Any] | None:
        """The overlay a TV may show. Camera motion stays on the phones."""
        text = f"{title}\n{body or ''}\n{group_key or ''}".lower()
        if "doorbell" in text:
            shown = "doorbell"
        elif level == "act_now" and cat in ("house", "kitchen", "weather", "doors"):
            shown = cat
        elif level == "decision" and cat == "requests":
            shown = "requests"
        else:
            return None
        return {"level": level, "cat": shown, "title": title, "body": body, "ref": ref}

    async def push_update(self, person_id: str, title: str, body: str | None = None, **kw: Any) -> None:
        """Push again for an alert already in the inbox. The phone replaces the one on screen."""
        await self._push_person(person_id, title, body, **kw)

    async def _push_person(self, person_id: str, title: str, body: str | None, **kw: Any) -> None:
        apns = self.hub.apns
        if not apns.enabled:
            return
        # The group is both the collapse id (a repeat replaces the alert) and the
        # thread (related alerts sit together). No group: still thread by category,
        # and don't collapse unrelated alerts into one.
        collapse = collapse_identifier(kw.get("group_key"))
        thread = collapse or kw["cat"]
        for d in self.hub.db.all(
            "SELECT * FROM devices WHERE person_id=? AND status='active' AND apns_token IS NOT NULL", [person_id]
        ):
            # Categories are the person's now (self.prefs), the same on every phone they use.
            payload = alert_payload(title, body, cat=kw["cat"], inbox_id=kw.get("inbox_id"), ref=kw.get("ref"),
                                    category=kw.get("category"), level=kw.get("level", "fyi"), thread=thread,
                                    silent=kw.get("silent", False), image=kw.get("image"),
                                    interruption=kw.get("interruption"), extra=kw.get("extra"))
            res = await apns.send(d["apns_token"], payload, env=d.get("apns_env") or "production",
                                  collapse_id=collapse)
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
