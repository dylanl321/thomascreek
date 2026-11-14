"""Pick a login that still has room, and move a failed stream to the next one.

Passwords stay in the vault. The TV asks with a canonical id and opens a hub
path. The provider address is only the redirect for that one TV.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .db import new_id
from .home import iso

log = logging.getLogger("hub.iptvplay")

KV_LOGINS = "iptv.logins"
KV_USAGE = "iptv.usage"
KV_SESSIONS = "iptv.sessions"
KV_NOTICES = "iptv.notices"
IDLE_S = 90
FAIL_S = 600
QUALITY = {"2160p": 0, "1080p": 1, "720p": 2, "sd": 3}


def accounts(h: Any, now: float | None = None) -> dict[str, Any]:
    """Which login is in use, how many slots are left, and when each one expires."""
    now = time.time() if now is None else now
    _release_idle(h, now)
    sessions = _sessions(h)
    usage = _usage(h)
    rows = []
    for login in logins(h):
        info = usage.get(_key(login)) or {}
        ours = sum(1 for row in sessions if row.get("provider") == login["provider"] and row.get("login") == login["login"])
        view = _view(login, info, ours, now)
        rows.append(view)
    rows.sort(key=lambda row: (row["provider"], row["login"]))
    playing = []
    names = {tv_id: (tv.get("name") or tv_id) for tv_id, tv in (h.tv_app.tvs() or {}).items()} if getattr(h, "tv_app", None) else {}
    for row in sessions:
        playing.append({
            "tv": row.get("tv"),
            "name": names.get(row.get("tv"), row.get("tv")),
            "provider": row.get("provider"),
            "login": row.get("login"),
            "canonical_id": row.get("canonical_id"),
            "since": iso(row.get("since")),
        })
    return {"logins": rows, "sessions": playing}


def add_login(h: Any, provider: str, server: str, login: str, username: str, password: str) -> str:
    """Save a login. The password is written to the vault and not returned."""
    provider, login = provider.strip(), login.strip()
    if not provider or not login or not username.strip() or not password:
        return ""
    secret = f"iptv.{provider}.{login}.password"
    h.vault.set(secret, password)
    rows = [row for row in _saved(h) if not (row.get("provider") == provider and row.get("login") == login)]
    rows.append({
        "provider": provider, "login": login, "server": server.strip().rstrip("/"),
        "username": username.strip(), "password_secret": secret,
    })
    h.db.kv_set(KV_LOGINS, rows)
    return "Saved the login."


def tune(h: Any, canonical_id: str, tv_id: str, *, avoid: set[str] | None = None, now: float | None = None) -> dict[str, Any]:
    """Give this TV one stream. The previous session for that TV is released."""
    now = time.time() if now is None else now
    _release_idle(h, now)
    item = _canonical(h, canonical_id)
    if item is None:
        return {"ok": False, "message": "That title isn't in the catalog."}
    _release_tv(h, tv_id)
    chosen = _pick(h, item, avoid or set(), now)
    if chosen is None:
        return {"ok": False, "message": "Every login is busy. Try again in a moment."}
    session = new_id("ses")
    row = {
        "session": session, "tv": tv_id, "provider": chosen["provider"], "login": chosen["login"],
        "canonical_id": canonical_id, "ref": chosen["ref"], "quality": chosen["quality"],
        "since": now, "seen": now,
    }
    sessions = _sessions(h)
    sessions.append(row)
    h.db.kv_set(KV_SESSIONS, sessions)
    stream = {"url": f"/v1/tv/stream/{session}", "ref": chosen["ref"], "quality": chosen["quality"]}
    event = {
        "install_id": tv_id, "session": session, "canonical_id": canonical_id, "stream": stream,
    }
    h.bus.publish({"type": "tv_tune", "data": event}, device_id=f"tv:{tv_id}")
    return {"ok": True, "session": session, "canonical_id": canonical_id, "stream": stream}


def fail(h: Any, session: str, tv_id: str, now: float | None = None) -> dict[str, Any]:
    """The picture did not start, or it dropped. Try the next login."""
    now = time.time() if now is None else now
    current = next((row for row in _sessions(h) if row.get("session") == session and row.get("tv") == tv_id), None)
    if current is None:
        return {"ok": False, "message": "That stream has ended."}
    _mark_failed(h, current, now)
    _drop(h, session)
    return tune(h, str(current.get("canonical_id") or ""), tv_id, avoid=_failed(h, now), now=now)


def release_tv(h: Any, tv_id: str) -> None:
    _release_tv(h, tv_id)


def touch(h: Any, tv_id: str, now: float | None = None) -> None:
    """A heartbeat keeps this TV's slot."""
    now = time.time() if now is None else now
    sessions = _sessions(h)
    changed = False
    for row in sessions:
        if row.get("tv") == tv_id:
            row["seen"] = now
            changed = True
    if changed:
        h.db.kv_set(KV_SESSIONS, sessions)


def target(h: Any, session: str, tv_id: str) -> str | None:
    """The provider address for this session. Callers must not log it."""
    row = next((item for item in _sessions(h) if item.get("session") == session and item.get("tv") == tv_id), None)
    if row is None:
        return None
    login = next((item for item in logins(h) if item["provider"] == row.get("provider") and item["login"] == row.get("login")), None)
    if login is None:
        return None
    password = h.vault.get(login["password_secret"]) if h.vault.has(login["password_secret"]) else None
    if not password:
        return None
    kind, _provider, stream_id = _split_ref(str(row.get("ref") or ""))
    folder = {"vod": "movie", "series": "series"}.get(kind, "live")
    ext = "mp4" if folder != "live" else "ts"
    return f"{login['server']}/{folder}/{quote(login['username'])}/{quote(str(password))}/{quote(stream_id)}.{ext}"


def note_usage(h: Any, provider: str, login: str, info: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Remember one player_api user_info. A login expiring soon tells an admin once."""
    now = time.time() if now is None else now
    usage = _usage(h)
    usage[_key({"provider": provider, "login": login})] = {
        "max_connections": _int(info.get("max_connections"), 1),
        "active_cons": _int(info.get("active_cons"), 0),
        "exp_date": str(info.get("exp_date") or ""),
        "status": str(info.get("status") or "Active"),
        "at": now,
    }
    h.db.kv_set(KV_USAGE, usage)
    _notice(h, provider, login, usage[_key({"provider": provider, "login": login})], now)
    ours = sum(1 for row in _sessions(h) if row.get("provider") == provider and row.get("login") == login)
    return _view({"provider": provider, "login": login}, usage[_key({"provider": provider, "login": login})], ours, now)


def logins(h: Any) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    cfg = getattr(h.config, "iptv", None)
    for provider in (getattr(cfg, "providers", None) or []):
        for login in provider.logins:
            found[(provider.id, login.id)] = {
                "provider": provider.id, "login": login.id, "server": provider.server.rstrip("/"),
                "username": login.username, "password_secret": login.password_secret,
                "name": provider.name or provider.id,
            }
    for row in _saved(h):
        if not isinstance(row, dict):
            continue
        found[(str(row.get("provider")), str(row.get("login")))] = {
            "provider": str(row.get("provider")), "login": str(row.get("login")),
            "server": str(row.get("server") or "").rstrip("/"), "username": str(row.get("username") or ""),
            "password_secret": str(row.get("password_secret") or ""),
            "name": str(row.get("provider")),
        }
    if not found and h.vault.has("iptv.xtream_server") and h.vault.has("iptv.xtream_username"):
        found[("thomas-creek", "main")] = {
            "provider": "thomas-creek", "login": "main",
            "server": str(h.vault.get("iptv.xtream_server") or "").rstrip("/"),
            "username": str(h.vault.get("iptv.xtream_username") or ""),
            "password_secret": "iptv.xtream_password", "name": "Thomas Creek",
        }
    return list(found.values())


def _pick(h: Any, item: dict[str, Any], avoid: set[str], now: float) -> dict[str, Any] | None:
    usage = _usage(h)
    sessions = _sessions(h)
    best = None
    best_key = None
    for source in item.get("sources") or []:
        for login in logins(h):
            if login["provider"] != source.get("provider"):
                continue
            mark = f"{login['provider']}|{login['login']}|{source.get('ref')}"
            if mark in avoid:
                continue
            info = usage.get(_key(login)) or {}
            ours = sum(1 for row in sessions if row.get("provider") == login["provider"] and row.get("login") == login["login"])
            view = _view(login, info, ours, now)
            if view["status"] != "ok":
                continue
            rank = (QUALITY.get(str(source.get("quality") or ""), 4), -view["slots_left"], ours, login["login"])
            if best_key is None or rank < best_key:
                best_key = rank
                best = {**login, "ref": source.get("ref"), "quality": source.get("quality")}
    return best


def _view(login: dict[str, Any], info: dict[str, Any], ours: int, now: float) -> dict[str, Any]:
    maximum = _int(info.get("max_connections"), 1)
    active = _int(info.get("active_cons"), 0)
    used = max(active, ours)
    expires, expired = _expires(info.get("exp_date"), now)
    raw = str(info.get("status") or "Active").lower()
    disabled = raw not in ("active", "1", "")
    if expired:
        status = "expired"
    elif disabled:
        status = "disabled"
    elif used >= maximum:
        status = "full"
    else:
        status = "ok"
    return {
        "provider": login["provider"], "login": login["login"], "max_connections": maximum,
        "active": used, "slots_left": max(0, maximum - used), "expires": expires, "status": status,
    }


def _expires(value: object, now: float) -> tuple[str | None, bool]:
    text = str(value or "")
    if text in ("", "0", "null", "None"):
        return None, False
    try:
        stamp = int(text)
    except ValueError:
        return None, False
    if stamp <= 0:
        return None, False
    day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
    return day, stamp < now


def _notice(h: Any, provider: str, login: str, info: dict[str, Any], now: float) -> None:
    expires, expired = _expires(info.get("exp_date"), now)
    raw = str(info.get("status") or "Active").lower()
    disabled = raw not in ("active", "1", "")
    soon = False
    if expires and not expired:
        day = datetime.fromisoformat(expires).date()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        soon = 0 <= (day - today).days <= 7
    if not soon and not disabled and not expired:
        return
    kind = "disabled" if disabled else "expired" if expired else "soon"
    token = f"{provider}|{login}|{kind}|{datetime.fromtimestamp(now, timezone.utc).date().isoformat()}"
    sent = h.db.kv_get(KV_NOTICES) or {}
    if not isinstance(sent, dict):
        sent = {}
    if sent.get(token):
        return
    sent[token] = True
    h.db.kv_set(KV_NOTICES, sent)
    if not getattr(h, "notify", None):
        return
    if kind == "soon":
        title, body = f"{provider} {login} expires {expires}", "Add another login when you can."
    elif kind == "expired":
        title, body = f"{provider} {login} has expired", "Playback will use the other logins."
    else:
        title, body = f"{provider} {login} was turned off", "Playback will use the other logins."
    try:
        import asyncio
        asyncio.get_running_loop().create_task(h.notify.send(
            title=title, body=body, cat="systems", level="fyi", roles=("admin",),
            group_key=f"iptv:{token}", ref="thomascreek://tv/accounts",
        ))
    except RuntimeError:
        pass


def _canonical(h: Any, canonical_id: str) -> dict[str, Any] | None:
    state = h.db.kv_get("tv.catalog.canonical") or {}
    for item in (state.get("items") or []):
        if isinstance(item, dict) and item.get("id") == canonical_id:
            return item
    return None


def _mark_failed(h: Any, row: dict[str, Any], now: float) -> None:
    fails = h.db.kv_get("iptv.fails") or {}
    if not isinstance(fails, dict):
        fails = {}
    fails[f"{row.get('provider')}|{row.get('login')}|{row.get('ref')}"] = now + FAIL_S
    h.db.kv_set("iptv.fails", fails)


def _failed(h: Any, now: float) -> set[str]:
    fails = h.db.kv_get("iptv.fails") or {}
    if not isinstance(fails, dict):
        return set()
    return {key for key, until in fails.items() if float(until or 0) > now}


def _release_idle(h: Any, now: float) -> None:
    sessions = [row for row in _sessions(h) if now - float(row.get("seen") or row.get("since") or 0) < IDLE_S]
    if len(sessions) != len(_sessions(h)):
        h.db.kv_set(KV_SESSIONS, sessions)


def _release_tv(h: Any, tv_id: str) -> None:
    h.db.kv_set(KV_SESSIONS, [row for row in _sessions(h) if row.get("tv") != tv_id])


def _drop(h: Any, session: str) -> None:
    h.db.kv_set(KV_SESSIONS, [row for row in _sessions(h) if row.get("session") != session])


def _sessions(h: Any) -> list[dict[str, Any]]:
    raw = h.db.kv_get(KV_SESSIONS) or []
    return list(raw) if isinstance(raw, list) else []


def _usage(h: Any) -> dict[str, Any]:
    raw = h.db.kv_get(KV_USAGE) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _saved(h: Any) -> list[dict[str, Any]]:
    raw = h.db.kv_get(KV_LOGINS) or []
    return list(raw) if isinstance(raw, list) else []


def _key(login: dict[str, Any]) -> str:
    return f"{login['provider']}|{login['login']}"


def _int(value: object, default: int) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _split_ref(ref: str) -> tuple[str, str, str]:
    parts = ref.split(":")
    if len(parts) < 3:
        return "live", "", ref
    return parts[0], parts[1], parts[-1]
