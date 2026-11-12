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
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
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
}
NEVER_EDIT = {"wireguard.server_private_key"}


def _expected(h: Hub) -> list[str]:
    cfg = h.config
    names = []
    for svc in ("home_assistant", "plex", "jellyseerr", "sonarr", "radarr", "truenas", "mealie", "homebox",
                "family_board", "show_player", "frigate"):
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


def _status_page(h: Hub, claims: dict[str, Any], flash: str | None) -> str:
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
    # Secrets
    names = list(dict.fromkeys(_expected(h) + sorted(h.vault.names())))
    sec_rows = []
    for n in names:
        if n in NEVER_EDIT:
            continue
        have = h.vault.has(n)
        desc = DESCRIPTIONS.get(n) or ("VPN client (WireGuard .conf)" if n.startswith(h.config.vpn.pool_prefix) else "")
        state = "<span class=dim>set</span>" if have else "<b style='color:var(--bad)'>missing</b>"
        remove = (f"<form method=post action=/admin/secrets/delete style=display:inline><input type=hidden name=csrf value={csrf}>"
                  f"<input type=hidden name=name value='{e(n)}'><button class=ghost onclick=\"return confirm('Remove {e(n)}?')\">Remove</button></form>"
                  if have else "")
        sec_rows.append(f"""<div class=row style="display:block"><div style="display:flex;justify-content:space-between;gap:12px">
<div><code>{e(n)}</code><div class=dim>{e(desc)}</div></div><div>{state}</div></div>
<details><summary>{'Replace' if have else 'Add'}</summary>
<form class=edit method=post action=/admin/secrets enctype=multipart/form-data>
<input type=hidden name=csrf value={csrf}><input type=hidden name=name value='{e(n)}'>
<textarea name=value rows=3 placeholder="Paste the new value" autocomplete=off spellcheck=false></textarea>
<div class=dim>or choose a file: <input type=file name=file></div><div><button>Save</button> {remove}</div></form></details></div>""")
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
<h2>Keys and tokens</h2><p class=dim>Values are never shown. Saving replaces the old value right away. For the Apple push key, choose the
<code>AuthKey_XXXXXXXXXX.p8</code> file: the key ID is taken from its name.</p>
<div class=card>{''.join(sec_rows)}</div>
<h2>Add another</h2><div class=card><form class=edit method=post action=/admin/secrets enctype=multipart/form-data>
<input type=hidden name=csrf value={csrf}>
<input type=text name=name placeholder="name, e.g. vpn.pool.6 or homebox.password" pattern="[a-z0-9_]+(\\.[a-z0-9_]+)+" required>
<textarea name=value rows=3 placeholder="Value" autocomplete=off spellcheck=false></textarea>
<div class=dim>or choose a file: <input type=file name=file></div><div><button>Save</button></div></form></div>
""")


@pages.get("/admin")
async def admin_home(request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    if not hasattr(h, "_tls_pin"):
        from ..tls import ensure_tls, spki_fingerprint

        cert, _ = ensure_tls(h.config)
        h._tls_pin = spki_fingerprint(cert)  # type: ignore[attr-defined]
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
    h.vault.set(name, value)
    if name.startswith(h.config.vpn.pool_prefix):
        h.vpn._pool = None
    h.db.log("admin.secret_set", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=",".join(saved),
             source="web")
    if name == h.config.onepassword.token_secret and h.onepassword.enabled:
        asyncio.create_task(h.onepassword.sync_safely())
    return _back(f"Saved {' and '.join(saved)}.")


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


@pages.post("/admin/logout")
async def logout(request: Request, h: Hub = Depends(hub)) -> Response:
    await _form_session(request, h)
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(COOKIE, path="/admin")
    return resp
