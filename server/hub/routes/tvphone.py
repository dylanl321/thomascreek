"""Watch a TV-library title on the phone itself.

The provider login never leaves the hub. The phone gets a short pass (a signed token that names the
title, not the address) and the hub fetches the stream for it:

- films and episodes: `/v1/tv/phone/file/{pass}`, a byte-range proxy of the provider file;
- channels: `/v1/tv/phone/live/{pass}/index.m3u8`, the provider's HLS with every line pointed back at
  the hub. The provider's own segment addresses stay in this process (`_Live`), never in the playlist.

Kids, guests and kids' devices only get kids' titles, the same rule as the guide.
"""

from __future__ import annotations

import ipaddress
import secrets
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..crypto import CryptoError
from ..state import Hub
from ..tvapp import TvAppError
from .deps import Caller, caller, error, hub
from .tvguide import _media, _ready

router = APIRouter(prefix="/v1/tv/phone", tags=["tv"])

AUD = "tv-phone"
TTL_S = 6 * 3600
# AVPlayer opens these. MKV, AVI and the rest only play on a TV.
PHONE_TYPES = {"mp4", "m4v", "mov"}
SOURCE_PREFIX = {"movie": "vod:", "episode": "ep:", "live": "xc:"}


def _stream_id(key: str) -> str:
    part = key.rsplit(":", 1)[-1]
    if not part or "/" in part or ".." in part:
        raise error(404, "not_found", "That isn't in the library")
    return part


@router.post("/play")
async def phone_play(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """{movie_id} | {episode_id} | {channel_key} -> a hub path the phone's player opens."""
    kids_only = _media(h, c)
    await _ready(h)
    cat = h.tv_catalog
    kind = next((k for k in ("movie_id", "episode_id", "channel_key") if body.get(k)), None)
    if kind is None:
        raise error(400, "bad_params", "Pick a film, an episode or a channel")
    key = str(body[kind])
    if kind == "movie_id":
        row, mode = cat.movie(key), "movie"
    elif kind == "episode_id":
        row, mode = cat.episode(key), "episode"
    else:
        row, mode = cat.channel(key), "live"
    if row is None or (kids_only and not row.get("kids")):
        raise error(404, "not_found", "That isn't in the library")
    ext = "m3u8" if mode == "live" else str(row.get("ext") or "mp4").lower()
    if mode != "live" and ext not in PHONE_TYPES:
        raise error(409, "tv_only", "This one only plays on a TV")
    token, exp = h.tokens.issue_scoped({"aud": AUD, "k": mode, "s": _stream_id(key), "x": ext,
                                        "sid": secrets.token_urlsafe(9), "dev": c.device["id"]}, TTL_S)
    title = row.get("name") or row.get("title")
    h.db.log("tv.phone.play", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=key,
             params={"title": title})
    path = f"/v1/tv/phone/live/{token}/index.m3u8" if mode == "live" else f"/v1/tv/phone/file/{token}"
    return {"url": path, "kind": mode, "title": title, "expires_at": exp}


def _claims(h: Hub, token: str, mode: set[str]) -> dict[str, Any]:
    try:
        claims = h.tokens.verify(token, audience=AUD)
    except CryptoError:
        raise error(401, "expired", "Playback pass expired") from None
    if claims.get("k") not in mode:
        raise error(404, "not_found", "Not part of this stream")
    return claims


def _provider(h: Hub, claims: dict[str, Any], ext: str) -> str:
    """The provider address. Never logged or returned."""
    try:
        server, user, password = h.tv_catalog._login()
    except TvAppError as exc:
        raise error(exc.status, exc.code, exc.message) from None
    folder = {"movie": "movie", "episode": "series"}.get(claims["k"], "live")
    return f"{server}/{folder}/{quote(user)}/{quote(password)}/{quote(str(claims['s']))}.{ext}"


def _client(h: Hub, timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=60), follow_redirects=True,
                             transport=getattr(h.tv_catalog, "_transport", None),
                             headers={"User-Agent": "ThomasCreekHub"})


async def _relay(client: httpx.AsyncClient, url: str, headers: dict[str, str], media: str | None) -> Response:
    try:
        upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise error(502, "unavailable", "The TV source didn't answer") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise error(502, "unavailable", "The TV source didn't answer")
    keep = {k: upstream.headers[k] for k in ("content-length", "content-range", "last-modified", "etag")
            if k in upstream.headers}
    keep["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    kind = media or upstream.headers.get("content-type") or "application/octet-stream"
    return StreamingResponse(body(), status_code=upstream.status_code, headers=keep, media_type=kind)


@router.get("/file/{token}")
async def phone_file(token: str, request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"movie", "episode"})
    ext = str(claims.get("x") or "mp4")
    headers = {"Range": request.headers["range"]} if request.headers.get("range") else {}
    media = {"mp4": "video/mp4", "m4v": "video/x-m4v", "mov": "video/quicktime"}.get(ext)
    return await _relay(_client(h), _provider(h, claims, ext), headers, media)


# -- live channels (HLS) -------------------------------------------------------------------------------------------
class _Live:
    """Per pass: the provider's playlist address after its redirect, and numbered provider lines."""

    MAX_SESSIONS = 40
    MAX_REFS = 400

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def get(self, sid: str) -> dict[str, Any]:
        row = self.sessions.get(sid)
        if row is None:
            row = {"media": None, "refs": OrderedDict(), "by_url": {}, "next": 0}
            self.sessions[sid] = row
            while len(self.sessions) > self.MAX_SESSIONS:
                self.sessions.popitem(last=False)
        self.sessions.move_to_end(sid)
        row["at"] = time.time()
        return row

    def number(self, row: dict[str, Any], url: str) -> int:
        n = row["by_url"].get(url)
        if n is None:
            n = row["next"]
            row["next"] += 1
            row["refs"][n] = url
            row["by_url"][url] = n
            while len(row["refs"]) > self.MAX_REFS:
                _old, gone = row["refs"].popitem(last=False)
                row["by_url"].pop(gone, None)
        return n


LIVE = _Live()


def _outside(url: str) -> bool:
    """A provider line may only send the hub to the internet, never to the house network."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def rewrite(text: str, base_url: str, row: dict[str, Any], token: str) -> str:
    """Every URI in a provider playlist becomes a hub path. Playlists (variants) end in .m3u8, media in .ts."""
    root = f"/v1/tv/phone/live/{token}"

    def hub_path(ref: str) -> str | None:
        absolute = urljoin(base_url, ref.strip())
        if not _outside(absolute):
            return None
        n = LIVE.number(row, absolute)
        is_list = absolute.split("?", 1)[0].lower().endswith((".m3u8", ".m3u"))
        return f"{root}/p/{n}.m3u8" if is_list else f"{root}/s/{n}"

    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                head, _, rest = stripped.partition('URI="')
                ref, _, tail = rest.partition('"')
                target = hub_path(ref)
                if target is None:
                    continue
                line = f'{head}URI="{target}"{tail}'
            out.append(line)
        elif stripped:
            target = hub_path(stripped)
            if target is not None:
                out.append(target)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


async def _playlist(h: Hub, url: str) -> tuple[str, str]:
    async with _client(h, 15) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            raise error(502, "unavailable", "The channel didn't answer") from None
    if resp.status_code >= 400 or "#EXTM3U" not in resp.text[:200]:
        raise error(502, "unavailable", "The channel didn't answer")
    return resp.text, str(resp.url)


def _m3u8(text: str) -> Response:
    return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


@router.get("/live/{token}/index.m3u8")
async def phone_live(token: str, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"live"})
    row = LIVE.get(str(claims.get("sid") or token[-16:]))
    text = None
    if row["media"]:
        # After the first answer, reload the provider's own (redirected) playlist, not the login address
        try:
            text, final = await _playlist(h, row["media"])
        except Exception:  # noqa: BLE001
            text = None
    if text is None:
        text, final = await _playlist(h, _provider(h, claims, "m3u8"))
    row["media"] = final
    return _m3u8(rewrite(text, final, row, token))


@router.get("/live/{token}/p/{n}.m3u8")
async def phone_live_list(token: str, n: int, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"live"})
    row = LIVE.get(str(claims.get("sid") or token[-16:]))
    url = row["refs"].get(n)
    if url is None:
        raise error(404, "not_found", "Not part of this stream")
    text, final = await _playlist(h, url)
    return _m3u8(rewrite(text, final, row, token))


@router.get("/live/{token}/s/{n}")
async def phone_live_segment(token: str, n: int, request: Request, h: Hub = Depends(hub)) -> Response:
    claims = _claims(h, token, {"live"})
    row = LIVE.get(str(claims.get("sid") or token[-16:]))
    url = row["refs"].get(n)
    if url is None:
        raise error(404, "not_found", "Not part of this stream")
    headers = {"Range": request.headers["range"]} if request.headers.get("range") else {}
    return await _relay(_client(h), url, headers, None)
