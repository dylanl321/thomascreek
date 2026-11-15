"""The People page of the hub web page: everyone, their phones and iPads, approving new ones and removing old ones.

A device can't be made here: it adds itself the first time the app opens on home Wi-Fi, then waits until
Dylan says whose it is (here, or from the notification in the app). Removing a device signs it out for good
and frees its VPN client; it has to enroll again to come back.
"""

from __future__ import annotations

import html
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..notify import CATEGORIES, CATEGORY, GROUPS, role_defaults
from ..policy import ROLES
from ..state import Hub
from .admin_health import ago
from .admin_web import _csrf, _form_session, _headers, _home_only, _login_page, _nav, _page, _session
from .deps import hub

pages = APIRouter(tags=["admin-web"], include_in_schema=False)

ROLE_LABELS = {"admin": "Admin", "adult": "Adult", "child": "Child", "guest": "Guest", "tablet": "Shared tablet"}
PICKABLE = tuple(r for r in ROLES if r in ROLE_LABELS)


def _go(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/people?m={quote(msg)}", status_code=303)


def _hid(csrf: str, **kv: str) -> str:
    return f"<input type=hidden name=csrf value={csrf}>" + "".join(
        f"<input type=hidden name={k} value='{html.escape(v)}'>" for k, v in kv.items())


def _device_row(d: dict[str, Any], csrf: str, people: list[dict[str, Any]], mine: str | None) -> str:
    e = html.escape
    tags = "".join(f"<span class=tag>{t}</span>" for t, on in (("VPN", bool(d.get("wg_public_key"))),
                                                               ("Push", bool(d.get("apns_token"))),
                                                               ("Test", d.get("kind") == "test"),
                                                               ("This browser's sign-in", d["id"] == mine)) if on)
    info = " · ".join(x for x in (e(d.get("model") or ""), e(d.get("app_version") or ""),
                                  f"seen {ago(d.get('last_seen_at'))}", f"added {ago(d.get('created_at'))}") if x)
    move = "".join(f"<option value='{e(p['id'])}'{' selected' if p['id'] == d.get('person_id') else ''}>{e(p['name'])}</option>"
                   for p in people)
    actions = (f"<form method=post action=/admin/people/assign class=inline>{_hid(csrf, device=d['id'])}"
               f"<select name=person aria-label='Whose device'>{move}</select> <button class=ghost2>Move</button></form> ")
    if d["id"] == mine:
        actions += "<span class=dim>(signed in here)</span>"
    else:
        actions += (f"<form method=post action=/admin/people/remove-device class=inline>{_hid(csrf, device=d['id'])}"
                    f"<button class=ghost onclick=\"return confirm('Remove {e(d['name'])}? It is signed out and has to "
                    f"join again from home Wi-Fi.')\">Remove</button></form>")
    return (f"<details class=key><summary><span>{e(d['name'])}{tags}</span><span class=dim>{ago(d.get('last_seen_at'))}</span></summary>"
            f"<div class=kbody><div class=dim>{info}</div><div class=factions>{actions}</div></div></details>")


def _notify_card(h: Hub, p: dict[str, Any], csrf: str) -> str:
    """Which kinds of notifications this person gets. Unticking or ticking one only changes them."""
    e = html.escape
    mine = h.notify.prefs(p)
    base = role_defaults(p["role"])
    changed = sum(1 for k, v in mine.items() if v != base.get(k))
    groups = []
    for g, label in GROUPS.items():
        boxes = "".join(
            f"<label style='display:flex;gap:8px;align-items:flex-start;padding:4px 0'>"
            f"<input type=checkbox name=on value={c['id']}{' checked' if mine.get(c['id']) else ''}>"
            f"<span>{e(c['title'])} <span class=dim>{e(c['detail'])}"
            f"{' · changed' if mine.get(c['id']) != base.get(c['id']) else ''}</span></span></label>"
            for c in CATEGORIES if c["group"] == g)
        groups.append(f"<div style='margin-top:6px'><b style='font-size:13px'>{e(label)}</b>{boxes}</div>")
    note = f"{sum(mine.values())} of {len(mine)} on" + (f", {changed} changed from the {e(ROLE_LABELS.get(p['role'], p['role']).lower())} defaults" if changed else "")
    return (f"<details class=key><summary><span>Notifications</span><span class=dim>{note}</span></summary>"
            f"<div class=kbody><form method=post action=/admin/people/notify>{_hid(csrf, person=p['id'])}"
            f"{''.join(groups)}<div class=factions><button>Save</button>"
            f"<button class=ghost name=reset value=1>Back to defaults</button></div></form></div></details>")


def _sorted_html(h: Hub, csrf: str) -> str:
    """What the sorter decided for free-text alerts (Home Assistant automations and the like), with a fix."""
    e = html.escape
    rows = h.notify.sorter.recent()[:25]
    if not rows:
        return ""
    opts_for = lambda cur: "".join(  # noqa: E731
        f"<option value={c['id']}{' selected' if c['id'] == cur else ''}>{e(c['title'])} ({e(c['group'])})</option>"
        for c in CATEGORIES)
    how_words = {"ai": "AI", "rules": "rules", "urgent": "urgent, sent at once by rules", "admin": "your fix"}
    lines = []
    for r in rows:
        cur = r.get("fixed") or r.get("cat")
        reset = (f"<button class=ghost name=reset value=1>Let the AI decide</button>" if r.get("fixed") else "")
        lines.append(
            f"<div class=row style='flex-wrap:wrap'><div style='flex:1;min-width:220px'><b>{e(r.get('title') or '')}</b>"
            f"<div class=dim>{ago(r.get('t'))} · {e(CATEGORY.get(r.get('cat'), {}).get('title', r.get('cat') or ''))}"
            f" · {e(how_words.get(r.get('how'), r.get('how') or ''))}"
            f"{' · ' + e(r['reason']) if r.get('reason') else ''}</div></div>"
            f"<form method=post action=/admin/people/sorted class=fform style='flex:0'>{_hid(csrf, sig=r['sig'])}"
            f"<select name=cat>{opts_for(cur)}</select><button class=ghost2>Always this</button>{reset}</form></div>")
    return (f"<h2 style='margin-top:28px'>How alerts were sorted</h2><div class=dim>Alerts written somewhere else "
            f"(Home Assistant automations) are sorted by AI so technical ones only reach admins. Urgent ones never "
            f"wait for it. Pick a category to always send that kind of alert there.</div>"
            f"<div class=card style='margin-top:10px'>{''.join(lines)}</div>")


def _page_html(h: Hub, claims: dict[str, Any], flash: str | None) -> str:
    e = html.escape
    csrf = _csrf(claims)
    me_person, my_device = claims.get("per"), claims.get("dev")
    people = h.db.people()
    devices = h.db.devices()
    by_person: dict[str, list[dict[str, Any]]] = {}
    for d in devices:
        if d["status"] == "active" and d.get("person_id"):
            by_person.setdefault(d["person_id"], []).append(d)
    pending = [d for d in devices if d["status"] == "pending"]
    removed = [d for d in devices if d["status"] == "revoked"]
    options = "".join(f"<option value='{e(p['id'])}'>{e(p['name'])}</option>" for p in people)

    wait_rows = "".join(
        f"<div class=row style='flex-wrap:wrap'><div><b>{e(d['name'])}</b> <span class=dim>{e(d.get('model') or '')} · "
        f"joined {ago(d.get('created_at'))}{' from ' + e(d['enrolled_ip']) if d.get('enrolled_ip') else ''}</span></div>"
        f"<div class=factions style='margin:0'><form method=post action=/admin/people/assign class=fform>{_hid(csrf, device=d['id'])}"
        f"<select name=person required><option value=''>Whose is it?</option>{options}</select><button>Approve</button></form>"
        f"<form method=post action=/admin/people/remove-device class=inline>{_hid(csrf, device=d['id'])}"
        f"<button class=ghost>Remove</button></form></div></div>" for d in pending)
    waiting = (f"<h2>Waiting for you <span class=pill-warn>{len(pending)}</span></h2><div class=card>{wait_rows}</div>"
               if pending else "")

    cards = []
    for p in people:
        devs = by_person.get(p["id"], [])
        role_opts = "".join(f"<option value={r}{' selected' if r == p['role'] else ''}>{ROLE_LABELS[r]}</option>" for r in PICKABLE)
        expires = (f" · access until {time.strftime('%b %-d', time.localtime(p['expires_at']))}" if p.get("expires_at") else "")
        if p["id"] == me_person:
            role = f"<span class=dim>{ROLE_LABELS.get(p['role'], p['role'])} (you)</span>"
        else:
            role = (f"<form method=post action=/admin/people/role class=inline>{_hid(csrf, person=p['id'])}"
                    f"<select name=role>{role_opts}</select> <button class=ghost2>Change</button></form>")
        rows = "".join(_device_row(d, csrf, people, my_device) for d in devs) or \
            "<div class=row><div class=dim>No devices. Open the app on their phone or iPad on home Wi-Fi.</div></div>"
        cards.append(f"<h3 style='display:flex;justify-content:space-between;align-items:center;text-transform:none;"
                     f"font-size:15px;color:var(--fg)'><span>{e(p['name'])} <span class=dim style='font-weight:400'>"
                     f"{len(devs)} device{'s' if len(devs) != 1 else ''}{e(expires)}</span></span>{role}</h3>"
                     f"<div class=card>{rows}{_notify_card(h, p, csrf)}</div>")

    gone = "".join(f"<div class=row><div>{e(d['name'])} <span class=dim>{e(d.get('model') or '')}</span></div>"
                   f"<div class=dim>last seen {ago(d.get('last_seen_at'))}</div></div>" for d in removed[-30:][::-1])
    gone_html = (f"<details style='margin-top:24px'><summary>Removed devices ({len(removed)})</summary>"
                 f"<div class=card style='margin-top:10px'>{gone}</div></details>" if removed else "")
    role_new = "".join(f"<option value={r}{' selected' if r == 'adult' else ''}>{ROLE_LABELS[r]}</option>" for r in PICKABLE)
    flash_html = f"<div class=flash>{e(flash)}</div>" if flash else ""
    return _page("People · Thomas Creek Hub", f"""
{_nav("/admin/people")}
<h1>People and devices</h1>
<div class=dim>{len(people)} people · {sum(len(v) for v in by_person.values())} devices in use</div>
{flash_html}
{waiting}
<div class=card style="margin-top:14px"><details class=key><summary><span>Add a device or a person</span><span class=dim>how</span></summary>
<div class=kbody><div class=dim><b>A phone or iPad:</b> install Thomas Creek from TestFlight and open it on home Wi-Fi. It joins by itself
and shows up under <i>Waiting for you</i> here (you also get a notification). Pick whose it is and it's ready.
<br><b>A person:</b> add them below first, then approve their device as theirs.</div>
<form method=post action=/admin/people/add class=fform style="margin-top:10px">{_hid(csrf)}
<input name=name placeholder="Name" maxlength=60 required autocomplete=off><select name=role>{role_new}</select><button>Add person</button></form>
</div></details></div>
{''.join(cards)}
{_sorted_html(h, csrf)}
{gone_html}
""")


@pages.get("/admin/people")
async def people_page(request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    return HTMLResponse(_page_html(h, claims, request.query_params.get("m")), headers=_headers())


@pages.post("/admin/people/assign")
async def assign(request: Request, h: Hub = Depends(hub)) -> Response:
    """Approve a waiting device as someone's, or move a device to someone else."""
    claims, form = await _form_session(request, h)
    d = h.db.device(str(form.get("device", "")))
    p = h.db.person(str(form.get("person", "")))
    if d is None or p is None:
        return _go("Choose whose device it is.")
    if d["status"] == "revoked":
        return _go("That device was removed; it has to join again from home Wi-Fi.")
    if d["id"] == claims.get("dev") and p["id"] != d.get("person_id"):
        return _go("That's the device this page is signed in with; move it from the app instead.")
    was_pending = d["status"] == "pending"
    h.db.update("devices", "id", d["id"], {"person_id": p["id"], "status": "active"})
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), f"device:{d['id']}"])
    h.db.log("device.assign", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=d["id"],
             params={"person_id": p["id"]}, source="web")
    h.bus.publish({"type": "snapshot", "data": {"reason": "role_changed"}})
    return _go(f"{d['name']} is now {p['name']}'s." if not was_pending else f"Approved {d['name']} for {p['name']}.")


@pages.post("/admin/people/remove-device")
async def remove_device(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    d = h.db.device(str(form.get("device", "")))
    if d is None or d["status"] == "revoked":
        return _go("That device is already gone.")
    if d["id"] == claims.get("dev"):
        return _go("That's the device this page is signed in with, so it stays.")
    who = h.db.person(claims.get("per")) or {}
    h.revoke_device(d, reason=f"by {who.get('name') or 'admin'} on the web page")
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), f"device:{d['id']}"])
    return _go(f"Removed {d['name']}.")


@pages.post("/admin/people/role")
async def change_role(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    p = h.db.person(str(form.get("person", "")))
    role = str(form.get("role", ""))
    if p is None or role not in PICKABLE:
        return _go("Pick a person and a role.")
    if p["id"] == claims.get("per"):
        return _go("You can't change your own role here.")
    if p["role"] == "admin" and role != "admin" and len(h.db.people_with_role("admin")) <= 1:
        return _go("The hub needs at least one admin.")
    if role == p["role"]:
        return _go(f"{p['name']} is already {ROLE_LABELS[role].lower()}.")
    h.db.update("people", "id", p["id"], {"role": role})
    h.db.log("people.edit", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=p["id"],
             params={"fields": ["role"]}, source="web")
    h.bus.publish({"type": "snapshot", "data": {"reason": "role_changed"}})
    return _go(f"{p['name']} is now {ROLE_LABELS[role].lower()}.")


@pages.post("/admin/people/add")
async def add_person(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    name = " ".join(str(form.get("name", "")).split())[:60]
    role = str(form.get("role", ""))
    if not name or role not in PICKABLE:
        return _go("A name and a role are needed.")
    if any(p["name"].lower() == name.lower() for p in h.db.people()):
        return _go(f"There's already someone called {name}.")
    p = h.db.add_person(name, role)
    h.db.log("people.create", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=p["id"], source="web")
    return _go(f"Added {name}. Approve their device under Waiting for you once they open the app at home.")


@pages.post("/admin/people/notify")
async def set_notify(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    p = h.db.person(str(form.get("person", "")))
    if p is None:
        return _go("Pick a person.")
    if form.get("reset"):
        h.db.kv_set(f"notify.cats.{p['id']}", {})
        msg = f"{p['name']} is back to the {ROLE_LABELS.get(p['role'], p['role']).lower()} notifications."
    else:
        on = set(form.getlist("on"))
        h.notify.set_prefs(p["id"], {c["id"]: c["id"] in on for c in CATEGORIES}, by_admin=True)
        msg = f"Saved {p['name']}'s notifications."
    h.db.log("notify.prefs", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=p["id"], source="web")
    h.bus.publish({"type": "settings", "data": {"reason": "notify"}}, person_id=p["id"])
    return _go(msg)


@pages.post("/admin/people/sorted")
async def fix_sorted(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    sig, cat = str(form.get("sig", ""))[:32], str(form.get("cat", ""))
    if not sig:
        return _go("Pick an alert.")
    if form.get("reset"):
        h.notify.sorter.correct(sig, None)
        return _go("The AI decides that kind of alert again.")
    if cat not in CATEGORY:
        return _go("Pick a category.")
    h.notify.sorter.correct(sig, cat)
    h.db.log("notify.sort_fix", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=sig,
             params={"cat": cat}, source="web")
    return _go(f"That kind of alert now always goes to {CATEGORY[cat]['title']}.")
