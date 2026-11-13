"""A small web page on the hub for Dylan: status, and replacing keys and tokens.

Open https://<hub>:8443/admin on the home network. Signing in goes through the
app: the page shows a short code, Dylan types it in the app (Systems, Hub web
page) and confirms with Face ID. The app can also open the page already signed
in. The session is a cookie for /admin only, 8 hours, home network only.

Values are write-only: the page lists which keys exist, never what they are.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .. import __version__, bambu_cloud, testkeys
from ..bambu_cloud import token_expiry
from ..crypto import CryptoError
from ..onepassword import NAME_RE
from ..state import Hub
from .deps import Caller, caller, caller_ip, error, hub, need, require_confirm

pages = APIRouter(tags=["admin-web"], include_in_schema=False)
api = APIRouter(prefix="/v1/admin", tags=["admin-web"])

COOKIE = "tc_admin"
AUD = "admin-web"
LINK_AUD = "admin-web-link"
SESSION_S = 8 * 3600
CODE_TTL_S = 300
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Pending sign-in codes: code -> {"id", "exp", "approved": claims | None}
_pending: dict[str, dict[str, Any]] = {}

# What each known secret is for, shown on the page.
DESCRIPTIONS = {
    "home_assistant.token": "Home Assistant long-lived access token",
    "plex.token": "Plex token",
    "jellyseerr.api_key": "Jellyseerr API key",
    "sonarr.api_key": "Sonarr API key",
    "radarr.api_key": "Radarr API key",
    "truenas.api_key": "TrueNAS API key",
    "mealie.token": "Mealie API token",
    "homebox.password": "Homebox password",
    "family_board.admin_token": "Family Command Center admin token",
    "show_player.app_token": "Christmas show player app token",
    "firewalla.msp_token": "Firewalla MSP personal access token",
    "frigate.password": "Frigate password (only for the login port)",
    "apns.auth_key_p8": "Apple push key (.p8 file)",
    "apns.key_id": "Apple push key ID (10 characters)",
    "onepassword.service_account_token": "1Password service account token",
    "guest_wifi.password": "Guest Wi-Fi password",
    "trek.api_key": "TREK API key",
    "trek.mcp_client_id": "TREK machine client ID (for changing trips)",
    "trek.mcp_client_secret": "TREK machine client secret (for changing trips)",
    "anthropic.api_key": "Claude API key for the Ask Thomas Creek assistant",
    "appstore.auth_key_p8": "App Store Connect API key (.p8 file), for spotting new app builds",
    "appstore.key_id": "App Store Connect API key ID",
    "appstore.issuer_id": "App Store Connect issuer ID",
    "printer.h2s.access_code": "Bambu H2S access code (LAN mode)",
    "printer.h2s.serial": "Bambu H2S serial number",
    "printer.h2s.host": "Bambu H2S IP address",
}
APPSTORE_KEYS = ("appstore.auth_key_p8", "appstore.key_id", "appstore.issuer_id")
NEVER_EDIT = {"wireguard.server_private_key"}


def _expected(h: Hub) -> list[str]:
    cfg = h.config
    names = []
    for svc in ("home_assistant", "plex", "jellyseerr", "sonarr", "radarr", "truenas", "mealie", "homebox",
                "family_board", "show_player", "frigate", "trek"):
        c = getattr(cfg, svc)
        if not getattr(c, "enabled", False):
            continue
        for attr in ("token_secret", "password_secret"):
            n = getattr(c, attr, None)
            if n:
                names.append(n)
    if cfg.firewalla.enabled:
        names.append(cfg.firewalla.token_secret)
    names += [cfg.apns.key_secret, "apns.key_id"]
    if cfg.onepassword.enabled:
        names.append(cfg.onepassword.token_secret)
    return list(dict.fromkeys(names))


# -- session ----------------------------------------------------------------
def _session(request: Request, h: Hub) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        claims = h.tokens.verify(token, audience=AUD)
    except CryptoError:
        return None
    dev = h.db.device(claims.get("dev", ""))
    person = h.db.person(claims.get("per")) if claims.get("per") else None
    if not dev or dev["status"] != "active" or not person or person["role"] != "admin":
        return None
    if not h.is_home(caller_ip(request, h)):
        return None
    claims["_token"] = token
    return claims


def _csrf(claims: dict[str, Any]) -> str:
    return hashlib.sha256(f"csrf:{claims['_token']}".encode()).hexdigest()[:32]


def _set_session(resp: Response, h: Hub, dev: str, per: str) -> None:
    token, _ = h.tokens.issue_scoped({"aud": AUD, "dev": dev, "per": per}, SESSION_S)
    resp.set_cookie(COOKIE, token, max_age=SESSION_S, httponly=True, secure=True, samesite="strict", path="/admin")


def _home_only(request: Request, h: Hub) -> None:
    if not h.is_home(caller_ip(request, h)):
        raise error(403, "home_only", "The hub's web page only opens on the home network")


# -- app side: approve a code, or get a signed-in link ------------------------
class CodeBody(BaseModel):
    code: str = Field(max_length=20)


@api.post("/web-approve")
async def web_approve(body: CodeBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Dylan typed the code the web page shows; Face ID confirms."""
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    code = re.sub(r"[^A-Z0-9]", "", body.code.upper())
    p = _pending.get(code)
    if not p or p["exp"] < time.time() or p["approved"]:
        raise error(404, "not_found", "That code is not valid any more. Reload the web page for a new one.")
    p["approved"] = {"dev": c.device["id"], "per": c.subject.person_id}
    h.db.log("admin.web_signin", "ok", person_id=c.subject.person_id, device_id=c.device["id"])
    return {"ok": True}


@api.post("/web-link")
async def web_link(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A one-minute link that opens the web page already signed in (for Safari on this phone)."""
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
    token, exp = h.tokens.issue_scoped({"aud": LINK_AUD, "dev": c.device["id"], "per": c.subject.person_id,
                                        "nonce": secrets.token_hex(8)}, 60)
    h.db.log("admin.web_signin", "ok", person_id=c.subject.person_id, device_id=c.device["id"], detail="link")
    return {"url": f"/admin/login/link?t={token}", "expires_in": 60}


_used_links: dict[str, float] = {}


@pages.get("/admin/login/link")
async def login_link(t: str, request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    try:
        claims = h.tokens.verify(t, audience=LINK_AUD)
    except CryptoError:
        return HTMLResponse(_page("Link expired", "<p>Open the web page again from the app.</p>"), status_code=401)
    now = time.time()
    for k in [k for k, v in _used_links.items() if v < now]:
        _used_links.pop(k, None)
    if claims["nonce"] in _used_links:
        return HTMLResponse(_page("Link already used", "<p>Open the web page again from the app.</p>"), status_code=401)
    _used_links[claims["nonce"]] = now + 120
    resp = RedirectResponse("/admin", status_code=303)
    _set_session(resp, h, claims["dev"], claims["per"])
    return resp


@pages.get("/admin/login/poll")
async def login_poll(id: str, request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    for code, p in list(_pending.items()):
        if p["exp"] < time.time():
            _pending.pop(code, None)
            continue
        if secrets.compare_digest(p["id"], id) and p["approved"]:
            _pending.pop(code, None)
            resp = JSONResponse({"ok": True})
            _set_session(resp, h, p["approved"]["dev"], p["approved"]["per"])
            return resp
    return JSONResponse({"ok": False})


# -- pages --------------------------------------------------------------------
CSS = """
:root{color-scheme:light dark;--bg:#f6f7f5;--card:#fff;--fg:#17201b;--dim:#5f6b64;--line:#dfe4e0;--ok:#2f8f5b;--bad:#c2410c;--acc:#2d6a5a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1412;--card:#18201c;--fg:#e8eee9;--dim:#9aa69f;--line:#2a332e;--ok:#5cc28a;--bad:#f08a5d;--acc:#7cc4ad}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.dim{color:var(--dim)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px 16px}
.row{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:var(--dim)}
.ok .dot{background:var(--ok)}.bad .dot{background:var(--bad)}.bad .msg{color:var(--bad)}
code{font:13px ui-monospace,Menlo,monospace}details summary{cursor:pointer;color:var(--acc)}
form.edit{display:grid;gap:8px;padding:8px 0 4px}textarea,input[type=text]{width:100%;font:13px ui-monospace,Menlo,monospace;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
button{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--bad);border-color:var(--line)}.code{font:600 34px ui-monospace,Menlo,monospace;letter-spacing:4px}
.flash{padding:10px 14px;border-radius:10px;background:var(--card);border:1px solid var(--line);margin:12px 0}
h3{font-size:13px;margin:22px 0 6px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;font-weight:600}
details.key{border-top:1px solid var(--line)}details.key:first-child{border-top:0}
details.key>summary{list-style:none;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:11px 0;color:var(--fg)}
details.key>summary::-webkit-details-marker{display:none}details.key>summary>span:first-child::before{content:"›";display:inline-block;width:14px;color:var(--dim);transition:transform .15s}
details.key[open]>summary>span:first-child::before{transform:rotate(90deg)}.kbody{padding:0 0 12px 14px}.field{padding:14px 0;border-top:1px solid var(--line)}.field:first-child{border-top:0}
.fhead{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:2px}
.factions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}.fform{display:flex;flex-wrap:wrap;gap:8px;align-items:center;flex:1}
.fform input[type=password]{flex:1;min-width:200px;font:13px ui-monospace,Menlo,monospace;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
form.inline{display:inline}.pill-ok,.pill-miss{font-size:12px;padding:2px 8px;border-radius:999px;white-space:nowrap}
.pill-ok{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}.pill-miss{background:color-mix(in srgb,var(--bad) 15%,transparent);color:var(--bad)}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def _headers() -> dict[str, str]:
    return {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
            "Cache-Control": "no-store", "X-Frame-Options": "DENY"}


def _login_page() -> str:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    pid = secrets.token_urlsafe(18)
    _pending[code] = {"id": pid, "exp": time.time() + CODE_TTL_S, "approved": None}
    shown = f"{code[:4]}-{code[4:]}"
    return _page("Thomas Creek Hub", f"""
<h1>Thomas Creek Hub</h1><p class=dim>Sign in with the app.</p>
<div class=card style="padding:20px 16px"><p>In the Thomas Creek app, open <b>Systems → Hub web page</b>, type this code and confirm with Face ID.</p>
<p class=code>{shown}</p><p class=dim id=s>Waiting for the app… the code works for 5 minutes.</p></div>
<script>
const id={pid!r};let n=0;
async function poll(){{n++;try{{const r=await fetch('/admin/login/poll?id='+encodeURIComponent(id),{{credentials:'same-origin'}});
const j=await r.json();if(j.ok){{location.replace('/admin');return}}}}catch(e){{}}
if(n<150)setTimeout(poll,2000);else document.getElementById('s').textContent='Code expired. Reload the page for a new one.'}}
poll();
</script>""")


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago"


# Labelled sections for the page: (title, note, [(secret, label, kind, where to find it)]).
# kind: "text" (paste), "file" (choose a file). Optional sections show only when their service is on,
# unless marked always.
SECTIONS: list[dict[str, Any]] = [
    {"title": "Push notifications", "always": True, "fields": [
        ("apns.auth_key_p8", "Apple push key", "file",
         "Choose the AuthKey_XXXXXXXXXX.p8 file from developer.apple.com (Certificates, Identifiers & Profiles, Keys). "
         "The Key ID is read from the file name."),
        ("apns.key_id", "Key ID", "text", "Only if the file was renamed: the 10-character Key ID shown on the key's page."),
    ]},
    {"title": "App updates (TestFlight)", "always": True, "optional": True, "fields": [
        ("appstore.auth_key_p8", "App Store Connect API key", "file",
         "The same AuthKey_XXXXXXXXXX.p8 the Mac uploads builds with (Finder, Go, Go to Folder: ~/.appstoreconnect/private_keys). "
         "Lets the hub see each new TestFlight build by itself. The Key ID is read from the file name."),
        ("appstore.issuer_id", "Issuer ID", "text",
         "App Store Connect, Users and Access, Integrations, App Store Connect API: the Issuer ID above the keys "
         "(also in ~/.appstoreconnect/config as ASC_ISSUER_ID)."),
        ("appstore.key_id", "Key ID", "text", "Only if the file was renamed: the 10-character Key ID next to the key."),
    ]},
    {"title": "AI assistant (Claude API key)", "always": True, "fields": [
        ("anthropic.api_key", "Claude API key", "text",
         "Powers Ask Thomas Creek (chat and voice). console.anthropic.com: Settings, API keys, Create key. "
         "It starts with sk-ant-. Setting a monthly spend limit there (Settings, Limits) is a good idea."),
    ]},
    {"title": "3D printer (Bambu H2S)", "service": "printers", "fields": [
        ("printer.h2s.access_code", "Access code", "text",
         "On the printer: Settings, Network (the gear, then the Wi-Fi page): the 8-character Access Code. "
         "Works with LAN Only off (watch only). Turn on LAN Only and Developer Mode there for full control; "
         "the hub notices by itself. Not needed if you sign in to the Bambu account below."),
        ("printer.h2s.serial", "Serial number", "text",
         "On the printer: Settings, Device (or the sticker on the back). It starts with 09."),
        ("printer.h2s.host", "IP address", "text",
         "On the printer: Settings, Network, IP. Best reserved in the Firewalla so it doesn't change."),
    ]},
    {"title": "Home Assistant", "service": "home_assistant", "fields": [
        ("home_assistant.token", "Long-lived access token", "text",
         "Home Assistant: your profile, Security tab, Long-lived access tokens, Create token."),
    ]},
    {"title": "Cameras", "service": "frigate", "fields": [
        ("frigate.password", "Frigate password", "text", "Not needed: the hub uses Frigate's open port 5000."),
    ], "optional": True},
    {"title": "Media", "service": "plex", "fields": [
        ("plex.token", "Plex token", "text", "In Plex Web, open any item, ... menu, Get Info, View XML: the X-Plex-Token in the address."),
        ("jellyseerr.api_key", "Jellyseerr API key", "text", "Jellyseerr: Settings, General, API Key."),
        ("sonarr.api_key", "Sonarr API key", "text", "Sonarr: Settings, General, Security, API Key."),
        ("radarr.api_key", "Radarr API key", "text", "Radarr: Settings, General, Security, API Key."),
    ]},
    {"title": "NAS (storage and apps)", "service": "truenas", "fields": [
        ("truenas.api_key", "TrueNAS API key", "text", "TrueNAS: user menu (top right), API Keys, Add."),
    ]},
    {"title": "Meals", "service": "mealie", "fields": [
        ("mealie.token", "Mealie API token", "text", "Mealie: your profile, API Tokens, Generate."),
    ]},
    {"title": "Kids' internet (Firewalla)", "service": "firewalla", "fields": [
        ("firewalla.msp_token", "Firewalla MSP token", "text", "Firewalla MSP: Account Settings, Create New Token."),
    ]},
    {"title": "Christmas show", "service": "show_player", "fields": [
        ("show_player.app_token", "Show player app token", "text",
         "The app token of the christmas-player service on the xLights computer."),
    ]},
    {"title": "Trips (TREK)", "service": "trek", "fields": [
        ("trek.api_key", "TREK API key", "text", "TREK: Settings, Integrations, API keys, create one with trips access. It starts with trek_ (an MCP token will not work)."),
        ("trek.mcp_client_id", "TREK machine client ID", "text",
         "Only for changing trips from the app or the assistant. TREK: Settings, Integrations, MCP Configuration, "
         "OAuth 2.1 Clients, New Client, tick Machine client, allow the trips, places, days, reservations and "
         "packing scopes, Register. Paste the client ID here."),
        ("trek.mcp_client_secret", "TREK machine client secret", "text",
         "The client secret TREK shows once after Register."),
    ]},
    {"title": "Family board", "service": "family_board", "fields": [
        ("family_board.admin_token", "Family Command Center admin token", "text",
         "The board's 6-digit parent PIN (its ADMIN_TOKEN). Needed to add or change chores and calendar events "
         "from the app or the assistant; marking chores done works without it on the home network."),
    ]},
    {"title": "Homebox", "always": True, "optional": True, "fields": [
        ("homebox.password", "Homebox password", "text", "Optional: the password of the Homebox account the hub signs in as."),
    ]},
    {"title": "1Password", "always": True, "optional": True, "fields": [
        ("onepassword.service_account_token", "Service account token", "text",
         "Optional: 1Password, Developer, Service Accounts, with read access to the \"Thomas Creek Hub\" vault. "
         "The hub then copies keys from there by itself."),
    ]},
]


def _section_on(h: Hub, sec: dict[str, Any]) -> bool:
    if sec.get("always"):
        return True
    svc = getattr(h.config, sec.get("service", ""), None)
    return bool(svc is not None and getattr(svc, "enabled", False))


def _field_html(h: Hub, csrf: str, name: str, label: str, kind: str, note: str) -> str:
    e = html.escape
    have = h.vault.has(name)
    badge = "<span class=pill-ok>Saved</span>" if have else "<span class=pill-miss>Missing</span>"
    if kind == "file":
        inp = "<input type=file name=file required>"
    else:
        inp = "<input type=password name=value placeholder='Paste here' autocomplete=off required>"
    remove = (f"<form method=post action=/admin/secrets/delete class=inline><input type=hidden name=csrf value={csrf}>"
              f"<input type=hidden name=name value='{e(name)}'><button class=ghost "
              f"onclick=\"return confirm('Remove the saved {e(label)}?')\">Remove</button></form>" if have else "")
    return f"""<details class=key><summary><span>{e(label)}</span>{badge}</summary><div class=kbody><div class=dim>{e(note)}</div>
<div class=factions><form method=post action=/admin/secrets enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}><input type=hidden name=name value='{e(name)}'>{inp}<button>{'Replace' if have else 'Save'}</button></form>{remove}</div></div></details>"""


BAMBU_KEYS = {bambu_cloud.TOKEN, bambu_cloud.REFRESH, bambu_cloud.UID}


def _bambu_html(h: Hub, csrf: str) -> str:
    """Sign in to the Bambu account: watch the printer without LAN Only mode."""
    e = html.escape
    c = h.bambu_cloud
    hid = f"<input type=hidden name=csrf value={csrf}>"
    note = ("Optional. Lets the hub watch the printer through Bambu's cloud when it can't reach it on the home "
            "network, and fills in the serial number and access code. The password is used once to sign in and "
            "isn't kept. Bambu then emails a 6-digit code (or asks for your authenticator code).")
    if c.signed_in:
        names = ", ".join(f"{d.get('name') or d.get('dev_id')}" for d in c.devices) or "no printers listed yet"
        exp = token_expiry(c.token)
        until = time.strftime("%b %-d", time.localtime(exp)) if exp else "?"
        body = (f"<div class=dim>Signed in. Printers on the account: {e(names)}. Renews by itself (good until {e(until)})."
                f"{' ' + e(c.last_error) if c.last_error else ''}</div><div class=factions>"
                f"<form method=post action=/admin/bambu/sign-out class=inline>{hid}<button class=ghost "
                f"onclick=\"return confirm('Sign the hub out of the Bambu account?')\">Sign out</button></form></div>")
        badge = "<span class=pill-ok>Signed in</span>"
    elif c.waiting_for:
        what = ("the 6-digit code Bambu just emailed" if c.waiting_for == "code"
                else "the 6-digit code from your authenticator app")
        body = (f"<div class=dim>Enter {what}.</div><div class=factions><form method=post action=/admin/bambu/code "
                f"class=fform>{hid}<input name=code inputmode=numeric autocomplete=one-time-code pattern='[0-9 ]{{6,7}}' "
                f"placeholder='123456' required><button>Sign in</button></form></div>")
        badge = "<span class=pill-miss>Waiting for code</span>"
    else:
        body = (f"<div class=dim>{e(note)}</div><div class=factions><form method=post action=/admin/bambu/sign-in "
                f"class=fform>{hid}<input type=email name=email placeholder='Bambu account email' autocomplete=off required>"
                f"<input type=password name=password placeholder='Password' autocomplete=off required>"
                f"<button>Next</button></form></div>")
        badge = "<span class=pill-miss>Not signed in</span>"
    return f"<details class=key><summary><span>Bambu account (optional)</span>{badge}</summary><div class=kbody>{body}</div></details>"


def _sections_html(h: Hub, csrf: str) -> str:
    e = html.escape
    out = []
    for sec in SECTIONS:
        if not _section_on(h, sec):
            continue
        fields = "".join(_field_html(h, csrf, *f) for f in sec["fields"])
        if sec.get("service") == "printers":
            fields += _bambu_html(h, csrf)
        opt = " <span class=dim>(optional)</span>" if sec.get("optional") else ""
        out.append(f"<h3>{e(sec['title'])}{opt}</h3><div class=card>{fields}</div>")
    # VPN: the Firewalla client files.
    if h.config.vpn.provider == "pool":
        rows = []
        for c in h.vpn.pool_status():
            who = e(c["device_name"]) if c["device_name"] else "free"
            label = c["name"][len(h.config.vpn.pool_prefix):]
            rows.append(f"<div class=row><div><b>{e(label.capitalize() if not label.isdigit() else 'Client ' + label)}</b> "
                        f"<span class=dim>{e(c['address'])}</span></div><div class=dim>{who} "
                        f"<form method=post action=/admin/secrets/delete class=inline><input type=hidden name=csrf value={csrf}>"
                        f"<input type=hidden name=name value='{e(c['name'])}'><button class=ghost "
                        f"onclick=\"return confirm('Remove this VPN client?')\">Remove</button></form></div></div>")
        out.append(f"""<h3>Away-from-home VPN</h3><div class=card>
<details class=key><summary><span>Add Firewalla VPN clients</span><span class=pill-{'ok' if rows else 'miss'}>{len(rows)} saved</span></summary>
<div class=kbody><div class=dim>In the Firewalla app, VPN Server, WireGuard: share each client's .conf file, then choose them all here at once.
A file named after someone in the hub (LAUREN.conf) is kept for that person; the others go to any phone.</div>
<div class=factions><form method=post action=/admin/vpn enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}><input type=file name=files multiple accept=".conf,text/plain" required><button>Add</button></form></div></div></details>
{''.join(rows)}</div>""")
    # House model
    from ..config import data_dir

    have_model = (data_dir() / "assets" / "house-model-web.json").is_file()
    out.append(f"""<h3>3D house</h3><div class=card><details class=key><summary><span>House model</span>
<span class=pill-{'ok' if have_model else 'miss'}>{'Saved' if have_model else 'Missing'}</span></summary>
<div class=kbody><div class=dim>The house-model-web.json file for the app's 3D view.</div>
<div class=factions><form method=post action=/admin/house-model enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}><input type=file name=file accept=".json,application/json" required>
<button>{'Replace' if have_model else 'Save'}</button></form></div></div></details></div>""")
    # Anything else in the vault the sections don't cover.
    known = {f[0] for sec in SECTIONS for f in sec["fields"]} | NEVER_EDIT | BAMBU_KEYS
    extra = [n for n in sorted(h.vault.names()) if n not in known and not n.startswith(h.config.vpn.pool_prefix)]
    rows = "".join(f"<div class=row><code>{e(n)}</code><form method=post action=/admin/secrets/delete class=inline>"
                   f"<input type=hidden name=csrf value={csrf}><input type=hidden name=name value='{e(n)}'>"
                   f"<button class=ghost onclick=\"return confirm('Remove {e(n)}?')\">Remove</button></form></div>" for n in extra)
    out.append(f"""<details style="margin-top:28px"><summary>Advanced: other saved keys</summary><div class=card style="margin-top:10px">{rows}
<form class=edit method=post action=/admin/secrets enctype=multipart/form-data>
<input type=hidden name=csrf value={csrf}>
<input type=text name=name placeholder="name, e.g. guest_wifi.password" pattern="[a-z0-9_]+(\\.[a-z0-9_]+)+" required>
<textarea name=value rows=2 placeholder="Value" autocomplete=off spellcheck=false></textarea>
<div class=dim>or choose a file: <input type=file name=file></div><div><button>Save</button></div></form></div></details>""")
    return "".join(out)


def _build_of(v: str | None) -> int | None:
    m = re.search(r"\((\d+)\)\s*$", v or "") or re.fullmatch(r"\s*(\d+)\s*", v or "")
    return int(m.group(1)) if m else None


def _app_update_html(h: Hub, csrf: str) -> str:
    """Latest app build, who is behind, and the "required" floor. Collapsed like the key rows."""
    e = html.escape
    au = h.app_update
    info = au.info()
    s = au.settings()
    latest = info["latest_build"]
    if latest:
        where = "from TestFlight" if info["source"] == "testflight" else "set by hand"
        head = (f"{e(info['latest_version'])} ({latest})" if info["latest_version"] else f"build {latest}") + f" {where}"
    else:
        head = "not known yet"
    if au.configured:
        check = (f"App Store Connect: {e(au.last_error)}" if au.last_error
                 else f"Checks App Store Connect every {h.config.app_update.check_minutes} min, last {_ago(au.last_check)}.")
    else:
        check = "Add the App Store Connect key under Keys and files so the hub spots new builds by itself, or type the build below."
    rows, behind = [], 0
    for d in h.db.all("SELECT d.name, d.app_version, d.last_seen_at, d.kind, p.name AS person FROM devices d "
                      "LEFT JOIN people p ON p.id = d.person_id WHERE d.status='active' ORDER BY d.last_seen_at DESC"):
        b = _build_of(d.get("app_version"))
        old = bool(latest and b is not None and b < latest)
        behind += old
        label = e(d["app_version"]) if b is not None else "<span class=dim>not reported yet</span>"
        tag = " · test" if d.get("kind") == "test" else ""
        rows.append(f"<div class=row><div>{e(d['name'])} <span class=dim>{e(d.get('person') or '')}{tag}</span></div>"
                    f"<div class=dim>{label}{' · <b>behind</b>' if old else ''} · {_ago(d.get('last_seen_at'))}</div></div>")
    pill = (f"<span class=pill-miss>{behind} behind</span>" if behind else
            ("<span class=pill-ok>All current</span>" if latest else "<span class=dim>off</span>"))
    minimum = info["min_build"] or ""
    manual = s.get("latest_build") or ""
    return f"""<div class=card style="margin-top:10px"><details class=key><summary><span>App updates · latest {head}</span>{pill}</summary>
<div class=kbody><div class=dim>{check} Phones show an Update button when a newer build is out. Below the required build they
can't use the app until they update. Turn on TestFlight's Automatic Updates and most phones never see either.</div>
<form method=post action=/admin/app-update class=fform style="margin:10px 0;flex-wrap:wrap">
<input type=hidden name=csrf value={csrf}>
<label class=dim>Require at least build <input type=number name=min_build min=0 value='{e(str(minimum))}' style="width:90px"></label>
<label class=dim>Latest build by hand <input type=number name=latest_build min=0 value='{e(str(manual))}' style="width:90px"></label>
<button>Save</button><button name=check value=1 class=ghost>Check now</button></form>
{''.join(rows) or '<div class=row><div class=dim>No phones yet</div></div>'}</div></details></div>"""


def _test_keys_html(h: Hub, csrf: str, new_key: str | None = None) -> str:
    """Test simulators: keys that let Simulator builds join as Dylan without approving each one."""
    e = html.escape
    rows = []
    sims = 0
    for k in testkeys.keys(h):
        person = h.db.person(k["person_id"]) or {"name": "?"}
        exp = time.strftime("%b %-d, %Y", time.localtime(k["expires_at"])) if k.get("expires_at") else "never"
        expired = bool(k.get("expires_at") and k["expires_at"] < time.time())
        devs = testkeys.active_devices(h, k["id"])
        sims += len(devs)
        dev_rows = "".join(
            f"<div class=row><div>{e(d['name'])} <span class=dim>{e(d.get('app_version') or '')}</span></div>"
            f"<div class=dim>seen {_ago(d.get('last_seen_at'))} "
            f"<form method=post action=/admin/test-device-remove style='display:inline'><input type=hidden name=csrf value={csrf}>"
            f"<input type=hidden name=id value={e(d['id'])}><button class=ghost>Remove</button></form></div></div>"
            for d in devs)
        rows.append(f"""<div class=row><div><b>{e(k['label'])}</b> <span class=dim>joins as {e(person['name'])} ·
made {_ago(k['created_at'])} · {'<b>expired</b>' if expired else 'expires ' + exp} · last used {_ago(k.get('last_used_at'))}</span></div>
<form method=post action=/admin/test-key-revoke><input type=hidden name=csrf value={csrf}><input type=hidden name=id value={e(k['id'])}>
<button class=ghost>Turn off</button></form></div>{dev_rows or '<div class=row><div class=dim>No simulators yet</div></div>'}""")
    shown = ""
    if new_key:
        cmd = (f"mkdir -p ~/.thomascreek && printf %s '{new_key}' > ~/.thomascreek/test-key "
               f"&& chmod 600 ~/.thomascreek/test-key")
        shown = f"""<div class=flash style="margin:10px 0"><b>New test key.</b> It is shown only this once. On the Mac that runs the
Simulators, paste this into Terminal (it saves the key where Simulator builds look for it):
<pre id=tkcmd style="white-space:pre-wrap;word-break:break-all;user-select:all">{e(cmd)}</pre>
<button type=button onclick="navigator.clipboard.writeText(document.getElementById('tkcmd').textContent).then(()=>this.textContent='Copied')">Copy</button></div>"""
    pill = (f"<span class=pill-ok>{sims} simulator{'s' if sims != 1 else ''}</span>" if rows else "<span class=dim>off</span>")
    return f"""<div class=card style="margin-top:10px"><details class=key{' open' if new_key else ''}><summary><span>Test simulators</span>{pill}</summary>
<div class=kbody><div class=dim>A test key lets Simulator builds of the app join as you without approving each one. The app sends it only
from the Simulator, and only on home Wi-Fi; real phones still wait for you. Simulators get a <b>Test</b> tag and no VPN client.
Turning a key off removes every simulator it let in.</div>{shown}
{''.join(rows)}
<form method=post action=/admin/test-key class=fform style="margin:10px 0"><input type=hidden name=csrf value={csrf}>
<input name=label placeholder="Name, e.g. Dylan's Mac" maxlength=60 value="Dylan's Mac"><button>Make a test key</button></form>
</div></details></div>"""


def _status_page(h: Hub, claims: dict[str, Any], flash: str | None, new_key: str | None = None) -> str:
    e = html.escape
    csrf = _csrf(claims)
    # Services
    svc_rows = []
    for s in h.health():
        if not s["enabled"]:
            continue
        cls = "ok" if s["ok"] else ("bad" if s["error"] else "")
        msg = e(s["error"]) if s["error"] else ("OK" if s["ok"] else "not checked yet")
        svc_rows.append(f"<div class='row {cls}'><div><span class=dot></span>{e(s['title'])} <span class=dim>({e(s['id'])})</span></div>"
                        f"<div class='msg dim'>{msg} · {_ago(s['last_ok'])}</div></div>")
    sections_html = _sections_html(h, csrf)
    # Push, VPN, hub
    push = "on" if h.apns.enabled else "off (add the Apple push key and its key ID below)"
    pool = h.vpn.pool_status() if h.config.vpn.provider == "pool" else []
    vpn = (f"{len(pool)} Firewalla clients, {sum(1 for p in pool if p['device_id'])} in use"
           if h.config.vpn.provider == "pool" else h.config.vpn.provider)
    devices = h.db.all("SELECT status, COUNT(*) AS n FROM devices GROUP BY status")
    dev = ", ".join(f"{r['n']} {r['status']}" for r in devices) or "none"
    up = int(time.time() - h.started_at)
    pin = getattr(h, "_tls_pin", None)
    flash_html = f"<div class=flash>{e(flash)}</div>" if flash else ""
    return _page("Thomas Creek Hub", f"""
<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">
<div><h1>Thomas Creek Hub</h1><div class=dim>v{e(__version__)} · up {up // 3600} h {up % 3600 // 60} min · {'practice mode' if h.config.hub.dry_run else 'live'}</div></div>
<form method=post action=/admin/logout><input type=hidden name=csrf value={csrf}><button class=ghost>Sign out</button></form></div>
{flash_html}
<h2>Services</h2><div class=card>{''.join(svc_rows) or '<div class=row>No services set up</div>'}</div>
<h2>Hub</h2><div class=card>
<div class=row><div>Push notifications</div><div class=dim>{e(push)}</div></div>
<div class=row><div>VPN</div><div class=dim>{e(vpn)}</div></div>
<div class=row><div>Devices</div><div class=dim>{e(dev)}</div></div>
<div class=row><div>TLS pin</div><div class=dim><code>{e(pin or '')}</code></div></div></div>
{_app_update_html(h, csrf)}
{_test_keys_html(h, csrf, new_key)}
<h2>Keys and files</h2><p class=dim>Values are never shown again; saving replaces the old one right away.</p>
<div class=card><details class=key><summary><span>Upload several files at once</span><span class=dim>VPN, push key, house model…</span></summary>
<div class=kbody><div class=dim>Choose any mix: the Firewalla VPN .conf files, the AuthKey_….p8 push key, house-model-web.json,
or text files named after a key (for example trek.api_key.txt). Each goes to the right place.</div>
<div class=factions><form method=post action=/admin/upload enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}><input type=file name=files multiple required><button>Upload</button></form></div></div></details></div>
{sections_html}
""")


@pages.get("/admin/status.json")
async def status_json(request: Request, h: Hub = Depends(hub)) -> JSONResponse:
    """Home network only: each service's health (no keys, no values). For checking a deploy."""
    _home_only(request, h)
    rows = [{k: s.get(k) for k in ("id", "ok", "last_ok", "error")} for s in h.health()]
    if request.query_params.get("detail"):
        import shutil
        import subprocess

        ff = shutil.which("ffmpeg")
        try:
            ver = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=5).stdout.splitlines()[0] \
                if ff else "not installed"
        except Exception as exc:  # noqa: BLE001
            ver = f"failed: {type(exc).__name__}"
        dl = getattr(h, "downloads", None)
        return JSONResponse({"services": rows, "build": os.environ.get("HUB_BUILD"), "ffmpeg": ver,
                             "download_failures": list(getattr(dl, "failures", [])),
                             "youtube": h.youtube.summary() if getattr(h, "youtube", None) else None})
    return JSONResponse(rows)


@pages.get("/admin")
async def admin_home(request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    if not hasattr(h, "_tls_pin"):
        from ..tls import ensure_tls, spki_fingerprint

        try:
            cert, _ = ensure_tls(h.config)
            h._tls_pin = spki_fingerprint(cert)  # type: ignore[attr-defined]
        except OSError:
            pass
    return HTMLResponse(_status_page(h, claims, request.query_params.get("m")), headers=_headers())


async def _form_session(request: Request, h: Hub) -> tuple[dict[str, Any], Any]:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        raise error(401, "unauthorized", "Sign in again")
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), _csrf(claims)):
        raise error(403, "forbidden", "Reload the page and try again")
    return claims, form


def _back(msg: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/admin?m={quote(msg)}", status_code=303)


@pages.post("/admin/secrets")
async def set_secret(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    name = str(form.get("name", "")).strip()
    if not NAME_RE.match(name) or name in NEVER_EDIT:
        return _back("That name isn't allowed: use lower-case words joined by dots, like plex.token.")
    value = str(form.get("value") or "")
    upload = form.get("file")
    filename = getattr(upload, "filename", "") or ""
    if upload is not None and filename:
        raw = await upload.read()
        if len(raw) > 64 * 1024:
            return _back("That file is too large for a key.")
        value = raw.decode("utf-8", errors="replace")
    value = value.strip()
    if not value:
        return _back(f"Nothing saved: {name} was empty.")
    if "\n" in value:
        value += "\n"
    saved = [name]
    m = re.fullmatch(r"AuthKey_([A-Z0-9]{10})\.p8", filename)
    if m and name in (h.config.apns.key_secret, "apns.key_id"):
        name = h.config.apns.key_secret
        h.vault.set("apns.key_id", m.group(1))
        saved = [name, "apns.key_id"]
    elif m and name in ("appstore.auth_key_p8", "appstore.key_id"):
        name = "appstore.auth_key_p8"
        h.vault.set("appstore.key_id", m.group(1))
        saved = [name, "appstore.key_id"]
    h.vault.set(name, value)
    if name in APPSTORE_KEYS:
        h.app_update._jwt = None
        asyncio.create_task(h.app_update.check())
    if name.startswith(h.config.vpn.pool_prefix):
        h.vpn._pool = None
    h.db.log("admin.secret_set", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=",".join(saved),
             source="web")
    if name == h.config.onepassword.token_secret and h.onepassword.enabled:
        asyncio.create_task(h.onepassword.sync_safely())
    label = next((f[1] for sec in SECTIONS for f in sec["fields"] if f[0] == name), " and ".join(saved))
    return _back(f"Saved {label}.")


@pages.post("/admin/secrets/delete")
async def delete_secret(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    name = str(form.get("name", "")).strip()
    if name in NEVER_EDIT or not h.vault.delete(name):
        return _back(f"{name} was not removed.")
    if name.startswith(h.config.vpn.pool_prefix):
        h.vpn._pool = None
    h.db.log("admin.secret_delete", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=name, source="web")
    return _back(f"Removed {name}.")


@pages.post("/admin/bambu/sign-in")
async def bambu_sign_in(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    try:
        step = await h.bambu_cloud.start(str(form.get("email") or ""), str(form.get("password") or ""))
    except bambu_cloud.CloudError as exc:
        return _back(f"Bambu sign-in: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        return _back(f"Couldn't reach Bambu: {str(exc)[:120]}")
    if step == "done":
        _bambu_done(h, claims)
        return _back("Signed in to the Bambu account.")
    return _back("Bambu emailed a 6-digit code. Enter it under 3D printer, Bambu account." if step == "code"
                 else "Enter the code from your authenticator app under 3D printer, Bambu account.")


@pages.post("/admin/bambu/code")
async def bambu_code(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    try:
        await h.bambu_cloud.finish(str(form.get("code") or ""))
    except bambu_cloud.CloudError as exc:
        return _back(f"Bambu sign-in: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        return _back(f"Couldn't reach Bambu: {str(exc)[:120]}")
    _bambu_done(h, claims)
    n = len(h.bambu_cloud.devices)
    return _back(f"Signed in to the Bambu account ({n} printer{'s' if n != 1 else ''} on it).")


def _bambu_done(h: Hub, claims: dict[str, Any]) -> None:
    h.db.log("admin.bambu_sign_in", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), source="web")


@pages.post("/admin/bambu/sign-out")
async def bambu_sign_out(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, _ = await _form_session(request, h)
    h.bambu_cloud.sign_out()
    h.db.log("admin.bambu_sign_out", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), source="web")
    return _back("Signed the hub out of the Bambu account.")


@pages.post("/admin/logout")
async def logout(request: Request, h: Hub = Depends(hub)) -> Response:
    await _form_session(request, h)
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


def _store_vpn(h: Hub, fname: str, text: str) -> str:
    """One Firewalla client file into the pool. LAUREN.conf -> vpn.pool.lauren, Client3.conf -> vpn.pool.3,
    anything else (or a number already taken) -> the next free number. Returns the slot label."""
    from ..vpn import parse_wg_conf, public_from_private

    conf = parse_wg_conf(text)
    public_from_private(conf["interface"]["privatekey"])
    if not (conf["interface"].get("address") and conf.get("peer", {}).get("publickey")):
        raise ValueError("not a client file")
    prefix = h.config.vpn.pool_prefix
    stem = re.sub(r"\.(conf|txt)$", "", fname, flags=re.I)
    num = re.fullmatch(r"(?i)client[ _-]?(\d+)", stem)
    label = num.group(1) if num else re.sub(r"[^a-z0-9_]", "", stem.lower())
    firsts = {(p.get("name") or "").strip().split(" ")[0].lower() for p in h.db.people()}
    if label and not label.isdigit() and label not in firsts:
        label = ""  # not a person in the hub: any phone may use it
    if label and (not label.isdigit() or not h.vault.has(prefix + label)):
        name = prefix + label
        h.vault.set(name, text.strip() + "\n")
    else:
        name = h.vpn.add_to_pool(text)
    h.vpn._pool = None
    return name[len(prefix):]


def _store_asset(name: str, raw: bytes) -> None:
    from ..config import data_dir

    if name.endswith(".json"):
        json.loads(raw)
    dest = data_dir() / "assets" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(dest)


def _store_any(h: Hub, fname: str, raw: bytes) -> str:
    """Route one uploaded file by its name or contents. Returns a friendly description of where it went."""
    from ..dropbox import ASSET_FILES

    base = fname.rsplit("/", 1)[-1]
    if base in ASSET_FILES:
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("too big")
        _store_asset(ASSET_FILES[base], raw)
        return "house model" if base.endswith(".json") else "header photo"
    if len(raw) > 64 * 1024:
        raise ValueError("too big")
    text = raw.decode("utf-8")
    m = re.fullmatch(r"AuthKey_([A-Z0-9]{10})\.p8", base)
    if m and h.vault.has("apns.key_id") and str(h.vault.get("apns.key_id")).strip() != m.group(1):
        # Push already has its key: another AuthKey is the App Store Connect one.
        h.vault.set("appstore.auth_key_p8", text.strip() + "\n")
        h.vault.set("appstore.key_id", m.group(1))
        h.app_update._jwt = None
        asyncio.create_task(h.app_update.check())
        return "App Store Connect key"
    if m or "BEGIN PRIVATE KEY" in text and base.lower().endswith(".p8"):
        h.vault.set("apns.auth_key_p8", text.strip() + "\n")
        if m:
            h.vault.set("apns.key_id", m.group(1))
        return "push key"
    if base.lower().endswith(".conf") or "[Interface]" in text:
        return "VPN " + _store_vpn(h, base, text)
    name = re.sub(r"\.(txt|key|token)$", "", base)
    known = {f[0] for sec in SECTIONS for f in sec["fields"]} | set(_expected(h))
    if name in known and name not in NEVER_EDIT:
        value = text.strip()
        h.vault.set(name, value + ("\n" if "\n" in value else ""))
        return name
    raise ValueError("unknown file")


@pages.post("/admin/upload")
async def upload_many(request: Request, h: Hub = Depends(hub)) -> Response:
    """Several files at once: VPN .conf files, the AuthKey .p8, the house model, or files named after a key."""
    claims, form = await _form_session(request, h)
    saved, bad = [], []
    for up in form.getlist("files"):
        fname = getattr(up, "filename", "") or ""
        if not fname:
            continue
        try:
            saved.append(_store_any(h, fname, await up.read()))
        except Exception:  # noqa: BLE001
            bad.append(fname)
    if saved:
        h.db.log("admin.upload", "ok", person_id=claims.get("per"), device_id=claims.get("dev"),
                 target=",".join(saved), source="web")
        if h.config.onepassword.token_secret in saved and h.onepassword.enabled:
            asyncio.create_task(h.onepassword.sync_safely())
    msg = f"Saved: {', '.join(saved)}." if saved else "Nothing saved."
    if bad:
        msg += f" Not recognised: {', '.join(bad)}."
    return _back(msg)


@pages.post("/admin/vpn")
async def add_vpn_clients(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    saved, bad = [], []
    for up in form.getlist("files"):
        fname = getattr(up, "filename", "") or ""
        if not fname:
            continue
        try:
            raw = await up.read()
            if len(raw) > 8 * 1024:
                raise ValueError
            saved.append(_store_vpn(h, fname, raw.decode("utf-8")))
        except Exception:  # noqa: BLE001
            bad.append(fname)
    if saved:
        h.db.log("admin.vpn_add", "ok", person_id=claims.get("per"), device_id=claims.get("dev"),
                 target=",".join(saved), source="web")
    msg = f"Added VPN clients: {', '.join(saved)}." if saved else "No VPN clients added."
    if bad:
        msg += f" Not a WireGuard client file: {', '.join(bad)}."
    return _back(msg)


@pages.post("/admin/app-update")
async def app_update_settings(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    if form.get("check"):
        if not h.app_update.configured:
            return _back("Add the App Store Connect key first.")
        await h.app_update.check()
        info = h.app_update.info()
        if h.app_update.last_error:
            return _back(f"Check failed: {h.app_update.last_error}")
        return _back(f"Latest build is {info['latest_build']}." if info["latest_build"] else "No build found yet.")

    def num(k: str) -> int | None:
        v = str(form.get(k) or "").strip()
        return int(v) if v.isdigit() and int(v) > 0 else None

    mn, lb = num("min_build"), num("latest_build")
    h.app_update.save_settings(min_build=mn, latest_build=lb, set_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if lb else None)
    h.db.log("admin.app_update", "ok", person_id=claims.get("per"), device_id=claims.get("dev"),
             params={"min_build": mn, "latest_build": lb}, source="web")
    h.bus.publish({"type": "app_update", "data": h.app_update.info()})
    return _back("Saved. Phones pick it up next time they open the app." if mn or lb else "Cleared.")


@pages.post("/admin/house-model")
async def house_model(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    up = form.get("file")
    raw = await up.read() if up is not None and not isinstance(up, str) else b""
    if not raw or len(raw) > 20 * 1024 * 1024:
        return _back("Choose the house-model-web.json file.")
    try:
        _store_asset("house-model-web.json", raw)
    except ValueError:
        return _back("That file isn't the house model (not JSON).")
    h.db.log("admin.asset_set", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target="house-model",
             source="web")
    return _back("Saved the house model.")


@pages.post("/admin/test-key")
async def make_test_key(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    label = str(form.get("label") or "").strip() or "Test simulators"
    _, key = testkeys.create(h, claims["per"], label, created_by=claims.get("per"))
    # Rendered straight into the reply (not a redirect) so the key never sits in a URL or the history.
    return HTMLResponse(_status_page(h, claims, "Made a test key.", new_key=key), headers=_headers())


@pages.post("/admin/test-key-revoke")
async def revoke_test_key(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    n = testkeys.revoke(h, str(form.get("id") or ""), by=claims.get("per"))
    return _back(f"Turned the test key off and removed {n} simulator{'s' if n != 1 else ''}.")


@pages.post("/admin/test-device-remove")
async def remove_test_device(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    d = h.db.device(str(form.get("id") or ""))
    if d is None or d.get("kind") != "test":
        return _back("That isn't a test simulator.")
    h.revoke_device(d, reason="test simulator removed on the web page")
    return _back(f"Removed {d['name']}.")
