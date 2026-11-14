"""Downloads page, and the short address a TV types into Downloader."""

from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import tvbuilds
from ..state import Hub
from ..tvapp import TvAppError
from .admin_web import _csrf, _form_session, _headers, _home_only, _login_page, _nav, _page, _session
from .deps import caller_ip, hub

router = APIRouter(tags=["downloads"])


def _plain(sentence: str, status: int = 200) -> HTMLResponse:
    body = f"<h1>{html.escape(sentence)}</h1>"
    return HTMLResponse(_page("Thomas Creek", body), status_code=status, headers=_headers())


def _away(request: Request, h: Hub) -> HTMLResponse | None:
    if not h.is_home(caller_ip(request, h)):
        return _plain("This link only works at home.", 403)
    return None


@router.get("/tv")
async def tv_latest(request: Request, h: Hub = Depends(hub)) -> Response:
    """The address registered with Downloader. It always follows the newest build."""
    away = _away(request, h)
    if away is not None:
        return away
    tvbuilds.adopt_inbox(h.tv_app)
    rows = [row for row in tvbuilds.builds(h.tv_app) if row["latest"]]
    if not rows:
        return _plain("No builds yet.", 404)
    return RedirectResponse(f"/tv/{rows[0]['version_code']}", status_code=302)


@router.get("/tv/{version_code}")
async def tv_file(version_code: str, request: Request, h: Hub = Depends(hub)) -> Response:
    away = _away(request, h)
    if away is not None:
        return away
    if not version_code.isdigit():
        return _plain("That build is not here.", 404)
    path = h.tv_app.apk_path(int(version_code))
    if not path.is_file():
        return _plain("That build is not here.", 404)
    return FileResponse(path, media_type="application/vnd.android.package-archive",
                        filename=f"ThomasCreekTV-{version_code}.apk")


@router.get("/admin/downloads")
async def downloads_page(request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    return HTMLResponse(_downloads_page(h, claims, request, request.query_params.get("m")), headers=_headers())


@router.post("/admin/downloads/code")
async def save_code(request: Request, h: Hub = Depends(hub)) -> Response:
    claims, form = await _form(request, h)
    raw = str(form.get("downloader_code") or "").strip()
    if not raw:
        h.db.kv_set(tvbuilds.KV_CODE, "")
        return _back("Downloader code cleared. The address above still works.")
    try:
        code = tvbuilds.save_code(raw)
    except TvAppError as exc:
        return _back(exc.message)
    h.db.kv_set(tvbuilds.KV_CODE, code)
    return _back("Saved the Downloader code.")


@router.post("/admin/downloads/upload")
async def upload_build(request: Request, h: Hub = Depends(hub)) -> Response:
    _claims, form = await _form(request, h)
    apk = form.get("apk")
    info = form.get("info")
    apk_name = getattr(apk, "filename", "") or ""
    info_name = getattr(info, "filename", "") or ""
    if not apk_name:
        return _back("Choose the APK.")
    if not info_name:
        return _back("Add the update.json that came with the build.")
    raw = await apk.read()
    try:
        parsed = json.loads(await info.read())
    except (UnicodeError, ValueError, TypeError):
        return _back("That update.json isn't the one from the TV build.")
    if not isinstance(parsed, dict):
        return _back("That update.json isn't the one from the TV build.")
    try:
        kept = h.tv_app.store_build(raw, parsed, tag=str(parsed.get("notes") or "")[:80] or None)
    except TvAppError as exc:
        return _back(tvbuilds.friendly(exc.message))
    name = kept.get("versionName") or kept.get("versionCode")
    return _back(f"Saved {name}.")


async def _form(request: Request, h: Hub) -> tuple[dict[str, Any], Any]:
    return await _form_session(request, h)


def _back(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/downloads?m={quote(msg)}", status_code=303)


def _downloads_page(h: Hub, claims: dict[str, Any], request: Request, flash: str | None) -> str:
    e = html.escape
    csrf = _csrf(claims)
    note = tvbuilds.adopt_inbox(h.tv_app)
    rows = tvbuilds.builds(h.tv_app)
    if note and not flash:
        flash = note
    origin = str(request.base_url).rstrip("/")
    short = f"{origin}/tv"
    code = tvbuilds.downloader_code(h.config.tv_app.downloader_code, h.db.kv_get(tvbuilds.KV_CODE))
    number = (f"<div class=dim>Downloader code</div><div class=codebig>{e(code)}</div>"
              f"<p class=dim>Type this in Downloader. It always opens the latest build.</p>"
              if code else
              "<p class=dim>Register the address at <a href=https://go.aftvnews.com/>go.aftvnews.com</a>, "
              "then paste the number it gives you. The hub does not make one up.</p>")
    flash_html = f"<div class=flash>{e(flash)}</div>" if flash else ""
    latest = next((row for row in rows if row["latest"]), None)
    older = [row for row in rows if latest and row["version_code"] != latest["version_code"]]
    if latest is None:
        listing = """<div class=card style="padding:18px 16px"><h2 style="margin:0 0 6px">No builds yet</h2>
<p class=dim style="margin:0">A <code>tc-*</code> tag in the TV repo publishes ThomasCreekTV.apk and update.json.
With the GitHub token saved, the hub downloads that release. Or put both files in the hub's tvapp/inbox folder,
or upload them below.</p></div>"""
    else:
        listing = _build_card(h, latest, primary=True) + _older(h, older)
    return _page("Downloads", f"""
{_nav("/admin/downloads")}
<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">
<div><h1>Downloads</h1><div class=dim>Install Thomas Creek TV</div></div>
<form method=post action=/admin/logout><input type=hidden name=csrf value={csrf}><button class=ghost>Sign out</button></form></div>
{flash_html}
<div class=card style="padding:18px 16px;margin-top:14px"><div class=dl>
<div style="flex:1;min-width:240px"><div class=dim>On the TV, open Downloader and enter</div>
<div class=hero>{e(short)}</div>
{number}
<form method=post action=/admin/downloads/code class=fform>
<input type=hidden name=csrf value={csrf}>
<input name=downloader_code inputmode=numeric pattern="[0-9]*" placeholder="Downloader code" value="{e(code or '')}" style="width:180px">
<button>Save</button></form></div>
<div class=qr>{tvbuilds.qr_svg(short)}</div>
</div></div>
<h2>Thomas Creek TV</h2>
{listing}
<details class=key style="margin-top:18px"><summary>Upload a build</summary>
<div class=kbody><p class=dim>The same two files a <code>tc-*</code> tag publishes: ThomasCreekTV.apk and update.json.</p>
<form method=post action=/admin/downloads/upload enctype=multipart/form-data class=fform>
<input type=hidden name=csrf value={csrf}>
<input type=file name=apk accept=".apk,application/vnd.android.package-archive" required>
<input type=file name=info accept=".json,application/json" required>
<button>Upload</button></form></div></details>
""")


def _build_card(h: Hub, row: dict[str, Any], *, primary: bool) -> str:
    e = html.escape
    date = tvbuilds.when(row["at"], h.config.hub.timezone)
    title = e(row["version_name"])
    meta = (f"version {row['version_code']} · {e(date)} · {e(tvbuilds.size_label(row['size']))}"
            + (" · TVs install this one overnight" if primary else ""))
    pad = "padding:16px" if primary else "padding:8px 0"
    return f"""<div class=card style="{pad};margin-top:10px"><div class=row style="border:0;padding:0;align-items:flex-start">
<div style="min-width:0;flex:1"><div style="font-size:{'22px' if primary else '16px'};font-weight:600">{title}</div>
<div class=dim>{meta}</div>
<div class=dim style="margin-top:6px;word-break:break-all"><code>{e(row['sha256'])}</code></div></div>
<div><a class=btn href="{e(row['path'])}">Download</a></div></div></div>"""


def _older(h: Hub, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    body = "".join(_build_card(h, row, primary=False) for row in rows)
    label = "older build" if len(rows) == 1 else f"{len(rows)} older builds"
    return f"""<details style="margin-top:14px"><summary>{label}</summary>{body}</details>"""
