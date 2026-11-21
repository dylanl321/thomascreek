"""Pick a login that still has room, and move a failed stream to the next one.

Passwords stay in the vault. The TV asks with a canonical id and opens a hub
path. The provider address is only the redirect for that one TV.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from . import iptvaddr
from .db import new_id
from .home import iso

log = logging.getLogger("hub.iptvplay")

# Every change to the lease pool (KV_SESSIONS) reads the list, changes it and writes it back. Those steps run as
# one under this lock, so a play may run in a worker thread (off the hub's loop) without two players ever taking
# the same login (hub rebuild phase 4, Nov 19 2026). Re-entrant: lease() calls _release_tv() and so on.
_POOL = threading.RLock()


def _pooled(fn: Any) -> Any:
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        with _POOL:
            _step("wait", started)
            return fn(*args, **kwargs)
    return run


# Where a lease spends its time, for the play route's Server-Timing (`lease.<step>`): a play on a worker thread
# carries the caller's dict here (asyncio.to_thread copies the context). Episode plays spent 2 to 3 s in the lease
# while films took 0.4 s, and the one "lease" figure could not say why (Nov 20 2026).
STEPS: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar("iptvplay_steps", default=None)


def collect(steps: dict[str, float]) -> Any:
    """Start collecting this play's lease steps (and its database waits) in `steps`; returns the reset token."""
    from .db import LOCK_WAITS

    return STEPS.set(steps), LOCK_WAITS.set(steps)


def stop_collecting(tokens: Any) -> None:
    from .db import LOCK_WAITS

    STEPS.reset(tokens[0])
    LOCK_WAITS.reset(tokens[1])


def _step(name: str, since: float) -> float:
    """Add the milliseconds since `since` to step `name` (when a caller is collecting); the time now."""
    now = time.perf_counter()
    found = STEPS.get()
    if found is not None and name not in found:  # the outermost call's figure; nested pooled calls add nothing
        found[name] = round((now - since) * 1000, 1)
    return now

KV_LOGINS = "iptv.logins"
KV_USAGE = "iptv.usage"
KV_SESSIONS = "iptv.sessions"
KV_NOTICES = "iptv.notices"
IDLE_S = 90
# The provider's own count of open streams is trusted only this long. Every player in the house leases through
# this pool, so after that the hub's own leases are the truth (Nov 19 2026: a count read minutes earlier kept a
# freed login "full" and plays waited until the phone gave up).
USAGE_FRESH_S = 90
FAIL_S = 600
# A provider that refuses one channel on one login in every format (405) keeps refusing it for hours while another
# login plays it (Nov 21 2026, a sports channel): that pair is set aside this long, so plays go to the login that works.
REFUSED_S = 6 * 3600
QUALITY = {"2160p": 0, "1080p": 1, "720p": 2, "sd": 3}
# A copy whose name starts with another language's tag ("TR - Community", "|DE| ...", "[FR]") or says it is
# dubbed is not played unless it is the copy asked for: the house watches in English, and a show folded with its
# dubbed copies played in Turkish because that provider had more free logins (Nov 20 2026). With every English
# copy busy, the busy answer (who holds the logins) is better than a surprise dub.
_FOREIGN_TAGS = {"ar", "al", "bg", "cz", "de", "dk", "es", "fi", "fr", "gr", "hi", "hu", "il", "in", "ir", "it",
                 "kr", "ku", "lat", "nl", "no", "pk", "pl", "pt", "ro", "ru", "se", "sk", "tr", "ua", "exyu", "ex-yu"}
_TAG = re.compile(r"^\W*([A-Z]{2,3}(?:-[A-Z]{2})?)\s*[-:|\]]")  # upper case: "It: Chapter Two" is a title
_BRACKETED = re.compile(r"[(\[|]\s*([A-Z]{2,3}(?:-[A-Z]{2})?)\s*[)\]|]")  # "Monk (TR)", "Monk [DE]"
_DUBBED = re.compile(r"(dublaj|t[üu]rk[çc]e|synchro|doblad|dubbed\b)", re.I)


def foreign(name: object) -> bool:
    """A source name that marks a copy in another language than the house's (English)."""
    text = str(name or "")
    tags = [m.group(1) for m in (_TAG.match(text), *_BRACKETED.finditer(text)) if m]
    return any(tag.lower() in _FOREIGN_TAGS for tag in tags) or bool(_DUBBED.search(text))


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
    catalog = getattr(h, "tv_catalog", None)
    try:
        lists = catalog.list_status() if catalog is not None and hasattr(catalog, "list_status") else []
    except Exception:  # noqa: BLE001 - the login rows still answer
        lists = []
    return {"logins": rows, "sessions": playing, "lists": lists}


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


@_pooled
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


@_pooled
def lease(h: Any, ref: str, holder: str, *, avoid: set[str] | None = None,
          now: float | None = None) -> dict[str, Any] | None:
    """Reserve one login for `holder` (a TV's install id, or `phone:<device id>`) to play `ref`.

    One login carries one stream, so every player in the house takes its login from this one pool: a TV through
    /v1/tv/tune, a phone through /v1/tv/phone/play (straight from the provider or relayed by the hub). The lease
    lives while the holder shows it is playing (a TV's heartbeat with something playing, a phone's /hold or the
    relayed bytes themselves) and is freed IDLE_S after that stops, or at once on stop. A holder has one lease:
    a new one replaces it. None when every login that carries this title is busy."""
    now = time.time() if now is None else now
    clock = time.perf_counter()
    _release_idle(h, now)
    clock = _step("idle", clock)
    _kind, provider, _stream = _split_ref(ref)
    item = _holding(h, ref)
    clock = _step("fold", clock)
    item = item or _episode(h, ref) or {"id": ref, "sources": [{"provider": provider, "ref": ref, "quality": None}]}
    clock = _step("item", clock)
    mine = {_key(row) for row in _sessions(h) if row.get("tv") == holder}
    _release_tv(h, holder)
    clock = _step("free", clock)
    # The stream this holder just let go may still be counted by the provider; it is this holder's to reuse.
    chosen = _pick(h, item, avoid if avoid is not None else _failed(h, now), now, mine=mine)
    clock = _step("pick", clock)
    if chosen is None:
        return None
    row = {"session": new_id("ses"), "tv": holder, "provider": chosen["provider"], "login": chosen["login"],
           "canonical_id": item.get("id"), "ref": chosen["ref"], "quality": chosen["quality"],
           "since": now, "seen": now}
    sessions = _sessions(h)
    sessions.append(row)
    h.db.kv_set(KV_SESSIONS, sessions)
    _step("save", clock)
    return row


def direct(h: Any, ref: str, holder: str, *, now: float | None = None) -> str | None:
    """The provider address itself, for a travel phone that plays straight from the provider (Dylan, Nov 2026).
    Live channels are HLS (`.m3u8`). Callers must not log the answer: it carries the login."""
    found = direct_all(h, ref, holder, now=now)
    return found[0] if found else None


@_pooled
def direct_all(h: Any, ref: str, holder: str, *, now: float | None = None) -> list[str]:
    """`direct`, then the same stream on each of the provider's backup addresses (same login), best first.
    The phone tries them in order when one doesn't play. Empty when every login is busy. Never logged."""
    row = lease(h, ref, holder, now=now)
    if row is None:
        return []
    clock = time.perf_counter()
    out: list[str] = []
    for server in [None, *addresses_for(h, row["session"], holder)[1:]]:
        url = target(h, row["session"], holder, server=server)
        if url and url.endswith(".ts") and "/live/" in url:
            url = url[:-3] + ".m3u8"
        if url and url not in out:
            out.append(url)
    _step("urls", clock)
    return out


def as_ts(urls: list[str]) -> list[str]:
    """The MPEG-TS form of live HLS addresses (what the TVs play). Some channels have no HLS on the provider: it
    answers 405 for `.m3u8` (Nov 21 2026) while the `.ts` stream plays. Never logged."""
    return [u[:-5] + ".ts" for u in urls if u.endswith(".m3u8") and "/live/" in u]


async def refresh_usage(h: Any, ref: str, *, budget_s: float = 3.0, now: float | None = None) -> None:
    """Ask the provider, live, how many streams each login that carries `ref` has open, when the last answer is
    older than USAGE_FRESH_S. Done before a play so a login is never called busy on an old count. Gives up after
    `budget_s`; the play then goes on with the hub's own leases."""
    from . import iptvaddr as addr

    now = time.time() if now is None else now
    _kind, provider, _stream = _split_ref(ref)
    item = _holding(h, ref) or _episode(h, ref)
    providers = {str(src.get("provider")) for src in (item or {}).get("sources") or []} or {provider}
    usage = _usage(h)
    transport = getattr(getattr(h, "tv_catalog", None), "_transport", None)
    jobs = []
    for login in logins(h):
        if login["provider"] not in providers:
            continue
        known = usage.get(_key(login)) or {}
        at = known.get("at")
        if isinstance(at, (int, float)) and now - float(at) <= USAGE_FRESH_S:
            continue
        if _int(known.get("active_cons"), 0) == 0:
            continue  # nothing outside the pool was seen on it; the hub's own leases decide
        secret = login.get("password_secret") or ""
        password = str(h.vault.get(secret) or "") if secret and h.vault.has(secret) else ""
        if not password:
            continue
        jobs.append(addr.check(login["server"], login["username"], password, transport,
                               on_info=lambda info, lg=login: note_usage(h, lg["provider"], lg["login"], info)))
    if not jobs:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), timeout=budget_s)
    except asyncio.TimeoutError:
        log.info("iptv: live login check took over %.0f s; playing on the hub's own counts", budget_s)


def busy_reason(h: Any, ref: str) -> str:
    """Why no login is free for this title, in words the phone can show: who holds each login that carries it."""
    item = _holding(h, ref) or _episode(h, ref)
    _kind, provider, _stream = _split_ref(ref)
    providers = {str(src.get("provider")) for src in (item or {}).get("sources") or []} or {provider}
    names = {tv_id: str(tv.get("name") or "a TV") for tv_id, tv in (h.tv_app.tvs() or {}).items()} \
        if getattr(h, "tv_app", None) else {}
    holders = []
    for row in _sessions(h):
        if row.get("provider") in providers:
            who = str(row.get("tv") or "")
            holders.append("a phone" if who.startswith("phone:") else names.get(who, "a TV"))
    known = {lg["provider"]: lg.get("name") or lg["provider"] for lg in logins(h)}
    known.update(provider_names(h))
    pnames = sorted({str(known.get(p) or p) for p in providers})
    where = " and ".join(pnames) if pnames else "this provider"
    if holders:
        return f"Every {where} login that carries this is in use ({', '.join(sorted(set(holders)))}). " \
               "Try again when it stops."
    return f"The {where} login that carries this says it's busy. Try again in a minute."


def held_by(h: Any, holder: str) -> dict[str, Any] | None:
    """The lease `holder` has now (its provider and login), or None."""
    return next((row for row in _sessions(h) if row.get("tv") == holder), None)


def source_view(h: Any, holder: str, *, route: str, address: int = 1,
                addresses: int | None = None) -> dict[str, Any] | None:
    """What a player may show about the stream `holder` has now (its "Stats" panel): the route it takes, the
    provider's name, which of that provider's logins it holds and how many addresses the provider has.

    Numbers and a name only: never an address, a username or a login's own id, so the answer is safe to show
    on a screen and to keep in the log."""
    row = held_by(h, holder)
    if row is None:
        return None
    provider = str(row.get("provider") or "")
    pool = logins(h)
    order = sorted(lg["login"] for lg in pool if lg["provider"] == provider)
    names = {lg["provider"]: str(lg.get("name") or lg["provider"]) for lg in pool}
    names.update(provider_names(h))
    total = addresses if addresses is not None else len(addresses_for(h, str(row.get("session")), holder))
    out: dict[str, Any] = {
        "route": route, "provider": names.get(provider) or provider,
        "login": order.index(row["login"]) + 1 if row.get("login") in order else 1, "logins": max(1, len(order)),
        "address": max(1, address), "addresses": max(1, total),
    }
    if row.get("quality"):
        out["quality"] = row["quality"]
    return out


@_pooled
def note_title(h: Any, holder: str, title: str | None, kids: bool = False) -> None:
    """Remember what `holder`'s lease is playing, so a busy answer can say it (see `holders`)."""
    sessions = _sessions(h)
    changed = False
    for row in sessions:
        if row.get("tv") == holder and (row.get("title") != title or row.get("kids") != kids):
            row["title"], row["kids"] = title, kids
            changed = True
    if changed:
        h.db.kv_set(KV_SESSIONS, sessions)


# The hub's own jobs that lease a login, named for the busy answer.
HUB_HOLDERS = {"hub:thumbs": "The hub (making previews)"}


def holders(h: Any, ref: str, *, titles: bool = True) -> list[dict[str, Any]]:
    """Who holds each login that carries `ref`, for the busy answer: {kind: tv|phone|hub, name, title?}.

    `titles=False` (a child or a kids' device asking) leaves the titles out, so a child never reads the name of
    something outside their library; only what the hub itself marked as a kids' title is named for them."""
    item = _holding(h, ref) or _episode(h, ref)
    _kind, provider, _stream = _split_ref(ref)
    providers = {str(src.get("provider")) for src in (item or {}).get("sources") or []} or {provider}
    tvs = (h.tv_app.tvs() or {}) if getattr(h, "tv_app", None) else {}
    out: list[dict[str, Any]] = []
    for row in _sessions(h):
        if row.get("provider") not in providers:
            continue
        who = str(row.get("tv") or "")
        title = row.get("title")
        if who.startswith("phone:"):
            device = h.db.one("SELECT name FROM devices WHERE id=?", [who[len("phone:"):]])
            entry: dict[str, Any] = {"kind": "phone", "name": str((device or {}).get("name") or "A phone")}
        elif who.startswith("hub:"):
            entry = {"kind": "hub", "name": HUB_HOLDERS.get(who, "The hub")}
        else:
            tv = tvs.get(who) or {}
            entry = {"kind": "tv", "name": str(tv.get("name") or "A TV")}
            playing = tv.get("playing")
            if isinstance(playing, dict):
                title = playing.get("title") or playing.get("name") or title
        if title and (titles or row.get("kids") is True):
            entry["title"] = str(title)[:120]
        out.append(entry)
    return sorted(out, key=lambda e: ({"tv": 0, "phone": 1}.get(e["kind"], 2), e["name"]))


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
    from .iptvcanon import recent

    return recent(h.db).by_ref.get(ref)


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


@_pooled
def fail(h: Any, session: str, tv_id: str, now: float | None = None) -> dict[str, Any]:
    """The picture did not start, or it dropped. Try the next login."""
    now = time.time() if now is None else now
    current = next((row for row in _sessions(h) if row.get("session") == session and row.get("tv") == tv_id), None)
    if current is None:
        return {"ok": False, "message": "That stream has ended."}
    _mark_failed(h, current, now)
    _drop(h, session)
    return tune(h, str(current.get("canonical_id") or ""), tv_id, avoid=_failed(h, now), now=now)


@_pooled
def release_tv(h: Any, tv_id: str) -> None:
    _release_tv(h, tv_id)


@_pooled
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


@_pooled
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


def _pick(h: Any, item: dict[str, Any], avoid: set[str], now: float,
          mine: set[str] | None = None) -> dict[str, Any] | None:
    sources = item.get("sources") or []
    # A copy in another language (a Turkish dub) plays only for a title that has no English copy at all. A folded
    # show lists its episodes from whichever copy came first, so the asked-for episode can itself be the dub
    # (Nov 20 2026: Community S1 E5 played in Turkish though it has an EN copy). With the English copies busy the
    # person gets the usual "all logins busy" answer, not a surprise dub.
    house = [source for source in sources if not foreign(source.get("name"))]
    return _pick_from(h, house or sources, avoid, now, mine)


def _pick_from(h: Any, sources: list[dict[str, Any]], avoid: set[str], now: float,
               mine: set[str] | None) -> dict[str, Any] | None:
    usage = _usage(h)
    sessions = _sessions(h)
    pool = logins(h)
    best = None
    best_key = None
    for source in sources:
        for login in pool:
            if login["provider"] != source.get("provider"):
                continue
            mark = f"{login['provider']}|{login['login']}|{source.get('ref')}"
            if mark in avoid:
                continue
            info = usage.get(_key(login)) or {}
            if mine and _key(login) in mine:
                info = {**info, "active_cons": max(0, _int(info.get("active_cons"), 0) - 1)}
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
    at = info.get("at")
    if isinstance(at, (int, float)) and now - float(at) > USAGE_FRESH_S:
        active = 0
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
    from .iptvcanon import index, recent

    # A folded id made since the last index (rare) is looked up again in a fresh one.
    return recent(h.db).by_id.get(canonical_id) or index(h.db).by_id.get(canonical_id)


def _episode(h: Any, ref: str) -> dict[str, Any] | None:
    """A series episode (`ep:<provider>:<id>`) the catalog knows, played like a film from its own provider."""
    if not ref.startswith("ep:"):
        return None
    catalog = getattr(h, "tv_catalog", None)
    clock = time.perf_counter()
    try:
        known = catalog.episode(ref) if catalog is not None else None
    except Exception:  # noqa: BLE001 - a catalog read must not stop playback
        known = None
    clock = _step("ep.row", clock)
    if not known:
        return None
    _kind, provider, _stream = _split_ref(ref)
    sources = [{"provider": provider, "ref": ref, "quality": None,
                "name": _series_name(h, str(known.get("series_id") or ""))}]
    clock = _step("ep.name", clock)
    sources += [{"provider": _split_ref(twin)[1], "ref": twin, "quality": None, "name": name}
                for twin, name in _named_twins(h, known)]
    _step("ep.twins", clock)
    return {"id": ref, "sources": sources}


def _series_name(h: Any, series_id: str) -> str | None:
    """The provider's own name for a series copy ("TR - Community"), from the folded catalog."""
    from .iptvcanon import recent

    item = recent(h.db).by_ref.get(series_id) if series_id else None
    return next((str(src.get("name")) for src in (item or {}).get("sources") or []
                 if src.get("ref") == series_id and src.get("name")), None)


def _twin_series(h: Any, series_id: str) -> list[str]:
    """The same show on the other providers: the folded series' other copies (Nov 19 2026: episodes played only
    from the provider they were listed under, so a show competed for that provider's logins alone)."""
    from .iptvcanon import recent

    item = recent(h.db).by_ref.get(series_id) if series_id else None
    return [str(src["ref"]) for src in (item or {}).get("sources") or []
            if src.get("ref") and str(src["ref"]).startswith("series:") and src["ref"] != series_id]


def _twins(h: Any, known: dict[str, Any]) -> list[str]:
    """The same episode (season and number) on the other providers, from series pages the hub has already read."""
    return [ref for ref, _name in _named_twins(h, known)]


def _named_twins(h: Any, known: dict[str, Any]) -> list[tuple[str, str | None]]:
    """`_twins`, each with its series copy's provider name (to tell a dubbed copy)."""
    season, number = known.get("season"), known.get("number")
    if season is None or number is None:
        return []
    catalog = getattr(h, "tv_catalog", None)
    try:
        read = catalog._details()["series"] if catalog is not None else {}
    except Exception:  # noqa: BLE001 - a catalog read must not stop playback
        return []
    out: list[tuple[str, str | None]] = []
    for other in _twin_series(h, str(known.get("series_id") or "")):
        for part in (read.get(other) or {}).get("seasons") or []:
            if part.get("number") != season:
                continue
            match = next((ep for ep in part.get("episodes") or [] if ep.get("number") == number), None)
            if match and match.get("id"):
                out.append((str(match["id"]), _series_name(h, other)))
    return out


def warm_twins(h: Any, ref: str) -> None:
    """`read_twins` in the background after an episode starts, so the next episode of the show has its copies."""
    if not ref.startswith("ep:"):
        return
    try:
        task = asyncio.get_running_loop().create_task(read_twins(h, ref, budget_s=30))
    except RuntimeError:
        return
    _WARMING.add(task)
    task.add_done_callback(_WARMING.discard)


_WARMING: set[Any] = set()


async def read_twins(h: Any, ref: str, *, budget_s: float = 4.0) -> int:
    """Read the other providers' pages for this episode's show, so the same episode can play from them. Pages read
    within a day are kept (`fetch_series`). Gives up after `budget_s`. How many copies of the episode are known."""
    catalog = getattr(h, "tv_catalog", None)
    known = catalog.episode(ref) if catalog is not None and ref.startswith("ep:") else None
    if not known or catalog is None:
        return 0
    others = _twin_series(h, str(known.get("series_id") or ""))
    if not others:
        return 0

    async def one(series_id: str) -> None:
        try:
            await catalog.fetch_series(series_id)
        except Exception as exc:  # noqa: BLE001 - another provider's page is a bonus, never a failure
            log.info("iptv: the other copy of a show wasn't read (%s)", type(exc).__name__)

    try:
        await asyncio.wait_for(asyncio.gather(*(one(s) for s in others[:3])), timeout=budget_s)
    except asyncio.TimeoutError:
        log.info("iptv: other copies of a show took over %.0f s", budget_s)
    # Off the loop: it reads every copy's page and the fold, and the loop stood still on it (Nov 20 2026).
    return len(await asyncio.to_thread(_twins, h, known))


@_pooled
def refused(h: Any, holder: str, now: float | None = None) -> bool:
    """The provider refused the stream `holder` leased in every format (Nov 21 2026: 405 to a sports channel's `.ts`
    and `.m3u8` on one login while another login played it). That copy on that login is set aside, for REFUSED_S
    when another login of the provider can take it (plays go there for hours) or FAIL_S when it is the only one (a
    short outage doesn't hide the channel for hours), and the lease freed. False when the holder has no lease."""
    now = time.time() if now is None else now
    row = next((item for item in _sessions(h) if item.get("tv") == holder), None)
    if row is None:
        return False
    others = [lg for lg in logins(h) if lg["provider"] == row.get("provider") and lg["login"] != row.get("login")]
    _mark_failed(h, row, now, REFUSED_S if others else FAIL_S)
    _drop(h, str(row.get("session")))
    return True


def all_refused(h: Any, ref: str, now: float | None = None) -> bool:
    """Every login and copy that carries `ref` was refused by its provider lately (see `refused`)."""
    now = time.time() if now is None else now
    _kind, provider, _stream = _split_ref(ref)
    item = _holding(h, ref) or _episode(h, ref) or {"sources": [{"provider": provider, "ref": ref}]}
    failed = _failed(h, now)
    pool = logins(h)
    marks = [f"{login['provider']}|{login['login']}|{source.get('ref')}" for source in item.get("sources") or []
             for login in pool if login["provider"] == source.get("provider")]
    return bool(marks) and all(mark in failed for mark in marks)


def _mark_failed(h: Any, row: dict[str, Any], now: float, for_s: float = FAIL_S) -> None:
    fails = h.db.kv_get("iptv.fails") or {}
    if not isinstance(fails, dict):
        fails = {}
    fails = {key: until for key, until in fails.items() if float(until or 0) > now}
    fails[f"{row.get('provider')}|{row.get('login')}|{row.get('ref')}"] = now + for_s
    h.db.kv_set("iptv.fails", fails)


def _failed(h: Any, now: float) -> set[str]:
    fails = h.db.kv_get("iptv.fails") or {}
    if not isinstance(fails, dict):
        return set()
    return {key for key, until in fails.items() if float(until or 0) > now}


@_pooled
def _release_idle(h: Any, now: float) -> None:
    sessions = [row for row in _sessions(h) if now - float(row.get("seen") or row.get("since") or 0) < IDLE_S]
    if len(sessions) != len(_sessions(h)):
        h.db.kv_set(KV_SESSIONS, sessions)


@_pooled
def _release_tv(h: Any, tv_id: str) -> None:
    h.db.kv_set(KV_SESSIONS, [row for row in _sessions(h) if row.get("tv") != tv_id])


@_pooled
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
