"""Backup addresses for an IPTV provider: the same logins on several domains, so one blocked address isn't an outage.

A provider is saved with one main address (`server`, as before). An admin can add backup addresses on the admin
page; the provider accepts the same usernames and passwords on each. The hub keeps a short health note per
address and every caller that builds a provider address (`iptvplay.logins`, the catalog reads, the phone relay)
uses the first address that is not marked down, in the saved order:

- a catalog read that can't reach an address marks it down and tries the next one;
- a TV's `/v1/tv/tune/fail` checks the address that stream used; when it doesn't answer, it is marked down and the
  same login plays again on the next address (before the hub moves on to another login);
- a watcher checks every address of a provider that has backups every few minutes, so the hub switches before a
  TV notices, and switches back when the main address answers again.

An address is "down" for DOWN_S after a failed check, then tried again. Nothing here logs an address: an Xtream
address is a hostname, and the full stream address carries the login.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger("hub.iptvaddr")

KV_BACKUPS = "iptv.addresses"
KV_HEALTH = "iptv.address_health"
DOWN_S = 15 * 60
CHECK_TIMEOUT_S = 8.0
WATCH_S = 5 * 60
MAX_BACKUPS = 8
ADDRESS_RE = re.compile(r"https?://[^\s/]+(:\d+)?(/\S*)?")


def clean(url: object) -> str:
    return str(url or "").strip().rstrip("/")


def parse_list(text: object) -> tuple[list[str], list[str]]:
    """Addresses typed one per line (or separated by commas or spaces). Returns (good, refused)."""
    good: list[str] = []
    bad: list[str] = []
    for part in re.split(r"[\s,]+", str(text or "")):
        url = clean(part)
        if not url:
            continue
        if ADDRESS_RE.fullmatch(url):
            if url not in good:
                good.append(url)
        else:
            bad.append(url)
    return good, bad


def backups(db: Any, pid: str) -> list[str]:
    raw = db.kv_get(KV_BACKUPS) or {}
    rows = raw.get(pid) if isinstance(raw, dict) else None
    return [clean(u) for u in rows if clean(u)] if isinstance(rows, list) else []


def set_backups(db: Any, pid: str, urls: list[str], primary: str = "") -> list[str]:
    """Save a provider's backup addresses (in order). The main address and repeats are dropped."""
    keep: list[str] = []
    for url in urls:
        url = clean(url)
        if url and url != clean(primary) and ADDRESS_RE.fullmatch(url) and url not in keep:
            keep.append(url)
    keep = keep[:MAX_BACKUPS]
    raw = db.kv_get(KV_BACKUPS) or {}
    raw = dict(raw) if isinstance(raw, dict) else {}
    if keep:
        raw[pid] = keep
    else:
        raw.pop(pid, None)
    db.kv_set(KV_BACKUPS, raw)
    health = _health(db)
    if pid in health:
        wanted = {clean(primary), *keep}
        health[pid] = {u: v for u, v in health[pid].items() if u in wanted}
        db.kv_set(KV_HEALTH, health)
    return keep


def forget(db: Any, pid: str) -> None:
    """A removed provider takes its backups and health notes with it."""
    for kv in (KV_BACKUPS, KV_HEALTH):
        raw = db.kv_get(kv) or {}
        if isinstance(raw, dict) and pid in raw:
            raw = dict(raw)
            raw.pop(pid, None)
            db.kv_set(kv, raw)


def addresses(db: Any, pid: str, primary: str) -> list[str]:
    """The main address, then the backups, as saved."""
    out = [clean(primary)] if clean(primary) else []
    out += [u for u in backups(db, pid) if u not in out]
    return out


def ordered(db: Any, pid: str, primary: str, now: float | None = None) -> list[str]:
    """Addresses to try, best first: those not marked down in saved order, then the down ones (soonest back first)."""
    now = time.time() if now is None else now
    every = addresses(db, pid, primary)
    if len(every) < 2:
        return every
    notes = _health(db).get(pid) or {}
    up = [u for u in every if float((notes.get(u) or {}).get("down_until") or 0) <= now]
    down = sorted((u for u in every if u not in up), key=lambda u: float((notes.get(u) or {}).get("down_until") or 0))
    return up + down


def best(db: Any, pid: str, primary: str, now: float | None = None) -> str:
    found = ordered(db, pid, primary, now)
    return found[0] if found else clean(primary)


def note(db: Any, pid: str, url: str, ok: bool, why: str = "", now: float | None = None) -> None:
    """Remember what one address just did. A failure keeps it out of the way for DOWN_S."""
    now = time.time() if now is None else now
    url = clean(url)
    if not pid or not url:
        return
    health = _health(db)
    rows = dict(health.get(pid) or {})
    before = rows.get(url) or {}
    if ok:
        row = {"state": "ok", "at": now, "ok_at": now}
    else:
        row = {"state": "down", "at": now, "why": why[:120], "down_until": now + DOWN_S,
               "since": before.get("since") if before.get("state") == "down" else now}
        if before.get("ok_at"):
            row["ok_at"] = before["ok_at"]
    if before.get("state") != row["state"]:
        log.info("iptv provider %s: an address is now %s%s", pid, row["state"], f" ({why})" if why else "")
    rows[url] = row
    health[pid] = rows
    db.kv_set(KV_HEALTH, health)


def status(db: Any, pid: str, primary: str, now: float | None = None) -> list[dict[str, Any]]:
    """Each address with its last check, for the admin page. `in_use` marks the one the hub uses now."""
    now = time.time() if now is None else now
    notes = _health(db).get(pid) or {}
    using = best(db, pid, primary, now)
    out = []
    for i, url in enumerate(addresses(db, pid, primary)):
        row = notes.get(url) or {}
        down = float(row.get("down_until") or 0) > now
        out.append({"address": url, "main": i == 0, "in_use": url == using,
                    "state": "down" if down else ("ok" if row.get("state") == "ok" else "unknown"),
                    "why": row.get("why") if down else None, "at": row.get("at"),
                    "since": row.get("since") if down else None})
    return out


async def check(url: str, username: str, password: str, transport: Any = None) -> tuple[bool, str]:
    """Does the provider answer on this address? (ok, short reason). Login refusals still count as answering:
    the address works, the login is a separate problem."""
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S, follow_redirects=False, transport=transport) as c:
            r = await c.get(f"{clean(url)}/player_api.php", params={"username": username, "password": password})
    except httpx.TimeoutException:
        return False, "didn't answer in time"
    except httpx.HTTPError:
        return False, "couldn't be reached"
    if r.status_code in (301, 302, 303, 307, 308):
        return False, "sent the hub somewhere else (often a block page)"
    if r.status_code in (401, 403):
        return True, "answered (the login was refused)"
    if r.status_code >= 400:
        return False, f"answered {r.status_code}"
    try:
        body = r.json()
    except ValueError:
        return False, "answered with a web page, not the provider (often a block page)"
    if not isinstance(body, (dict, list)):
        return False, "answered with something that isn't the provider"
    return True, "answered"


async def check_provider(db: Any, pid: str, primary: str, username: str, password: str,
                         transport: Any = None, now: float | None = None) -> list[dict[str, Any]]:
    """Check every address of one provider (in parallel) and note the answers."""
    every = addresses(db, pid, primary)
    answers = await asyncio.gather(*(check(u, username, password, transport) for u in every))
    for url, (ok, why) in zip(every, answers, strict=True):
        note(db, pid, url, ok, "" if ok else why, now)
    return status(db, pid, primary, now)


async def check_all(h: Any, *, only_with_backups: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Check each provider's addresses with its first login. Providers without backups are skipped by default:
    there is nothing to switch to."""
    from . import iptvplay

    out: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    transport = getattr(getattr(h, "tv_catalog", None), "_transport", None)
    for lg in iptvplay.logins(h):
        pid = lg["provider"]
        if pid in seen:
            continue
        primary = lg.get("primary") or lg["server"]
        if only_with_backups and not backups(h.db, pid):
            continue
        secret = lg.get("password_secret") or ""
        password = str(h.vault.get(secret) or "") if secret and h.vault.has(secret) else ""
        if not password:
            continue
        seen.add(pid)
        try:
            out[pid] = await check_provider(h.db, pid, primary, lg["username"], password, transport)
        except Exception as exc:  # noqa: BLE001 - one provider's check must not stop the rest
            log.info("iptv provider %s: address check failed (%s)", pid, type(exc).__name__)
    return out


class AddressWatch:
    """Checks every provider with backup addresses every WATCH_S, so playback moves before anyone sees a failure."""

    def __init__(self, h: Any) -> None:
        self.h = h
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await check_all(self.h)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("iptv address watch: %s", type(exc).__name__)
            await asyncio.sleep(WATCH_S)


def _health(db: Any) -> dict[str, dict[str, Any]]:
    raw = db.kv_get(KV_HEALTH) or {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
