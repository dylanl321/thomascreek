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

FAVORITE_TAG = "Favorite"
OVEN_TAG = "Oven"


def _servings(r: dict[str, Any]) -> str | None:
    v = r.get("recipeServings") or r.get("recipeYield") or r.get("recipeYieldQuantity")
    if v in (None, "", 0):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


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
            "recipe_id": rid, "total_time": r.get("totalTime"),
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

    # -- recipes ---------------------------------------------------------------
    def _summary(self, r: dict[str, Any]) -> dict[str, Any]:
        rid = r.get("id")
        tags = [t.get("name") for t in (r.get("tags") or []) if t.get("name")]
        return {
            "id": rid, "slug": r.get("slug"), "title": r.get("name"),
            "image_url": f"/v1/meals/image/{rid}" if rid and r.get("image") else None,
            "total_time": r.get("totalTime"), "tags": tags,
            "favorite": FAVORITE_TAG in tags, "oven": OVEN_TAG in tags,
            "last_made": r.get("lastMade"), "rating": r.get("rating"),
        }

    async def recipes(self, search: str = "", tag: str | None = None, page: int = 1, per_page: int = 60) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "perPage": per_page, "orderBy": "name", "orderDirection": "asc"}
        if search:
            params["search"] = search
        if tag:
            params["tags"] = [tag]
        data = await self.get_json("/api/recipes", params=params)
        return {"items": [self._summary(r) for r in data.get("items", [])], "total": data.get("total", 0),
                "page": data.get("page", page), "pages": data.get("total_pages", 1)}

    async def tags(self) -> list[dict[str, Any]]:
        data = await self.get_json("/api/organizers/tags", params={"perPage": 200})
        return [{"id": t["id"], "name": t["name"], "slug": t["slug"]} for t in data.get("items", [])]

    async def recipe(self, slug: str) -> dict[str, Any]:
        from ..kitchen import oven_plan, saved_oven

        r = await self.get_json(f"/api/recipes/{slug}")
        steps = [s.get("text", "") for s in r.get("recipeInstructions") or []]
        oven = saved_oven(r.get("extras") or {}, r.get("name")) or \
            oven_plan(steps, r.get("performTime") or r.get("cookTime"), r.get("name"))
        return {
            **self._summary(r),
            "oven": oven,
            "image_url": f"/v1/meals/image/{r['id']}" if r.get("id") else None,
            "total_time": r.get("totalTime"), "prep_time": r.get("prepTime"), "cook_time": r.get("performTime"),
            "servings": _servings(r),
            "ingredients": [i.get("display") or i.get("note") or i.get("originalText") or ""
                            for i in r.get("recipeIngredient") or []],
            "steps": steps,
            "description": r.get("description"), "source_url": r.get("orgURL"),
        }

    async def create_from_url(self, url: str) -> str:
        resp = await self.request("POST", "/api/recipes/create/url", json={"url": url, "includeTags": True},
                                  timeout=90)
        return str(resp.json()).strip('"')

    async def create(self, name: str) -> str:
        resp = await self.request("POST", "/api/recipes", json={"name": name})
        return str(resp.json()).strip('"')

    async def update(self, slug: str, fields: dict[str, Any]) -> str:
        """Change the parts the app edits, keeping everything else Mealie knows."""
        import uuid

        r = await self.get_json(f"/api/recipes/{slug}")
        if "title" in fields and fields["title"]:
            r["name"] = fields["title"]
        for ours, theirs in (("prep_time", "prepTime"), ("cook_time", "performTime"), ("total_time", "totalTime"),
                             ("description", "description")):
            if ours in fields:
                r[theirs] = fields[ours] or None
        if fields.get("servings") is not None:
            try:
                r["recipeServings"] = float(fields["servings"])
            except (TypeError, ValueError):
                r["recipeYield"] = str(fields["servings"])
        if "ingredients" in fields:
            old = {(i.get("display") or i.get("note") or i.get("originalText") or ""): i
                   for i in r.get("recipeIngredient") or []}
            out = []
            for text in fields["ingredients"]:
                text = str(text).strip()
                if not text:
                    continue
                if text in old:
                    out.append(old[text])
                else:
                    out.append({"note": text, "originalText": text, "display": text, "quantity": 0,
                                "unit": None, "food": None, "referenceId": str(uuid.uuid4())})
            r["recipeIngredient"] = out
        if "steps" in fields:
            r["recipeInstructions"] = [{"id": str(uuid.uuid4()), "title": "", "summary": "", "text": str(t).strip(),
                                        "ingredientReferences": []} for t in fields["steps"] if str(t).strip()]
        if "tags" in fields:
            r["tags"] = await self._tag_objs(fields["tags"])
        extras = dict(r.get("extras") or {})
        if "oven" in fields:
            for k in ("tc_oven_mode", "tc_oven_temp", "tc_oven_minutes", "tc_oven_none"):
                extras.pop(k, None)
            o = fields["oven"]
            if o is None:
                extras["tc_oven_none"] = "1"
            elif isinstance(o, dict) and o.get("mode"):
                extras["tc_oven_mode"] = str(o["mode"])
                if o.get("temp"):
                    extras["tc_oven_temp"] = str(int(o["temp"]))
                if o.get("minutes"):
                    extras["tc_oven_minutes"] = str(int(o["minutes"]))
        r["extras"] = extras
        # Keep the "Oven" tag in step with the oven setting, so the Recipes filter works.
        from ..kitchen import oven_plan, saved_oven

        steps = [s.get("text", "") for s in r.get("recipeInstructions") or []]
        has_oven = extras.get("tc_oven_none") != "1" and bool(
            saved_oven(extras, r.get("name")) or oven_plan(steps, r.get("performTime"), r.get("name")))
        names = [t["name"] for t in r.get("tags") or []]
        if has_oven != (OVEN_TAG in names):
            names = [n for n in names if n != OVEN_TAG] + ([OVEN_TAG] if has_oven else [])
            r["tags"] = await self._tag_objs(names)
        resp = await self.request("PUT", f"/api/recipes/{slug}", json=r)
        body = resp.json() if resp.content else {}
        return (body or {}).get("slug") or slug

    async def _tag_objs(self, names: list[str]) -> list[dict[str, Any]]:
        existing = {t["name"].lower(): t for t in await self.tags()}
        out = []
        for n in names:
            n = str(n).strip()[:40]
            if not n:
                continue
            t = existing.get(n.lower())
            if t is None:
                made = await self.post_json("/api/organizers/tags", {"name": n})
                t = {"id": made["id"], "name": made["name"], "slug": made["slug"]}
                existing[n.lower()] = t
            out.append({"id": t["id"], "name": t["name"], "slug": t["slug"]})
        return out

    async def delete(self, slug: str) -> None:
        await self.request("DELETE", f"/api/recipes/{slug}")

    async def set_image(self, slug: str, data: bytes, ext: str) -> None:
        await self.request("PUT", f"/api/recipes/{slug}/image", files={"image": (f"image.{ext}", data)},
                           data={"extension": ext}, timeout=60)

    async def made(self, slug: str) -> None:
        from datetime import datetime, timezone

        await self.request("PATCH", f"/api/recipes/{slug}/last-made",
                           json={"timestamp": datetime.now(timezone.utc).isoformat()})

    # -- meal plan -------------------------------------------------------------
    async def plan(self, start: date, days: int = 7) -> list[dict[str, Any]]:
        data = await self.get_json(
            "/api/households/mealplans",
            params={"start_date": start.isoformat(), "end_date": (start + timedelta(days=days - 1)).isoformat(),
                    "perPage": 100},
        )
        out = []
        for e in data.get("items", []):
            row = self._recipe(e)
            row["id"] = e.get("id")
            row["note"] = e.get("text") or None
            out.append(row)
        return out

    async def plan_set(self, day: date, recipe_id: str | None, title: str | None, meal: str = "dinner") -> None:
        for e in await self.plan(day, 1):
            if e.get("meal") == meal and e.get("id") is not None:
                await self.request("DELETE", f"/api/households/mealplans/{e['id']}")
        if recipe_id or title:
            body: dict[str, Any] = {"date": day.isoformat(), "entryType": meal}
            if recipe_id:
                body["recipeId"] = recipe_id
            if title:
                body["title"] = title
                body["text"] = ""
            await self.post_json("/api/households/mealplans", body)

    async def add_recipe_to_list(self, list_id: str, recipe_id: str) -> None:
        await self.post_json(f"/api/households/shopping/lists/{list_id}/recipe",
                             [{"recipeId": recipe_id, "recipeIncrementQuantity": 1}])

    async def first_list(self) -> str | None:
        data = await self.get_json("/api/households/shopping/lists", params={"perPage": 20})
        items = data.get("items", [])
        return items[0]["id"] if items else None

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
