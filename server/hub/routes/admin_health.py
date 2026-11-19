"""The admin page's status parts: what needs attention, live service status, key checks, AI spend, the hub itself
and its recent warnings. Rendering only; the routes live in admin_web.py. Nothing here shows a saved value."""

from __future__ import annotations

import html
import os
import shutil
import time
from typing import Any

from .. import __version__
from ..recentlog import RECENT, redact
from ..state import Hub

e = html.escape

PILL = {"ok": ("ok", "Works"), "warn": ("warn", "Check soon"), "bad": ("miss", "Not working"),
        "down": ("warn", "Can't reach"), "skip": (None, None)}


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago"


def span(s: float) -> str:
    s = int(s)
    return f"{s // 60} min" if s < 3600 else f"{s // 3600} h {s % 3600 // 60} min" if s < 86400 else f"{s // 86400} d {s % 86400 // 3600} h"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# -- keys -------------------------------------------------------------------------------------------------------------
_RANK = {"bad": 0, "warn": 1, "down": 2, "ok": 3}


def key_state(h: Hub, name: str) -> dict[str, Any] | None:
    """The worst check result that covers this key, or None when no check applies / nothing has run yet."""
    best: dict[str, Any] | None = None
    for c, r in h.keychecks.for_secret(name):
        try:
            if not c.ready(h) or not r or r.get("state") not in _RANK:
                continue
        except Exception:  # noqa: BLE001
            continue
        if best is None or _RANK[r["state"]] < _RANK[best["state"]]:
            best = {**r, "title": c.title, "check": c.id}
    return best


def key_pill(r: dict[str, Any] | None) -> str:
    if not r:
        return "<span class=pill-ok>Saved</span>"
    cls, label = PILL.get(r["state"], (None, None))
    if not cls:
        return "<span class=pill-ok>Saved</span>"
    if r["state"] == "bad" and "expired" in (r.get("detail") or ""):
        label = "Expired"
    elif r["state"] == "warn" and r.get("expires"):
        label = "Expires " + time.strftime("%b %-d", time.localtime(r["expires"]))
    return f"<span class=pill-{cls}>{e(label)}</span>"


def key_line(r: dict[str, Any] | None) -> str:
    if not r:
        return "<div class=dim style='margin-top:6px'>Not tested yet.</div>"
    ms = f" · {r['ms']} ms" if r.get("ms") else ""
    return (f"<div class='kstate {e(r['state'])}' style='margin-top:6px'><span class=dot></span>"
            f"{e(r.get('detail') or r['state'])} <span class=dim>· tested {ago(r.get('at'))}{ms}</span></div>")


def test_button(csrf: str, name: str) -> str:
    return (f"<form method=post action=/admin/keys/test class=inline><input type=hidden name=csrf value={csrf}>"
            f"<input type=hidden name=name value='{e(name)}'><button class=ghost2>Test</button></form>")


def keys_overview(h: Hub, csrf: str) -> str:
    """One compact row per check, under the Keys heading."""
    rows = [r for r in h.keychecks.summary() if r["ready"]]
    if not rows:
        return ""
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("ok", "warn", "bad", "down")}
    untested = sum(1 for r in rows if not r["state"])
    bits = [f"{counts['ok']} working"] + [f"{n} {w}" for n, w in ((counts["bad"], "not working"), (counts["warn"], "to check"),
                                                                 (counts["down"], "unreachable"), (untested, "not tested")) if n]
    pill = ("<span class=pill-miss>" if counts["bad"] else "<span class=pill-warn>" if counts["warn"] or counts["down"]
            else "<span class=pill-ok>") + e(" · ".join(bits)) + "</span>"
    last = max((r["at"] or 0 for r in rows), default=0)
    body = "".join(
        f"<div class='row {_cls(r['state'])}'><div><span class=dot></span>{e(r['title'])}</div>"
        f"<div class='msg dim'>{e(r['detail'] or 'not tested yet')} · {ago(r['at'])}</div></div>" for r in rows)
    return f"""<div class=card><details class=key><summary><span>Key check</span>{pill}</summary>
<div class=kbody><div class=dim>Each saved key is tried against its service every 6 hours, right after it's saved, and when you
press Test. Last run {ago(last)}. A key that stops working sends you a notice.</div>
<div class=factions><form method=post action=/admin/keys/test><input type=hidden name=csrf value={csrf}>
<input type=hidden name=name value=all><button>Test all keys now</button></form></div>{body}</div></details></div>"""


def _cls(state: str | None) -> str:
    return {"ok": "ok", "bad": "bad", "warn": "warn", "down": "warn"}.get(state or "", "")


# -- services ---------------------------------------------------------------------------------------------------------
def service_rows(h: Hub) -> list[dict[str, Any]]:
    """Every enabled service with its live state, plus the hub's own connections."""
    out = []
    by_id = {a.name: a for a in h.adapters}
    for s in h.health():
        if not s["enabled"]:
            continue
        a = by_id.get(s["id"])
        out.append({**s, "ms": getattr(a, "last_ms", None), "checked": getattr(a, "last_checked", None),
                    "down_since": getattr(a, "down_since", None)})
    # Connections that aren't adapters.
    ws = h.ha_ws.health() if getattr(h.config.home_assistant, "websocket", False) and h.ha.enabled else None
    if ws is not None:
        out.append({"id": "ha_live", "title": "Home Assistant live updates", "enabled": True, "ok": ws["connected"],
                    "last_ok": ws.get("last_event"), "error": None if ws["connected"] else (ws.get("error") or "not connected"),
                    "note": f"connected {ago(ws.get('since'))}" if ws["connected"] else None})
    if h.apns.enabled:
        out.append({"id": "apns", "title": "Push notifications", "enabled": True, "ok": not h.apns.last_error,
                    "last_ok": None, "error": h.apns.last_error, "note": f"{h.apns.sent} sent since start"})
    if h.app_update.configured:
        out.append({"id": "app_update", "title": "TestFlight builds", "enabled": True, "ok": not h.app_update.last_error,
                    "last_ok": h.app_update.last_check, "error": h.app_update.last_error})
    if h.onepassword.enabled:
        rep = h.onepassword.last
        out.append({"id": "onepassword", "title": "1Password sync", "enabled": True, "ok": not h.onepassword.last_error,
                    "last_ok": None, "error": h.onepassword.last_error,
                    "note": rep.summary() if rep else "not synced yet"})
    if h.pia.cfg.enabled and h.pia.configured:
        out.append({"id": "pia", "title": "Private VPN (PIA)", "enabled": True, "ok": not h.pia.last_error,
                    "last_ok": h.pia.last_ok, "error": h.pia.last_error})
    return out


def services_html(h: Hub, csrf: str) -> str:
    rows = service_rows(h)
    bad = [r for r in rows if r.get("error")]
    html_rows = []
    for s in sorted(rows, key=lambda r: (not r.get("error"), r["title"])):
        if s.get("error"):
            since = f"down {span(time.time() - s['down_since'])} · " if s.get("down_since") else ""
            msg = f"{since}{e(s['error'])}"
            cls = "bad"
        elif s["ok"]:
            bits = ["OK"] + ([f"{s['ms']} ms"] if s.get("ms") else []) + ([e(s["note"])] if s.get("note") else [])
            msg = " · ".join(bits)
            cls = "ok"
        else:
            msg, cls = e(s.get("note") or "not checked yet"), ""
        when = s.get("checked") or s.get("last_ok")
        html_rows.append(f"<div class='row {cls}'><div><span class=dot></span>{e(s['title'])}</div>"
                         f"<div class='msg dim' style='text-align:right'>{msg} · {ago(when)}</div></div>")
    pill = (f"<span class=pill-miss>{len(bad)} down</span>" if bad else f"<span class=pill-ok>All {len(rows)} OK</span>")
    return f"""<h2 style="display:flex;justify-content:space-between;align-items:center">Services {pill}</h2>
<div class=card>{''.join(html_rows) or '<div class=row>No services set up</div>'}
<div class=row><div class=dim>Each service is asked every 2 minutes.</div><form method=post action=/admin/services/check>
<input type=hidden name=csrf value={csrf}><button class=ghost2>Check now</button></form></div></div>"""


# -- needs attention --------------------------------------------------------------------------------------------------
def attention(h: Hub) -> list[tuple[str, str, str]]:
    """(level, text, link) for things worth fixing: broken or expiring keys, missing keys, services down, phones waiting."""
    out: list[tuple[str, str, str]] = []
    for r in h.keychecks.summary():
        if r["ready"] and r["state"] == "bad":
            out.append(("bad", f"{r['title']}: {r['detail']}", "#keys"))
        elif r["ready"] and r["state"] == "warn":
            out.append(("warn", f"{r['title']}: {r['detail']}", "#keys"))
    for a in h.adapters:
        names = [n for n in (getattr(a.cfg, "token_secret", None), getattr(a.cfg, "password_secret", None)) if n]
        if a.name in ("frigate", "family_board"):
            continue  # both work without a key
        if a.enabled and names and not any(h.vault.has(n) for n in names):
            out.append(("warn", f"{a.title} is on but its key isn't saved", "#keys"))
    if (h.config.assistant.enabled or h.config.brain.enabled) and not h.vault.has(h.config.brain.key_secret):
        out.append(("warn", "No Claude API key: the assistant and house brain are off", "#keys"))
    if not h.apns.enabled:
        out.append(("warn", "Push notifications are off: add the Apple push key", "#keys"))
    for s in service_rows(h):
        if s.get("error") and not any(s["title"] in t for _, t, _ in out):
            out.append(("bad", f"{s['title']} is down: {s['error']}", "#services"))
    waiting = h.db.one("SELECT COUNT(*) AS n FROM devices WHERE status='pending'")["n"]  # type: ignore[index]
    if waiting:
        out.append(("warn", f"{waiting} device{'s' if waiting != 1 else ''} waiting for you to say whose", "/admin/people"))
    return out


def attention_html(h: Hub) -> str:
    items = attention(h)
    if not items:
        return "<div class=card><div class='row ok'><div><span class=dot></span>Nothing needs attention</div></div></div>"
    rows = "".join(f"<div class='row {'bad' if lvl == 'bad' else 'warn'}'><div><span class=dot></span>{e(t)}</div>"
                   f"<a href='{e(link)}' class=dim>Open</a></div>" for lvl, t, link in items)
    return f"<h2>Needs attention</h2><div class=card>{rows}</div>"


# -- hub, AI, warnings ------------------------------------------------------------------------------------------------
def hub_rows(h: Hub) -> str:
    from ..config import data_dir

    build = os.environ.get("HUB_BUILD") or ""
    started = time.strftime("%b %-d, %-I:%M %p", time.localtime(h.started_at))
    rows = [("Version", f"v{__version__}" + (f" · build {build[:7]}" if build else "") + f" · started {started}")]
    try:
        d = data_dir()
        du = shutil.disk_usage(d)
        db = d / "hub.db"
        rows.append(("Storage", f"{size(du.free)} free of {size(du.total)}"
                     + (f" · database {size(db.stat().st_size)}" if db.is_file() else "")))
    except OSError:
        pass
    return "".join(f"<div class=row><div>{e(k)}</div><div class=dim>{e(v)}</div></div>" for k, v in rows)


def speed_html(h: Hub) -> str:
    """The slowest routes over the last hour and how busy the NAS is: route templates and numbers only."""
    t, host = getattr(h, "timings", None), getattr(h, "host_load", None)
    if t is None:
        return ""
    every = t.summary(window_s=3600)
    hh = host.hour() if host is not None else {}
    load = "NAS load: measuring"
    if hh:
        load = (f"NAS CPU {hh['busy_avg_pct']:.0f}% busy on average (peak {hh['busy_max_pct']:.0f}%), "
                f"{hh['iowait_avg_pct']:.0f}% waiting on disk; the hub uses {hh['hub_cpu_avg_pct']:.0f}% "
                f"of one thread (peak {hh['hub_cpu_max_pct']:.0f}%), last {hh['minutes']} min")
    lines = []
    for r in every[:12]:
        slow = f" · <b>{r['over_1s']} over 1 s</b>" if r["over_1s"] else ""
        lines.append(f"<div class=row><div><code>{e(r['method'])} {e(r['route'])}</code></div>"
                     f"<div class=dim style='white-space:nowrap'>p50 {r['p50_ms']:.0f} ms · p95 {r['p95_ms']:.0f} ms"
                     f" · max {r['max_ms']:.0f} ms · {r['n']} calls{slow}</div></div>")
    n_slow = sum(r["over_1s"] for r in every)
    pill = f"<span class=pill-{'warn' if n_slow else 'ok'}>{n_slow} answers over 1 s</span>"
    empty = "<div class=row><div class=dim>No calls yet</div></div>"
    return f"""<div class=card style="margin-top:10px"><details class=key>
<summary><span>Speed · last hour</span>{pill}</summary>
<div class=kbody><div class=dim>{e(load)}. Time until each answer starts, measured on the hub.</div>
{''.join(lines) or empty}</div></details></div>"""


def ai_html(h: Hub) -> str:
    try:
        s = h.brain.summary()
        runs = h.brain.runs(limit=8)
    except Exception as exc:  # noqa: BLE001
        return f"<div class=card><div class=row><div>AI</div><div class=dim>{e(type(exc).__name__)}</div></div></div>"
    status = {"on": "on", "off": "off", "paused": "paused", "needs_key": "needs the Claude key", "capped": "stopped: monthly cap reached"}
    cap = f" of ${s['cap_usd']:.2f} cap" if s.get("cap_usd") else ""
    pill_cls = "miss" if s["status"] in ("capped", "needs_key") else "ok" if s["status"] == "on" else "warn"
    pill = f"<span class=pill-{pill_cls}>${s['total_usd']:.2f} this month{e(cap)}</span>"
    rows = "".join(
        f"<div class=row><div>{e(r['job_title'])} <span class=dim>{e(r.get('headline') or r.get('action') or '')}</span></div>"
        f"<div class=dim style='white-space:nowrap'>{e((r.get('model') or '—').replace('claude-', ''))} · "
        f"${(r.get('cost_usd') or 0):.3f} · {ago(r.get('ts'))}</div></div>" for r in runs)
    return f"""<div class=card style="margin-top:10px"><details class=key><summary><span>AI · house brain {e(status.get(s['status'], s['status']))}</span>{pill}</summary>
<div class=kbody><div class=dim>House brain ${s['spend_usd']:.2f} over {s['runs']} runs, Ask Thomas Creek ${s['assistant_usd']:.2f} over
{s['assistant_runs']} questions ({e(s['month'])}). Models: {e(s['models']['cheap'])} and {e(s['models']['smart'])}.
{s['cards']} cards shown, {s['used']} used.</div>{rows or '<div class=row><div class=dim>No runs yet</div></div>'}</div></details></div>"""


def warnings_html(h: Hub) -> str:
    rows = RECENT.latest(25)
    if not rows:
        return ""
    values = []
    for n in h.vault.names():
        try:
            v = h.vault.get(n)
            if v:
                values.extend(x.strip() for x in str(v).splitlines() if len(x.strip()) >= 8)
        except Exception:  # noqa: BLE001
            continue
    body = "".join(
        f"<div class=row style='align-items:flex-start'><div class=dim style='white-space:nowrap'>{ago(r['ts'])}</div>"
        f"<div style='flex:1'><code>{e(r['name'])}</code> {e(redact(r['msg'], values))}</div></div>" for r in rows)
    errs = sum(1 for r in rows if r["level"] in ("ERROR", "CRITICAL"))
    pill = f"<span class=pill-{'miss' if errs else 'warn'}>{len(rows)}{' · ' + str(errs) + ' errors' if errs else ''}</span>"
    return f"""<div class=card style="margin-top:10px"><details class=key><summary><span>Recent warnings</span>{pill}</summary>
<div class=kbody><div class=dim>The hub's latest warnings and errors since it started (newest first).</div>{body}</div></details></div>"""
