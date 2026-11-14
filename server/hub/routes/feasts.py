"""Recipe sections, holiday/feast plans and the merged shopping list (see hub/feasts.py)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import feasts as F
from ..adapters.base import AdapterError
from ..state import Hub
from .deps import Caller, caller, error, hub, need
from .recipes import _down, _edit, _slug, _today

router = APIRouter(prefix="/v1/meals", tags=["feasts"])


def _store(h: Hub) -> F.FeastStore:
    return F.FeastStore(h.db)


def _day(v: str | None, field: str) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise error(400, "bad_params", f"Use a date like 2026-11-26 for {field}") from None


def _plan_or_404(h: Hub, plan_id: str) -> dict[str, Any]:
    p = _store(h).get(plan_id[:40])
    if not p:
        raise error(404, "not_found", "No such plan")
    return p


def _with_timeline(p: dict[str, Any]) -> dict[str, Any]:
    return {**p, "timeline": F.timeline(p)}


# -- sections -----------------------------------------------------------------------------
@router.get("/sections")
async def sections(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        lib = (await h.mealie.recipes(per_page=500))["items"]
    except AdapterError as exc:
        raise _down(exc) from None
    out = F.group_by_section(lib)
    out["total"] = len(lib)
    return out


class SectionsBody(BaseModel):
    sections: list[str] = Field(default_factory=list, max_length=12)


@router.put("/recipes/{slug}/sections")
async def set_sections(slug: str, body: SectionsBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _slug(slug)
    names = [n.strip()[:40] for n in body.sections if n.strip()]
    if not _edit(h, c, "sections", slug, sections=names):
        return {"ok": True, "dry_run": True}
    try:
        new = await h.mealie.update(slug, {"sections": names})
    except AdapterError as exc:
        raise _down(exc) from None
    return {"ok": True, "slug": new, "sections": [F.section_name(n) or n for n in names]}


# -- feast plans ---------------------------------------------------------------------------
@router.get("/feasts")
async def feasts(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    return {"plans": _store(h).list(), "templates": F.templates(_today(h)),
            "courses": [{"key": k, "title": v} for k, v in F.COURSES.items()],
            "when": [{"key": k, "title": F.WHEN_LABELS[k]} for k in F.WHEN]}


class NewFeast(BaseModel):
    template: str = Field(default="feast", max_length=30)
    name: str | None = Field(default=None, max_length=80)
    start: str | None = None
    end: str | None = None
    guests: int | None = Field(default=None, ge=1, le=200)


@router.post("/feasts")
async def create_feast(body: NewFeast, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    s, e = _day(body.start, "start"), _day(body.end, "end")
    if s and e and e < s:
        raise error(400, "bad_params", "The end is before the start")
    if e and s and (e - s).days > 62:
        raise error(400, "bad_params", "Keep a plan under two months")
    plan = F.new_plan(body.template, _today(h), body.name and body.name.strip(), s, e, body.guests)
    if not _edit(h, c, "feast.create", plan["name"]):
        return {"ok": True, "dry_run": True}
    return {"ok": True, "plan": _with_timeline(_store(h).create(plan, c.subject.person_id))}


@router.get("/feasts/{plan_id}")
async def feast(plan_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    return {"plan": _with_timeline(_plan_or_404(h, plan_id))}


class EditFeast(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    start: str | None = None
    end: str | None = None
    guests: int | None = Field(default=None, ge=1, le=200)
    notes: str | None = Field(default=None, max_length=2000)
    courses: list[dict[str, Any]] | None = Field(default=None, max_length=12)


@router.put("/feasts/{plan_id}")
async def edit_feast(plan_id: str, body: EditFeast, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    p = _plan_or_404(h, plan_id)
    s = _day(body.start, "start") or date.fromisoformat(p["start_date"])
    e = _day(body.end, "end") or (s if body.start and not body.end else date.fromisoformat(p["end_date"]))
    if e < s:
        e = s
    if (e - s).days > 62:
        raise error(400, "bad_params", "Keep a plan under two months")
    if body.name is not None and not body.name.strip():
        raise error(400, "bad_params", "Give the plan a name")
    p.update({"start_date": s.isoformat(), "end_date": e.isoformat()})
    if body.name is not None:
        p["name"] = body.name.strip()
    if body.guests is not None:
        p["guests"] = body.guests
    if body.notes is not None:
        p["notes"] = body.notes
    p["courses"] = F.clean_courses(body.courses if body.courses is not None else p["courses"], s, e)
    if not _edit(h, c, "feast.edit", plan_id):
        return {"ok": True, "dry_run": True}
    return {"ok": True, "plan": _with_timeline(_store(h).save(plan_id, p))}


@router.delete("/feasts/{plan_id}")
async def delete_feast(plan_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _plan_or_404(h, plan_id)
    if not _edit(h, c, "feast.delete", plan_id):
        return {"ok": True, "dry_run": True}
    _store(h).delete(plan_id)
    return {"ok": True}


class FeastItem(BaseModel):
    course: str = Field(max_length=30)
    slug: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=120)
    servings: int | None = Field(default=None, ge=1, le=200)
    when: str | None = Field(default=None, max_length=20)
    day: str | None = None


@router.post("/feasts/{plan_id}/items")
async def add_item(plan_id: str, body: FeastItem, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    p = _plan_or_404(h, plan_id)
    if not body.slug and not (body.title and body.title.strip()):
        raise error(400, "bad_params", "Pick a recipe or type a dish")
    item: dict[str, Any] = {"title": body.title, "servings": body.servings, "when": body.when, "day": body.day}
    if body.slug:
        try:
            r = await h.mealie.recipe_parts(_slug(body.slug))
        except AdapterError as exc:
            raise _down(exc) from None
        item.update({"slug": r["slug"], "recipe_id": r.get("id"), "title": body.title or r.get("title"),
                     "image_url": r.get("image_url"), "base_servings": F.servings_number(r.get("servings"))})
    courses = p["courses"]
    course = next((x for x in courses if x["key"] == body.course), None)
    if course is None:
        course = {"key": body.course, "title": F.COURSES.get(body.course, body.course.title()), "items": []}
        courses.append(course)
    course["items"].append(item)
    p["courses"] = F.clean_courses(courses, date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"]))
    if not _edit(h, c, "feast.add", plan_id, slug=body.slug, course=body.course):
        return {"ok": True, "dry_run": True}
    return {"ok": True, "plan": _with_timeline(_store(h).save(plan_id, p))}


@router.delete("/feasts/{plan_id}/items/{item_id}")
async def remove_item(plan_id: str, item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    p = _plan_or_404(h, plan_id)
    for course in p["courses"]:
        course["items"] = [i for i in course["items"] if i.get("id") != item_id]
    if not _edit(h, c, "feast.remove", plan_id, item=item_id):
        return {"ok": True, "dry_run": True}
    return {"ok": True, "plan": _with_timeline(_store(h).save(plan_id, p))}


_MEAL_FOR = {"mains": "dinner", "breakfast": "breakfast"}


@router.post("/feasts/{plan_id}/mealplan")
async def to_mealplan(plan_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Copies the plan's recipes onto Mealie's meal plan (mains as dinner, the rest as sides)."""
    p = _plan_or_404(h, plan_id)
    if not _edit(h, c, "feast.mealplan", plan_id):
        return {"ok": True, "dry_run": True}
    start, end = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
    added = 0
    try:
        have = {(e.get("date"), e.get("recipe_id")) for e in await h.mealie.plan(start, (end - start).days + 1)}
        for course in p["courses"]:
            for it in course["items"]:
                if not it.get("recipe_id"):
                    continue
                day = it.get("day") or p["start_date"]
                if (day, it["recipe_id"]) in have:
                    continue
                await h.mealie.plan_add(date.fromisoformat(day), it["recipe_id"],
                                        _MEAL_FOR.get(course["key"].split("-")[0], "side"))
                have.add((day, it["recipe_id"]))
                added += 1
    except AdapterError as exc:
        raise _down(exc) from None
    h.home.slow_at = 0
    return {"ok": True, "added": added, "message": f"Put {added} dishes on the meal plan" if added else "Already on the meal plan"}


# -- the merged shopping list --------------------------------------------------------------
class Scope(BaseModel):
    feasts: list[str] = Field(default_factory=list, max_length=10)
    start: str | None = None     # the meal plan from start to end (both included)
    end: str | None = None
    skip_staples: bool = False


class PushBody(Scope):
    list_id: str | None = Field(default=None, max_length=40)
    list_name: str | None = Field(default=None, max_length=80)
    skip: list[str] = Field(default_factory=list, max_length=500)


async def _sources(h: Hub, scope: Scope) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Each recipe in the scope with its scale factor, plus a short description of the scope."""
    wanted: list[tuple[str, float | None, str]] = []   # (slug, target servings or None, why)
    names = []
    for fid in scope.feasts:
        p = _plan_or_404(h, fid)
        names.append(p["name"])
        for course in p["courses"]:
            for it in course["items"]:
                if it.get("slug"):
                    wanted.append((it["slug"], float(it.get("servings") or p["guests"]), p["name"]))
    s, e = _day(scope.start, "start"), _day(scope.end, "end")
    if s or e:
        s = s or e
        e = e or s
        if e < s:  # type: ignore[operator]
            raise error(400, "bad_params", "The end is before the start")
        days = (e - s).days + 1  # type: ignore[operator]
        if days > 63:
            raise error(400, "bad_params", "Pick nine weeks or less")
        for entry in await h.mealie.plan(s, days):
            if entry.get("slug"):
                wanted.append((entry["slug"], None, entry.get("date") or ""))
        names.append(f"Meals {s:%b} {s.day}–{e:%b} {e.day}" if days > 1 else f"Meals {s:%b} {s.day}")  # type: ignore[union-attr]
    if not wanted and not names:
        raise error(400, "bad_params", "Pick a plan or some weeks")
    cache: dict[str, dict[str, Any]] = {}
    sources, info = [], []
    for slug, target, why in wanted:
        if slug not in cache:
            try:
                cache[slug] = await h.mealie.recipe_parts(slug)
            except AdapterError:
                info.append({"title": slug, "missing": True, "why": why})
                continue
        r = cache[slug]
        base = F.servings_number(r.get("servings"))
        factor = round(target / base, 3) if target and base else 1.0
        sources.append({"title": r.get("title") or slug, "factor": factor, "ingredients": r["ingredients"]})
        info.append({"title": r.get("title") or slug, "slug": slug, "factor": factor, "servings": target,
                     "base_servings": base, "why": why, "scaled": bool(target and base)})
    return sources, info, " + ".join(names)


@router.post("/shopping/build")
async def build(body: Scope, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "family.view")
    try:
        sources, info, label = await _sources(h, body)
        lists = await h.mealie.lists()
    except AdapterError as exc:
        raise _down(exc) from None
    out = F.build_list(sources, body.skip_staples)
    return {**out, "sources": info, "label": label, "lists": lists}


@router.post("/shopping/push")
async def push(body: PushBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "shopping.add")
    try:
        sources, _info, label = await _sources(h, body)
        out = F.build_list(sources, body.skip_staples)
        skip = set(body.skip)
        texts = [F.push_text(i) for i in out["items"] if i["key"] not in skip]
        if not texts:
            raise error(409, "nothing", "Everything is checked off already")
        if not h.actions.is_live("shopping.add"):
            h.db.log("shopping.build", "dry_run", person_id=c.subject.person_id, device_id=c.device["id"],
                     target=label[:120], params={"items": len(texts)})
            return {"ok": True, "dry_run": True}
        list_id = body.list_id
        if list_id and list_id not in {x["id"] for x in await h.mealie.lists()}:
            raise error(400, "bad_params", "Unknown list")
        if not list_id:
            list_id = await h.mealie.create_list((body.list_name or label or "Shopping").strip()[:80])
        n = await h.mealie.add_items(list_id, texts)
    except AdapterError as exc:
        raise _down(exc) from None
    h.db.log("shopping.build", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=label[:120],
             params={"items": n, "list": list_id})
    return {"ok": True, "list_id": list_id, "added": n, "message": f"Added {n} items to Shopping"}
