"""The Playlists page of the hub web page: every provider side by side, and what the hub's cleaners make of them.

Read-only. It draws the report `hub/playlists.py` counted at the end of the last provider read (never the lists
themselves), so it opens at once however big the library is. Hiding, renaming and regrouping come later, as
rules; this page is where a grown-up sees what there is to fix first.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from .. import iptvplay
from ..state import Hub
from .admin_health import ago
from .admin_web import _headers, _home_only, _login_page, _nav, _page, _session
from .deps import hub

pages = APIRouter(tags=["admin-web"], include_in_schema=False)

KIND_WORDS = {"live": "Channels", "films": "Films", "series": "Series"}
MAX_TAG_ROWS = 30
MAX_CATEGORY_ROWS = 60


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _n(value: Any) -> str:
    return f"{_int(value):,}"


def _share(part: Any, whole: Any) -> str:
    """"1,812 (10%)", or "none" when the provider's list doesn't carry the field at all."""
    have, total = _int(part), _int(whole)
    if not total or not have:
        return "<span class=s>none</span>"
    return f"{have:,} <span class=s>({round(have * 100 / total)}%)</span>"


def _with(block: dict[str, Any], field: str) -> str:
    return _share((block.get("with") or {}).get(field), block.get("rows"))


def _table(head: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p class=dim>Nothing to show.</p>"
    ths = "".join(f"<th>{h}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return ("<div class=card style='overflow-x:auto'><table class=tvs>"
            f"<thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table></div>")


def _providers(report: dict[str, Any], names: dict[str, str]) -> str:
    e = html.escape
    rows = []
    for p in report.get("providers") or []:
        live, films, series = p.get("live") or {}, p.get("films") or {}, p.get("series") or {}
        name = e(names.get(p["id"], p["id"])) + (" <span class=tag>Main</span>" if p.get("main") else "")
        noise = f"{_n(live.get('events'))} events, {_n(live.get('dividers'))} headers"
        rows.append([
            f"<b>{name}</b><div class=s>read {ago(p.get('fetched_at'))}</div>",
            f"{_n(live.get('rows'))}<div class=s>{noise}</div>",
            _with(live, "guide_id"),
            _with(live, "programmes"),
            f"{_n(films.get('rows'))}<div class=s>{_with(films, 'tmdb')} with a TMDB id</div>",
            f"{_n(series.get('rows'))}<div class=s>{_with(series, 'tmdb')} with a TMDB id</div>",
            _share(sum(_int(((p.get(k) or {}).get("with") or {}).get("category")) for k in KIND_WORDS),
                   sum(_int((p.get(k) or {}).get("rows")) for k in KIND_WORDS)),
        ])
    return _table(["Provider", "Channels", "With a guide id", "With programmes", "Films", "Series",
                   "Rows with a category"], rows)


def _cleaning(report: dict[str, Any], names: dict[str, str]) -> str:
    e = html.escape
    rows = []
    for p in report.get("providers") or []:
        cells = [f"<b>{e(names.get(p['id'], p['id']))}</b>"]
        for kind in KIND_WORDS:
            block = p.get(kind) or {}
            cells.append(_share(block.get("tag_left"), block.get("rows")))
        for kind in ("films", "series"):
            block = p.get(kind) or {}
            cells.append(_share(block.get("cleaners_differ"), block.get("rows")))
        rows.append(cells)
    return _table(["Provider", "Channels shown with a tag", "Films shown with a tag", "Series shown with a tag",
                   "Films named differently for matching", "Series named differently for matching"], rows)


def _overlap(report: dict[str, Any], names: dict[str, str]) -> str:
    e = html.escape
    out = []
    for kind, word in KIND_WORDS.items():
        rows = []
        for row in (report.get("overlap") or {}).get(kind) or []:
            who = " + ".join(e(names.get(pid, pid)) for pid in row.get("providers") or [])
            only = " only" if len(row.get("providers") or []) == 1 else ""
            rows.append([f"{who}{only}", _n(row.get("count"))])
        by = "by folded name" if kind == "live" else "by TMDB id (rows without one aren't counted)"
        out.append(f"<h3>{word} <span class=dim style='font-weight:400'>{by}</span></h3>"
                   + _table(["Carried by", word], rows))
    return "".join(out)


def _tags(report: dict[str, Any], names: dict[str, str]) -> str:
    e = html.escape
    out = []
    for kind, word in KIND_WORDS.items():
        rows = []
        for row in ((report.get("tags") or {}).get(kind) or [])[:MAX_TAG_ROWS]:
            left = sum(int(v) for v in (row.get("left") or {}).values())
            total = sum(int(v) for v in (row.get("rows") or {}).values())
            where = ", ".join(f"{e(names.get(pid, pid))} {_n(n)}"
                              for pid, n in sorted((row.get("rows") or {}).items()))
            if left * 2 > total:
                state = "<span class=tag style='white-space:nowrap'>Left in names</span>"
            else:
                state = "Cleaned" if not left else "Partly cleaned"
            rows.append([f"<b>{e(str(row.get('tag')))}</b>", _n(total), _n(left), state,
                         f"<span class=s>{where}</span>", f"<span class=s>{e(str(row.get('example') or ''))}</span>"])
        count = len((report.get("tags") or {}).get(kind) or [])
        out.append(f"<details class=key{' open' if kind == 'films' else ''}><summary><span>{word}</span>"
                   f"<span class=dim>{count} tags</span></summary><div class=kbody>"
                   + _table(["Tag", "Rows", "Still shown", "", "Where", "Example"], rows) + "</div></details>")
    return "<div class=card>" + "".join(out) + "</div>"


def _categories(report: dict[str, Any], names: dict[str, str]) -> str:
    e = html.escape
    out = []
    for pid, kinds in (report.get("categories") or {}).items():
        for kind, word in KIND_WORDS.items():
            table = (kinds or {}).get(kind) or []
            if not table:
                continue
            rows = [[e(str(r.get("raw"))), f"<b>{e(str(r.get('label')))}</b>", e(str(r.get("country") or "")),
                     _n(r.get("rows"))] for r in table[:MAX_CATEGORY_ROWS]]
            extra = len(table) - MAX_CATEGORY_ROWS
            more = f"<p class=dim>And {extra} smaller ones.</p>" if extra > 0 else ""
            out.append(f"<details class=key><summary><span>{e(names.get(pid, pid))}: {word.lower()}</span>"
                       f"<span class=dim>{len(table)} categories</span></summary><div class=kbody>"
                       + _table(["As the provider writes it", "Shown as", "Country", "Rows"], rows) + more
                       + "</div></details>")
    if not out:
        return "<p class=dim>No categories stored yet.</p>"
    return "<div class=card>" + "".join(out) + "</div>"


def _examples(report: dict[str, Any], names: dict[str, str]) -> str:
    e = html.escape
    rows = [[e(names.get(x.get("provider", ""), x.get("provider", ""))), e(x.get("raw", "")),
             f"<b>{e(x.get('shown', ''))}</b>", e(x.get("folded", ""))] for x in (report.get("examples") or [])[:16]]
    return _table(["Provider", "As the provider writes it", "Shown as", "Matched as"], rows)


def _body(h: Hub, building: bool) -> str:
    report = h.tv_catalog.playlist_report()
    names = {p["id"]: p["name"] for p in iptvplay.providers(h)}
    if not report:
        wait = ("The hub counts the playlists in the background, about a minute after a provider is first read. "
                "Reload the page shortly. With no provider added yet under Watching there is nothing to count.")
        return f"<div class=card style='padding:18px 16px'><p class=dim style='margin:0'>{wait}</p></div>"
    note = " A newer count is being made now; reload in a minute." if building else ""
    took = html.escape(str(report.get("took_s")))
    return f"""<p class=dim>Counted {ago(report.get('built_at'))} in {took} s, from the lists the hub last read.
Nothing on this page changes what the TVs or phones show.{note}</p>
<h2>Providers</h2>{_providers(report, names)}
<h2>How well names are cleaned</h2>
<p class=dim>A tag is what a provider writes in front of a name ("EN - ", "[US] ", "TOP | "). "Shown with a tag"
counts rows where it is still there after the hub cleaned the name. "Named differently for matching" counts titles
where the name used to match copies together isn't the name on the card.</p>{_cleaning(report, names)}
<h2>Tags</h2><p class=dim>The tags left in the most names come first.</p>{_tags(report, names)}
<h2>Who carries what</h2>{_overlap(report, names)}
<h2>Categories</h2>{_categories(report, names)}
<h2>Shown name and matched name</h2>{_examples(report, names)}"""


@pages.get("/admin/playlists")
async def playlists_page(request: Request, h: Hub = Depends(hub)) -> Response:
    _home_only(request, h)
    claims = _session(request, h)
    if not claims:
        return HTMLResponse(_login_page(), headers=_headers())
    building = h.tv_catalog.playlist_report_soon()
    return HTMLResponse(_page("Playlists", f"""
{_nav("/admin/playlists", h)}
<h1>Playlists</h1><div class=dim>Every provider side by side, and what the hub makes of their names</div>
{_body(h, building)}"""), headers=_headers())
