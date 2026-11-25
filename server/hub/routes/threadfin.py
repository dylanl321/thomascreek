"""The Threadfin feed: playlist, guide, stream relay, logos and the connection budget (see hub/threadfin.py).

Every route takes `?token=` (Threadfin can't send headers) and refuses callers from the public internet. A Claude
agent login with `admin.read` on the home network may read them too (Bearer), to time the feed after a deploy.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .. import agentlogins, threadfin
from ..artwork import ArtError
from ..iptvaddr import note as note_address
from ..iptvplay import (_split_ref, credentials, lease, logins, note_title, refresh_usage, refused, release_tv,
                        touch)
from ..state import Hub
from .deps import caller_ip, error, hub

log = logging.getLogger("hub.threadfin")

router = APIRouter(prefix="/v1/threadfin", tags=["tv"])

# A channel the provider refuses on one login is tried on the next: at most this many leases per stream request.
TRIES = 3


def _allowed(request: Request, h: Hub, token: str | None) -> None:
    ip = caller_ip(request, h)
    if not threadfin.allowed_from(h, ip):
        raise error(404, "not_found", "Not here")
    if threadfin.check(h, token):
        return
    key = agentlogins.bearer(request.headers)
    if key and agentlogins.check(h.db, key, "admin.read") is not None:
        return
    raise error(401, "unauthorized", "The Threadfin token is missing or was replaced. Copy the link again from the "
                                     "hub web page (Watching, Threadfin).")


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _home(h: Hub) -> tuple[float, float] | None:
    try:
        return await h.smart.weather._location()
    except Exception:  # noqa: BLE001 - the basic lineup falls back to no home market
        return None


def _groups(group: str | None) -> set[str] | None:
    picked = {g.strip().casefold() for g in (group or "").split(",") if g.strip()}
    return picked or None


def _send(request: Request, body: threadfin.Reply, started: float) -> Response:
    headers = {"ETag": body.tag, "Cache-Control": "no-cache",
               "Server-Timing": f"app;dur={(time.perf_counter() - started) * 1000:.0f}"}
    if request.headers.get("if-none-match") == body.tag:
        return Response(status_code=304, headers=headers)
    if body.packed is not None and "gzip" in (request.headers.get("accept-encoding") or "").lower():
        return Response(body.packed, media_type=body.media, headers={**headers, "Content-Encoding": "gzip"})
    return Response(body.raw, media_type=body.media, headers=headers)


async def _feed(request: Request, h: Hub, kind: str, token: str | None, which: str, group: str | None) -> Response:
    started = time.perf_counter()
    _allowed(request, h, token)
    if which not in threadfin.LISTS:
        raise error(400, "bad_params", "list is basic or all")
    from .tvguide import _ready

    await _ready(h)
    home = await _home(h) if which == "basic" else None
    body = await asyncio.to_thread(threadfin.reply, h, kind, which, home, _groups(group), _base(request))
    return _send(request, body, started)


@router.get("/playlist.m3u")
async def playlist(request: Request, token: str | None = None, list: str = "basic", group: str | None = None,
                   h: Hub = Depends(hub)) -> Response:
    """The curated channels as M3U, each stream relayed by the hub. `list=basic` (default) or `list=all`."""
    return await _feed(request, h, "m3u", token, list, group)


@router.get("/guide.xml")
async def guide(request: Request, token: str | None = None, list: str = "basic", group: str | None = None,
                h: Hub = Depends(hub)) -> Response:
    """XMLTV for the same channels; channel ids are the playlist's `tvg-id`."""
    return await _feed(request, h, "xml", token, list, group)


@router.get("/status")
async def status(request: Request, token: str | None = None, h: Hub = Depends(hub)) -> JSONResponse:
    """The provider connection budget: total, in use, free, Threadfin's own, and the tuner count to set."""
    _allowed(request, h, token)
    return JSONResponse(await asyncio.to_thread(threadfin.budget, h))


@router.get("/logo/{key}")
async def logo(key: str, request: Request, token: str | None = None, h: Hub = Depends(hub)) -> Response:
    _allowed(request, h, token)
    try:
        path, etag, media = await h.artwork.file("logo", key, "m")
    except ArtError as exc:
        raise error(exc.status, exc.code, exc.message) from None
    headers = {"ETag": etag, "Cache-Control": "public, max-age=86400"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media, headers=headers)


class _NoStream(Exception):
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


async def _open(client: httpx.AsyncClient, url: str) -> httpx.Response:
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.TimeoutException:
        raise _NoStream("timeout") from None
    except httpx.HTTPError:
        raise _NoStream("unreachable") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise _NoStream(f"http{upstream.status_code}")
    return upstream


def _addresses(h: Hub, held: dict[str, Any]) -> list[tuple[str, str]]:
    """(server, stream address) for the leased login on each of its provider's addresses, best first. Never logged."""
    found = credentials(h, str(held["provider"]), str(held["login"]))
    if found is None:
        return []
    _server, user, password = found
    row = next((lg for lg in logins(h) if lg["provider"] == held["provider"] and lg["login"] == held["login"]), None)
    servers = list((row or {}).get("servers") or [_server])
    _kind, _provider, stream = _split_ref(str(held.get("ref") or ""))
    if not stream or "/" in stream or ".." in stream:
        return []
    tail = f"/live/{quote(user)}/{quote(password)}/{quote(stream)}.ts"
    return [(server, server + tail) for server in servers]


@router.get("/stream/{key}.ts")
async def stream(key: str, request: Request, token: str | None = None, h: Hub = Depends(hub)) -> Response:
    """One channel as MPEG-TS on a login of its own from the house's pool, freed when Threadfin hangs up."""
    _allowed(request, h, token)
    cat = h.tv_catalog
    channel = (await asyncio.to_thread(lambda: cat._shown_channels(cat.load()))).get(key)
    if channel is None:
        raise error(404, "not_found", "That channel isn't in the lineup")
    holder = f"{threadfin.HOLDER_PREFIX}{secrets.token_hex(4)}"
    agent = (request.headers.get("user-agent") or "").strip()[:200] or "ThomasCreekHub"
    client = httpx.AsyncClient(timeout=httpx.Timeout(15, read=60), follow_redirects=True,
                               transport=getattr(cat, "_transport", None), headers={"User-Agent": agent})
    tries: list[str] = []
    upstream: httpx.Response | None = None
    try:
        for attempt in range(TRIES):
            held = await asyncio.to_thread(lease, h, key, holder)
            if held is None and attempt == 0:
                await refresh_usage(h, key)  # an old provider count may call a free login busy
                held = await asyncio.to_thread(lease, h, key, holder)
            if held is None:
                break
            refusals = 0
            every = _addresses(h, held)
            for server, url in every:
                try:
                    upstream = await _open(client, url)
                    break
                except _NoStream as exc:
                    tries.append(exc.why)
                    if exc.why in ("unreachable", "timeout"):
                        note_address(h.db, str(held["provider"]), server, False, "couldn't be reached")
                    elif exc.why.startswith("http4"):
                        refusals += 1
            if upstream is not None:
                break
            if every and refusals == len(every):
                # The provider refuses this channel on this login: set the pair aside, the next lease takes another.
                await asyncio.to_thread(refused, h, holder)
            else:
                await asyncio.to_thread(release_tv, h, holder)
        if upstream is None:
            await asyncio.to_thread(release_tv, h, holder)
            await client.aclose()
            why = ", ".join(tries) or "every login busy"
            log.info("threadfin stream: no stream for a channel (%s)", why)
            message = ("Every provider login is in use. Lower the tuner count or stop another stream."
                       if not tries else "The provider didn't send this channel. Try another.")
            return Response(message + "\n", status_code=503, media_type="text/plain",
                            headers={"Retry-After": "30", "Server-Timing": f"why;desc=\"{why[:80]}\""})
    except BaseException:
        await asyncio.to_thread(release_tv, h, holder)
        await client.aclose()
        raise
    await asyncio.to_thread(note_title, h, holder, str(channel.get("name") or key), False)
    opened = upstream

    async def body():
        try:
            async for chunk in opened.aiter_bytes(1 << 16):
                touch(h, holder)  # at most every 15 s (iptvplay.TOUCH_EVERY_S)
                yield chunk
        finally:
            # Threadfin hanging up cancels this body, and every await in a cancelled scope ends at once: the login
            # stayed leased until the 90 s idle timeout on the live hub (Nov 25 2026). Shielded, so it is freed now.
            with anyio.CancelScope(shield=True):
                await asyncio.to_thread(release_tv, h, holder)
                await opened.aclose()
                await client.aclose()

    return StreamingResponse(body(), media_type="video/mp2t", headers={"Cache-Control": "no-store"})
