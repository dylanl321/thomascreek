"""The Devices page: health, one-tap commands, rollout, and device-owner setup."""

from __future__ import annotations

import base64
import html
import json
from typing import Any
from urllib.parse import quote

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..state import Hub
from ..tvbuilds import builds
from ..tvfleet import SHELL_TEMPLATES, FleetError, command, devices, history, kill_list, policy, rollout, set_kill_list, set_policy, shell, start_rollout
from .admin_web import _csrf, _form_session, _headers, _home_only, _login_page, _nav, _page, _session
from .deps import hub

pages = APIRouter(tags=["admin-web"], include_in_schema=False)
OWNER = "adb shell dpm set-device-owner me.dlewis.thomascreek.tv/.DeviceAdminReceiver"


def _go(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?m={quote(msg)}", status_code=303)


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
        return _go(f"/admin/devices/{install_id}", exc.message)
    return _go(f"/admin/devices/{install_id}", "Asked the TV.")


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
    return HTMLResponse(_device_html(h, claims, install_id, request.query_params.get("m")), headers=_headers())


def _devices_html(h: Hub, claims: dict[str, Any], msg: str | None) -> str:
    csrf = _csrf(claims)
    rows = devices(h)["devices"]
    if rows:
        body = "".join(
            f"<div class=row><div><a href='/admin/devices/{html.escape(row['id'])}'>{html.escape(row['name'])}</a>"
            f"<div class=dim>{'online' if row['online'] else 'offline'} · {html.escape(str(row['health']))} · "
            f"{html.escape(str(row['capability']))} · {html.escape(str(row.get('app_version') or ''))}</div></div></div>"
            for row in rows)
    else:
        body = "<p>No TVs have checked in yet.</p>"
    stored = builds(h.tv_app)
    options = "".join(f"<option value={row['version_code']}>{html.escape(str(row['version_name']))}</option>" for row in stored)
    checks = "".join(
        f"<label><input type=checkbox name=stage value={html.escape(row['id'])}> {html.escape(row['name'])}</label>"
        for row in rows)
    current = rollout(h)["rollout"]
    roll = ""
    if current:
        roll = f"<p class=dim>Rollout {html.escape(str(current['version_code']))} is {html.escape(str(current['state']))}.</p>"
    blocked = "\n".join(kill_list(h)["packages"])
    chosen = policy(h)
    return _page("Devices", f"""
{_nav("/admin/devices")}
<h1>Devices</h1>
{f'<p class=flash>{html.escape(msg)}</p>' if msg else ''}
<div class=card>{body}</div>
<h2>Rollout</h2>
<div class=card><form method=post action=/admin/devices/rollout class=edit>
<input type=hidden name=csrf value={csrf}>
<select name=version_code>{options}</select>
{checks}
<label><input type=checkbox name=then value=all checked> Then the rest</label>
<label><input type=checkbox name=now value=1> Install now</label>
<button>Start rollout</button></form>{roll}</div>
<h2>Block list</h2>
<div class=card><form method=post action=/admin/devices/kill-list class=edit>
<input type=hidden name=csrf value={csrf}>
<textarea name=packages rows=6>{html.escape(blocked)}</textarea>
<button>Save block list</button></form></div>
<h2>Policy</h2>
<div class=card><form method=post action=/admin/devices/policy class=edit>
<input type=hidden name=csrf value={csrf}>
<textarea name=blocked rows=3 placeholder="Blocked apps">{html.escape(chr(10).join(chosen['blocked']))}</textarea>
<label><input type=checkbox name=restrict_settings {'checked' if chosen['restrict_settings'] else ''}> Restrict settings</label>
<label><input type=checkbox name=hide_status_bar {'checked' if chosen['hide_status_bar'] else ''}> Hide the status bar</label>
<label><input type=checkbox name=disable_keyguard {'checked' if chosen['disable_keyguard'] else ''}> Keyguard off</label>
<button>Save policy</button></form></div>
<p><a href=/admin/devices/provision>Set up a new box</a></p>
""")


def _device_html(h: Hub, claims: dict[str, Any], install_id: str, msg: str | None) -> str:
    csrf = _csrf(claims)
    match = next((row for row in devices(h)["devices"] if row["id"] == install_id), None)
    if match is None:
        return _page("Devices", f"{_nav('/admin/devices')}<h1>Devices</h1><p>That TV isn't in the house.</p>")
    charts = []
    for metric, label in (("cpu", "CPU"), ("ram", "Memory"), ("storage", "Storage"), ("temperature", "Temperature"), ("ping", "Ping")):
        points = history(h, install_id, metric, 24)["points"]
        if not points:
            continue
        peak = max(abs(float(point["v"])) for point in points) or 1
        bars = "".join(f"<i style='height:{max(4, int(abs(float(point['v'])) / peak * 56))}px'></i>" for point in points[-24:])
        charts.append(f"<h3>{label}</h3><div class=bars>{bars}</div>")
    buttons = [
        ("reboot", "Reboot", {}),
        ("restart_app", "Restart the app", {}),
        ("screenshot", "Screenshot", {}),
        ("set_kiosk", "Lock to the launcher", {"on": "true"}),
        ("set_kiosk", "Leave the launcher", {"on": "false"}),
        ("tv_power", "Power", {"on": "false"}),
    ]
    forms = []
    for action, label, extra in buttons:
        hidden = "".join(f"<input type=hidden name={html.escape(key)} value={html.escape(value)}>" for key, value in extra.items())
        forms.append(
            f"<form method=post action='/admin/devices/{html.escape(install_id)}/command' class=inline>"
            f"<input type=hidden name=csrf value={csrf}><input type=hidden name=action value={action}>{hidden}"
            f"<button class=ghost>{label}</button></form>")
    forms.append(
        f"<form method=post action='/admin/devices/{html.escape(install_id)}/command' class=fform>"
        f"<input type=hidden name=csrf value={csrf}><input type=hidden name=action value=show_message>"
        f"<input name=text placeholder='A short note'><input type=hidden name=style value=toast><button class=ghost>Show a note</button></form>")
    shell_html = ""
    if match.get("capability") == "root":
        shells = "".join(
            f"<form method=post action='/admin/devices/{html.escape(install_id)}/shell' class=inline>"
            f"<input type=hidden name=csrf value={csrf}><input type=hidden name=template value={html.escape(name)}>"
            f"<button class=ghost>{html.escape(name)}</button></form>"
            for name in SHELL_TEMPLATES)
        free = ""
        if h.config.tv_fleet.shell:
            free = (f"<form method=post action='/admin/devices/{html.escape(install_id)}/shell' class=fform>"
                    f"<input type=hidden name=csrf value={csrf}><input name=command placeholder='One command'>"
                    f"<button class=ghost>Run</button></form>")
        shell_html = f"<h2>Shell</h2><div class=card><div class=factions>{shells}</div>{free}</div>"
    return _page(match["name"], f"""
{_nav("/admin/devices")}
<h1>{html.escape(match['name'])}</h1>
<p class=dim>{'online' if match['online'] else 'offline'} · {html.escape(str(match['health']))} · {html.escape(str(match['capability']))}</p>
{f'<p class=flash>{html.escape(msg)}</p>' if msg else ''}
<div class=card>{''.join(charts) or '<p class=dim>No chart yet.</p>'}</div>
<h2>Ask</h2>
<div class=card><div class=factions>{''.join(forms)}</div></div>
{shell_html}
<p><a href=/admin/devices>All TVs</a></p>
""")


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
{_nav("/admin/devices")}
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
