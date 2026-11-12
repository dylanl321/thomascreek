"""Mealie (meals, shopping), Homebox (inventory), Family Command Center
(chores, rewards, calendar) and the holiday show player.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import httpx

from ..config import ShowPlayerConfig
from .base import Adapter, AdapterError


class Mealie(Adapter):
    name = "mealie"
    title = "Meals"
    ping_path = "/api/users/self"

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _recipe(self, entry: dict[str, Any]) -> dict[str, Any]:
        r = entry.get("recipe") or {}
        rid = r.get("id")
        return {
            "date": entry.get("date"),
            "meal": entry.get("entryType"),
            "title": r.get("name") or entry.get("title"),
            "slug": r.get("slug"),
            "image_url": f"/v1/meals/image/{rid}" if rid else None,
        }

    async def today(self) -> list[dict[str, Any]]:
        data = await self.get_json("/api/households/mealplans/today")
        return [self._recipe(e) for e in (data or [])]

    async def week(self) -> list[dict[str, Any]]:
        start = date.today()
        data = await self.get_json(
            "/api/households/mealplans",
            params={"start_date": start.isoformat(), "end_date": (start + timedelta(days=6)).isoformat(), "perPage": 50},
        )
        return [self._recipe(e) for e in data.get("items", [])]

    async def image(self, recipe_id: str) -> tuple[bytes, str]:
        resp = await self.request("GET", f"/api/media/recipes/{recipe_id}/images/min-original.webp")
        return resp.content, resp.headers.get("content-type", "image/webp")

    async def shopping(self) -> list[dict[str, Any]]:
        lists = await self.get_json("/api/households/shopping/lists", params={"perPage": 20})
        out = []
        for sl in lists.get("items", []):
            detail = await self.get_json(f"/api/households/shopping/lists/{sl['id']}")
            out.append({
                "id": sl["id"],
                "name": sl.get("name"),
                "items": [
                    {"id": i["id"], "text": i.get("display") or i.get("note") or "", "done": bool(i.get("checked"))}
                    for i in detail.get("listItems", [])
                ],
            })
        return out

    async def set_item_done(self, item_id: str, done: bool) -> Any:
        item = await self.get_json(f"/api/households/shopping/items/{item_id}")
        item["checked"] = bool(done)
        resp = await self.request("PUT", f"/api/households/shopping/items/{item_id}", json=item)
        return resp.json() if resp.content else None

    async def recipe(self, slug: str) -> dict[str, Any]:
        r = await self.get_json(f"/api/recipes/{slug}")
        return {
            "id": r.get("id"), "slug": r.get("slug"), "title": r.get("name"),
            "image_url": f"/v1/meals/image/{r['id']}" if r.get("id") else None,
            "total_time": r.get("totalTime"), "prep_time": r.get("prepTime"), "cook_time": r.get("performTime"),
            "servings": r.get("recipeServings") or r.get("recipeYield"),
            "ingredients": [i.get("display") or i.get("note") or i.get("originalText") or ""
                            for i in r.get("recipeIngredient", [])],
            "steps": [s.get("text", "") for s in r.get("recipeInstructions", [])],
        }

    async def add_item(self, list_id: str, text: str) -> Any:
        return await self.post_json("/api/households/shopping/items", {"shoppingListId": list_id, "note": text, "isFood": False})


class Homebox(Adapter):
    name = "homebox"
    title = "Inventory"

    def __init__(self, cfg, vault):
        super().__init__(cfg, vault)
        self._token: str | None = None
        self._token_at = 0.0

    async def _login(self) -> None:
        if self._token and time.time() - self._token_at < 3600:
            return
        resp = await self.client().post(
            "/api/v1/users/login",
            json={"username": self.cfg.username, "password": self.secret(self.cfg.password_secret), "stayLoggedIn": True},
        )
        if resp.status_code != 200:
            self._fail("Homebox refused the hub's login")
            raise AdapterError(self.last_error)
        self._token, self._token_at = resp.json().get("token"), time.time()

    def auth_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        t = self._token
        return {"Authorization": t if t.lower().startswith("bearer") else f"Bearer {t}"}

    async def ping(self) -> None:
        await self._login()
        await self.request("GET", "/api/v1/groups/statistics")

    async def search(self, q: str) -> list[dict[str, Any]]:
        await self._login()
        data = await self.get_json("/api/v1/items", params={"q": q, "pageSize": 30})
        return [
            {"id": i["id"], "name": i.get("name"), "location": (i.get("location") or {}).get("name"),
             "quantity": i.get("quantity"), "description": i.get("description")}
            for i in data.get("items", [])
        ]


class FamilyBoard(Adapter):
    """Family Command Center (github.com/dylanl321/family-command-center).

    Reads use its public routes (/api/today/{person}, /api/bootstrap,
    /api/export/calendar.ics). Completing a chore and redeeming a reward are
    its "family actions", which it allows from a private LAN address with no
    X-Forwarded-Proto, so the hub on the NAS needs no credential. If the hub
    ever reaches it through a proxy, store the parent PIN as
    family_board.admin_token and the hub sends it as X-Admin-Token.
    The board itself handles parent approval of chores and redemptions.
    """

    name = "family_board"
    title = "Family board"
    ping_path = "/api/bootstrap"

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        return {"X-Admin-Token": token} if token else {}

    async def people(self) -> list[dict[str, Any]]:
        data = await self.get_json("/api/bootstrap")
        return [{"id": p.get("id"), "name": p.get("name"), "emoji": p.get("emoji"), "role": p.get("role")}
                for p in data.get("people", []) if not p.get("archived")]

    async def today(self, board_person_id: str, day: str | None = None) -> dict[str, Any]:
        params = {"date": day} if day else None
        return await self.get_json(f"/api/today/{board_person_id}", params=params)

    @staticmethod
    def normalize_today(raw: dict[str, Any]) -> dict[str, Any]:
        rewards = raw.get("rewards") or {}
        eligibility = rewards.get("eligibility") or {}
        items = []
        for it in raw.get("items", []):
            if not it.get("trackable"):
                continue
            items.append({
                "id": it.get("id"), "title": it.get("title"), "icon": it.get("emoji") or it.get("word") or None,
                "word": it.get("word") or None, "done": bool(it.get("completed")), "points": int(it.get("points") or 0),
                "time": it.get("time") or None, "waiting": bool(it.get("reported")), "locked": bool(it.get("locked")),
                "kind": it.get("kind"),
            })
        streaks = raw.get("streaks") or {}
        streak = max((int((v or {}).get("count") or 0) for v in streaks.values() if isinstance(v, dict)), default=0)
        progress = raw.get("progress") or {}
        out_rewards = []
        for r in rewards.get("catalog", []):
            elig = eligibility.get(str(r.get("id"))) or {}
            row = {"id": r.get("id"), "title": r.get("title"), "icon": r.get("emoji"), "cost": int(r.get("cost") or 0)}
            if isinstance(elig, dict) and "eligible" in elig:
                row["available"] = bool(elig["eligible"])
            out_rewards.append(row)
        return {
            "date": raw.get("date"),
            "points": int(rewards.get("points") or 0),
            "streak": streak,
            "goal": int(progress.get("total") or 0),
            "done": int(progress.get("done") or 0),
            "items": items,
            "rewards": out_rewards,
        }

    async def chore_done(self, item_id: str, board_person_id: str, day: str | None = None) -> Any:
        body: dict[str, Any] = {"person_id": board_person_id, "completed": True}
        if day:
            body["date"] = day
        return await self.post_json(f"/api/items/{item_id}/complete", body)

    async def redeem(self, reward_id: str, board_person_id: str) -> Any:
        return await self.post_json(f"/api/rewards/{reward_id}/redeem", {"person_id": board_person_id})

    async def calendar(self, days: int) -> dict[str, Any]:
        resp = await self.request("GET", "/api/export/calendar.ics", params={"days": days})
        return {"events": parse_ics(resp.text)}


def _ics_time(value: str) -> tuple[str | None, bool]:
    """ICS DATE or DATE-TIME to ISO 8601. Returns (iso, all_day)."""
    v = value.strip()
    try:
        if len(v) == 8:
            return f"{v[:4]}-{v[4:6]}-{v[6:8]}", True
        base = f"{v[:4]}-{v[4:6]}-{v[6:8]}T{v[9:11]}:{v[11:13]}:{v[13:15] or '00'}"
        return base + ("Z" if v.endswith("Z") else ""), False
    except IndexError:
        return None, False


def parse_ics(text: str) -> list[dict[str, Any]]:
    # Unfold continuation lines first (RFC 5545 3.1).
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name = key.split(";", 1)[0].upper()
            val = val.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n")
            if name in ("DTSTART", "DTEND"):
                iso, all_day = _ics_time(val)
                cur["start" if name == "DTSTART" else "end"] = iso
                if name == "DTSTART":
                    cur["all_day"] = all_day
            elif name == "SUMMARY":
                cur["title"] = val
            elif name == "UID":
                cur["id"] = val
            elif name in ("DESCRIPTION", "LOCATION"):
                cur[name.lower()] = val
            elif name == "X-FCC-WHO" or name == "ATTENDEE":
                cur["who"] = val
    out = []
    for e in events:
        title, who = e.get("title") or "", e.get("who")
        # The board's export appends assignees to the title: "Swim (Theo, Juni)".
        if not who and title.endswith(")") and " (" in title:
            title, who = title[:-1].rsplit(" (", 1)
        out.append({"id": e.get("id"), "title": title, "start": e.get("start"), "end": e.get("end"),
                    "who": who, "all_day": e.get("all_day", False), "location": e.get("location")})
    return out


class ShowPlayer(Adapter):
    """The holiday lights show_player.py. It stays its own backend; the hub adds
    the app token and checks the path against the caller's role."""

    name = "show_player"
    title = "Holiday lights"
    ping_path = "/status"
    cfg: ShowPlayerConfig

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        return {self.cfg.token_header: f"{self.cfg.token_prefix}{token}"} if token else {}

    def path_allowed(self, role: str, path: str) -> bool:
        path = path.lstrip("/")
        if ".." in path:
            return False
        for prefix in self.cfg.allowed_paths.get(role, []):
            if prefix == "" or path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
                return True
        return False

    async def forward(self, method: str, path: str, params: Any, body: bytes | None, content_type: str | None) -> httpx.Response:
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        return await self.request(method, "/" + path.lstrip("/"), params=params, content=body, headers=headers)
