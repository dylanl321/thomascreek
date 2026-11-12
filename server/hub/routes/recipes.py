"""Recipes and the dinner plan, stored in Mealie: add from a link, a scan or by
hand, edit (including the oven setting), favorite, plan nights, shop for them."""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..adapters.family import FAVORITE_TAG
from ..kitchen import parse_recipe_text
from ..state import Hub
from .deps import Caller, caller, error, hub, need

router = APIRouter(prefix="/v1/meals", tags=["recipes"])

SLUG = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,200}$")
UUID = re.compile(r"^[0-9a-fA-F\-]{32,36}$")


def _slug(slug: str) -> str:
    if not SLUG.match(slug):
        raise error(404, "not_found", "No such recipe")
    return slug


def _down(exc: Exception):
    return error(503, "unavailable", str(exc))


def _edit(h: Hub, c: Caller, what: str, target: str | None = None, **params: Any) -> bool:
    """Checks the person may edit meals; logs; returns False in practice mode."""
    need(h, c, "family.edit")
    live = h.actions.is_live("meals.edit")
    h.db.log(f"meals.{what}", "ok" if live else "dry_run", person_id=c.subject.person_id, device_id=c.device["id"],
             target=target, params={k: v for k, v in params.items() if v is not None})
    return live


def _today(h: Hub) -> date:
    return h.policy.now().date()


# -- library -----------------------------------------------------------------------
@router.get("/recipes")
async def recipes(search: str = "", tag: str | None = None, page: int = 1, c: Caller = Depends(caller),
                  h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        return await h.mealie.recipes(search[:80], tag, max(1, page))
    except AdapterError as exc:
        raise _down(exc) from None


@router.get("/tags")
async def tags(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        return {"tags": await h.mealie.tags()}
    except AdapterError as exc:
        raise _down(exc) from None


class ParseBody(BaseModel):
    text: str = Field(min_length=1, max_length=30000)


@router.post("/recipes/parse")
async def parse(body: ParseBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Scanned or pasted text -> a draft the person checks before saving."""
    need(h, c, "family.view")
    return parse_recipe_text(body.text)


class OvenBody(BaseModel):
    mode: str = Field(max_length=40)
    temp: int | None = Field(default=None, ge=100, le=600)
    minutes: int | None = Field(default=None, ge=1, le=600)


class RecipeBody(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    servings: str | None = Field(default=None, max_length=40)
    prep_time: str | None = Field(default=None, max_length=40)
    cook_time: str | None = Field(default=None, max_length=40)
    total_time: str | None = Field(default=None, max_length=40)
    ingredients: list[str] | None = Field(default=None, max_length=150)
    steps: list[str] | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=30)
    oven: OvenBody | None = None
    no_oven: bool = False


def _fields(body: RecipeBody) -> dict[str, Any]:
    f = body.model_dump(exclude_unset=True, exclude={"url", "no_oven", "oven"})
    if body.no_oven:
        f["oven"] = None
    elif body.oven is not None:
        f["oven"] = body.oven.model_dump()
    return f


@router.post("/recipes")
async def create(body: RecipeBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if body.url:
        if not re.match(r"^https?://[^\s/]+\.[^\s]+$", body.url.strip()):
            raise error(400, "bad_params", "That doesn't look like a web link")
        if not _edit(h, c, "add", body.url[:200], source="link"):
            return {"ok": True, "dry_run": True}
        try:
            slug = await h.mealie.create_from_url(body.url.strip())
            # Find the oven setting and tag it, so the Oven filter works from day one.
            slug = await h.mealie.update(slug, {})
            return {"ok": True, "slug": slug, "recipe": await h.mealie.recipe(slug)}
        except AdapterError:
            raise error(422, "cant_read", "Couldn't read a recipe from that page. Try Type it in.") from None
    if not body.title:
        raise error(400, "bad_params", "Give the recipe a title")
    if not _edit(h, c, "add", body.title, source="typed"):
        return {"ok": True, "dry_run": True}
    try:
        slug = await h.mealie.create(body.title)
        slug = await h.mealie.update(slug, _fields(body))
        return {"ok": True, "slug": slug, "recipe": await h.mealie.recipe(slug)}
    except AdapterError as exc:
        raise _down(exc) from None


@router.put("/recipes/{slug}")
async def update(slug: str, body: RecipeBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    if not _edit(h, c, "edit", slug):
        return {"ok": True, "dry_run": True}
    try:
        new = await h.mealie.update(slug, _fields(body))
        return {"ok": True, "slug": new, "recipe": await h.mealie.recipe(new)}
    except AdapterError as exc:
        raise _down(exc) from None


@router.delete("/recipes/{slug}")
async def delete(slug: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    if not _edit(h, c, "delete", slug):
        return {"ok": True, "dry_run": True}
    try:
        await h.mealie.delete(slug)
    except AdapterError as exc:
        raise _down(exc) from None
    return {"ok": True}


@router.put("/recipes/{slug}/image")
async def image(slug: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    data = await request.body()
    if not data or len(data) > 12_000_000:
        raise error(400, "bad_params", "Send a photo under 12 MB")
    ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "webp" if data[8:12] == b"WEBP" else "jpg"
    if ext == "jpg" and data[:3] != b"\xff\xd8\xff":
        raise error(400, "bad_params", "Send a JPEG, PNG or WebP photo")
    if not _edit(h, c, "image", slug):
        return {"ok": True, "dry_run": True}
    try:
        await h.mealie.set_image(slug, data, ext)
    except AdapterError as exc:
        raise _down(exc) from None
    return {"ok": True}


class FavBody(BaseModel):
    on: bool


@router.post("/recipes/{slug}/favorite")
async def favorite(slug: str, body: FavBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    if not _edit(h, c, "favorite", slug, on=body.on):
        return {"ok": True, "dry_run": True}
    try:
        r = await h.mealie.recipe(slug)
        names = [t for t in r["tags"] if t != FAVORITE_TAG] + ([FAVORITE_TAG] if body.on else [])
        await h.mealie.update(slug, {"tags": names})
    except AdapterError as exc:
        raise _down(exc) from None
    return {"ok": True}


class ShopBody(BaseModel):
    list_id: str | None = Field(default=None, max_length=40)


@router.post("/recipes/{slug}/shopping")
async def shop(slug: str, body: ShopBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    need(h, c, "shopping.add")
    if body.list_id and not UUID.match(body.list_id):
        raise error(400, "bad_params", "Unknown list")
    if not h.actions.is_live("shopping.add"):
        return {"ok": True, "dry_run": True}
    try:
        r = await h.mealie.recipe(slug)
        list_id = body.list_id or await h.mealie.first_list()
        if not list_id:
            raise error(409, "no_list", "Make a shopping list in Mealie first")
        await h.mealie.add_recipe_to_list(list_id, r["id"])
    except AdapterError as exc:
        raise _down(exc) from None
    h.db.log("shopping.recipe", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=slug)
    return {"ok": True, "message": f"Added {len(r['ingredients'])} ingredients"}


@router.post("/recipes/{slug}/made")
async def made(slug: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    if not _edit(h, c, "made", slug):
        return {"ok": True, "dry_run": True}
    try:
        await h.mealie.made(slug)
    except AdapterError as exc:
        raise _down(exc) from None
    return {"ok": True}


# -- dinner plan ------------------------------------------------------------------
def _parse_day(v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise error(400, "bad_params", "Use a date like 2026-11-14") from None


async def _week(h: Hub, start: date, days: int) -> list[dict[str, Any]]:
    entries = await h.mealie.plan(start, days)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        dinner = next((e for e in entries if e.get("date") == d.isoformat() and e.get("meal") == "dinner"), None)
        others = [e for e in entries if e.get("date") == d.isoformat() and e.get("meal") != "dinner"]
        out.append({"date": d.isoformat(), "dinner": dinner, "other": others})
    return out


@router.get("/plan")
async def plan(start: str | None = None, days: int = 7, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    s = _parse_day(start) if start else _today(h)
    try:
        return {"days": await _week(h, s, max(1, min(days, 21)))}
    except AdapterError as exc:
        raise _down(exc) from None


class PlanBody(BaseModel):
    slug: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=120)


@router.put("/plan/{day}")
async def plan_set(day: str, body: PlanBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    d = _parse_day(day)
    if not _edit(h, c, "plan", d.isoformat(), slug=body.slug, title=body.title):
        return {"ok": True, "dry_run": True}
    try:
        rid = None
        if body.slug:
            rid = (await h.mealie.recipe(_slug(body.slug)))["id"]
        await h.mealie.plan_set(d, rid, None if rid else (body.title or None))
        h.home.slow_at = 0  # refresh tonight's dinner on Home soon
        return {"ok": True, "days": await _week(h, d, 1)}
    except AdapterError as exc:
        raise _down(exc) from None


async def _suggestions(h: Hub, exclude: set[str], n: int) -> list[dict[str, Any]]:
    """Recipes not made lately come first; never-made ones mix in; favorites get a nudge."""
    lib = (await h.mealie.recipes(per_page=300))["items"]
    pool = [r for r in lib if r["slug"] not in exclude and "Dessert" not in r["tags"] and "Side" not in r["tags"]]

    def score(r: dict[str, Any]) -> float:
        last = r.get("last_made")
        try:
            age = (datetime.now() - datetime.fromisoformat(str(last)[:19])).days if last else 60
        except ValueError:
            age = 60
        return min(age, 90) + (20 if r["favorite"] else 0) + random.random() * 15

    pool.sort(key=score, reverse=True)
    return pool[:n]


@router.get("/suggest")
async def suggest(n: int = 6, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        week = await _week(h, _today(h), 7)
        planned = {d["dinner"]["slug"] for d in week if d["dinner"] and d["dinner"].get("slug")}
        return {"items": await _suggestions(h, planned, max(1, min(n, 20)))}
    except AdapterError as exc:
        raise _down(exc) from None


class FillBody(BaseModel):
    start: str | None = None
    days: int = Field(default=7, ge=1, le=14)


@router.post("/plan/fill")
async def fill(body: FillBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    s = _parse_day(body.start) if body.start else _today(h)
    try:
        week = await _week(h, s, body.days)
        empty = [d for d in week if not d["dinner"]]
        planned = {d["dinner"]["slug"] for d in week if d["dinner"] and d["dinner"].get("slug")}
        picks = await _suggestions(h, planned, len(empty))
        if not picks:
            raise error(409, "no_recipes", "Add a few recipes first")
        if not _edit(h, c, "fill", s.isoformat(), nights=len(picks)):
            return {"ok": True, "dry_run": True}
        for d, r in zip(empty, picks):
            await h.mealie.plan_set(date.fromisoformat(d["date"]), r["id"], None)
        h.home.slow_at = 0
        return {"ok": True, "filled": len(picks), "days": await _week(h, s, body.days)}
    except AdapterError as exc:
        raise _down(exc) from None
