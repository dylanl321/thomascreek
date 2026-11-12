"""Chores and rewards, calendar, meals, shopping, inventory, house assets."""

from __future__ import annotations

import time

import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..config import data_dir
from ..state import Hub
from .deps import Caller, caller, error, hub, need

router = APIRouter(prefix="/v1", tags=["family"])


def _unavailable(exc: Exception):
    return error(503, "unavailable", str(exc))


async def link_board_people(h: Hub) -> None:
    """Link hub people to the family board by first name, once, for anyone not linked yet."""
    unlinked = [p for p in h.db.people() if not p.get("family_board_id")]
    if not unlinked or not h.family_board.enabled:
        return
    last = h.__dict__.get("_board_link_at", 0.0)
    if time.time() - last < 300:
        return
    h.__dict__["_board_link_at"] = time.time()
    try:
        board = await h.family_board.people()
    except AdapterError:
        return
    taken = {p.get("family_board_id") for p in h.db.people() if p.get("family_board_id")}
    by_name = {}
    for b in board:
        first = str(b.get("name") or "").strip().split(" ")[0].lower()
        if first and b.get("id") not in taken:
            by_name.setdefault(first, []).append(b)
    for p in unlinked:
        first = str(p.get("name") or "").strip().split(" ")[0].lower()
        match = by_name.get(first) or []
        if len(match) == 1:
            h.db.update("people", "id", p["id"], {"family_board_id": match[0]["id"]})


async def _board_person(h: Hub, c: Caller, person: str | None) -> dict[str, Any]:
    await link_board_people(h)
    pid = person or c.subject.person_id
    if pid != c.subject.person_id:
        need(h, c, "chores.manage")
    p = h.db.person(pid)
    if p is None:
        raise error(404, "not_found", "No such person")
    if not p.get("family_board_id"):
        raise error(409, "not_linked", f"{p['name']} isn't linked to the family board yet")
    return p


@router.get("/chores")
async def chores(person: str | None = None, date: str | None = None, c: Caller = Depends(caller),
                 h: Hub = Depends(hub)) -> dict[str, Any]:
    if not (h.policy.has(c.subject, "chores.do") or h.policy.has(c.subject, "chores.manage")):
        raise error(403, "forbidden", "Not available")
    p = await _board_person(h, c, person)
    try:
        raw = await h.family_board.today(p["family_board_id"], date)
    except AdapterError as exc:
        raise _unavailable(exc) from None
    return {"person": {"id": p["id"], "name": p["name"]}, **h.family_board.normalize_today(raw)}


@router.get("/chores/all")
async def chores_all(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "chores.manage")
    await link_board_people(h)
    boards = []
    for p in h.db.people():
        if not p.get("family_board_id") or p["role"] not in ("child", "adult", "admin"):
            continue
        try:
            raw = await h.family_board.today(p["family_board_id"])
        except AdapterError as exc:
            raise _unavailable(exc) from None
        boards.append({"person": {"id": p["id"], "name": p["name"], "role": p["role"]},
                       **h.family_board.normalize_today(raw)})
    return {"boards": boards}


@router.get("/family/board-people")
async def board_people(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Admin: the board's people, for linking each hub person (family_board_id)."""
    need(h, c, "people")
    try:
        return {"people": await h.family_board.people()}
    except AdapterError as exc:
        raise _unavailable(exc) from None


class ForPerson(BaseModel):
    person: str | None = None
    date: str | None = None


@router.post("/chores/{chore_id}/done")
async def chore_done(chore_id: str, body: ForPerson | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if not h.policy.has(c.subject, "chores.manage"):
        need(h, c, "chores.do")
    p = await _board_person(h, c, body.person if body else None)
    if not h.actions.is_live("chores.done"):
        h.db.log("chores.done", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"], target=chore_id)
        return {"ok": True, "dry_run": True, "message": "Practice mode: not sent to the family board"}
    try:
        await h.family_board.chore_done(chore_id, p["family_board_id"], body.date if body else None)
    except AdapterError as exc:
        raise _unavailable(exc) from None
    h.db.log("chores.done", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=chore_id)
    return {"ok": True}


@router.post("/rewards/{reward_id}/redeem")
async def redeem(reward_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The family board already asks a parent to approve redemptions."""
    need(h, c, "chores.do")
    p = await _board_person(h, c, None)
    if not h.actions.is_live("chores.redeem"):
        h.db.log("chores.redeem", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"], target=reward_id)
        return {"ok": True, "dry_run": True}
    try:
        res = await h.family_board.redeem(reward_id, p["family_board_id"])
    except AdapterError as exc:
        raise _unavailable(exc) from None
    h.db.log("chores.redeem", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=reward_id)
    return {"ok": True, "result": res}


@router.get("/calendar")
async def calendar(days: int = 7, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Any:
    need(h, c, "family.view")
    from .trips import calendar_events  # trip days appear as all-day entries

    days = max(1, min(days, 31))
    trip_days = await calendar_events(h, c, days)
    if not h.family_board.enabled and trip_days:
        return {"events": trip_days}
    try:
        cal = await h.family_board.calendar(days)
    except AdapterError as exc:
        if trip_days:
            return {"events": trip_days}
        raise _unavailable(exc) from None
    if trip_days:
        cal = {**cal, "events": trip_days + list(cal.get("events", []))}
    return cal


@router.get("/meals/today")
async def meals_today(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        return {"items": await h.mealie.today()}
    except AdapterError as exc:
        raise _unavailable(exc) from None


@router.get("/meals/week")
async def meals_week(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        return {"items": await h.mealie.week()}
    except AdapterError as exc:
        raise _unavailable(exc) from None


@router.get("/meals/image/{recipe_id}")
async def meal_image(recipe_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    need(h, c, "family.view")
    if not recipe_id.replace("-", "").isalnum():
        raise error(404, "not_found", "No image")
    try:
        data, ctype = await h.mealie.image(recipe_id)
    except AdapterError:
        raise error(404, "not_found", "No image") from None
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/shopping")
async def shopping(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if not (h.policy.has(c.subject, "family.view") or h.policy.has(c.subject, "shopping.add")):
        raise error(403, "forbidden", "Not available")
    try:
        return {"lists": await h.mealie.shopping()}
    except AdapterError as exc:
        raise _unavailable(exc) from None


class ItemBody(BaseModel):
    text: str = Field(min_length=1, max_length=200)


@router.post("/shopping/{list_id}/items")
async def shopping_add(list_id: str, body: ItemBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "shopping.add")
    if not h.actions.is_live("shopping.add"):
        h.db.log("shopping.add", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"], target=list_id,
                 params={"text": body.text})
        return {"ok": True, "dry_run": True}
    try:
        await h.mealie.add_item(list_id, body.text)
    except AdapterError as exc:
        raise _unavailable(exc) from None
    h.db.log("shopping.add", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=list_id,
             params={"text": body.text})
    return {"ok": True}


class DoneBody(BaseModel):
    done: bool


@router.patch("/shopping/{list_id}/items/{item_id}")
async def shopping_done(list_id: str, item_id: str, body: DoneBody, c: Caller = Depends(caller),
                        h: Hub = Depends(hub)) -> dict[str, Any]:
    if not h.policy.has(c.subject, "shopping.add"):
        need(h, c, "family.edit")
    if not item_id.replace("-", "").isalnum():
        raise error(404, "not_found", "No such item")
    if not h.actions.is_live("shopping.add"):
        h.db.log("shopping.check", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"], target=item_id,
                 params={"done": body.done})
        return {"ok": True, "dry_run": True}
    try:
        await h.mealie.set_item_done(item_id, body.done)
    except AdapterError as exc:
        raise _unavailable(exc) from None
    h.db.log("shopping.check", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=item_id,
             params={"done": body.done})
    return {"ok": True}


@router.get("/meals/recipes/{slug}")
async def recipe(slug: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    if not slug.replace("-", "").replace("_", "").isalnum():
        raise error(404, "not_found", "No such recipe")
    try:
        return await h.mealie.recipe(slug)
    except AdapterError as exc:
        raise _unavailable(exc) from None


@router.get("/inventory/search")
async def inventory(q: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "inventory.search")
    try:
        return {"items": await h.homebox.search(q.strip()[:100])}
    except AdapterError as exc:
        raise _unavailable(exc) from None


# -- house assets that can't live in the public repo ------------------------
ASSET_TYPES = {"house-model": "application/json", "header": "image/jpeg"}


@router.get("/assets/{name}")
async def asset(name: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    if name not in ASSET_TYPES:
        raise error(404, "not_found", "No such asset")
    rel = h.config.assets.get(name) or {"house-model": "assets/house-model-web.json", "header": "assets/header.jpg"}[name]
    base = data_dir().resolve()
    path = (base / rel).resolve()
    if base not in path.parents or not path.is_file():
        raise error(404, "not_found", "Asset not uploaded yet")
    etag = _etag(path)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    media = ASSET_TYPES[name]
    if name == "header" and path.suffix.lower() in (".png", ".heic", ".webp"):
        media = {".png": "image/png", ".heic": "image/heic", ".webp": "image/webp"}[path.suffix.lower()]
    return FileResponse(path, media_type=media, headers={"ETag": etag, "Cache-Control": "private, max-age=3600"})


def _etag(path: Path) -> str:
    st = path.stat()
    return '"' + hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:20] + '"'


# -- Adding and changing chores and calendar entries on the family board ------------------------
# The board calls everything an "item". Chores, tasks and routines are jobs someone does (and may
# earn points for); schedule, activity and alert items are what the calendar shows.

JOB_KINDS = ("chore", "task", "routine")
CALENDAR_KINDS = ("schedule", "activity", "alert")
ITEM_FIELDS = ("title", "kind", "date", "end_date", "time", "repeat", "weekdays", "interval", "points",
               "notes", "priority", "word")


class BoardItemBody(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    kind: str | None = Field(default=None, description="chore, task or routine (jobs); schedule, activity or alert "
                                                       "(calendar events)")
    people: list[str] | None = Field(default=None, description="Hub person ids or names, or ['all'] for the family")
    date: str | None = Field(default=None, description="YYYY-MM-DD; the first day for repeating items")
    end_date: str | None = None
    time: str | None = Field(default=None, description="HH:MM, 24-hour; empty for any time / all day")
    repeat: str | None = Field(default=None, description="once, daily, weekdays, weekends, weekly or custom")
    weekdays: list[str] | None = Field(default=None, description="For weekly/custom: monday ... sunday")
    interval: int | None = None
    points: int | None = Field(default=None, ge=0, le=1000)
    notes: str | None = Field(default=None, max_length=500)
    priority: str | None = None
    word: str | None = Field(default=None, max_length=24, description="One-word label for little kids")


def _item_need(h: Hub, c: Caller, kind: str | None) -> None:
    if kind and kind not in JOB_KINDS + CALENDAR_KINDS:
        raise error(400, "bad_kind", f"kind must be one of {', '.join(JOB_KINDS + CALENDAR_KINDS)}")
    need(h, c, "chores.manage" if kind in JOB_KINDS else "family.edit")


async def _board_ids(h: Hub, people: list[str]) -> list[str]:
    if people == ["all"] or "all" in people:
        return ["all"]
    await link_board_people(h)
    out = []
    hub_people = h.db.people()
    for ref in people:
        low = str(ref).strip().lower()
        p = next((x for x in hub_people if x["id"] == ref or str(x.get("name", "")).lower() == low), None)
        if p is None:
            p = next((x for x in hub_people if str(x.get("name", "")).split(" ")[0].lower() == low), None)
        if p is None:
            raise error(404, "not_found", f"No one called {ref}")
        if not p.get("family_board_id"):
            raise error(409, "not_linked", f"{p['name']} isn't linked to the family board yet")
        out.append(p["family_board_id"])
    return out


def _item_out(h: Hub, it: dict[str, Any]) -> dict[str, Any]:
    names = {p.get("family_board_id"): p["name"] for p in h.db.people() if p.get("family_board_id")}
    ids = it.get("person_ids") or ([it["person_id"]] if it.get("person_id") else [])
    return {"id": it.get("id"), "title": it.get("title"), "kind": it.get("kind"),
            "people": ["everyone" if i == "all" else names.get(i, i) for i in ids],
            **{k: it.get(k) for k in ("date", "end_date", "time", "repeat", "weekdays", "points", "notes")
               if it.get(k) not in (None, "", [], 0)}}


async def _board_write(coro) -> Any:
    from ..adapters.family import BoardRefused

    try:
        return await coro
    except BoardRefused as exc:
        raise error(exc.status, "refused", exc.message) from None
    except AdapterError as exc:
        raise _unavailable(exc) from None


@router.get("/family/items")
async def board_items(kind: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Every chore, routine and calendar entry on the family board (ids for changing them)."""
    need(h, c, "family.view")
    items = await _board_write(h.family_board.items())
    return {"items": [_item_out(h, i) for i in items if not kind or i.get("kind") == kind]}


@router.post("/family/items")
async def board_item_add(body: BoardItemBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Add a chore for someone, or an event to the family calendar."""
    if not body.title or not body.kind:
        raise error(400, "bad_request", "title and kind are required")
    _item_need(h, c, body.kind)
    data = {k: v for k, v in body.model_dump().items() if k in ITEM_FIELDS and v is not None}
    data["person_ids"] = await _board_ids(h, body.people or ["all"])
    if not h.actions.is_live("family.items"):
        return _practice(h, c, "family.item_add", body.title)
    item = await _board_write(h.family_board.create_item(data))
    h.db.log("family.item_add", "ok", person_id=c.subject.person_id, device_id=c.subject.device_id,
             target=item.get("id"), detail=body.title, source=_via(request))
    return {"ok": True, "item": _item_out(h, item)}


async def _existing(h: Hub, item_id: str) -> dict[str, Any]:
    items = await _board_write(h.family_board.items())
    it = next((i for i in items if i.get("id") == item_id), None)
    if it is None:
        raise error(404, "not_found", "No such chore or event")
    return it


@router.patch("/family/items/{item_id}")
async def board_item_change(item_id: str, body: BoardItemBody, request: Request, c: Caller = Depends(caller),
                            h: Hub = Depends(hub)) -> dict[str, Any]:
    """Change a chore or event: who it's for, when, how often, points, notes."""
    it = await _existing(h, item_id)
    _item_need(h, c, it.get("kind"))
    if body.kind and body.kind != it.get("kind"):
        _item_need(h, c, body.kind)
    data = {k: v for k, v in body.model_dump().items() if k in ITEM_FIELDS and v is not None}
    if body.people is not None:
        data["person_ids"] = await _board_ids(h, body.people)
    if not h.actions.is_live("family.items"):
        return _practice(h, c, "family.item_change", item_id)
    item = await _board_write(h.family_board.update_item(item_id, data))
    h.db.log("family.item_change", "ok", person_id=c.subject.person_id, device_id=c.subject.device_id,
             target=item_id, detail=str(item.get("title"))[:120], source=_via(request))
    return {"ok": True, "item": _item_out(h, item)}


@router.delete("/family/items/{item_id}")
async def board_item_remove(item_id: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Take a chore or event off the board (archived there, so it can be restored)."""
    it = await _existing(h, item_id)
    _item_need(h, c, it.get("kind"))
    if not h.actions.is_live("family.items"):
        return _practice(h, c, "family.item_remove", item_id)
    await _board_write(h.family_board.archive_item(item_id))
    h.db.log("family.item_remove", "ok", person_id=c.subject.person_id, device_id=c.subject.device_id,
             target=item_id, detail=str(it.get("title"))[:120], source=_via(request))
    return {"ok": True}


def _practice(h: Hub, c: Caller, action: str, target: str | None) -> dict[str, Any]:
    h.db.log(action, "dry_run", person_id=c.subject.person_id, device_id=c.subject.device_id, target=target)
    return {"ok": True, "dry_run": True, "message": "Practice mode: not sent to the family board"}


def _via(request: Request) -> str:
    return "ask" if request.headers.get("x-tc-via") == "ask" else "app"
