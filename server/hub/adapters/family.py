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

    async def search(self, q: str) -> list[dict[str, Any]]:
        await self._login()
        data = await self.get_json("/api/v1/items", params={"q": q, "pageSize": 30})
        return [
            {"id": i["id"], "name": i.get("name"), "location": (i.get("location") or {}).get("name"),
             "quantity": i.get("quantity"), "description": i.get("description")}
            for i in data.get("items", [])
        ]


class FamilyBoard(Adapter):
    """Family Command Center. Its routes are read from config (extra.paths) so the
    hub follows whatever that app exposes without a code change."""

    name = "family_board"
    title = "Family board"

    DEFAULT_PATHS = {
        "chores": "/api/people/{person}/today",
        "chore_done": "/api/chores/{id}/complete",
        "reward_redeem": "/api/rewards/{id}/redeem",
        "calendar": "/api/calendar",
    }

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def path(self, key: str, **kw: str) -> str:
        paths = {**self.DEFAULT_PATHS, **self.cfg.extra.get("paths", {})}
        return paths[key].format(**kw)

    async def chores(self, board_person_id: str) -> dict[str, Any]:
        return await self.get_json(self.path("chores", person=board_person_id))

    async def chore_done(self, chore_id: str, board_person_id: str) -> Any:
        return await self.post_json(self.path("chore_done", id=chore_id), {"person": board_person_id})

    async def redeem(self, reward_id: str, board_person_id: str) -> Any:
        return await self.post_json(self.path("reward_redeem", id=reward_id), {"person": board_person_id})

    async def calendar(self, days: int) -> Any:
        return await self.get_json(self.path("calendar"), params={"days": days})


class ShowPlayer(Adapter):
    """The holiday lights show_player.py. It stays its own backend; the hub adds
    the app token and checks the path against the caller's role."""

    name = "show_player"
    title = "Holiday lights"
    cfg: ShowPlayerConfig

    def auth_headers(self) -> dict[str, str]:
        token = self.secret(self.cfg.token_secret)
        return {self.cfg.token_header: token} if token else {}

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
