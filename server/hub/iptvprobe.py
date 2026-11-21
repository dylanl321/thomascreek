"""A reading of every IPTV provider from the hub itself: does each address answer, and does each login work.

`GET /v1/ping/providers` (home network only, no sign-in) runs it, so "the new provider refuses connections" can be
read off the hub instead of guessed at. For each address of a provider (the main one and any backups, in the saved
order) it says how far a connection got: the name lookup, the TCP connection, TLS, then the provider's
`player_api.php` answer, once with the hub's own client name and once with a TV player's, since some providers turn
away clients they don't know. For each login it says whether the provider accepts it and how many streams are in use.

Nothing here returns or logs an address, a username or a password: addresses are numbered (0 is the main one) and
logins are named by their id on the hub. One reading at a time, and a fresh one at most every MIN_GAP_S; between
those the last reading is returned, so the page can't be used to hammer a provider.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("hub.iptvprobe")

TIMEOUT_S = 8.0
MIN_GAP_S = 60.0
FRESH_GAP_S = 10.0  # `fresh=true` still waits this long between readings
# What the TVs' players call themselves to a provider (ExoPlayer's default and VLC), next to the hub's own.
AGENTS = {"hub": None, "player": "VLC/3.0.20 LibVLC/3.0.20"}

_last: dict[str, Any] = {}
_lock = asyncio.Lock()


def _why(exc: BaseException) -> str:
    """A short, address-free name for how a connection failed."""
    text = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, socket.gaierror) or "name or service" in text or "nodename" in text or "getaddrinfo" in text:
        return "name lookup failed"
    if isinstance(exc, ConnectionRefusedError) or "refused" in text:
        return "connection refused"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in text or "timed out" in text:
        return "no answer (timed out)"
    if isinstance(exc, ConnectionResetError) or "reset" in text:
        return "connection reset"
    if isinstance(exc, ssl.SSLError) or "ssl" in text or "certificate" in text or "tls" in text:
        return "TLS failed"
    if "unreachable" in text:
        return "network unreachable"
    if "disconnected" in text or "remoteprotocol" in text or "eof" in text:
        return "connection closed without an answer"
    return type(exc).__name__


async def _tcp(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    tls = parts.scheme == "https"
    port = parts.port or (443 if tls else 80)
    out: dict[str, Any] = {"scheme": parts.scheme, "port": port}
    t0 = time.monotonic()
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                                       timeout=TIMEOUT_S)
        out["dns"] = "ok"
        out["families"] = sorted({"v6" if i[0] == socket.AF_INET6 else "v4" for i in infos})
    except Exception as exc:  # noqa: BLE001
        out.update(dns=_why(exc), reached="name lookup")
        return out
    try:
        ctx = ssl.create_default_context() if tls else None
        if ctx is not None:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # how far it gets, not whether the certificate is good
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), timeout=TIMEOUT_S)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        out.update(connect="ok", connect_ms=int((time.monotonic() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001
        out.update(connect=_why(exc), reached="tcp" if not tls else "tcp or tls")
    return out


async def _api(url: str, username: str, password: str, agent: str | None) -> dict[str, Any]:
    headers = {"User-Agent": agent} if agent else {}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, verify=False) as c:
            r = await c.get(f"{url}/player_api.php", params={"username": username, "password": password},
                            headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"answer": _why(exc), "ms": int((time.monotonic() - t0) * 1000)}
    out: dict[str, Any] = {"status": r.status_code, "ms": int((time.monotonic() - t0) * 1000)}
    if 300 <= r.status_code < 400:
        out["answer"] = "redirect"
        return out
    if r.status_code >= 400:
        out["answer"] = "refused" if r.status_code in (401, 403) else f"error {r.status_code}"
        server = r.headers.get("server")
        if server:
            out["server"] = server[:40]  # "cloudflare" etc.: who turned it away
        return out
    try:
        info = (r.json() or {}).get("user_info") or {}
    except Exception:  # noqa: BLE001
        out["answer"] = "not an Xtream answer"
        return out
    if str(info.get("auth")) != "1":
        out["answer"] = "login refused"
        return out
    out["answer"] = "ok"
    out["account"] = str(info.get("status") or "Active").lower()
    try:
        out["streams"] = f"{int(info.get('active_cons') or 0)} of {int(info.get('max_connections') or 0)} in use"
    except (TypeError, ValueError):
        pass
    try:
        raw_exp = info.get("exp_date")
        exp = float(raw_exp) if raw_exp not in (None, "", "null") else None
        if exp:
            out["expires_in_days"] = round((exp - time.time()) / 86400, 1)
    except (TypeError, ValueError):
        pass
    return out


async def _provider(h: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = rows[0]
    servers = list(first.get("servers") or [first.get("server")])
    out: dict[str, Any] = {"id": first["provider"], "name": first.get("name") or first["provider"],
                           "logins": len(rows), "addresses": []}
    password0 = str(h.vault.get(first["password_secret"]) or "")
    working = None
    for i, url in enumerate(servers):
        url = str(url or "").rstrip("/")
        row: dict[str, Any] = {"address": i, "main": url == str(first.get("primary") or "").rstrip("/")}
        if not url.startswith(("http://", "https://")):
            row["problem"] = "not an http(s) address"
            out["addresses"].append(row)
            continue
        row.update(await _tcp(url))
        if row.get("connect") == "ok":
            answers = await asyncio.gather(*(_api(url, first["username"], password0, a) for a in AGENTS.values()))
            row["api"] = dict(zip(AGENTS, answers, strict=True))
            if working is None and any(a.get("answer") == "ok" for a in answers):
                working = url
        out["addresses"].append(row)
    if working is not None:
        logins = []
        for lg in rows:
            res = await _api(working, lg["username"], str(h.vault.get(lg["password_secret"]) or ""), AGENTS["player"])
            logins.append({"login": lg["login"], **{k: v for k, v in res.items() if k != "server"}})
        out["login_checks"] = logins
    return out


async def reading(h: Any, *, force: bool = False) -> dict[str, Any]:
    """Every provider, read now (or the last reading, if one is under MIN_GAP_S old)."""
    async with _lock:
        gap = FRESH_GAP_S if force else MIN_GAP_S
        if _last and time.time() - float(_last.get("at") or 0) < gap:
            return {**_last, "cached": True}
        from .iptvplay import logins

        groups: dict[str, list[dict[str, Any]]] = {}
        for lg in logins(h):
            groups.setdefault(lg["provider"], []).append(lg)
        t0 = time.monotonic()
        providers = list(await asyncio.gather(*(_provider(h, rows) for rows in groups.values())))
        for p in providers:
            states = [a.get("connect") or a.get("dns") or a.get("problem") for a in p["addresses"]]
            log.info("provider reading %s: %s", p["id"], ", ".join(str(s) for s in states))
        _last.clear()
        _last.update(at=round(time.time()), ms=int((time.monotonic() - t0) * 1000), providers=providers)
        return dict(_last)
