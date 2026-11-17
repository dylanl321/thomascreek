"""The Devices page: health, one-tap commands, rollout, and device-owner setup."""

from __future__ import annotations

import base64
import html
import json
import time
from typing import Any
from urllib.parse import quote

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..state import Hub
from .. import tvlauncher, tvstate, tvs as tvsettings
from ..tvbuilds import builds
from ..tvfleet import (SHELL_TEMPLATES, FleetError, command, history, kill_list, policy, rollout, set_kill_list, set_policy,
                       shell, start_rollout)
from .admin_web import _csrf, _form_session, _headers, _home_only, _login_page, _nav, _page, _session
from .deps import hub

pages = APIRouter(tags=["admin-web"], include_in_schema=False)
OWNER = "adb shell dpm set-device-owner me.dlewis.thomascreek.tv/.DeviceAdminReceiver"


def _go(path: str, msg: str, tab: str | None = None) -> RedirectResponse:
    return RedirectResponse(f"{path}?m={quote(msg)}" + (f"&tab={quote(tab)}" if tab else ""), status_code=303)


def _open(request: Request, h: Hub) -> dict[str, Any] | None | HTMLResponse:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    return claims


@pages.get("/admin/devices")
async def devices_page(request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _open(request, h)
    if not isinstance(claims, dict):
        return claims
    return HTMLResponse(_devices_html(h, claims, request.query_params.get("m")), headers=_headers())


@pages.get("/admin/devices/provision")
async def provision_page(request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _open(request, h)
    if not isinstance(claims, dict):
        return claims
    return HTMLResponse(_provision_html(h, claims, request, None, None), headers=_headers())


@pages.post("/admin/devices/provision")
async def provision_qr(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    ssid = str(form.get("ssid") or "").strip()
    password = str(form.get("password") or "")
    return HTMLResponse(_provision_html(h, _claims, request, ssid or None, password or None), headers=_headers())


@pages.post("/admin/devices/rollout")
async def devices_rollout(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    stage = form.getlist("stage") if hasattr(form, "getlist") else [form.get("stage")]
    try:
        start_rollout(h, {
            "version_code": form.get("version_code"),
            "stage": [str(item) for item in stage if item],
            "then": "all" if form.get("then") else None,
            "now": bool(form.get("now")),
        })
    except FleetError as exc:
        return _go("/admin/devices", exc.message)
    return _go("/admin/devices", "The first rooms will get that build.")


@pages.post("/admin/devices/kill-list")
async def devices_kill(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    names = [line.strip() for line in str(form.get("packages") or "").splitlines() if line.strip()]
    try:
        set_kill_list(h, names)
    except FleetError as exc:
        return _go("/admin/devices", exc.message)
    return _go("/admin/devices", "Saved the block list.")


@pages.post("/admin/devices/policy")
async def devices_policy(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    blocked = [line.strip() for line in str(form.get("blocked") or "").splitlines() if line.strip()]
    try:
        set_policy(h, {
            "blocked": blocked,
            "restrict_settings": bool(form.get("restrict_settings")),
            "hide_status_bar": bool(form.get("hide_status_bar")),
            "disable_keyguard": bool(form.get("disable_keyguard")),
        })
    except FleetError as exc:
        return _go("/admin/devices", exc.message)
    return _go("/admin/devices", "Saved the policy.")


@pages.post("/admin/devices/{install_id}/command")
async def devices_command(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    params: dict[str, Any] = {}
    action = str(form.get("action") or "")
    if action == "show_message":
        params = {"style": str(form.get("style") or "toast"), "text": str(form.get("text") or "")}
    elif action == "tv_volume" and str(form.get("level") or "").isdigit():
        params = {"level": int(form.get("level"))}
    elif action == "set_kiosk":
        params = {"on": form.get("on") == "true"}
    elif action == "tv_power":
        params = {"on": form.get("on") == "true"}
    try:
        command(h, install_id, action, params, claims.get("name"))
    except FleetError as exc:
        return _go(f"/admin/devices/{install_id}", exc.message, "remote")
    return _go(f"/admin/devices/{install_id}", "Asked the TV.", "remote")


@pages.post("/admin/devices/{install_id}/launcher")
async def devices_launcher(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form_session(request, h)
    picked = form.getlist("app") if hasattr(form, "getlist") else []
    tv = h.tv_app.tvs().get(install_id) or {}
    before = tvlauncher.view(tv)["apps"]
    # Keep the order the tiles already had; new picks go at the end.
    apps = [p for p in before if p in picked] + [str(p) for p in picked if p not in before]
    extra = str(form.get("other") or "").strip()
    if extra:
        apps.append(extra)
    try:
        mode = str(form.get("mode") or "")
        on = mode in ("all", "only") if mode else bool(form.get("on"))
        guard = mode == "only" if mode else bool(form.get("guard"))
        saved = tvlauncher.set_launcher(h, install_id, {
            "on": on,
            "person_id": str(form.get("person_id") or "") or None,
            "apps": apps,
            "guard": guard,
        })
    except tvlauncher.LauncherError as exc:
        return _go(f"/admin/devices/{install_id}", exc.message, "home")
    if not saved["on"]:
        return _go(f"/admin/devices/{install_id}", "The launcher is off. The TV's own home screen comes back.", "home")
    return _go(f"/admin/devices/{install_id}", "Saved. The TV switches in a few seconds.", "home")


@pages.post("/admin/devices/{install_id}/launcher/drop-missing")
async def devices_launcher_drop(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    """Take the picked apps that aren't installed on this TV off its home row."""
    _claims, _form = await _form_session(request, h)
    tv = h.tv_app.tvs().get(install_id) or {}
    hs = tvstate.home_screen(tv)
    if not hs["missing"]:
        return _go(f"/admin/devices/{install_id}", "Every picked app is on this TV.", "home")
    block = tvlauncher.view(tv)
    keep = [p for p in block["apps"] if p not in hs["missing"]]
    try:
        tvlauncher.set_launcher(h, install_id, {**block, "apps": keep, "guard": block["guard"] and bool(keep)})
    except tvlauncher.LauncherError as exc:
        return _go(f"/admin/devices/{install_id}", exc.message, "home")
    gone = len(hs["missing"])
    extra = "" if keep or not block["guard"] else " With nothing left to allow, \"only the picked apps\" is off."
    done = f"Took {gone} app{'s' if gone != 1 else ''} off the list.{extra}"
    return _go(f"/admin/devices/{install_id}", done, "home")


@pages.post("/admin/devices/{install_id}/launcher/store")
async def devices_launcher_store(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    """Open one app's Play Store page on the TV, so someone there can press Install."""
    _claims, form = await _form_session(request, h)
    result = await tvlauncher.open_store(h, install_id, str(form.get("package") or ""))
    return _go(f"/admin/devices/{install_id}", result, "home")


@pages.post("/admin/devices/{install_id}/settings")
async def devices_settings(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    """The settings card: every field in the hub's settings schema. Unticked boxes save as off."""
    _claims, form = await _form_session(request, h)
    path = f"/admin/devices/{install_id}"
    source = str(form.get("copy_from") or "")
    try:
        if source:
            tvsettings.copy(h, install_id, source, by="admin")
            return _go(path, "Copied. The TV takes the settings in a few seconds.", "settings")
        tv = h.tv_app.tvs().get(install_id) or {}
        current = tvsettings.settings_of(tv)
        changes: dict[str, Any] = {}
        for field in tvsettings.SCHEMA:
            key = field["key"]
            top, _, sub = key.partition(".")
            held = current.get(top)
            held = held.get(sub) if sub and isinstance(held, dict) else (None if sub else held)
            if field["type"] == "bool":
                value: Any = bool(form.get(key))
                if not value and held is None:
                    continue  # unticked and never set: the TV keeps its own default
            elif field["type"] == "multi":
                value = [v for v in (form.getlist(key) if hasattr(form, "getlist") else []) if v in field["choices"]]
                if not value and held is None:
                    continue
            else:
                value = str(form.get(key) or "") or None
                if value is None and held is None:
                    continue
            if sub:
                block = dict(changes.get(top) or current.get(top) or {})
                if value is None:
                    block.pop(sub, None)
                else:
                    block[sub] = value
                changes[top] = block
            else:
                changes[top] = value
        changes = {k: v for k, v in changes.items() if v != current.get(k) and not (v is None and k not in current)}
        if changes:
            tvsettings.change(h, install_id, {"settings": changes}, by="admin")
    except tvsettings.TvsError as exc:
        return _go(path, exc.message, "settings")
    return _go(path, "Saved. The TV takes it in a few seconds." if changes else "Nothing changed.", "settings")


def _settings_card(h: Hub, csrf: str, install_id: str) -> str:
    """The TV's settings as the hub keeps them: one form from the shared schema, plus copy from another TV."""
    tv = h.tv_app.tvs().get(install_id) or {}
    current = tvsettings.settings_of(tv)

    def value(key: str) -> Any:
        top, _, sub = key.partition(".")
        v = current.get(top)
        return v.get(sub) if sub and isinstance(v, dict) else (None if sub else v)

    rows = []
    for field in tvsettings.SCHEMA:
        key, label, kind = field["key"], html.escape(field["label"]), field["type"]
        v = value(key)
        name = html.escape(key)
        if kind == "bool":
            rows.append(f"<label><input type=checkbox name={name} value=1 {'checked' if v else ''}> {label}"
                        f"{'' if v is not None else ' <span class=dim>(TV default)</span>'}</label>")
        elif kind == "choice":
            opts = "<option value=''>TV default</option>" + "".join(
                f"<option {'selected' if v == c else ''}>{html.escape(c)}</option>" for c in field["choices"])
            rows.append(f"<label>{label} <select name={name}>{opts}</select></label>")
        else:
            boxes = " ".join(
                f"<label style='display:inline-flex;gap:4px'><input type=checkbox name={name} value='{html.escape(c)}' "
                f"{'checked' if isinstance(v, list) and c in v else ''}> {html.escape(c)}</label>" for c in field["choices"])
            rows.append(f"<div>{label}: {boxes}</div>")
    device = current.get("device") if isinstance(current.get("device"), dict) else {}
    kept = ", ".join(f"{html.escape(p)} {len(v)}" for p, v in device.items() if isinstance(v, dict))
    others = [(i, t.get("name") or i) for i, t in h.tv_app.tvs().items()
              if i != install_id and t.get("status") == "approved" and tvsettings.settings_of(t)]
    copy = ""
    if others:
        options = "".join(f"<option value='{html.escape(i)}'>{html.escape(str(n))}</option>" for i, n in others)
        copy = (f"<form method=post action='/admin/devices/{html.escape(install_id)}/settings' class=fform>"
                f"<input type=hidden name=csrf value={csrf}><select name=copy_from>{options}</select>"
                f"<button class=ghost>Copy every setting from that TV</button></form>")
    rev = int(tv.get("settings_rev") or 0)
    return f"""<h2>Settings</h2>
<div class=card><form method=post action='/admin/devices/{html.escape(install_id)}/settings' class=edit>
<input type=hidden name=csrf value={csrf}>
<p class=dim>Kept on the hub; the TV only caches them. A person's own choices (on their phone) sit on top while they watch.
Change {rev}{f', last by {html.escape(str(tv.get("settings_by")))}' if tv.get('settings_by') else ''}.
{f'Other app settings kept: {kept}.' if kept else ''}</p>
{''.join(rows)}
<button>Save settings</button></form>{copy}</div>
"""


@pages.post("/admin/devices/{install_id}/shell")
async def devices_shell(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    body: dict[str, Any] = {"template": str(form.get("template") or "")} if form.get("template") else {"command": str(form.get("command") or "")}
    try:
        result = shell(h, install_id, body, claims.get("name"), True)
    except FleetError as exc:
        return _go(f"/admin/devices/{install_id}", exc.message)
    if result.get("ok") is False:
        return _go(f"/admin/devices/{install_id}", result.get("message") or "Shell commands are off.")
    return _go(f"/admin/devices/{install_id}", "Asked the TV.")


@pages.get("/admin/devices/{install_id}")
async def device_page(install_id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _open(request, h)
    if not isinstance(claims, dict):
        return claims
    q = request.query_params
    return HTMLResponse(_device_html(h, claims, install_id, q.get("m"), q.get("tab")), headers=_headers())


SIG_LABEL = (("app", "App"), ("report", "Device report"), ("link", "Network debugging"))


def _chip(level: str, text: str) -> str:
    return f"<span class='chip {html.escape(level)}'>{html.escape(text)}</span>"


def _sig(key: str, label: str, sig: dict[str, str]) -> str:
    tip = html.escape(f"{label}: {sig['text']}")
    return f"<span class='sig {html.escape(sig['state'])}' title='{tip}'><i></i>{html.escape(label)}</span>"


def _ico(label: str) -> str:
    return f"<span class=ico title='{html.escape(label)}'>{html.escape((label or '?')[:1].upper())}</span>"


def _devices_html(h: Hub, claims: dict[str, Any], msg: str | None) -> str:
    csrf = _csrf(claims)
    rows = tvstate.rows(h)
    body = []
    for r in rows:
        home = r["home"]
        if r["apple"]:
            home_cell = "<span class=dim>Apple TV home</span>"
        elif home["mode"] == "tv_own":
            home_cell = "<span class=dim>TV's own home</span>"
        else:
            tone = "bad" if home["state"] == "bad" else "warn"
            cls = "" if home["state"] == "ok" else f" style='color:var(--{tone})'"
            home_cell = (f"<div>{'Only picked apps' if home['mode'] == 'only' else 'Thomas Creek home'}</div>"
                         f"<div class=s{cls}>{html.escape(home['line'])}</div>")
        short = (("app", "App"), ("report", "Report"), ("link", "Debugging"))
        sigs = "".join(_sig(k, label, r["signals"][k]) for k, label in short)
        now = html.escape(r["playing"]) if r["playing"] else "<span class=dim>Nothing playing</span>"
        version = html.escape(str(r["version"] or "?"))
        if r["behind"]:
            version += f" <span class='chip warn'>{r['behind']} behind</span>"
        body.append(
            f"<tr><td><a class=name href='/admin/devices/{html.escape(r['id'])}'>{html.escape(r['name'])}</a>"
            f"<div class=s>{html.escape(str(r['model'] or r['platform']))}</div></td>"
            f"<td>{_chip(r['state'], r['label'])}<div class=s>heard {html.escape(r['last_text'])}</div></td>"
            f"<td class=wide>{sigs}</td><td class=wide>{now}</td><td class=wide>{home_cell}</td>"
            f"<td class=wide><code>{version}</code></td></tr>")
    head = ("<tr><th>TV</th><th>State</th><th class=wide>Signals</th><th class=wide>On screen</th>"
            "<th class=wide>Home screen</th><th class=wide>Version</th></tr>")
    table = (f"<div class='card scroll' style='padding:0'><table class=tvs><thead>{head}</thead>"
             f"<tbody>{''.join(body)}</tbody></table></div>") if rows else "<div class='card pad'>No TVs have joined yet.</div>"
    stored = builds(h.tv_app)
    options = "".join(f"<option value={row['version_code']}>{html.escape(str(row['version_name']))}</option>" for row in stored)
    checks = "".join(
        f"<label><input type=checkbox name=stage value={html.escape(r['id'])}> {html.escape(r['name'])}</label>"
        for r in rows if not r["apple"])
    current = rollout(h)["rollout"]
    roll = ""
    if current:
        roll = f"<p class=dim>Rollout {html.escape(str(current['version_code']))} is {html.escape(str(current['state']))}.</p>"
    blocked = "\n".join(kill_list(h)["packages"])
    chosen = policy(h)
    return _page("TVs · Thomas Creek Hub", f"""
{_nav("/admin/devices", h)}
<div class=head><div><h1>TVs</h1></div><a class='btn light' href=/admin/devices/provision>Set up a new box</a></div>
<p class=lede>Each TV reaches the hub three ways: the <b>app</b> checking in, the app's <b>device report</b>
(Android, every 15 minutes even when closed), and the hub's own <b>network debugging</b> link that sets the home
screen and installs builds. A TV only counts as off when the app and its report are both quiet. Open a TV to see
why it's in its state.</p>
{f'<p class=flash>{html.escape(msg)}</p>' if msg else ''}
{table}
<h2 id=rollout>Roll out a TV build</h2>
<div class=card><form method=post action=/admin/devices/rollout class=edit>
<input type=hidden name=csrf value={csrf}>
<select name=version_code>{options}</select>
<div class=dim>First TVs:</div>{checks}
<label><input type=checkbox name=then value=all checked> Then the rest</label>
<label><input type=checkbox name=now value=1> Install now</label>
<div><button>Start rollout</button></div></form>{roll}</div>
<details style="margin-top:18px"><summary>Rules for every Android TV: blocked apps and lock-down</summary>
<h3>Block list</h3>
<div class=card><form method=post action=/admin/devices/kill-list class=edit>
<input type=hidden name=csrf value={csrf}>
<div class=dim>Apps removed from every TV on sight (one package name a line).</div>
<textarea name=packages rows=6>{html.escape(blocked)}</textarea>
<div><button>Save block list</button></div></form></div>
<h3>Policy</h3>
<div class=card><form method=post action=/admin/devices/policy class=edit>
<input type=hidden name=csrf value={csrf}>
<textarea name=blocked rows=3 placeholder="Blocked apps">{html.escape(chr(10).join(chosen['blocked']))}</textarea>
<label><input type=checkbox name=restrict_settings {'checked' if chosen['restrict_settings'] else ''}> Restrict settings</label>
<label><input type=checkbox name=hide_status_bar {'checked' if chosen['hide_status_bar'] else ''}> Hide the status bar</label>
<label><input type=checkbox name=disable_keyguard {'checked' if chosen['disable_keyguard'] else ''}> Keyguard off</label>
<div><button>Save policy</button></div></form></div></details>
""")


TABS = (("status", "Status"), ("home", "Home screen"), ("settings", "Settings"), ("remote", "Remote and commands"))


def _device_html(h: Hub, claims: dict[str, Any], install_id: str, msg: str | None, tab: str | None = None) -> str:
    csrf = _csrf(claims)
    r = tvstate.one(h, install_id)
    if r is None:
        return _page("TVs", f"{_nav('/admin/devices', h)}<h1>TVs</h1><p>That TV isn't in the house.</p>"
                            "<p><a href=/admin/devices>All TVs</a></p>")
    tabs = [t for t in TABS if not (r["apple"] and t[0] == "home")]
    tab = tab if tab in {t[0] for t in tabs} else "status"
    base = f"/admin/devices/{html.escape(install_id)}"
    nav = "".join(f"<a href='{base}?tab={k}'{' class=on' if k == tab else ''}>{label}</a>" for k, label in tabs)
    if tab == "home":
        body = _launcher_card(h, csrf, install_id)
    elif tab == "settings":
        body = _settings_card(h, csrf, install_id)
    elif tab == "remote":
        body = _remote_html(h, csrf, install_id, r)
    else:
        body = _status_html(h, install_id, r)
    sub = " · ".join(html.escape(str(x)) for x in (r["model"], r["platform"], r["version"]) if x)
    return _page(f"{r['name']} · TVs", f"""
{_nav("/admin/devices", h)}
<div class=dim><a href=/admin/devices>TVs</a> › {html.escape(r['name'])}</div>
<div class=head style="margin-top:6px"><div><h1>{html.escape(r['name'])}</h1><div class=dim>{sub}</div></div>
<div>{_chip(r['state'], r['label'])}</div></div>
{f'<p class=flash>{html.escape(msg)}</p>' if msg else ''}
<nav class=tabs>{nav}</nav>
{body}
""")


def _status_html(h: Hub, install_id: str, r: dict[str, Any]) -> str:
    why = ""
    if r["why"]:
        level = "bad" if r["state"] == "bad" else "warn" if r["state"] == "warn" else ""
        link = (f" <a href='/admin/devices/{html.escape(install_id)}?tab=home'>Open the home screen</a>"
                if r["label"] == "Home screen differs" else "")
        why = f"<div class='callout {level}'><b>{html.escape(r['label'])}.</b> {html.escape(r['why'])}{link}</div>"
    explain = {
        "app": "The Thomas Creek TV app checks in every 30 seconds while it's open.",
        "report": "Sent by the app every 15 minutes, even when it's closed: health, and the apps the TV can open.",
        "link": "The hub's own link to the box. It sets the home screen and installs builds without anyone at the TV.",
    }
    rows = "".join(
        f"<div class=item><span class='sig {html.escape(r['signals'][k]['state'])}'><i></i></span>"
        f"<div class=grow><div class=t>{label}</div><div class=s>{explain[k]}</div></div>"
        f"<div class=s style='max-width:45%;text-align:right'>{html.escape(r['signals'][k]['text'])}</div></div>"
        for k, label in SIG_LABEL)
    charts = []
    for metric, label in (("cpu", "CPU"), ("ram", "Memory"), ("storage", "Storage"), ("temperature", "Temperature"), ("ping", "Ping")):
        try:
            points = history(h, install_id, metric, 24)["points"]
        except FleetError:
            points = []
        if not points:
            continue
        peak = max(abs(float(point["v"])) for point in points) or 1
        bars = "".join(f"<i style='height:{max(4, int(abs(float(point['v'])) / peak * 56))}px'></i>" for point in points[-24:])
        unit = {"cpu": "%", "ram": "%", "storage": "%", "temperature": " °C", "ping": " ms"}[metric]
        charts.append(f"<div class='card pad'><div class=dim>{label}</div><b>{html.escape(str(points[-1]['v']))}{unit}</b>"
                      f"<div class=bars>{bars}</div></div>")
    who = "Whoever picks"
    home = r["home"]
    if home and home.get("person_id"):
        person = next((p for p in tvlauncher._people(h) if str(p.get("id")) == home["person_id"]), None)
        if person:
            who = str(person.get("name"))
    on_screen = html.escape(r["playing"]) if r["playing"] else "<span class=dim>Nothing playing</span>"
    late = f" · {r['behind']} behind" if r["behind"] else ""
    return f"""{why}
<h2>Why it's in this state</h2><div class='card list'>{rows}</div>
<h2>On screen</h2><div class='card pad'>{on_screen}
<div class=dim>Last heard {html.escape(r['last_text'])}</div></div>
{f"<h2>Last 24 hours</h2><div class=metrics>{''.join(charts)}</div>" if charts else ''}
<h2>About</h2><div class='card pad'><dl class=kv><dt>Signs in as</dt><dd>{html.escape(who)}</dd>
<dt>Profile</dt><dd>{'Kids' if r['profile'] == 'kids' else 'Grown-ups'}</dd>
<dt>Room</dt><dd>{html.escape(str(r['room'] or '—'))}</dd>
<dt>App version</dt><dd><code>{html.escape(str(r['version'] or '?'))}</code>{late}</dd>
<dt>Control</dt><dd>{html.escape(r['capability'])}</dd></dl></div>"""


def _remote_html(h: Hub, csrf: str, install_id: str, r: dict[str, Any]) -> str:
    buttons = [
        ("restart_app", "Restart the app", {}),
        ("screenshot", "Take a screenshot", {}),
        ("tv_power", "Turn the TV off", {"on": "false"}),
        ("reboot", "Reboot", {}),
        ("set_kiosk", "Lock to the launcher", {"on": "true"}),
        ("set_kiosk", "Leave the launcher", {"on": "false"}),
    ]
    forms = []
    for action, label, extra in buttons:
        hidden = "".join(f"<input type=hidden name={html.escape(key)} value={html.escape(value)}>" for key, value in extra.items())
        forms.append(
            f"<form method=post action='/admin/devices/{html.escape(install_id)}/command' class=inline>"
            f"<input type=hidden name=csrf value={csrf}><input type=hidden name=action value={action}>{hidden}"
            f"<button class=light>{label}</button></form>")
    note = (f"<form method=post action='/admin/devices/{html.escape(install_id)}/command' class=fform>"
            f"<input type=hidden name=csrf value={csrf}><input type=hidden name=action value=show_message>"
            f"<input name=text placeholder='Dinner in 5 minutes'><input type=hidden name=style value=toast>"
            f"<button class=light>Show it on the TV</button></form>")
    shell_html = ""
    if r.get("capability") == "root":
        shells = "".join(
            f"<form method=post action='/admin/devices/{html.escape(install_id)}/shell' class=inline>"
            f"<input type=hidden name=csrf value={csrf}><input type=hidden name=template value={html.escape(name)}>"
            f"<button class=light>{html.escape(name)}</button></form>"
            for name in SHELL_TEMPLATES)
        free = ""
        if h.config.tv_fleet.shell:
            free = (f"<form method=post action='/admin/devices/{html.escape(install_id)}/shell' class=fform>"
                    f"<input type=hidden name=csrf value={csrf}><input name=command placeholder='One command'>"
                    f"<button class=light>Run</button></form>")
        shell_html = (f"<details style='margin-top:18px'><summary>Advanced: shell commands</summary>"
                      f"<div class='card pad'><div class=factions>{shells}</div>{free}</div></details>")
    if r["apple"]:
        return ("<div class='card pad dim'>The Apple TV takes commands through the Thomas Creek app only "
                "(play, stop, notes from the phone).</div>")
    return f"""<h2>Quick</h2><div class='card pad'><div class=factions>{''.join(forms)}</div>
<div class=dim style='margin-top:8px'>The TV answers in a few seconds while the app is running.</div></div>
<h2>Show a note on screen</h2><div class='card pad'>{note}</div>{shell_html}"""


MODES = (
    ("tv_own", "The TV's own home screen", "Thomas Creek is just an app. Nothing is hidden or blocked."),
    ("all", "Thomas Creek home, every app allowed",
     "Thomas Creek opens when the TV turns on, with your picked apps as its tiles. "
     "Other apps still open from the TV's menu."),
    ("only", "Thomas Creek home, only the picked apps",
     "Good for a kids' TV. Any app that isn't picked sends the TV back home. Pick only apps installed on this TV."),
)


def _launcher_card(h: Hub, csrf: str, install_id: str) -> str:
    """The home screen: what you asked for next to what the TV reports, the three modes, who, and the app tiles."""
    tv = h.tv_app.tvs().get(install_id) or {}
    hs = tvstate.home_screen(tv)
    e = html.escape
    installed = {a["package"]: a["label"] for a in hs["installed"]}
    names = lambda pkgs: "".join(_ico(tvstate.app_name(p, installed)) for p in pkgs)  # noqa: E731
    mode_name = {k: t for k, t, _ in MODES}[hs["mode"]]
    applied = hs["applied"] or {}
    when = f" · {tvstate.ago(applied.get('at'), time.time())}" if applied.get("at") else ""
    listed = (f"Apps listed {tvstate.ago(hs['listed_at'], time.time())}" if hs["listed_at"]
              else "The TV hasn't listed its apps yet")
    asked = names(hs["asked"]) or "<span class=dim>Every app on the TV</span>"
    if hs["mode"] == "tv_own":
        asked, shows = "", "<span class=dim>Its own home screen</span>"
    else:
        shows = names(hs["shows"]) or "<span class=dim>No app tiles</span>"
    said = f"<div class=dim>{e(str(applied.get('note')))}{e(when)}</div>" if applied.get("note") else ""
    level = hs["state"]
    verdict = ("Matches" if level == "ok" else "Different")
    why = hs["why"] or "The TV can show what you asked for."
    versus = f"""<div class=versus>
<div><span class=dim>You asked for</span><b>{e(mode_name)}</b><div class=icos>{asked}</div></div>
<div><span class=dim>The TV reports · {e(listed)}</span><b>{e(hs['line'])}</b><div class=icos>{shows}</div></div>
<div class='why {e(level)}'>{_chip(level, verdict)}<div>{e(why)}{said}</div></div></div>"""
    fix = ""
    if hs["missing"]:
        rows = "".join(
            f"<div class=item><div class=grow><b>{e(tvstate.app_name(p, installed))}</b> <span class=dim>{e(p)}</span></div>"
            f"<form method=post action='/admin/devices/{e(install_id)}/launcher/store' class=inline>"
            f"<input type=hidden name=csrf value={csrf}><input type=hidden name=package value='{e(p)}'>"
            f"<button class='light small'>Open in the Play Store on the TV</button></form></div>"
            for p in hs["missing"])
        them = "it" if len(hs["missing"]) == 1 else "them"
        fix = f"""<div class='card' style='margin-top:12px'><div class=pad><b>Not installed on this TV</b>
<div class=dim>Install them on the TV, then open Thomas Creek TV once so it lists them,
or take them off the list.</div></div>
<div class='list' style='padding:0 16px'>{rows}</div>
<div class=pad><form method=post action='/admin/devices/{e(install_id)}/launcher/drop-missing' class=inline>
<input type=hidden name=csrf value={csrf}><button class=light>Take {them} off the list</button></form></div></div>"""
    people = tvlauncher._people(h)
    choices = "<option value=''>Whoever picks on the TV</option>" + "".join(
        f"<option value='{e(p['id'])}' {'selected' if p['id'] == hs['person_id'] else ''}>"
        f"{e(p['name'])}{' (child: kids rows only)' if p['role'] == 'child' else ''}</option>" for p in people)
    modes = "".join(
        f"<label class=mode><input type=radio name=mode value={k} {'checked' if k == hs['mode'] else ''}>"
        f"<span><b>{e(t)}</b><span>{e(d)}</span></span></label>" for k, t, d in MODES)
    order = hs["asked"] + [p for p in installed if p not in hs["asked"] and p != h.tv_app.cfg.package]
    tiles = "".join(
        f"<label class='apptile{'' if p in installed or not hs['known'] else ' missing'}'>"
        f"<input type=checkbox name=app value='{e(p)}' "
        f"{'checked' if p in hs['asked'] else ''}>{_ico(tvstate.app_name(p, installed))}<span style='min-width:0'>"
        f"<span class=nm>{e(tvstate.app_name(p, installed))}</span><span class=st>"
        f"{'Installed' if p in installed else 'Not on this TV' if hs['known'] else 'Picked'}</span></span></label>"
        for p in order)
    if not tiles:
        tiles = "<p class=dim>The TV hasn't listed its apps yet. Open Thomas Creek TV on it once.</p>"
    on_row = hs["shows"] if hs["asked"] else list(installed)[:6]
    preview_apps = ["Thomas Creek"] + [tvstate.app_name(p, installed) for p in on_row]
    if hs["mode"] == "tv_own":
        inner = "<div style='color:#9fb0aa'>The TV's own launcher. Thomas Creek is one of its apps.</div>"
    else:
        inner = "<div class=tiles2>" + "".join(f"<div class=pt>{e(n)}</div>" for n in preview_apps) + "</div>"
        if hs["mode"] == "only":
            inner += "<div style='font-size:11px;color:#9fb0aa'>Other apps are blocked</div>"
    preview = ("<div class=preview><div class=row1><span>Thomas Creek</span><span>Home row as the TV shows it now</span></div>"
               + inner + "</div>")
    return f"""<h2>Right now</h2>{versus}{fix}
<h2>Preview</h2>{preview}
<form method=post action='/admin/devices/{e(install_id)}/launcher' class=edit>
<input type=hidden name=csrf value={csrf}>
<h2>1 · What the TV opens to</h2><div class=modes>{modes}</div>
<h2>2 · Who's watching</h2><select name=person_id>{choices}</select>
<h2>3 · Apps on the home row</h2><div class=dim>Ticked apps become the tiles, in this order. None ticked shows every app
(not allowed with "only the picked apps").</div>
<div class=apps>{tiles}</div>
<input name=other placeholder="Another app by package name, e.g. com.netflix.ninja">
<div><button>Send to TV</button></div></form>"""


def _provision_html(h: Hub, claims: dict[str, Any], request: Request, ssid: str | None, password: str | None) -> str:
    rows = builds(h.tv_app)
    origin = str(request.base_url).rstrip("/")
    payload = ""
    qr = ""
    if rows:
        digest = base64.urlsafe_b64encode(bytes.fromhex(rows[0]["sha256"])).decode()
        body = {
            "android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME":
                "me.dlewis.thomascreek.tv/me.dlewis.thomascreek.tv.DeviceAdminReceiver",
            "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION": f"{origin}/tv",
            "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM": digest,
            "android.app.extra.PROVISIONING_SKIP_ENCRYPTION": True,
            "android.app.extra.PROVISIONING_LEAVE_ALL_SYSTEM_APPS_ENABLED": True,
        }
        if ssid and password is not None:
            body["android.app.extra.PROVISIONING_WIFI_SSID"] = ssid
            body["android.app.extra.PROVISIONING_WIFI_PASSWORD"] = password
            body["android.app.extra.PROVISIONING_WIFI_SECURITY_TYPE"] = "WPA"
        encoded = json.dumps(body, separators=(",", ":"))
        qr = f"<div class=qr>{segno.make(encoded).svg_inline(scale=4)}</div>"
        if not password:
            payload = f"<pre><code>{html.escape(json.dumps(body, indent=2))}</code></pre>"
    if password:
        payload += "<p class=dim>The Wi-Fi is in the QR only.</p>"
    csrf = _csrf(claims)
    script = "#!/bin/sh\nadb install -r ThomasCreekTV.apk\n" + OWNER
    return _page("Set up a box", f"""
{_nav("/admin/devices", h)}
<h1>Set up a box</h1>
<ol>
<li>Factory reset the box and skip the Google account.</li>
<li>Install the app from the same /tv link as Downloads.</li>
<li>Run this one command.</li>
</ol>
<pre><code>{html.escape(OWNER)}</code></pre>
<pre><code>{html.escape(script)}</code></pre>
<p>Certified Google TV often has no QR screen and rejects that command once the setup wizard has finished. If debugging cannot be turned on, the box stays a normal app.</p>
{qr}{payload}
<form method=post action=/admin/devices/provision class=edit>
<input type=hidden name=csrf value={csrf}>
<input name=ssid placeholder="Wi-Fi name">
<input type=password name=password placeholder="Wi-Fi password" autocomplete=off>
<button class=ghost>Put Wi-Fi in the QR</button>
</form>
<p class=dim>The password is not saved.</p>
<p><a href=/admin/devices>All TVs</a></p>
""")
