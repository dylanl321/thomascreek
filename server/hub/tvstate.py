"""One plain answer per TV for the admin pages: is it working, and if not, why and what to do.

A TV reaches the hub three ways, and today's trouble was that the page mixed them up:

- **App**: the Thomas Creek TV app's heartbeat (every 30 s while open) and token refresh.
- **Device report**: the app's background report with CPU, memory and its app list (every 15 min, Android only).
- **Network debugging**: the hub's own adb link to the box, which sets the home screen and installs builds
  (Android only, and only for boxes the hub was given).

A TV is "off" only when the app and the device report are both quiet. The home screen (launcher) is compared
with what the TV last said it can open, so "you asked for six apps, the TV shows one" comes with the reason.
Everything here reads hub state only; nothing is sent to a TV.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import tvlauncher, tvupdate
from .tvfleet import ONLINE_S, REPORT_ONLINE_S, _devices as fleet_rows, _health as fleet_health

# How each state reads on the page, worst first.
RANK = {"bad": 0, "warn": 1, "off": 2, "ok": 3}

# Friendly names for apps a TV hasn't listed (it lists only what is installed).
APP_NAMES = {
    "com.netflix.ninja": "Netflix",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvkids": "YouTube Kids",
    "com.disney.disneyplus": "Disney+",
    "com.plexapp.android": "Plex",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.hulu.livingroomplus": "Hulu",
    "com.pbs.kids.pbskidsvideo": "PBS Kids",
    "com.spotify.tv.android": "Spotify",
    "com.espn.score_center": "ESPN",
    "com.wbd.stream": "Max",
    "com.peacocktv.peacockandroid": "Peacock",
    "com.cbs.ott": "Paramount+",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "com.google.android.youtube.tvunplugged": "YouTube TV",
}


def ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    s = int(now - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def is_apple(tv: dict[str, Any], report: dict[str, Any]) -> bool:
    if str(tv.get("platform") or "").lower() in ("appletv", "tvos"):
        return True
    raw = tv.get("health")
    health = raw if isinstance(raw, dict) else {}
    if str(health.get("platform") or "").lower() in ("appletv", "tvos"):
        return True
    model = str(tv.get("model") or report.get("model") or "").lower().replace(" ", "")
    return model.startswith("appletv")


def app_name(pkg: str, installed: dict[str, str]) -> str:
    return installed.get(pkg) or APP_NAMES.get(pkg) or pkg


def home_screen(tv: dict[str, Any]) -> dict[str, Any]:
    """The launcher as asked, next to what the TV can actually show, with the reason when they differ."""
    block = tvlauncher.view(tv)
    listed = tvlauncher.installed(tv)
    installed = {a["package"]: a["label"] for a in listed}
    known = bool(listed)
    mode = "tv_own" if not block["on"] else "only" if block["guard"] else "all"
    asked = list(block["apps"])
    shows = [p for p in asked if p in installed] if known else list(asked)
    missing = [p for p in asked if p not in installed] if known else []
    applied = tvlauncher.status(tv)
    out: dict[str, Any] = {
        "mode": mode, "person_id": block["person_id"], "asked": asked, "shows": shows, "missing": missing,
        "installed": listed, "known": known, "applied": applied,
        "listed_at": tv.get("launchable_at"),
    }
    names = [app_name(p, installed) for p in missing]
    if mode == "tv_own":
        out.update(state="ok", line="The TV's own home screen", why=None)
    elif missing:
        joined = ", ".join(names)
        verb = "isn't" if len(missing) == 1 else "aren't"
        if mode == "only":
            why = (f"{joined} {verb} installed on this TV, so the home row "
                   f"leaves {'it' if len(missing) == 1 else 'them'} out, "
                   f"and \"Only these apps\" stops the TV opening anything else.")
            if not shows:
                why += " Right now the TV can open nothing but Thomas Creek."
        else:
            why = (f"{joined} {verb} installed on this TV, so the home row "
                   f"leaves {'it' if len(missing) == 1 else 'them'} out.")
        out.update(state="bad" if mode == "only" else "warn",
                   line=f"Shows {len(shows)} of {len(asked)} picked apps", why=why)
    elif applied and applied.get("ok") is False and applied.get("adb"):
        out.update(state="warn", line="Not set on the TV yet",
                   why=str(applied.get("note") or "The TV hasn't taken it yet."))
    elif not known:
        out.update(state="ok", line=f"{len(asked)} apps" if asked else "Every app",
                   why="The TV hasn't listed its apps yet, so the hub can't check them. Open Thomas Creek TV on it "
                       "once.")
    else:
        out.update(state="ok", line=f"{len(shows)} apps" if asked else "Every app", why=None)
    return out


def _signal(state: str, text: str) -> dict[str, str]:
    return {"state": state, "text": text}


def signals(h: Any, tv: dict[str, Any], report: dict[str, Any], apple: bool, now: float) -> dict[str, dict[str, str]]:
    last_app = max(float(tv.get("state_at") or 0), float(tv.get("last_seen_at") or 0))
    if last_app and now - last_app < ONLINE_S:
        app = _signal("ok", "Open now" if tv.get("foreground", True) else "Running in the background")
    elif last_app and now - last_app < REPORT_ONLINE_S:
        app = _signal("ok", f"Checked in {ago(last_app, now)}")
    else:
        app = _signal("off", f"Quiet since {ago(last_app, now)}" if last_app else "Never checked in")
    if apple:
        na = _signal("na", "Not on Apple TV")
        return {"app": app, "report": na, "link": na}
    seen = float(report.get("seen") or 0)
    if seen and now - seen < REPORT_ONLINE_S:
        rep = _signal("ok", f"Reported {ago(seen, now)}")
    elif seen:
        rep = _signal("off", f"Last report {ago(seen, now)}")
    else:
        rep = _signal("warn", "Never reported. This app build may be too old to send device reports.")
    ip = tv.get("ip")
    box = (h.tv_app.boxes() or {}).get(ip) if ip else None
    if box is None:
        link = _signal("warn", "Not added. The hub can't set the home screen or install updates by itself.")
    else:
        st = str(box.get("state") or "")
        link = {
            "ok": _signal("ok", f"Connected, checked {ago(box.get('checked_at'), now)}"),
            "needs_allow": _signal("warn", "Waiting for someone to press Allow on the TV"),
            "no_adb": _signal("off", "This hub has no adb, so it can't reach any TV this way"),
        }.get(st, _signal("warn", "Network debugging is off or the TV is asleep"))
    return {"app": app, "report": rep, "link": link}


def _playing(tv: dict[str, Any]) -> str | None:
    p = tv.get("playing")
    if not isinstance(p, dict):
        return None
    return str(p.get("title") or p.get("name") or p.get("channel_name") or p.get("channel_key") or "Something") or None


def _tc(value: Any) -> int | None:
    m = re.search(r"tc\.(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def behind(h: Any, tv: dict[str, Any], apple: bool) -> int:
    """How many Thomas Creek builds (tc.N) this TV is behind the newest one on the hub; 0 when unknown."""
    if apple:
        return 0
    latest = h.tv_app.latest() or {}
    newest = _tc(latest.get("versionName"))
    have = _tc(tv.get("app_version"))
    if newest is None or have is None:
        newest, have = tvupdate._number(str(latest.get("versionCode") or "")), tvupdate._number(tv.get("app_version"))
    if not newest or have is None or have >= newest:
        return 0
    return newest - have


def row(h: Any, install_id: str, tv: dict[str, Any], report: dict[str, Any], now: float) -> dict[str, Any]:
    apple = is_apple(tv, report)
    sig = signals(h, tv, report, apple, now)
    home = None if apple else home_screen(tv)
    late = behind(h, tv, apple)
    playing = _playing(tv)
    last = max(float(tv.get("state_at") or 0), float(tv.get("last_seen_at") or 0), float(report.get("seen") or 0))
    health = fleet_health(h, report, last, now) if report else "ok"
    on = sig["app"]["state"] == "ok" or sig["report"]["state"] == "ok"
    if not on:
        state, label = "off", "Off or asleep"
        why = f"Nothing from this TV since {ago(last, now)}." if last else "This TV has never checked in."
    elif home and home["state"] == "bad":
        state, label, why = "bad", "Home screen differs", home["why"]
    elif home and home["state"] == "warn":
        state, label, why = "warn", "Home screen differs", home["why"]
    elif health in ("hot", "storage", "battery"):
        state, label = "warn", {"hot": "Running hot", "storage": "Storage nearly full",
                                "battery": "Remote battery low"}[health]
        why = {"hot": "The box is warmer than the limit set on the hub.",
               "storage": "Less than a tenth of its storage is free.",
               "battery": "The remote's battery is low."}[health]
    elif not apple and sig["link"]["state"] == "warn":
        state, label, why = "warn", "Limited control", sig["link"]["text"]
    elif late:
        state, label = "warn", "Update waiting"
        why = f"{late} build{'s' if late != 1 else ''} behind the newest one on the hub."
    else:
        state, label, why = "ok", "Working", None
    model = tv.get("model") or report.get("model")
    return {
        "id": install_id, "name": tv.get("name") or "TV", "room": tv.get("room"), "model": model,
        "platform": "Apple TV" if apple else "Android TV", "apple": apple, "profile": tv.get("profile") or "adult",
        "version": tv.get("app_version") or report.get("app_version"), "behind": late,
        "state": state, "label": label, "why": why, "signals": sig, "home": home,
        "playing": playing, "foreground": bool(tv.get("foreground", True)), "last": last, "last_text": ago(last, now),
        "capability": report.get("capability") or "basic",
    }


def rows(h: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Every approved TV, worst state first, then by name."""
    now = time.time() if now is None else now
    reports = fleet_rows(h)
    out = [row(h, i, tv, reports.get(i) or {}, now) for i, tv in (h.tv_app.tvs() or {}).items()
           if tv.get("status") == "approved"]
    out.sort(key=lambda r: (RANK.get(r["state"], 9), str(r["name"]).lower()))
    return out


def one(h: Any, install_id: str, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    tv = (h.tv_app.tvs() or {}).get(install_id)
    if not tv or tv.get("status") != "approved":
        return None
    return row(h, install_id, tv, fleet_rows(h).get(install_id) or {}, now)


def attention(h: Any, now: float | None = None) -> list[dict[str, str]]:
    """TVs that need someone, for the Overview's "Needs you" list."""
    out = []
    for r in rows(h, now):
        if r["state"] in ("bad", "warn"):
            tab = "home" if r["label"] == "Home screen differs" else "status"
            out.append({"level": r["state"], "title": f"{r['name']}: {r['label'].lower()}", "why": r["why"] or "",
                        "link": f"/admin/devices/{r['id']}?tab={tab}",
                        "action": "Fix the home screen" if tab == "home" else "See why"})
    return out
