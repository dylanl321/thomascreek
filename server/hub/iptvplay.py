"""Pick a login that still has room, and move a failed stream to the next one.

Passwords stay in the vault. The TV asks with a canonical id and opens a hub
path. The provider address is only the redirect for that one TV.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from . import iptvaddr
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


MAIN = "thomas-creek"
KV_NAMES = "iptv.provider_names"
MAIN_SECRETS = ("iptv.xtream_server", "iptv.xtream_username", "iptv.xtream_password")
_BAD_ID = re.compile(r"[./|:\s]+")


def _main_saved(h: Any) -> bool:
    return all(h.vault.has(n) for n in MAIN_SECRETS)


def provider_names(h: Any) -> dict[str, str]:
    raw = h.db.kv_get(KV_NAMES) or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def providers(h: Any) -> list[dict[str, Any]]:
    """Every provider with its name, address and logins: the first (main) provider first.

    The main provider is the one the hub's guide, films and series are loaded from. Its first login
    is the original iptv.xtream_* vault login, shown here like any other login."""
    names = provider_names(h)
    from_config = {p.id for p in (getattr(getattr(h.config, "iptv", None), "providers", None) or [])}
    out: dict[str, dict[str, Any]] = {}
    if _main_saved(h):
        out[MAIN] = {"id": MAIN, "name": names.get(MAIN) or "Main provider", "main": True, "config": False,
                     "server": str(h.vault.get("iptv.xtream_server") or "").rstrip("/"),
                     "logins": [{"provider": MAIN, "login": "main", "username": str(h.vault.get("iptv.xtream_username") or ""),
                                 "password_secret": "iptv.xtream_password", "first": True}]}
    for lg in logins(h):
        if lg["provider"] == MAIN and (lg["login"] == "main" or (MAIN in out and lg["username"] == out[MAIN]["logins"][0]["username"])):
            continue
        p = out.setdefault(lg["provider"], {
            "id": lg["provider"], "name": names.get(lg["provider"]) or lg.get("name") or lg["provider"],
            "main": lg["provider"] == MAIN, "config": lg["provider"] in from_config, "server": lg.get("primary") or lg["server"],
            "logins": []})
        p["logins"].append({"provider": lg["provider"], "login": lg["login"], "username": lg["username"],
                            "password_secret": lg["password_secret"], "first": False})
    rows = sorted(out.values(), key=lambda p: (not p["main"], p["name"].lower()))
    main_server = out.get(MAIN, {}).get("server")
    for p in rows:
        p["same_as_main"] = bool(not p["main"] and main_server and p["server"] == main_server)
        p["backups"] = iptvaddr.backups(h.db, p["id"])
        p["addresses"] = iptvaddr.status(h.db, p["id"], p["server"])
    return rows


def _next_login_id(h: Any, provider: str) -> str:
    taken = {lg["login"] for lg in logins(h) if lg["provider"] == provider}
    n = 2 if provider == MAIN else 1
    while str(n) in taken:
        n += 1
    return str(n)


def add_provider(h: Any, name: str, server: str, username: str, password: str,
                 backups: list[str] | None = None) -> tuple[str, str]:
    """A new provider with its first login. Returns (provider id, message); the id is "" when refused.

    With no main provider yet, this one becomes the main provider (the original vault keys), so the
    hub's guide, films and series load from it."""
    name, server, username = name.strip()[:40], server.strip().rstrip("/"), username.strip()
    if not name or not username or not password:
        return "", "Fill in the provider name, address, username and password."
    if not _main_saved(h):
        h.vault.set("iptv.xtream_server", server)
        h.vault.set("iptv.xtream_username", username)
        h.vault.set("iptv.xtream_password", password)
        names = provider_names(h)
        names[MAIN] = name
        h.db.kv_set(KV_NAMES, names)
        if backups:
            iptvaddr.set_backups(h.db, MAIN, backups, server)
        return MAIN, f"Added {name}."
    pid = _BAD_ID.sub("-", name).strip("-")[:40]
    taken = {p["id"].lower() for p in providers(h)} | {p["name"].lower() for p in providers(h)}
    if not pid or pid.lower() in taken or name.lower() in taken:
        return "", f"{name} is already listed. Open it and add the login there."
    add_login(h, pid, server, _next_login_id(h, pid), username, password)
    names = provider_names(h)
    names[pid] = name
    h.db.kv_set(KV_NAMES, names)
    if backups:
        iptvaddr.set_backups(h.db, pid, backups, server)
    return pid, f"Added {name}."


def add_provider_login(h: Any, provider: str, username: str, password: str) -> tuple[str, str]:
    """Another username and password for a listed provider. The same username again changes its password.
    Returns (vault secret written, message); the secret is "" when refused."""
    username = username.strip()
    p = next((p for p in providers(h) if p["id"] == provider), None)
    if p is None:
        return "", "That provider isn't listed."
    if p["config"]:
        return "", "That provider comes from the hub's config file."
    if not username or not password:
        return "", "Type the username and the password."
    for lg in p["logins"]:
        if lg["username"] == username:
            h.vault.set(lg["password_secret"], password)
            return lg["password_secret"], f"Changed the password for {username}."
    login = _next_login_id(h, provider)
    add_login(h, provider, p["server"], login, username, password)
    return f"iptv.{provider}.{login}.password", f"Added {username} to {p['name']}."


def update_provider(h: Any, provider: str, name: str, server: str, backups: list[str] | None = None) -> str:
    """Rename a provider or change its address; every login of that provider follows. `backups` (when given)
    replaces the provider's backup addresses: the same logins on other domains, tried when one is blocked."""
    name, server = name.strip()[:40], server.strip().rstrip("/")
    p = next((p for p in providers(h) if p["id"] == provider), None)
    if p is None or p["config"]:
        return ""
    if name:
        names = provider_names(h)
        names[provider] = name
        h.db.kv_set(KV_NAMES, names)
    if server and server != p["server"]:
        if p["main"]:
            h.vault.set("iptv.xtream_server", server)
        rows = _saved(h)
        for row in rows:
            if row.get("provider") == provider:
                row["server"] = server
        h.db.kv_set(KV_LOGINS, rows)
    if backups is not None:
        iptvaddr.set_backups(h.db, provider, backups, server or p["server"])
    return f"Saved {name or p['name']}."


def merge_into_main(h: Any, provider: str) -> int:
    """Move a provider's saved logins under the main provider (same service typed under another name).
    Returns how many logins moved."""
    rows = _saved(h)
    moving = [r for r in rows if r.get("provider") == provider]
    if provider == MAIN or not moving or not _main_saved(h):
        return 0
    main_server = str(h.vault.get("iptv.xtream_server") or "").rstrip("/")
    main_users = {str(h.vault.get("iptv.xtream_username") or "")} | {
        str(r.get("username")) for r in rows if r.get("provider") == MAIN}
    keep = [r for r in rows if r.get("provider") != provider]
    moved = 0
    for r in moving:
        if str(r.get("username")) in main_users:
            if r.get("password_secret"):
                h.vault.delete(str(r["password_secret"]))
            continue
        h.db.kv_set(KV_LOGINS, keep)
        r = {**r, "provider": MAIN, "login": _next_login_id(h, MAIN), "server": main_server}
        keep.append(r)
        main_users.add(str(r.get("username")))
        moved += 1
    h.db.kv_set(KV_LOGINS, keep)
    names = provider_names(h)
    if names.pop(provider, None) is not None:
        h.db.kv_set(KV_NAMES, names)
    # The moved provider's other addresses serve the same logins: keep them as the main provider's backups.
    extra = [str(r.get("server") or "") for r in moving] + iptvaddr.backups(h.db, provider)
    iptvaddr.set_backups(h.db, MAIN, iptvaddr.backups(h.db, MAIN) + extra, main_server)
    iptvaddr.forget(h.db, provider)
    return moved


def remove_provider_login(h: Any, provider: str, login: str) -> str:
    """Forget one login. The main provider's first login is replaced by its next one, so the guide,
    films and series keep loading; the provider's only login can't be removed that way."""
    if provider == MAIN and login == "main":
        rows = _saved(h)
        nxt = next((r for r in sorted(rows, key=lambda r: str(r.get("login"))) if r.get("provider") == MAIN), None)
        if nxt is None:
            return "That is the provider's only login. Add another one first, or remove the whole provider."
        h.vault.set("iptv.xtream_username", str(nxt.get("username") or ""))
        h.vault.set("iptv.xtream_password", str(h.vault.get(str(nxt.get("password_secret"))) or ""))
        remove_login(h, MAIN, str(nxt.get("login")))
        return "Removed the login."
    return "Removed the login." if remove_login(h, provider, login) else "That login isn't saved on the hub."


def remove_provider(h: Any, provider: str) -> str:
    p = next((p for p in providers(h) if p["id"] == provider), None)
    if p is None or p["config"]:
        return ""
    for lg in p["logins"]:
        if not lg["first"]:
            remove_login(h, provider, lg["login"])
    if p["main"]:
        for n in MAIN_SECRETS:
            h.vault.delete(n)
    names = provider_names(h)
    if names.pop(provider, None) is not None:
        h.db.kv_set(KV_NAMES, names)
    iptvaddr.forget(h.db, provider)
    return f"Removed {p['name']}."


def remove_login(h: Any, provider: str, login: str) -> bool:
    """Forget a login saved on the admin page, and its password. Logins from the config file stay."""
    rows = _saved(h)
    keep = [row for row in rows if not (row.get("provider") == provider and row.get("login") == login)]
    if len(keep) == len(rows):
        return False
    for row in rows:
        if row not in keep and row.get("password_secret"):
            h.vault.delete(str(row["password_secret"]))
    h.db.kv_set(KV_LOGINS, keep)
    return True


def tune(h: Any, canonical_id: str, tv_id: str, *, avoid: set[str] | None = None, now: float | None = None) -> dict[str, Any]:
    """Give this TV one stream. The previous session for that TV is released."""
    now = time.time() if now is None else now
    _release_idle(h, now)
    item = _canonical(h, canonical_id) or _episode(h, canonical_id)
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


def lease(h: Any, ref: str, holder: str, *, avoid: set[str] | None = None,
          now: float | None = None) -> dict[str, Any] | None:
    """Reserve one login for `holder` (a TV's install id, or `phone:<device id>`) to play `ref`.

    One login carries one stream, so every player in the house takes its login from this one pool: a TV through
    /v1/tv/tune, a phone through /v1/tv/phone/play (straight from the provider or relayed by the hub). The lease
    lives while the holder shows it is playing (a TV's heartbeat with something playing, a phone's /hold or the
    relayed bytes themselves) and is freed IDLE_S after that stops, or at once on stop. A holder has one lease:
    a new one replaces it. None when every login that carries this title is busy."""
    now = time.time() if now is None else now
    _release_idle(h, now)
    _kind, provider, _stream = _split_ref(ref)
    item = _holding(h, ref) or {"id": ref, "sources": [{"provider": provider, "ref": ref, "quality": None}]}
    _release_tv(h, holder)
    chosen = _pick(h, item, avoid if avoid is not None else _failed(h, now), now)
    if chosen is None:
        return None
    row = {"session": new_id("ses"), "tv": holder, "provider": chosen["provider"], "login": chosen["login"],
           "canonical_id": item.get("id"), "ref": chosen["ref"], "quality": chosen["quality"],
           "since": now, "seen": now}
    sessions = _sessions(h)
    sessions.append(row)
    h.db.kv_set(KV_SESSIONS, sessions)
    return row


def direct(h: Any, ref: str, holder: str, *, now: float | None = None) -> str | None:
    """The provider address itself, for a travel phone that plays straight from the provider (Dylan, Nov 2026).
    Live channels are HLS (`.m3u8`). Callers must not log the answer: it carries the login."""
    found = direct_all(h, ref, holder, now=now)
    return found[0] if found else None


def direct_all(h: Any, ref: str, holder: str, *, now: float | None = None) -> list[str]:
    """`direct`, then the same stream on each of the provider's backup addresses (same login), best first.
    The phone tries them in order when one doesn't play. Empty when every login is busy. Never logged."""
    row = lease(h, ref, holder, now=now)
    if row is None:
        return []
    out: list[str] = []
    for server in [None, *addresses_for(h, row["session"], holder)[1:]]:
        url = target(h, row["session"], holder, server=server)
        if url and url.endswith(".ts") and "/live/" in url:
            url = url[:-3] + ".m3u8"
        if url and url not in out:
            out.append(url)
    return out


def held_by(h: Any, holder: str) -> dict[str, Any] | None:
    """The lease `holder` has now (its provider and login), or None."""
    return next((row for row in _sessions(h) if row.get("tv") == holder), None)


def addresses_for(h: Any, session: str, holder: str) -> list[str]:
    """Every address of the provider this session plays from, best first."""
    row = next((item for item in _sessions(h) if item.get("session") == session and item.get("tv") == holder), None)
    login = _login_of(h, row) if row else None
    return list(login.get("servers") or [login["server"]]) if login else []


def _login_of(h: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in logins(h) if item["provider"] == row.get("provider") and item["login"] == row.get("login")), None)


def credentials(h: Any, provider: str, login: str) -> tuple[str, str, str] | None:
    """Server, username and password of one login in the pool. Never logged."""
    found = next((row for row in logins(h) if row["provider"] == provider and row["login"] == login), None)
    if found is None or not h.vault.has(found["password_secret"]):
        return None
    password = str(h.vault.get(found["password_secret"]) or "")
    return (found["server"], found["username"], password) if password else None


def _holding(h: Any, ref: str) -> dict[str, Any] | None:
    """The folded title (several copies of one film) this provider ref belongs to."""
    from .iptvcanon import index

    return index(h.db).by_ref.get(ref)


async def fail_checked(h: Any, session: str, tv_id: str, now: float | None = None) -> dict[str, Any]:
    """`fail`, first asking whether the provider address itself stopped answering (a blocked domain).

    When the provider has backup addresses and the one this stream used doesn't answer, that address is marked
    down for the house and the title plays again on the next address, on the same login. Otherwise the login
    is moved on as before."""
    now = time.time() if now is None else now
    current = next((row for row in _sessions(h) if row.get("session") == session and row.get("tv") == tv_id), None)
    login = _login_of(h, current) if current else None
    used = str((current or {}).get("server") or "")
    if current is not None and login is not None and used and len(login.get("servers") or []) > 1:
        secret = login.get("password_secret") or ""
        password = str(h.vault.get(secret) or "") if secret and h.vault.has(secret) else ""
        transport = getattr(getattr(h, "tv_catalog", None), "_transport", None)
        ok, why = await iptvaddr.check(used, login["username"], password, transport)
        iptvaddr.note(h.db, login["provider"], used, ok, "" if ok else why, now)
        if not ok:
            _drop(h, session)
            return tune(h, str(current.get("canonical_id") or ""), tv_id, avoid=_failed(h, now), now=now)
    return fail(h, session, tv_id, now)


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


def target(h: Any, session: str, tv_id: str, *, server: str | None = None) -> str | None:
    """The provider address for this session. Callers must not log it.

    Built on the provider's best address now (see hub/iptvaddr.py), which the session remembers so a failure
    report can check that address. `server` builds it on another of the provider's addresses instead."""
    sessions = _sessions(h)
    row = next((item for item in sessions if item.get("session") == session and item.get("tv") == tv_id), None)
    if row is None:
        return None
    login = _login_of(h, row)
    if login is None:
        return None
    base = login["server"] if server is None else server
    if server is None and row.get("server") != base:
        row["server"] = base
        h.db.kv_set(KV_SESSIONS, sessions)
    password = h.vault.get(login["password_secret"]) if h.vault.has(login["password_secret"]) else None
    if not password:
        return None
    ref = str(row.get("ref") or "")
    kind, _provider, stream_id = _split_ref(ref)
    folder = {"vod": "movie", "series": "series", "ep": "series"}.get(kind, "live")
    ext = "ts" if folder == "live" else _file_type(h, kind, ref)
    return f"{base}/{folder}/{quote(login['username'])}/{quote(str(password))}/{quote(stream_id)}.{ext}"


def _file_type(h: Any, kind: str, ref: str) -> str:
    """The provider's own file type for a film or episode (mkv, avi, ...). The provider only serves a
    film under its real extension, so guessing mp4 left an .mkv film black. mp4 when the catalog has none."""
    catalog = getattr(h, "tv_catalog", None)
    row = None
    if catalog is not None:
        try:
            row = catalog.movie(ref) if kind == "vod" else catalog.episode(ref) if kind == "ep" else None
            if row is None and kind == "vod" and hasattr(catalog, "extra_movie"):
                row = catalog.extra_movie(ref)
        except Exception:  # noqa: BLE001 - a catalog read must not stop playback
            row = None
    ext = str((row or {}).get("ext") or "").strip().lower().lstrip(".")
    return ext if ext.isalnum() and 0 < len(ext) <= 5 else "mp4"


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
    # The first login, saved on the setup page, stays in the pool after a second one is added on the
    # admin page. Before, any saved login replaced it, and with one stream per login every tune was busy.
    if h.vault.has("iptv.xtream_server") and h.vault.has("iptv.xtream_username"):
        server = str(h.vault.get("iptv.xtream_server") or "").rstrip("/")
        username = str(h.vault.get("iptv.xtream_username") or "")
        same = any(row["provider"] == "thomas-creek" and row["server"] == server and row["username"] == username
                   for row in found.values())
        if ("thomas-creek", "main") not in found and not same:
            found[("thomas-creek", "main")] = {
                "provider": "thomas-creek", "login": "main", "server": server, "username": username,
                "password_secret": "iptv.xtream_password", "name": "Thomas Creek",
            }
    # Backup addresses: `primary` is the address as saved, `servers` every address best first, and `server` the
    # one to use now (the first that isn't marked down). See hub/iptvaddr.py.
    db = getattr(h, "db", None)
    for row in found.values():
        row["primary"] = row["server"]
        row["servers"] = iptvaddr.ordered(db, row["provider"], row["server"]) if db is not None else [row["server"]]
        row["server"] = row["servers"][0] if row["servers"] else row["server"]
    return list(found.values())


def _pick(h: Any, item: dict[str, Any], avoid: set[str], now: float) -> dict[str, Any] | None:
    usage = _usage(h)
    sessions = _sessions(h)
    pool = logins(h)
    best = None
    best_key = None
    for source in item.get("sources") or []:
        for login in pool:
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
    from .iptvcanon import index

    return index(h.db).by_id.get(canonical_id)


def _episode(h: Any, ref: str) -> dict[str, Any] | None:
    """A series episode (`ep:<provider>:<id>`) the catalog knows, played like a film from its own provider."""
    if not ref.startswith("ep:"):
        return None
    catalog = getattr(h, "tv_catalog", None)
    try:
        known = catalog.episode(ref) if catalog is not None else None
    except Exception:  # noqa: BLE001 - a catalog read must not stop playback
        known = None
    if not known:
        return None
    _kind, provider, _stream = _split_ref(ref)
    return {"id": ref, "sources": [{"provider": provider, "ref": ref, "quality": None}]}


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
