"""Family life: Today, who's home, arriving/leaving, intercom, kids' bedtime,
asking a parent, guest passes (with their public page) and Tell Dylan feedback."""

from __future__ import annotations

import base64
import html
import time
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..home import iso, kind_for
from ..household import INTERCOM_PHRASES, default_pass_window
from ..state import Hub
from .deps import Caller, caller, caller_ip, error, hub, need, require_confirm

router = APIRouter(prefix="/v1", tags=["household"])
pages = APIRouter(tags=["household"])


def _grown_up(c: Caller) -> bool:
    return c.subject.role in ("admin", "adult")


# -- Today ------------------------------------------------------------------
@router.get("/today")
async def today(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """One glance at the day: weather, calendar, dinner, who's home, chores and bedtime.
    Chores are the caller's own (children) or every child's progress (grown-ups)."""
    chores: dict[str, Any] | None = None
    if h.family_board.enabled:
        from .family import link_board_people

        try:
            await link_board_people(h)
            if c.subject.role == "child":
                p = h.db.person(c.subject.person_id)
                if p and p.get("family_board_id"):
                    b = h.family_board.normalize_today(await h.family_board.today(p["family_board_id"]))
                    chores = {"mine": {"done": b["done"], "total": b["goal"] or len(b["items"]), "points": b["points"],
                                       "next": next((i["title"] for i in b["items"] if not i["done"]), None)}}
            elif h.policy.has(c.subject, "chores.manage"):
                kids = []
                for p in h.db.people_with_role("child"):
                    if not p.get("family_board_id"):
                        continue
                    b = h.family_board.normalize_today(await h.family_board.today(p["family_board_id"]))
                    kids.append({"id": p["id"], "name": p["name"].split()[0], "done": b["done"],
                                 "total": b["goal"] or len(b["items"]), "points": b["points"]})
                chores = {"kids": kids}
        except AdapterError:
            chores = None
    out = h.household.today(c.subject, chores)
    if h.policy.has(c.subject, "trips.view"):
        from ..adapters.trips import next_trip

        try:
            hidden = set(h.config.trek.hidden_from_children) if c.subject.role in ("child", "tablet") else set()
            nxt = next_trip([t for t in h.smart.trips if t.get("id") not in hidden])
            if nxt:
                out["trip"] = {k: nxt.get(k) for k in ("id", "title", "start_date", "end_date", "status", "days_until")
                               if nxt.get(k) is not None}
        except Exception:  # noqa: BLE001
            pass
    return out


@router.get("/family/presence")
async def presence(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if c.subject.role in ("guest", "pending"):
        raise error(403, "forbidden", "Not available")
    return {"people": h.household.family(c.subject)}


class PresenceReport(BaseModel):
    event: Literal["arrive", "leave"]


@router.post("/presence")
async def report_presence(body: PresenceReport, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The phone crossed the edge of home (its geofence). Leaving may answer with what's still open;
    arriving may run the person's welcome mode."""
    return await h.household.report(c.subject, body.event)


@router.get("/departure")
async def departure(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """"Last one out?": who's still home and what's still open, each with its fix."""
    if not _grown_up(c):
        raise error(403, "forbidden", "Not available")
    return h.household.departure(c.subject)


class Welcome(BaseModel):
    mode: str | None = None


@router.get("/me/welcome")
async def get_welcome(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    modes = [{"id": m.id, "name": m.name, "symbol": m.symbol} for m in h.config.modes if c.subject.role in m.roles]
    out: dict[str, Any] = {"mode": h.household.welcome_mode(c.subject.person_id), "modes": modes}
    if c.subject.role in ("admin", "adult", "child"):
        try:
            loc = await h.smart.weather._location()  # the house, for the phone's arrive/leave circle
        except Exception:  # noqa: BLE001
            loc = None
        if loc:
            out["location"] = {"lat": loc[0], "lon": loc[1]}
    return out


@router.put("/me/welcome")
async def set_welcome(body: Welcome, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    if not _grown_up(c) or not c.subject.person_id:
        raise error(403, "forbidden", "Not available")
    if body.mode and not any(m.id == body.mode for m in h.config.modes):
        raise error(404, "not_found", "No such mode")
    h.household.set_welcome_mode(c.subject.person_id, body.mode)
    return {"mode": body.mode}


# -- Intercom ---------------------------------------------------------------
@router.get("/intercom")
async def intercom(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Quick phrases and the speakers an announcement can go to (send it with action "announce")."""
    if not h.policy.has(c.subject, "family.edit"):
        raise error(403, "forbidden", "Not available")
    rooms: dict[str, dict[str, Any]] = {}
    for eid, st in h.home.states.items():
        if kind_for(st) != "speaker":
            continue
        area = h.home.entity_area.get(eid)
        if not area:
            continue
        a = h.home.areas.get(area, {})
        r = rooms.setdefault(area, {"id": area, "name": a.get("name") or area, "speakers": 0})
        r["speakers"] += 1
    return {"phrases": INTERCOM_PHRASES, "rooms": sorted(rooms.values(), key=lambda r: r["name"])}


class AskParent(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    emoji: str | None = Field(default=None, max_length=8)


@router.post("/ask-parent")
async def ask_parent(body: AskParent, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A child's picture button ("I'm hungry", "Come here please"): a push to the grown-ups at home first."""
    if c.subject.role not in ("child", "admin", "adult"):
        raise error(403, "forbidden", "Not available")
    me = h.db.person(c.subject.person_id) if c.subject.person_id else None
    first = (me or {}).get("name", "Someone").split()[0]
    grownups = [p for p in h.db.people_with_role("admin", "adult")]
    home = [p["id"] for p in grownups if h.household.presence(p)["home"]]
    to = home or [p["id"] for p in grownups]
    await h.notify.send(person_ids=to, level="decision", cat="kids", title=f"{(body.emoji or '').strip()} {first} asks".strip(),
                        body=body.text, ref=f"thomascreek://people/{c.subject.person_id}", interruption="time-sensitive")
    h.db.log("ask_parent", "ok", person_id=c.subject.person_id, device_id=c.subject.device_id, params={"text": body.text})
    names = [h.db.person(p)["name"].split()[0] for p in to if h.db.person(p)]
    return {"ok": True, "sent_to": names}


# -- Kids' bedtime -------------------------------------------------------------
class Bedtime(BaseModel):
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    wake: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    enabled: bool | None = None


def _child(h: Hub, c: Caller, person_id: str) -> dict[str, Any]:
    p = h.db.person(person_id)
    if p is None or p["role"] != "child":
        raise error(404, "not_found", "Bedtime is only for children")
    if c.subject.person_id != person_id:
        need(h, c, "chores.manage")
    return p


@router.get("/people/{person_id}/bedtime")
async def get_bedtime(person_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return h.household.bedtime(_child(h, c, person_id)) or {}


@router.put("/people/{person_id}/bedtime")
async def put_bedtime(person_id: str, body: Bedtime, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    p = _child(h, c, person_id)
    need(h, c, "chores.manage")
    h.household.set_bedtime(p["id"], time_=body.time, wake=body.wake, enabled=body.enabled)
    return h.household.bedtime(p) or {}


class Extend(BaseModel):
    minutes: int = Field(default=30, ge=5, le=180)


@router.post("/people/{person_id}/bedtime/extend")
async def extend_bedtime(person_id: str, body: Extend, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    p = _child(h, c, person_id)
    need(h, c, "chores.manage")
    until = h.household.extend_bedtime(p["id"], body.minutes)
    h.db.log("bedtime.extend", "ok", person_id=c.subject.person_id, device_id=c.subject.device_id, target=p["id"],
             params={"minutes": body.minutes})
    return {"extended_until": iso(until), **(h.household.bedtime(p) or {})}


# -- Live Activity tokens ------------------------------------------------------
class LiveToken(BaseModel):
    push_to_start_token: str | None = Field(default=None, max_length=400)
    env: Literal["production", "sandbox"] = "production"


@router.post("/devices/live-activity")
async def live_token(body: LiveToken, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The phone's ActivityKit push-to-start token, so the doorbell can open a Live Activity."""
    h.household.set_live_token(c.device["id"], body.push_to_start_token, body.env)
    return {"ok": True}


# -- Guest passes -----------------------------------------------------------------
class NewPass(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    days: float | None = Field(default=None, gt=0, le=60)
    until: float | None = None          # unix seconds
    starts: float | None = None
    show_wifi: bool = True
    door_code: str | None = Field(default=None, pattern=r"^\d{4,8}$")
    note: str | None = Field(default=None, max_length=500)


def _base(request: Request, h: Hub) -> str:
    return h.config.hub.remote_url or str(request.base_url)


@router.get("/guest-passes")
async def list_passes(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "guests")
    rows = h.db.all("SELECT * FROM guest_passes WHERE revoked_at IS NULL OR revoked_at > ? ORDER BY expires_at DESC LIMIT 30",
                    [time.time() - 14 * 86400])
    g = h.config.guest
    return {"passes": [h.household.guest_pass_row(r, _base(request, h)) for r in rows],
            "wifi_ssid": g.wifi_ssid, "can_set_lock": bool(g.lock_code and g.lock_code.entity),
            "door_name": g.lock_code.door_name if g.lock_code else None, "remote": bool(h.config.hub.remote_url)}


@router.post("/guest-passes")
async def create_pass(body: NewPass, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "guests")
    if body.door_code:
        await require_confirm(request, h, c, "secure")  # a door code is a key to the house
    start, end = default_pass_window(body.days, body.until, body.starts)
    if end <= time.time():
        raise error(400, "bad_params", "That pass would already be over")
    row = await h.household.create_guest_pass(c.subject, name=body.name.strip(), starts_at=start, expires_at=end,
                                              show_wifi=body.show_wifi, door_code=body.door_code, note=body.note)
    return h.household.guest_pass_row(row, _base(request, h))


@router.delete("/guest-passes/{pass_id}")
async def revoke_pass(pass_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "guests")
    if not h.db.one("SELECT id FROM guest_passes WHERE id=?", [pass_id]):
        raise error(404, "not_found", "No such pass")
    await h.household.end_guest_pass(pass_id, revoked_by=c.subject.person_id)
    return {"ok": True}


_PASS_HITS: dict[str, list[float]] = {}


def _limited(ip: str | None) -> bool:
    now = time.monotonic()
    k = ip or "?"
    recent = [t for t in _PASS_HITS.get(k, []) if now - t < 60][-30:]
    recent.append(now)
    _PASS_HITS[k] = recent
    if len(_PASS_HITS) > 2000:
        _PASS_HITS.clear()
    return len(recent) > 20


def _pass_for(token: str, request: Request, h: Hub) -> dict[str, Any] | None:
    if _limited(caller_ip(request, h)) or len(token) > 64:
        return None
    row = h.household.find_pass(token)
    if row is None:
        return None
    h.db.execute("UPDATE guest_passes SET views=views+1, last_viewed_at=? WHERE id=?", [time.time(), row["id"]])
    return row


@pages.get("/g/{token}.json")
async def pass_json(token: str, request: Request, h: Hub = Depends(hub)) -> JSONResponse:
    row = _pass_for(token, request, h)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    now = time.time()
    if row.get("revoked_at") or row["expires_at"] <= now:
        return JSONResponse({"state": "ended", "house": h.config.hub.name})
    if row["starts_at"] > now:
        return JSONResponse({"state": "upcoming", "starts_at": iso(row["starts_at"]), "house": h.config.hub.name})
    return JSONResponse({"state": "active", **h.household.guest_pass_public(row)})


@pages.get("/g/{token}", response_class=HTMLResponse)
async def pass_page(token: str, request: Request, h: Hub = Depends(hub)) -> HTMLResponse:
    """The page a guest opens from a text message: Wi-Fi, door code, house notes. No app, no sign-in."""
    row = _pass_for(token, request, h)
    house = html.escape(h.config.hub.name)
    if row is None:
        return HTMLResponse(_page(house, "<h1>Link not found</h1><p>Ask your host for a new one.</p>"), status_code=404)
    now = time.time()
    if row.get("revoked_at") or row["expires_at"] <= now:
        return HTMLResponse(_page(house, f"<h1>Thanks for visiting</h1><p>This pass for {html.escape(row['name'])} has ended.</p>"))
    if row["starts_at"] > now:
        when = datetime.fromtimestamp(row["starts_at"], h.policy.tz).strftime("%A %-d %B, %-I:%M %p")
        return HTMLResponse(_page(house, f"<h1>See you soon, {html.escape(row['name'])}</h1><p>Your pass starts {when}.</p>"))
    p = h.household.guest_pass_public(row)
    until = datetime.fromtimestamp(row["expires_at"], h.policy.tz).strftime("%A %-d %B, %-I:%M %p")
    parts = [f"<p class=eyebrow>Welcome to {house}</p><h1>Hi {html.escape(p['name'])}!</h1>",
             f"<p class=dim>Your pass is good until {until}.</p>"]
    if p.get("wifi"):
        w = p["wifi"]
        parts.append("<section><h2>📶 Wi-Fi</h2>"
                     f"<div class=row><span>Network</span><b>{html.escape(w['ssid'] or '')}</b></div>"
                     + (f"<div class=row><span>Password</span><b class=copy data-v='{html.escape(w['password'])}'>"
                        f"{html.escape(w['password'])}</b></div>" if w.get("password") else "")
                     + "</section>")
    if p.get("door_code"):
        parts.append(f"<section><h2>🔑 {html.escape(p.get('door_name') or 'Door')} code</h2>"
                     f"<div class=code>{html.escape(p['door_code'])}</div>"
                     "<p class=dim>Type it on the keypad, then press the lock button.</p></section>")
    if p.get("address"):
        q = html.escape(p["address"])
        parts.append(f"<section><h2>📍 Address</h2><p>{q}</p>"
                     f"<a class=btn href='https://maps.apple.com/?q={q}'>Open in Maps</a></section>")
    if p.get("note"):
        parts.append(f"<section><h2>📝 From your host</h2><p>{html.escape(p['note'])}</p></section>")
    if p.get("guidance"):
        items = "".join(f"<li><b>{html.escape(g.get('title', ''))}</b><span>{html.escape(g.get('detail', ''))}</span></li>"
                        for g in p["guidance"])
        parts.append(f"<section><h2>🏡 Good to know</h2><ul>{items}</ul></section>")
    if p.get("host_phone"):
        ph = html.escape(p["host_phone"])
        parts.append(f"<a class='btn big' href='sms:{ph}'>Text {html.escape(p.get('host') or 'your host')}</a>")
    return HTMLResponse(_page(house, "".join(parts)))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>{title}</title><style>
:root{{--bg:#0f1a14;--card:#17261d;--fg:#f3f1ea;--dim:#a9b5ac;--acc:#e9b45a}}
@media (prefers-color-scheme:light){{:root{{--bg:#f4f1e8;--card:#fff;--fg:#1e2a22;--dim:#5d6b61;--acc:#b9772a}}}}
*{{box-sizing:border-box}}body{{margin:0;font:17px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:520px;margin:0 auto;padding:28px 16px 48px}}
h1{{font:700 32px/1.1 ui-rounded,-apple-system,system-ui;margin:4px 0 6px}}h2{{font-size:17px;margin:0 0 10px}}
.eyebrow{{color:var(--acc);font-weight:600;margin:0}}.dim{{color:var(--dim)}}
section{{background:var(--card);border-radius:20px;padding:16px;margin:14px 0}}
.row{{display:flex;justify-content:space-between;gap:12px;padding:6px 0}}.row span{{color:var(--dim)}}
.code{{font:700 44px/1 ui-monospace,monospace;letter-spacing:.18em;text-align:center;padding:10px 0}}
ul{{list-style:none;padding:0;margin:0}}li{{padding:6px 0;display:flex;flex-direction:column}}li span{{color:var(--dim)}}
.btn{{display:inline-block;margin-top:8px;padding:10px 16px;border-radius:999px;background:var(--acc);color:#1b1206;font-weight:600;text-decoration:none}}
.btn.big{{display:block;text-align:center;margin-top:18px;padding:14px}}
</style></head><body><main>{body}</main><script>
document.querySelectorAll('.copy').forEach(e=>e.onclick=()=>navigator.clipboard&&navigator.clipboard.writeText(e.dataset.v).then(()=>{{e.textContent='Copied ✓';setTimeout(()=>e.textContent=e.dataset.v,1500)}}));
</script></body></html>"""


# -- Tell Dylan ----------------------------------------------------------------
class Feedback(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    screen: str | None = Field(default=None, max_length=200)
    app_build: str | None = Field(default=None, max_length=20)
    screenshot_jpeg_b64: str | None = Field(default=None, max_length=4_000_000)


@router.post("/feedback")
async def send_feedback(body: Feedback, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Anyone in the family: a note (and optional screenshot) for Dylan. Lands in his inbox."""
    if c.subject.role == "pending":
        raise error(403, "forbidden", "Not available")
    shot = None
    if body.screenshot_jpeg_b64:
        try:
            shot = base64.b64decode(body.screenshot_jpeg_b64, validate=True)
        except ValueError:
            raise error(400, "bad_params", "Screenshot isn't valid") from None
        if not shot.startswith(b"\xff\xd8"):
            raise error(400, "bad_params", "Screenshot must be a JPEG")
    fid = await h.household.add_feedback(c.subject, body.text.strip(), body.screen, body.app_build, shot)
    return {"ok": True, "id": fid}


@router.get("/feedback")
async def list_feedback(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "people")
    rows = h.db.all("SELECT * FROM feedback ORDER BY ts DESC LIMIT 100")
    names = {p["id"]: p["name"] for p in h.db.people()}
    return {"items": [{"id": r["id"], "t": iso(r["ts"]), "who": names.get(r["person_id"] or "", "Someone"),
                       "text": r["text"], "screen": r.get("screen"), "app_build": r.get("app_build"),
                       "shot": f"/v1/feedback/{r['id']}/shot.jpg" if r["has_shot"] else None,
                       "done": r.get("done_at") is not None} for r in rows]}


@router.get("/feedback/{fid}/shot.jpg")
async def feedback_shot(fid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> FileResponse:
    need(h, c, "people")
    if not fid.replace("_", "").isalnum():
        raise error(404, "not_found", "No screenshot")
    path = h.household.feedback_dir() / f"{fid}.jpg"
    if not path.exists():
        raise error(404, "not_found", "No screenshot")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/feedback/{fid}/done")
async def feedback_done(fid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    need(h, c, "people")
    h.db.update("feedback", "id", fid, {"done_at": time.time()})
    return {"ok": True}
