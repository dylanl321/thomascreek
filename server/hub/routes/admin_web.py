"""A small web page on the hub for Dylan: status, and replacing keys and tokens.

Open https://<hub>:8443/admin on the home network. Signing in goes through the
app: the page shows a short code, Dylan types it in the app (Systems, Hub web
page) and confirms with Face ID. The app can also open the page already signed
in. The session is a cookie for /admin only, home network or home VPN only. It
lasts 7 days from the last visit (renewed at most hourly) and never more than
30 days from the code sign-in. It is signed with the hub's stored key, so a hub
restart or update keeps it. Signing out revokes it on the hub, and it stops
working as soon as the phone that approved it is no longer an active admin.

Values are write-only: the page lists which keys exist, never what they are.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .. import __version__, aikeys, bambu_cloud, testkeys
from ..iptvcanon import fold_stats as catalog_fold_stats
from ..iptvplay import accounts as iptv_accounts
from ..iptvplay import add_login
from ..iptvplay import logins as iptv_logins
from .. import iptvaddr, iptvplay
from ..bambu_cloud import token_expiry
from ..crypto import CryptoError
from ..mcp_oauth import OAuthError
from ..onepassword import NAME_RE
from ..state import Hub
from . import admin_health
from .deps import Caller, caller, caller_ip, error, hub, need, require_confirm
from ..faults import note

log = logging.getLogger("hub.admin_web")

pages = APIRouter(tags=["admin-web"], include_in_schema=False)
api = APIRouter(prefix="/v1/admin", tags=["admin-web"])

COOKIE = "tc_admin"
AUD = "admin-web"
LINK_AUD = "admin-web-link"
SESSION_IDLE_S = 7 * 86400   # signed out after a week without a visit
SESSION_MAX_S = 30 * 86400   # and always 30 days after the code sign-in
SESSION_RENEW_S = 3600       # a visit renews the cookie at most once an hour
REVOKED_KV = "admin.web_revoked"  # session id -> expiry, for signed-out cookies
CODE_TTL_S = 300
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_RETURN: contextvars.ContextVar[str] = contextvars.ContextVar("admin_return", default="/admin")

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
    "youtube.api_key": "YouTube Data API key (optional: channel video counts in the add wizard)",
    "tmdb.api_key": "TMDB API key (posters, descriptions, cast)",
    "fanart.api_key": "Fanart.tv API key (logos and backdrops)",
    "omdb.api_key": "OMDb API key (IMDb and Rotten Tomatoes ratings)",
    "opensubtitles.api_key": "OpenSubtitles API key (subtitle search)",
    "subdl.api_key": "SubDL API key (subtitle search)",
    "vpn.pia.admin_username": "PIA username (Private VPN)",
    "vpn.pia.admin_password": "PIA password (Private VPN)",
    "appstore.auth_key_p8": "App Store Connect API key (.p8 file), for spotting new app builds",
    "appstore.key_id": "App Store Connect API key ID",
    "appstore.issuer_id": "App Store Connect issuer ID",
    "printer.h2s.access_code": "Bambu H2S access code (LAN mode)",
    "printer.h2s.serial": "Bambu H2S serial number",
    "printer.h2s.host": "Bambu H2S IP address",
    "github.tv_app_token": "GitHub token for downloading Thomas Creek TV builds",
    "iptv.xtream_server": "IPTV Xtream server address",
    "iptv.xtream_username": "IPTV Xtream username",
    "iptv.xtream_password": "IPTV Xtream password",
    "iptv.m3u_url": "IPTV M3U playlist address",
    "iptv.epg_url": "IPTV TV guide (XMLTV) address",
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
        log.info("admin web: sign-in cookie refused (expired or not signed by this hub)")
        return None
    sid = claims.get("sid")
    if sid and sid in (h.db.kv_get(REVOKED_KV) or {}):
        log.info("admin web: sign-in cookie refused (signed out)")
        return None
    dev = h.db.device(claims.get("dev", ""))
    person = h.db.person(claims.get("per")) if claims.get("per") else None
    if not dev or dev["status"] != "active" or not person or person["role"] != "admin":
        log.info("admin web: sign-in cookie refused (the approving phone is no longer an active admin)")
        return None
    if not h.can_open_web(caller_ip(request, h)):
        return None
    claims["_token"] = token
    # Renew a session that is still in use; the middleware in main.py sets the cookie.
    if sid and time.time() - claims.get("iat", 0) > SESSION_RENEW_S:
        request.state.admin_renew = claims
    return claims


def _csrf(claims: dict[str, Any]) -> str:
    # Tied to the session, not the cookie, so a renewed cookie keeps open forms working.
    return hashlib.sha256(f"csrf:{claims.get('sid') or claims['_token']}".encode()).hexdigest()[:32]


def _set_session(resp: Response, h: Hub, dev: str, per: str, *, sid: str | None = None,
                 started: int | None = None) -> None:
    now = int(time.time())
    started = started or now
    ttl = min(SESSION_IDLE_S, started + SESSION_MAX_S - now)
    if ttl <= 0:
        return
    token, _ = h.tokens.issue_scoped({"aud": AUD, "dev": dev, "per": per, "sid": sid or secrets.token_hex(16),
                                      "st": started}, ttl)
    # Lax, not Strict: Strict drops the cookie when the page is opened from a link on another site (a chat,
    # an email, the app), which showed the sign-in code again. Lax still keeps it off cross-site form posts,
    # and every form also needs the session's CSRF token.
    resp.set_cookie(COOKIE, token, max_age=ttl, httponly=True, secure=True, samesite="lax", path="/admin")


def renew_session(request: Request, resp: Response, h: Hub) -> None:
    """Called after an /admin response: push the expiry out for a session that was just used."""
    claims = getattr(request.state, "admin_renew", None)
    if not claims or resp.status_code >= 400 or "set-cookie" in resp.headers:
        return
    _set_session(resp, h, claims["dev"], claims["per"], sid=claims["sid"], started=int(claims.get("st") or claims["iat"]))


def _revoke(h: Hub, claims: dict[str, Any]) -> None:
    if not claims.get("sid"):
        return
    now = time.time()
    revoked = {k: v for k, v in (h.db.kv_get(REVOKED_KV) or {}).items() if v > now}
    revoked[claims["sid"]] = int(claims.get("st") or claims.get("iat") or now) + SESSION_MAX_S
    h.db.kv_set(REVOKED_KV, revoked)


def _home_only(request: Request, h: Hub) -> None:
    if not h.can_open_web(caller_ip(request, h)):
        raise error(403, "home_only", "The hub's web page only opens on the home network or the home VPN")


# -- app side: approve a code, or get a signed-in link ------------------------
class CodeBody(BaseModel):
    code: str = Field(max_length=20)


@api.post("/web-approve")
async def web_approve(body: CodeBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Dylan typed the code the web page shows; Face ID confirms.

    The same box takes the code an AI app's sign-in page shows (hub/mcp_oauth.py): that connects
    the AI app as the person approving, any grown-up, not only Dylan."""
    code = re.sub(r"[^A-Z0-9]", "", body.code.upper())
    if h.mcp_oauth.is_pending(code):
        await require_confirm(request, h, c, "secure")
        person = h.db.person(c.subject.person_id) if c.subject.person_id else None
        if person is None:
            raise error(403, "forbidden", "This phone isn't assigned to anyone yet")
        try:
            name = h.mcp_oauth.approve(code, person, c.device["id"])
        except OAuthError as exc:
            raise error(exc.status, exc.error, exc.description) from None
        h.db.log("mcp.connect_approve", "ok", person_id=person["id"], device_id=c.device["id"], params={"app": name})
        return {"ok": True, "connected": name}
    need(h, c, "people")
    await require_confirm(request, h, c, "admin")
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
nav.nav{display:flex;gap:18px;margin:0 0 18px}nav.nav a{color:var(--dim);text-decoration:none;font-weight:650}
.bars{display:flex;align-items:flex-end;gap:2px;height:64px;padding:8px 0}.bars i{display:block;width:6px;background:var(--acc);border-radius:2px 2px 0 0}
nav.nav a.on{color:var(--fg)}
.hero{font:600 40px/1.15 ui-monospace,Menlo,monospace;letter-spacing:-0.03em;word-break:break-all;margin:8px 0}
.codebig{font:600 72px/1 ui-monospace,Menlo,monospace;letter-spacing:6px;margin:8px 0}
.qr svg{width:168px;height:168px;background:#fff;border-radius:12px}
.dl{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
a.btn{display:inline-block;font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;text-decoration:none}
:root{--warn:#b7791f}@media (prefers-color-scheme:dark){:root{--warn:#e0b04f}}
.warn .dot,.kstate.warn .dot,.kstate.down .dot{background:var(--warn)}.warn .msg{color:var(--warn)}.kstate.ok .dot{background:var(--ok)}.kstate.bad .dot{background:var(--bad)}.kstate.bad{color:var(--bad)}
.pill-warn{font-size:12px;padding:2px 8px;border-radius:999px;white-space:nowrap;background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
button.ghost2{background:transparent;color:var(--acc);border-color:var(--line)}
select,.fform input:not([type]),.fform input[type=email],.fform input[type=number]{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.tag{font-size:11px;padding:1px 6px;border-radius:6px;border:1px solid var(--line);color:var(--dim);margin-left:6px}
a{color:var(--acc)}
.fform input.kin{flex:1;min-width:200px;font:13px ui-monospace,Menlo,monospace;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.chg{font-size:12px;padding:2px 8px;border-radius:999px;background:color-mix(in srgb,var(--acc) 18%,transparent);color:var(--acc)}
.savebar{position:sticky;bottom:0;display:flex;gap:10px;align-items:center;justify-content:flex-end;margin:24px -4px 0;padding:12px;
border-radius:12px 12px 0 0;background:var(--card);border:1px solid var(--line);box-shadow:0 -4px 16px rgb(0 0 0 / 8%)}
.savebar span{flex:1}.savebar:not(.dirty) #savebtn{opacity:.55}.savebar.dirty{border-color:var(--acc)}
"""

# The admin suite: a left menu of areas and one working column. Summary first; detail folds away. Every state has a
# shape as well as a colour (round = fine, diamond = look soon, square = broken, grey = off).
SUITE_CSS = (Path(__file__).parent / "admin_suite.css").read_text()

ICONS = {
    "/admin": '<path d="M4 13h6V4H4zm10 7h6v-9h-6zM4 20h6v-4H4zm10-11h6V4h-6z"/>',
    "/admin/devices": '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/>',
    "/admin/people": ('<circle cx="9" cy="8" r="3.5"/>'
                      '<path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>'),
    "/admin/watching": '<path d="M6 4l13 8-13 8z"/>',
    "/admin/connections": '<path d="M9 7H6a5 5 0 0 0 0 10h3m6-10h3a5 5 0 0 1 0 10h-3M8 12h8"/>',
    "/admin/ai": ('<path d="M12 3v3m0 12v3M3 12h3m12 0h3M6 6l2 2m8 8 2 2M6 18l2-2m8-8 2-2"/>'
                  '<circle cx="12" cy="12" r="3"/>'),
    "/admin/updates": '<path d="M12 3v12m0 0-4-4m4 4 4-4M4 19h16"/>',
    "/admin/security": '<path d="M12 3 4 6v6c0 4.5 3.4 8.2 8 9 4.6-.8 8-4.5 8-9V6z"/>',
}
AREAS = (("/admin", "Overview"), ("/admin/devices", "TVs"), ("/admin/people", "People"),
         ("/admin/watching", "Watching"),
         ("/admin/connections", "Connections"), ("/admin/ai", "AI"), ("/admin/updates", "App updates"),
         ("/admin/security", "Hub and security"))
AREA_PATHS = {path for path, _ in AREAS}
CSS = CSS + SUITE_CSS
# Pages that sit under an area in the menu.
UNDER = {"/admin/downloads": "/admin/watching", "/admin/playlists": "/admin/watching", "/admin/cloud": "/admin/watching"}


def _counts(h: Hub) -> dict[str, tuple[int, str]]:
    """Badges for the menu: how many things in each area need someone. Never raises."""
    out: dict[str, tuple[int, str]] = {}
    try:
        from .. import tvstate

        tvs = [r for r in tvstate.rows(h) if r["state"] in ("bad", "warn")]
        if tvs:
            out["/admin/devices"] = (len(tvs), "bad" if any(r["state"] == "bad" for r in tvs) else "warn")
    except Exception:  # noqa: BLE001 - a badge never breaks a page
        pass
    try:
        waiting = h.db.one("SELECT COUNT(*) AS n FROM devices WHERE status='pending'")["n"]  # type: ignore[index]
        if waiting:
            out["/admin/people"] = (int(waiting), "warn")
    except Exception:  # noqa: BLE001
        pass
    try:
        keys = [r for r in h.keychecks.summary() if r["ready"] and r["state"] in ("bad", "warn")]
        down = [s for s in admin_health.service_rows(h) if s.get("error")]
        n = len(keys) + len(down)
        if n:
            out["/admin/connections"] = (n, "bad" if down or any(r["state"] == "bad" for r in keys) else "warn")
    except Exception:  # noqa: BLE001
        pass
    return out


def _nav(here: str, h: Hub | None = None) -> str:
    """The left menu. `here` is the page's path; pages under an area light that area."""
    here = UNDER.get(here, here)
    if here not in AREA_PATHS:
        longest = sorted((p for p, _ in AREAS if p != "/admin"), key=len, reverse=True)
        here = next((p for p in longest if here.startswith(p)), here)
    counts = _counts(h) if h is not None else {}
    links = []
    for href, label in AREAS:
        n, level = counts.get(href, (0, ""))
        badge = f"<span class='n {level}'>{n}</span>" if n else ""
        icon = (f"<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 stroke-linecap=round "
                f"stroke-linejoin=round aria-hidden=true>{ICONS[href]}</svg>")
        links.append(f"<a href={href}{' class=on' if href == here else ''}>{icon}{html.escape(label)}{badge}</a>")
    build = (os.environ.get("HUB_BUILD") or "")[:7]
    foot = f"<div class=railfoot>v{html.escape(__version__)}{' · ' + html.escape(build) if build else ''}"
    if h is not None:
        foot += " · " + ("practice mode" if h.config.hub.dry_run else "live")
    foot += "</div>"
    return ("<aside class=rail><a class=brand href=/admin><i>&#8962;</i>"
            "<span><b>Thomas Creek</b><small>Hub admin</small></span></a>"
            f"<nav class=areas>{''.join(links)}</nav>{foot}</aside>")


def _page(title: str, body: str) -> str:
    head = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>")
    text = body.lstrip()
    if text.startswith("<aside class=rail>"):
        cut = text.index("</aside>") + len("</aside>")
        return f"{head}<div class=app>{text[:cut]}<main>{text[cut:]}</main></div></body></html>"
    return f"{head}<main>{body}</main></body></html>"


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
    {"title": "YouTube (optional key)", "always": True, "optional": True, "fields": [
        ("youtube.api_key", "YouTube Data API key", "text",
         "Optional. Search works without it; with it, the add wizard also shows how many videos a channel has. "
         "console.cloud.google.com: a project, APIs and Services, Library, YouTube Data API v3, Enable; then "
         "Credentials, Create credentials, API key (restrict it to the YouTube Data API v3). It starts with AIza."),
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
    {"title": "Family board", "service": "family_board", "optional": True, "fields": [
        ("family_board.admin_token", "Family Command Center admin token", "text",
         "Optional. The board accepts chore and event changes from the hub on the home network without it. "
         "Only needed if the hub ever reaches the board through the public address."),
    ]},
    {"title": "TV app (Thomas Creek TV)", "always": True, "optional": True, "fields": [
        ("github.tv_app_token", "GitHub token", "text",
         "github.com, Settings, Developer settings, Fine-grained tokens, Generate new token: only the "
         "dylanl321/thomascreek-tv repository, permission Contents read-only. The hub uses it to download new "
         "builds of the TV app from the repo's releases."),
    ]},
    {"title": "IPTV providers", "always": True, "iptv": True, "fields": [
        ("iptv.m3u_url", "M3U playlist address", "text", "Only if a provider gave you an M3U link instead of a username and password."),
        ("iptv.epg_url", "TV guide address (XMLTV)", "text", "Optional. Providers with a username and password usually include the guide already."),
    ]},
    {"title": "Metadata sources (artwork, ratings, subtitles)", "always": True, "optional": True, "fields": [
        ("tmdb.api_key", "TMDB API key", "text",
         "Posters, backdrops, descriptions, cast and trailers, for the hub's artwork and the TV app. themoviedb.org: "
         "Settings, API, create a developer key; paste the \"API Key\" (32 letters and digits), not the long Read Access Token."),
        ("fanart.api_key", "Fanart.tv API key", "text",
         "Clear logos and better backdrops on top of TMDB. fanart.tv: sign in, Profile, API, Personal API key."),
        ("omdb.api_key", "OMDb API key", "text",
         "IMDb and Rotten Tomatoes ratings in the TV app. omdbapi.com/apikey.aspx: the free key arrives by email "
         "and has to be activated from that email."),
        ("opensubtitles.api_key", "OpenSubtitles API key", "text",
         "Online subtitle search in the TV app's player. opensubtitles.com/consumers: New consumer, then copy its API key."),
        ("subdl.api_key", "SubDL API key", "text",
         "A second subtitle search, used alongside OpenSubtitles. subdl.com/panel/api: copy the API key."),
    ]},
    {"title": "Homebox", "always": True, "optional": True, "fields": [
        ("homebox.password", "Homebox password", "text", "Optional: the password of the Homebox account the hub signs in as."),
    ]},
    {"title": "Private VPN (PIA)", "always": True, "optional": True, "fields": [
        ("vpn.pia.admin_username", "PIA username", "text",
         "The app's Private VPN button. Your Private Internet Access username (it starts with p, e.g. p1234567). "
         "Used first; the 1Password item PIA is only used when this is empty."),
        ("vpn.pia.admin_password", "PIA password", "text", "The password for that PIA account."),
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
    state = admin_health.key_state(h, name) if have else None
    badge = admin_health.key_pill(state) if have else "<span class=pill-miss>Missing</span>"
    tested = h.keychecks.for_secret(name) if have else []
    remove = (f"<form method=post action=/admin/secrets/delete class=inline><input type=hidden name=csrf value={csrf}>"
              f"<input type=hidden name=name value='{e(name)}'><button class=ghost "
              f"onclick=\"return confirm('Remove the saved {e(label)}?')\">Remove</button></form>" if have else "")
    line = admin_health.key_line(state) if tested else ""
    test = admin_health.test_button(csrf, name) if tested else ""
    if kind != "file":
        # Text keys belong to the one "Save all" form at the bottom of the page (form=saveall), so
        # several can be pasted and saved together without the page reloading between them.
        inp = (f"<input type=password form=saveall name='v:{e(name)}' data-key='{e(label)}' class=kin "
               f"placeholder='{'Paste a new value to replace it' if have else 'Paste here'}' autocomplete=off>")
        return f"""<details class=key id='k-{e(name)}'><summary><span>{e(label)}</span><span><span class=chg hidden>Changed</span> {badge}</span></summary><div class=kbody><div class=dim>{e(note)}</div>{line}
<div class=factions><div class=fform>{inp}</div>{test}{remove}</div></div></details>"""
    inp = "<input type=file name=file required>"
    return f"""<details class=key id='k-{e(name)}'><summary><span>{e(label)}</span>{badge}</summary><div class=kbody><div class=dim>{e(note)}</div>{line}
<div class=factions>{test}<form method=post action=/admin/secrets enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}><input type=hidden name=name value='{e(name)}'>{inp}<button>{'Replace' if have else 'Save'}</button></form>{remove}</div></div></details>"""


BAMBU_KEYS = {bambu_cloud.TOKEN, bambu_cloud.REFRESH, bambu_cloud.UID}


def _bambu_html(h: Hub, csrf: str) -> str:
    """Sign in to the Bambu account: watch the printer without LAN Only mode."""
    e = html.escape
    c = h.bambu_cloud
    hid = f"<input type=hidden name=csrf value={csrf}>"
    note = ("Optional. Lets the hub watch the printer through Bambu's cloud when it can't reach it on the home "
            "network, and fills in the serial number and access code. Signed up with Google or Apple? Leave the "
            "password empty and Bambu emails you a 6-digit login code. A password is used once to sign in and isn't kept.")
    if c.signed_in:
        names = ", ".join(f"{d.get('name') or d.get('dev_id')}" for d in c.devices) or "no printers listed yet"
        exp = token_expiry(c.token)
        until = time.strftime("%b %-d", time.localtime(exp)) if exp else "?"
        body = (f"<div class=dim>Signed in. Printers on the account: {e(names)}. Renews by itself (good until {e(until)})."
                f"{' ' + e(c.last_error) if c.last_error else ''}</div><div class=factions>"
                f"<form method=post action=/admin/bambu/sign-out class=inline>{hid}<button class=ghost "
                f"onclick=\"return confirm('Sign the hub out of the Bambu account?')\">Sign out</button></form></div>")
        badge = "<span class=pill-ok>Signed in</span>"
    elif c.needs_account:
        body = (f"<div class=dim>Signed in, but the hub couldn't read which account it is yet."
                f"{' ' + e(c.last_error) if c.last_error else ''}</div><div class=factions>"
                f"<form method=post action=/admin/bambu/retry class=inline>{hid}<button>Try again</button></form>"
                f"<form method=post action=/admin/bambu/sign-out class=inline>{hid}<button class=ghost>Start over</button></form></div>")
        badge = "<span class=pill-miss>Almost signed in</span>"
    elif c.waiting_for:
        what = ("the 6-digit code Bambu just emailed" if c.waiting_for == "code"
                else "the 6-digit code from your authenticator app")
        body = (f"<div class=dim>Enter {what}.</div><div class=factions><form method=post action=/admin/bambu/code "
                f"class=fform>{hid}<input name=code inputmode=numeric autocomplete=one-time-code pattern='[0-9 ]{{6,7}}' "
                f"placeholder='123456' required><button>Sign in</button></form>"
                f"<form method=post action=/admin/bambu/cancel class=inline>{hid}<button class=ghost>Start over</button></form></div>")
        badge = "<span class=pill-miss>Waiting for code</span>"
    else:
        body = (f"<div class=dim>{e(note)}</div><div class=factions><form method=post action=/admin/bambu/sign-in "
                f"class=fform>{hid}<input type=email name=email placeholder='Bambu account email' autocomplete=off required>"
                f"<input type=password name=password placeholder='Password (empty for Google or Apple)' autocomplete=off>"
                f"<button>Next</button></form></div>")
        badge = "<span class=pill-miss>Not signed in</span>"
    return f"<details class=key><summary><span>Bambu account (optional)</span>{badge}</summary><div class=kbody>{body}</div></details>"


def _iptv_addresses_html(p: dict[str, Any]) -> str:
    """Each address of a provider and what it last did. Shown once the provider has a backup address."""
    e = html.escape
    rows = p.get("addresses") or []
    if len(rows) < 2:
        return ""
    out = []
    for a in rows:
        kind = "Main address" if a["main"] else "Backup"
        if a["state"] == "down":
            pill = "<span class=pill-miss>Not answering</span>"
            line = f"{a.get('why') or 'not answering'} · since {_ago(a.get('since'))}; tried again within 15 min"
        elif a["state"] == "ok":
            pill = "<span class=pill-ok>Answering</span>"
            line = f"checked {_ago(a.get('at'))}"
        else:
            pill = "<span class=pill-miss>Not checked yet</span>"
            line = "checked within a few minutes"
        use = " · <b>in use</b>" if a["in_use"] else ""
        out.append(f"<div class=row><div><b>{e(kind)}</b> {pill}<div class=dim>{e(a['address'])}{use}</div>"
                   f"<div class=dim>{e(line)}</div></div></div>")
    return "".join(out)


def _iptv_providers_html(h: Hub, csrf: str) -> str:
    """One row per provider (name and address), with every username and password under it."""
    e = html.escape
    hid = f"<input type=hidden name=csrf value={csrf}>"
    try:
        usage = {(r["provider"], r["login"]): r for r in iptv_accounts(h)["logins"]}
    except Exception as exc:  # noqa: BLE001  the providers section shows without usage
        note("admin: IPTV logins in use", exc, log)
        usage = {}
    results = getattr(getattr(h, "keychecks", None), "results", None) or {}
    out = []
    for p in iptvplay.providers(h):
        pid = e(p["id"])
        rows = []
        for lg in p["logins"]:
            u = usage.get((lg["provider"], lg["login"])) or {}
            bits = []
            if u:
                bits.append(f"{u.get('active', 0)} of {u.get('max_connections', 1)} in use")
                if u.get("expires"):
                    bits.append(f"until {str(u['expires'])[:10]}")
                if u.get("status") not in (None, "ok", "full"):
                    bits.append(str(u["status"]))
            check = results.get(f"iptv:{lg['provider']}:{lg['login']}")
            pill = admin_health.key_pill(check) if check else ""
            if p["config"]:
                action = "<span class=dim>config file</span>"
            else:
                action = (f"<form method=post action=/admin/iptv/login/remove class=inline>{hid}"
                          f"<input type=hidden name=provider value='{pid}'><input type=hidden name=login value='{e(lg['login'])}'>"
                          f"<button class=ghost onclick=\"return confirm('Remove the login {e(lg['username'])}?')\">Remove</button></form>")
            rows.append(f"<div class=row><div><b>{e(lg['username'] or '?')}</b> {pill}"
                        f"<div class=dim>{e(' · '.join(bits) or 'Not used yet')}</div></div><div>{action}</div></div>")
        n = len(p["logins"])
        in_use = sum(int((usage.get((lg["provider"], lg["login"])) or {}).get("active") or 0) for lg in p["logins"])
        slots = sum(int((usage.get((lg["provider"], lg["login"])) or {}).get("max_connections") or 1) for lg in p["logins"])
        summary_pill = f"<span class=pill-ok>{n} {'login' if n == 1 else 'logins'} · {in_use} of {slots} in use</span>"
        role = ("The hub loads the guide, films and series from this provider." if p["main"]
                else "The TVs get this provider's channels and films as their own list.")
        addresses = _iptv_addresses_html(p)
        if any(a["state"] == "down" for a in p.get("addresses") or []):
            summary_pill = summary_pill.replace("<span class=pill-ok>", "<span class=pill-miss>", 1).replace(
                "</span>", " · an address is down</span>", 1)
        backups_text = e("\n".join(p.get("backups") or []))
        if p["config"]:
            body = f"<div class=dim>{e(p['server'])} · set in the hub's config file.</div>{addresses}{''.join(rows)}"
        else:
            merge = ""
            if p["same_as_main"]:
                main_name = next((q["name"] for q in iptvplay.providers(h) if q["main"]), "the main provider")
                merge = (f"<div class=dim style='margin-top:8px'>Same address as {e(main_name)}. Move these logins there so the hub "
                         f"can use them for the films and channels it already knows.</div>"
                         f"<form method=post action=/admin/iptv/provider/merge class=inline>{hid}"
                         f"<input type=hidden name=provider value='{pid}'><button>Move to {e(main_name)}</button></form>")
            body = f"""<div class=dim>{e(role)}</div>
<form method=post action=/admin/iptv/provider/save class=fform style="padding:8px 0">{hid}<input type=hidden name=provider value='{pid}'>
<input name=name value='{e(p['name'])}' placeholder="Name" maxlength=40 required>
<input name=server value='{e(p['server'])}' placeholder="Address (http://…:8080)" required>
<textarea name=backups rows=2 placeholder="Backup addresses, one per line (optional)" autocomplete=off spellcheck=false>{backups_text}</textarea>
<div class=dim style="flex-basis:100%">Other addresses this provider answers on with the same logins. When one is blocked or down, the hub,
the TVs and the phones move to the next one by themselves, and back when the main address answers again.</div>
<button class=ghost2>Save</button></form>{addresses}{merge}
{''.join(rows)}
<form method=post action=/admin/iptv/provider/login class=fform style="padding:10px 0">{hid}<input type=hidden name=provider value='{pid}'>
<input name=username placeholder="Username" required autocomplete=off>
<input type=password name=password placeholder="Password" autocomplete=off required>
<button>Add login</button></form>
<div class=dim>Type a username that's already listed to change its password. Passwords are saved and not shown again.</div>
<form method=post action=/admin/iptv/provider/remove class=inline>{hid}<input type=hidden name=provider value='{pid}'>
<button class=ghost onclick="return confirm('Remove {e(p['name'])} and all its logins?')">Remove provider</button></form>"""
        out.append(f"<details class=key id='iptv-{pid}'><summary><span>{e(p['name'])}</span>{summary_pill}</summary>"
                   f"<div class=kbody>{body}</div></details>")
    first = not any(p["main"] for p in iptvplay.providers(h))
    out.append(f"""<details class=key id=iptv-add{' open' if not out else ''}><summary><span>Add a provider</span>
<span class=pill-{'miss' if first else 'ok'}>{'None yet' if first else 'Optional'}</span></summary><div class=kbody>
<div class=dim>A provider is one IPTV service: its name and address, then every username and password you have with it.
To add another login to a provider above, open it instead. The hub spreads the TVs over the logins so none goes over its
connection limit, and moves a TV to the next login when a stream fails.</div>
<form method=post action=/admin/iptv/provider/add class=fform style="padding:10px 0">{hid}
<input name=name placeholder="Name, e.g. Strong" required maxlength=40>
<input name=server placeholder="Address (http://…:8080)" required>
<input name=username placeholder="First username" required autocomplete=off>
<input type=password name=password placeholder="Password" autocomplete=off required>
<textarea name=backups rows=2 placeholder="Backup addresses, one per line (optional)" autocomplete=off spellcheck=false></textarea>
<button>Add provider</button></form></div></details>""")
    return "".join(out)


# Which area of the admin suite each key section lives in; anything not listed is under Connections.
SECTION_AREA = {
    "Push notifications": "security", "Private VPN (PIA)": "security", "1Password": "security",
    "App updates (TestFlight)": "updates", "TV app (Thomas Creek TV)": "updates",
    "AI assistant (Claude API key)": "ai",
    "YouTube (optional key)": "watching", "Media": "watching", "IPTV providers": "watching",
    "Metadata sources (artwork, ratings, subtitles)": "watching",
}


def _sections_html(h: Hub, csrf: str, area: str | None = None) -> str:
    e = html.escape
    out = []
    for sec in SECTIONS:
        if not _section_on(h, sec):
            continue
        if area is not None and SECTION_AREA.get(sec["title"], "connections") != area:
            continue
        fields = "".join(_field_html(h, csrf, *f) for f in sec["fields"])
        if sec.get("service") == "printers":
            fields += _bambu_html(h, csrf)
        if sec.get("iptv"):
            fields = _iptv_providers_html(h, csrf) + fields
        opt = " <span class=dim>(optional)</span>" if sec.get("optional") else ""
        out.append(f"<h3>{e(sec['title'])}{opt}</h3><div class=card>{fields}</div>")
    # VPN: the Firewalla client files.
    if h.config.vpn.provider == "pool" and area in (None, "security"):
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
    if area not in (None, "connections"):
        return "".join(out)
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
    known = {f[0] for sec in SECTIONS for f in sec["fields"]} | NEVER_EDIT | BAMBU_KEYS | set(iptvplay.MAIN_SECRETS)
    login_secrets = {str(r.get("password_secret")) for r in (h.db.kv_get("iptv.logins") or []) if isinstance(r, dict)}
    extra = [n for n in sorted(h.vault.names()) if n not in known and n not in login_secrets
             and not n.startswith(h.config.vpn.pool_prefix)]
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


def _catalog_html(h: Hub, csrf: str) -> str:
    """One status line: the AI folds the TV provider's duplicate copies by itself, nothing to answer here."""
    stats = catalog_fold_stats(h.db)
    return (f"<p class=dim>TV catalog: the AI merged {stats['merged']} duplicate copies and kept {stats['apart']} "
            f"look-alikes separate. Nothing to do here.</p>")


def _app_update_html(h: Hub, csrf: str) -> str:
    """Latest app build, who is behind, and the "required" floor. Collapsed like the key rows."""
    e = html.escape
    au = h.app_update
    info = au.info()
    s = au.settings()
    latest = info["latest_build"]
    if latest:
        where = {"testflight": "from TestFlight", "phone": "seen on a phone", "upload": "from the Mac's upload"}.get(info["source"], "set by hand")
        head = (f"{e(info['latest_version'])} ({latest})" if info["latest_version"] else f"build {latest}") + f" {where}"
    else:
        head = "not known yet"
    if au.configured:
        check = (f"App Store Connect: {e(au.last_error)}" if au.last_error
                 else f"Checks App Store Connect every {h.config.app_update.check_minutes} min, last {_ago(au.last_check)}.")
    else:
        check = ("No App Store Connect key, so the hub learns new builds when the first phone installs one. "
                 "Add the key under Keys and files to hear the moment TestFlight has it, or type the build below.")
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


def _ai_keys_html(h: Hub, csrf: str, new_ai_key: str | None = None) -> str:
    """AI connections: keys that let an AI app (Claude and other MCP clients) control the house as one person."""
    e = html.escape
    names = {p["id"]: p for p in h.db.people()}
    rows = []
    for k in aikeys.keys(h):
        person = names.get(k["person_id"]) or {"name": "?"}
        how = "signed in from the app" if k.get("created_by") == k["person_id"] else "made here"
        rows.append(f"""<div class=row><div><b>{e(k['label'])}</b> <span class=dim>acts as {e(person['name'])} · {how}
{_ago(k['created_at'])} · last used {_ago(k.get('last_used_at'))}</span></div>
<form method=post action=/admin/ai-key-revoke><input type=hidden name=csrf value={csrf}><input type=hidden name=id value={e(k['id'])}>
<button class=ghost>Turn off</button></form></div>""")
    base = (h.config.hub.remote_url or "https://<hub address>").rstrip("/")
    shown = ""
    if new_ai_key:
        cmd = f'claude mcp add --transport http thomas-creek {base}/mcp --header "Authorization: Bearer {new_ai_key}"'
        shown = f"""<div class=flash style="margin:10px 0"><b>New AI key.</b> It is shown only this once. For Claude Code, paste this
into Terminal. Other AI apps take the address <code>{e(base)}/mcp</code> and the key as a Bearer token.
<pre id=akcmd style="white-space:pre-wrap;word-break:break-all;user-select:all">{e(cmd)}</pre>
<button type=button onclick="navigator.clipboard.writeText(document.getElementById('akcmd').textContent).then(()=>this.textContent='Copied')">Copy</button></div>"""
    opts = "".join(f"<option value={e(p['id'])}>{e(p['name'])} ({e(p['role'])})</option>"
                   for p in names.values() if p["role"] != "pending")
    state = "on" if h.config.mcp.enabled else "off"
    where = "from anywhere" if h.config.mcp.away else "home and VPN only"
    pill = f"<span class=pill-ok>{len(rows)} connected</span>" if rows else f"<span class=dim>{state}</span>"
    return f"""<div class=card style="margin-top:10px"><details class=key{' open' if new_ai_key else ''}><summary><span>AI connections (MCP)</span>{pill}</summary>
<div class=kbody><div class=dim>AI apps such as Claude control the house through <code>{e(base)}/mcp</code> ({state}, {where}), with the same
tools and the same limits as Ask Thomas Creek for the person they act as. On claude.ai, add a custom connector with that address and
sign in: the page shows a code to type in the app (Systems, Hub web page). Locks, the garage and deletes always wait for Face ID
on that person's iPhone. Turning a connection off stops it straight away.</div>{shown}
{''.join(rows)}
<form method=post action=/admin/ai-key class=fform style="margin:10px 0"><input type=hidden name=csrf value={csrf}>
<input name=label placeholder="Name, e.g. Claude Code on the Mac" maxlength=60 value="Claude Code">
<select name=person>{opts}</select><button>Make an AI key</button></form>
</div></details></div>"""


def _chip(level: str, text: str) -> str:
    return f"<span class='chip {html.escape(level)}'>{html.escape(text)}</span>"


def _needs_you(h: Hub) -> list[dict[str, str]]:
    """Everything that needs someone, worst first: TVs, then keys, services and phones waiting."""
    from .. import tvstate

    items: list[dict[str, str]] = []
    try:
        items.extend(tvstate.attention(h))
    except Exception as exc:  # noqa: BLE001 - the overview still opens
        log.warning("admin: TV states failed: %s", type(exc).__name__)
    links = {"#keys": "/admin/connections#keys", "#services": "/admin/connections#services"}
    for level, text, link in admin_health.attention(h):
        title, _, why = text.partition(": ")
        target = links.get(link, link)
        action = "Approve" if target == "/admin/people" else "Open"
        items.append({"level": level, "title": title, "why": why, "link": target, "action": action})
    items.sort(key=lambda i: 0 if i["level"] == "bad" else 1)
    return items


def _tiles(h: Hub) -> str:
    """One tile per area, each with a line and its state chips."""
    from .. import tvstate

    def tile(href: str, title: str, line: str, chips: list[str]) -> str:
        return (f"<a class=tile href={href}><b>{html.escape(title)}</b><span class=s>{html.escape(line)}</span>"
                f"<span class=chips>{''.join(chips)}</span></a>")

    out = []
    try:
        rows = tvstate.rows(h)
        n = {k: sum(1 for r in rows if r["state"] == k) for k in ("ok", "warn", "bad", "off")}
        playing = next((r for r in rows if r["playing"]), None)
        line = f"{len(rows)} TVs · {len(rows) - n['off']} on now"
        if playing:
            line += f" · {playing['name']}: {playing['playing']}"
        words = (("ok", "working"), ("bad", "broken"), ("warn", "need a look"), ("off", "off"))
        chips = [_chip(k, f"{n[k]} {w}") for k, w in words if n[k]]
        out.append(tile("/admin/devices", "TVs", line, chips or [_chip("off", "No TVs yet")]))
    except Exception:  # noqa: BLE001
        out.append(tile("/admin/devices", "TVs", "Couldn't read the TVs", []))
    people = len(h.db.people())
    devs = {r["status"]: r["n"] for r in h.db.all("SELECT status, COUNT(*) AS n FROM devices GROUP BY status")}
    chips = [_chip("ok", f"{devs.get('active', 0)} phones and iPads")]
    if devs.get("pending"):
        chips.insert(0, _chip("warn", f"{devs['pending']} waiting"))
    out.append(tile("/admin/people", "People", f"{people} people", chips))
    provs = iptvplay.providers(h)
    try:
        fold = catalog_fold_stats(h.db)
        cat = f" · AI merged {fold['merged']} copies"
    except Exception:  # noqa: BLE001
        cat = ""
    out.append(tile("/admin/watching", "Watching", f"{len(provs)} IPTV provider{'s' if len(provs) != 1 else ''}{cat}",
                    [_chip("ok" if provs else "warn",
                           "Main provider set" if any(p["main"] for p in provs) else "No provider yet")]))
    keys = [r for r in h.keychecks.summary() if r["ready"]]
    svcs = admin_health.service_rows(h)
    down = sum(1 for s in svcs if s.get("error"))
    kbad = sum(1 for r in keys if r["state"] == "bad")
    kwarn = sum(1 for r in keys if r["state"] in ("warn", "down"))
    chips = [_chip("ok", f"{len(svcs) - down} working")]
    if down or kbad:
        chips.append(_chip("bad", f"{down + kbad} broken"))
    if kwarn:
        chips.append(_chip("warn", f"{kwarn} to check"))
    out.append(tile("/admin/connections", "Connections", f"{len(svcs)} services · {len(keys)} keys tested", chips))
    try:
        b = h.brain.summary()
        line = f"House brain {b['status']} · ${b['total_usd']:.2f} this month"
        chips = [_chip("ok" if b["status"] == "on" else "bad" if b["status"] in ("capped", "needs_key") else "warn",
                       {"on": "On", "off": "Off", "paused": "Paused", "needs_key": "Needs the Claude key",
                        "capped": "Monthly cap reached"}.get(b["status"], b["status"]))]
    except Exception:  # noqa: BLE001
        line, chips = "House brain", []
    out.append(tile("/admin/ai", "AI", line, chips))
    info = h.app_update.info()
    latest_tv = (h.tv_app.latest() or {}).get("versionName")
    line = f"iPhone build {info['latest_build'] or '?'}" + (f" · TV {latest_tv}" if latest_tv else "")
    out.append(tile("/admin/updates", "App updates", line, []))
    out.append(tile("/admin/security", "Hub and security", "VPN, push, sign-ins, warnings",
                    [_chip("ok" if h.apns.enabled else "warn", "Push on" if h.apns.enabled else "Push off")]))
    return "".join(out)


def _overview_html(h: Hub, claims: dict[str, Any], flash: str | None) -> str:
    e = html.escape
    csrf = _csrf(claims)
    up = int(time.time() - h.started_at)
    items = _needs_you(h)
    bad = sum(1 for i in items if i["level"] == "bad")
    warn = len(items) - bad
    if not items:
        sentence, level = "Everything is working.", "ok"
    else:
        parts = []
        if bad:
            parts.append(f"{bad} thing{'s are' if bad != 1 else ' is'} broken")
        if warn:
            parts.append(f"{warn} need{'s' if warn == 1 else ''} a look")
        sentence, level = " and ".join(parts).capitalize() + ".", "bad" if bad else "warn"
    rows = "".join(
        f"<div class=item><span class='sev {e(i['level'])}'></span><div class=grow><div class=t>{e(i['title'])}</div>"
        f"<div class=s>{e(i['why'])}</div></div>"
        f"<a class='btn light small' href='{e(i['link'])}'>{e(i['action'])}</a></div>"
        for i in items)
    needs = (f"<h2>Needs you</h2><div class='card list'>{rows}</div>" if items else "")
    flash_html = f"<div class=flash>{e(flash)}</div>" if flash else ""
    word = "All good" if level == "ok" else "Needs you" if bad else "Check soon"
    return _page("Thomas Creek Hub", f"""
{_nav("/admin", h)}
<div class=head><div><h1>Thomas Creek Hub</h1><div class=dim>v{e(__version__)} · up {admin_health.span(up)} ·
{'practice mode' if h.config.hub.dry_run else 'live'}</div></div>
<form method=post action=/admin/logout><input type=hidden name=csrf value={csrf}>
<button class=light>Sign out</button></form></div>
{flash_html}
<div class='card health' style='margin-top:16px'>{_chip(level, word)}
<div style='flex:1;min-width:220px'><b>{e(sentence)}</b><span class=dim>Anything that needs you is listed first;
every area below opens on its own.</span></div>
<form method=post action=/admin/services/check><input type=hidden name=csrf value={csrf}>
<button class=light>Check every service now</button></form></div>
{needs}
<h2>Areas</h2><div class=tiles>{_tiles(h)}</div>
""")


AREA_TEXT = {
    "connections": ("Connections", "Every service the hub talks to, whether it answers, and the keys it uses. "
                    "Values are never shown again; saving replaces the old one right away and tests it."),
    "watching": ("Watching", "Where channels, films and artwork come from. "
                 "The hub reads every provider and spreads the TVs over the logins."),
    "ai": ("AI", "The house brain judges fit, writes the reasons on rows and sorts alerts. "
           "Every AI step has a plain fallback, so the home still fills without it."),
    "updates": ("App updates", "The iPhone app comes from TestFlight; the TV app is sent by the hub."),
    "security": ("Hub and security", "The hub itself, how phones reach it from away, push, and what it logged."),
}


def _area_html(h: Hub, claims: dict[str, Any], area: str, flash: str | None, new_key: str | None = None,
               new_ai_key: str | None = None) -> str:
    e = html.escape
    csrf = _csrf(claims)
    title, lede = AREA_TEXT[area]
    sections = _sections_html(h, csrf, area)
    body = ""
    if area == "connections":
        body = f"""<div id=services>{admin_health.services_html(h, csrf)}</div>
<h2 id=keys>Keys and files</h2>
{admin_health.keys_overview(h, csrf)}
<div class=card style="margin-top:10px"><details class=key><summary><span>Upload several files at once</span><span class=dim>VPN, push key, house model…</span></summary>
<div class=kbody><div class=dim>Choose any mix: the Firewalla VPN .conf files, the AuthKey_….p8 push key, house-model-web.json,
or text files named after a key (for example trek.api_key.txt). Each goes to the right place.</div>
<div class=factions><form method=post action=/admin/upload enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}><input type=file name=files multiple required><button>Upload</button></form></div></div></details></div>
{sections}"""
    elif area == "watching":
        body = f"""{_catalog_html(h, csrf)}
{sections}
<h3>Playlists</h3><div class=card><div class=row><div>Every provider side by side
<div class=dim>What each one carries, how they overlap, and which names the hub still shows with a provider tag</div></div>
<a class='btn light small' href=/admin/playlists>Open</a></div></div>
<h3>Downloads</h3><div class=card><div class=row><div>Kids' YouTube and saved films
<div class=dim>Queue, failures and space used</div></div>
<a class='btn light small' href=/admin/downloads>Open</a></div></div>
<h3>Cloud media</h3><div class=card><div class=row><div>Lighter streams for slow connections
<div class=dim>The worker on the cloud server: approve it, see how busy it is, switch it off</div></div>
<a class='btn light small' href=/admin/cloud>Open</a></div></div>"""
    elif area == "ai":
        body = f"""{admin_health.ai_html(h)}
{_ai_keys_html(h, csrf, new_ai_key)}
{sections}"""
    elif area == "updates":
        body = f"""{_app_update_html(h, csrf)}
<div class=card style="margin-top:10px"><div class=row><div>TV app<div class=dim>Newest build on the hub:
{e(str((h.tv_app.latest() or {}).get('versionName') or 'none yet'))}. Roll it out from the TVs page.</div></div>
<a class='btn light small' href=/admin/devices#rollout>Roll out</a></div></div>
{_test_keys_html(h, csrf, new_key)}
{sections}"""
    elif area == "security":
        push = "on" if h.apns.enabled else "off (add the Apple push key and its key ID below)"
        pool = h.vpn.pool_status() if h.config.vpn.provider == "pool" else []
        vpn = (f"{len(pool)} Firewalla clients, {sum(1 for p in pool if p['device_id'])} in use"
               if h.config.vpn.provider == "pool" else h.config.vpn.provider)
        devices = h.db.all("SELECT status, COUNT(*) AS n FROM devices GROUP BY status")
        dev = ", ".join(f"{r['n']} {r['status']}" for r in devices) or "none"
        pin = getattr(h, "_tls_pin", None)
        body = f"""<h2>Hub</h2><div class=card>
{admin_health.hub_rows(h)}
<div class=row><div>Push notifications</div><div class=dim>{e(push)}</div></div>
<div class=row><div>VPN</div><div class=dim>{e(vpn)}</div></div>
<div class=row><div>Private VPN (PIA)</div>
<div class=dim>{e(h.pia.status())} · {e(', '.join(h.config.vpn.pia.roles))}</div></div>
<div class=row><div>People and devices</div>
<div class=dim>{len(h.db.people())} people · {e(dev)} · <a href=/admin/people>Manage</a></div></div>
<div class=row><div>TLS pin</div><div class=dim><code>{e(pin or '')}</code></div></div>
<div class=row><div>This sign-in</div><form method=post action=/admin/logout>
<input type=hidden name=csrf value={csrf}><button class=ghost>Sign out</button></form></div></div>
{admin_health.speed_html(h)}
{admin_health.warnings_html(h)}
{sections}"""
    savebar = ""
    if "class=kin" in body:
        savebar = f"""<form id=saveall method=post action=/admin/secrets/bulk class=savebar>
<input type=hidden name=csrf value={csrf}>
<span id=savecount class=dim>Paste new values above, then save them together.</span>
<button type=button class=ghost id=discard hidden>Discard</button><button id=savebtn>Save all</button></form>
<script>{SAVE_JS}</script>"""
    flash_html = f"<div class=flash>{e(flash)}</div>" if flash else ""
    return _page(f"{title} · Thomas Creek Hub", f"""
{_nav('/admin/' + area, h)}
<h1>{e(title)}</h1><p class=lede>{e(lede)}</p>
{flash_html}
{body}
{savebar}
""")


# Marks changed key rows, counts them in the save bar, warns before leaving with unsaved values, and keeps
# the scroll position and open rows across the reload that follows any form on the page.
SAVE_JS = r"""
(function(){
const ins=[...document.querySelectorAll('input.kin')],bar=document.getElementById('saveall'),
cnt=document.getElementById('savecount'),dis=document.getElementById('discard');let sending=false;
function upd(){const ch=ins.filter(i=>i.value.trim());ins.forEach(i=>{i.closest('details').querySelector('.chg').hidden=!i.value.trim()});
bar.classList.toggle('dirty',ch.length>0);dis.hidden=!ch.length;
cnt.textContent=ch.length?(ch.length+(ch.length==1?' change: ':' changes: ')+ch.map(i=>i.dataset.key).join(', ')):'Paste new values above, then save them together.'}
ins.forEach(i=>i.addEventListener('input',upd));
dis.addEventListener('click',()=>{ins.forEach(i=>i.value='');upd()});
bar.addEventListener('submit',e=>{if(!ins.some(i=>i.value.trim())){e.preventDefault();return}sending=true});
window.addEventListener('beforeunload',e=>{if(!sending&&ins.some(i=>i.value.trim())){e.preventDefault();e.returnValue=''}});
const KEY='tc-admin-view';
document.querySelectorAll('form').forEach(f=>f.addEventListener('submit',()=>{
try{sessionStorage.setItem(KEY,JSON.stringify({y:scrollY,open:[...document.querySelectorAll('details')].map((d,i)=>d.open?(d.id||i):null).filter(x=>x!==null)}))}catch(e){}}));
try{const v=JSON.parse(sessionStorage.getItem(KEY)||'null');sessionStorage.removeItem(KEY);
if(v){const all=document.querySelectorAll('details');v.open.forEach(k=>{const d=typeof k=='number'?all[k]:document.getElementById(k);if(d)d.open=true});scrollTo(0,v.y)}}catch(e){}
upd()})();
"""


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
        keys = [{k: r.get(k) for k in ("id", "state", "detail", "expires", "at")} for r in h.keychecks.summary() if r["ready"]]
        return JSONResponse({"services": rows, "keys": keys, "version": __version__, "build": os.environ.get("HUB_BUILD"), "ffmpeg": ver,
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
    return HTMLResponse(_overview_html(h, claims, request.query_params.get("m")), headers=_headers())


async def _area(request: Request, h: Hub, area: str) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    return HTMLResponse(_area_html(h, claims, area, request.query_params.get("m")), headers=_headers())


@pages.get("/admin/connections")
async def admin_connections(request: Request, h: Hub = Depends(hub)) -> Response:
    return await _area(request, h, "connections")


@pages.get("/admin/watching")
async def admin_watching(request: Request, h: Hub = Depends(hub)) -> Response:
    return await _area(request, h, "watching")


@pages.get("/admin/ai")
async def admin_ai(request: Request, h: Hub = Depends(hub)) -> Response:
    return await _area(request, h, "ai")


@pages.get("/admin/updates")
async def admin_updates(request: Request, h: Hub = Depends(hub)) -> Response:
    return await _area(request, h, "updates")


@pages.get("/admin/security")
async def admin_security(request: Request, h: Hub = Depends(hub)) -> Response:
    return await _area(request, h, "security")


async def _form_session(request: Request, h: Hub) -> tuple[dict[str, Any], Any]:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        raise error(401, "unauthorized", "Sign in again")
    form = await request.form()
    # A form goes back to the area page it came from (same task, so _back sees it).
    from urllib.parse import urlparse

    ref = urlparse(request.headers.get("referer") or "").path
    _RETURN.set(ref if ref in AREA_PATHS else "/admin")
    if not secrets.compare_digest(str(form.get("csrf", "")), _csrf(claims)):
        raise error(403, "forbidden", "Reload the page and try again")
    return claims, form


def _back(msg: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"{_RETURN.get()}?m={quote(msg)}", status_code=303)


@pages.post("/admin/iptv/login")
async def add_iptv_login(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    provider = re.sub(r"\s+", " ", str(form.get("provider") or "")).strip()[:40]
    login = re.sub(r"\s+", " ", str(form.get("login") or "")).strip()[:40]
    server = str(form.get("server") or "").strip()
    if not server:
        # Another login with a provider already listed: reuse its server.
        server = next((lg["server"] for lg in iptv_logins(h) if lg["provider"].lower() == provider.lower()), "")
        provider = next((lg["provider"] for lg in iptv_logins(h) if lg["provider"].lower() == provider.lower()), provider)
    if not re.fullmatch(r"https?://[^\s/]+(:\d+)?(/\S*)?", server):
        return _back("Add the provider's server address, like http://example.tv:8080.")
    if any(c in provider + login for c in "./|"):
        return _back("Provider and login names can't contain . / or |.")
    saved = add_login(h, provider, server, login, str(form.get("username") or ""), str(form.get("password") or ""))
    if saved:
        h.db.log("admin.iptv_login", "ok", person_id=claims.get("per"), device_id=claims.get("dev"),
                 target=f"{provider}/{login}", source="web")
        if getattr(h, "tv_app", None):
            h.tv_app.bump_config()
        saved = f"Saved the {provider} login {login}."
    return _back(saved or "Fill in the provider, login name, username and password.")


@pages.post("/admin/iptv/login/remove")
async def remove_iptv_login(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    provider, login = str(form.get("provider") or ""), str(form.get("login") or "")
    msg = iptvplay.remove_provider_login(h, provider, login)
    if msg.startswith("Removed"):
        _iptv_changed(h, claims, "admin.iptv_login_remove", f"{provider}/{login}", list(iptvplay.MAIN_SECRETS))
    return _back(msg)


def _iptv_changed(h: Hub, claims: dict[str, Any], what: str, target: str, secrets_: list[str]) -> None:
    """A provider or login changed: log it (no secret), tell the TVs, and test the logins again."""
    h.db.log(what, "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=target, source="web")
    if getattr(h, "tv_app", None):
        h.tv_app.bump_config()
    if secrets_ and getattr(h, "keychecks", None):
        h.keychecks.after_save_many(secrets_)
    # Read the other providers' lists now, so their copies count for playback without waiting for the next refresh.
    catalog = getattr(h, "tv_catalog", None)
    if catalog is not None:
        try:
            asyncio.get_running_loop().create_task(catalog.refresh_extras())
        except RuntimeError:
            pass
    # Check every provider address now (backups included), so the page shows which ones answer.
    try:
        asyncio.get_running_loop().create_task(iptvaddr.check_all(h))
    except RuntimeError:
        pass


_SERVER_RE = re.compile(r"https?://[^\s/]+(:\d+)?(/\S*)?")


@pages.post("/admin/iptv/provider/add")
async def add_iptv_provider(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    name = re.sub(r"\s+", " ", str(form.get("name") or "")).strip()
    server = str(form.get("server") or "").strip()
    if not _SERVER_RE.fullmatch(server):
        return _back("Add the provider's address, like http://example.tv:8080.")
    backups, refused = iptvaddr.parse_list(form.get("backups"))
    if refused:
        return _back(f"{refused[0][:60]} isn't an address. Use one like http://example.tv:8080.")
    pid, msg = iptvplay.add_provider(h, name, server, str(form.get("username") or ""), str(form.get("password") or ""),
                                     backups)
    if pid:
        names = list(iptvplay.MAIN_SECRETS) if pid == iptvplay.MAIN else [f"iptv.{pid}.1.password"]
        _iptv_changed(h, claims, "admin.iptv_provider_add", pid, names)
    return _back(msg)


@pages.post("/admin/iptv/provider/save")
async def save_iptv_provider(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    provider = str(form.get("provider") or "")
    server = str(form.get("server") or "").strip()
    if server and not _SERVER_RE.fullmatch(server):
        return _back("The address should look like http://example.tv:8080.")
    backups, refused = iptvaddr.parse_list(form.get("backups"))
    if refused:
        return _back(f"{refused[0][:60]} isn't an address. Use one like http://example.tv:8080.")
    msg = iptvplay.update_provider(h, provider, re.sub(r"\s+", " ", str(form.get("name") or "")), server,
                                   backups if "backups" in form else None)
    if not msg:
        return _back("That provider can't be changed here.")
    secrets_ = [lg["password_secret"] for p in iptvplay.providers(h) if p["id"] == provider for lg in p["logins"]]
    _iptv_changed(h, claims, "admin.iptv_provider_save", provider, secrets_)
    return _back(msg)


@pages.post("/admin/iptv/provider/login")
async def add_iptv_provider_login(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    provider = str(form.get("provider") or "")
    secret, msg = iptvplay.add_provider_login(h, provider, str(form.get("username") or ""), str(form.get("password") or ""))
    if secret:
        _iptv_changed(h, claims, "admin.iptv_login", provider, [secret])
    return _back(msg)


@pages.post("/admin/iptv/provider/merge")
async def merge_iptv_provider(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    provider = str(form.get("provider") or "")
    moved = iptvplay.merge_into_main(h, provider)
    _iptv_changed(h, claims, "admin.iptv_provider_merge", provider, list(iptvplay.MAIN_SECRETS))
    return _back(f"Moved {moved} {'login' if moved == 1 else 'logins'}." if moved else "Nothing to move.")


@pages.post("/admin/iptv/provider/remove")
async def remove_iptv_provider(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    provider = str(form.get("provider") or "")
    msg = iptvplay.remove_provider(h, provider)
    if not msg:
        return _back("That provider can't be removed here.")
    _iptv_changed(h, claims, "admin.iptv_provider_remove", provider, [])
    return _back(msg)


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
    _after_saved(h, claims, list(dict.fromkeys([name, *saved])))
    label = next((f[1] for sec in SECTIONS for f in sec["fields"] if f[0] == name), " and ".join(saved))
    return _back(f"Saved {label}.")


def _after_saved(h: Hub, claims: dict[str, Any], names: list[str]) -> None:
    """Everything that follows saving keys: drop stale clients, log it, test each affected key once."""
    if any(n in APPSTORE_KEYS for n in names):
        h.app_update._jwt = None
        asyncio.create_task(h.app_update.check())
    if any(n.startswith(h.config.vpn.pool_prefix) for n in names):
        h.vpn._pool = None
    h.db.log("admin.secret_set", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=",".join(names),
             source="web")
    if h.config.onepassword.token_secret in names and h.onepassword.enabled:
        asyncio.create_task(h.onepassword.sync_safely())
    if any(n.startswith("iptv.") for n in names) and getattr(h, "tv_app", None):
        h.tv_app.bump_config()
    h.keychecks.after_save_many(names)


TEXT_FIELDS = {f[0]: f[1] for sec in SECTIONS for f in sec["fields"] if f[2] != "file"}


@pages.post("/admin/secrets/bulk")
async def set_secrets_bulk(request: Request, h: Hub = Depends(hub)) -> Response:
    """The "Save all" bar: every key row that has a new value, saved together, one reload."""
    claims, form = await _form_session(request, h)
    saved: list[str] = []
    for field, raw in form.multi_items():
        if not field.startswith("v:") or not isinstance(raw, str):
            continue
        name, value = field[2:], raw.strip()
        if not value or name not in TEXT_FIELDS or name in NEVER_EDIT or not NAME_RE.match(name):
            continue
        if len(value) > 64 * 1024:
            return _back(f"Nothing saved: {TEXT_FIELDS[name]} is too long for a key.")
        h.vault.set(name, value)
        saved.append(name)
    if not saved:
        return _back("Nothing to save: paste a new value into a key row first.")
    _after_saved(h, claims, saved)
    labels = [TEXT_FIELDS[n] for n in saved]
    return _back(f"Saved {len(saved)} {'key' if len(saved) == 1 else 'keys'}: {', '.join(labels)}. Testing them now.")


@pages.post("/admin/secrets/delete")
async def delete_secret(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    name = str(form.get("name", "")).strip()
    if name in NEVER_EDIT or not h.vault.delete(name):
        return _back(f"{name} was not removed.")
    if name.startswith(h.config.vpn.pool_prefix):
        h.vpn._pool = None
    from ..keycheck import forget_cached

    forget_cached(h, name)
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


@pages.post("/admin/bambu/cancel")
async def bambu_cancel(request: Request, h: Hub = Depends(hub)) -> Response:
    await _form_session(request, h)
    h.bambu_cloud.cancel()
    return _back("Bambu sign-in cancelled.")


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


@pages.post("/admin/bambu/retry")
async def bambu_retry(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, _ = await _form_session(request, h)
    try:
        await h.bambu_cloud.retry()
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
    claims, _ = await _form_session(request, h)
    _revoke(h, claims)
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
        h.keychecks.after_save("appstore.auth_key_p8")
        return "App Store Connect key"
    if m or "BEGIN PRIVATE KEY" in text and base.lower().endswith(".p8"):
        h.vault.set("apns.auth_key_p8", text.strip() + "\n")
        if m:
            h.vault.set("apns.key_id", m.group(1))
        h.keychecks.after_save("apns.auth_key_p8")
        return "push key"
    if base.lower().endswith(".conf") or "[Interface]" in text:
        return "VPN " + _store_vpn(h, base, text)
    name = re.sub(r"\.(txt|key|token)$", "", base)
    known = {f[0] for sec in SECTIONS for f in sec["fields"]} | set(_expected(h))
    if name in known and name not in NEVER_EDIT:
        value = text.strip()
        h.vault.set(name, value + ("\n" if "\n" in value else ""))
        h.keychecks.after_save(name)
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
    return HTMLResponse(_area_html(h, claims, "updates", "Made a test key.", new_key=key), headers=_headers())


@pages.post("/admin/ai-key")
async def make_ai_key(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    person = h.db.person(str(form.get("person") or ""))
    if person is None or person["role"] == "pending":
        return _back("Choose who the AI app acts as.")
    _, key = aikeys.create(h, person["id"], str(form.get("label") or ""), created_by=claims.get("per"))
    # Rendered straight into the reply (not a redirect) so the key never sits in a URL or the history.
    return HTMLResponse(_area_html(h, claims, "ai", f"Made an AI key for {person['name']}.", new_ai_key=key),
                        headers=_headers())


@pages.post("/admin/ai-key-revoke")
async def revoke_ai_key(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form_session(request, h)
    ok = aikeys.revoke(h, str(form.get("id") or ""), by=claims.get("per"))
    return _back("Turned the AI connection off." if ok else "That AI connection was already off.")


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


@pages.post("/admin/keys/test")
async def test_keys(request: Request, h: Hub = Depends(hub)) -> Response:
    """Try a saved key (or every key) against its service now. Says whether it works, never what it is."""
    claims, form = await _form_session(request, h)
    name = str(form.get("name", "")).strip()
    if name == "all":
        done = await h.keychecks.run()
        bad = [c.title for c, r in done if r.get("state") == "bad"]
        warn = [c.title for c, r in done if r.get("state") in ("warn", "down")]
        msg = f"Tested {len(done)} keys: {len(done) - len(bad) - len(warn)} work"
        msg += (f"; not working: {', '.join(bad)}" if bad else "") + (f"; to check: {', '.join(warn)}" if warn else "") + "."
    else:
        if not NAME_RE.match(name) or not h.vault.has(name):
            return _back("Nothing saved there to test.")
        done = await h.keychecks.run(secret=name)
        if not done:
            return _back("The hub has no way to test that one.")
        msg = " ".join(f"{c.title}: {r.get('detail') or r.get('state')}." for c, r in done)
    h.db.log("admin.key_test", "ok", person_id=claims.get("per"), device_id=claims.get("dev"), target=name, source="web")
    return _back(msg)


@pages.post("/admin/services/check")
async def check_services(request: Request, h: Hub = Depends(hub)) -> Response:
    await _form_session(request, h)
    try:
        await asyncio.wait_for(h.probe_once(), timeout=25)
    except asyncio.TimeoutError:
        pass
    down = [s["title"] for s in admin_health.service_rows(h) if s.get("error")]
    return _back(f"Checked every service. Down: {', '.join(down)}." if down else "Checked every service: all OK.")
