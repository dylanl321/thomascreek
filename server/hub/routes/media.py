"""Watch, requests and approvals, the kids' built-in player, pipeline."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..adapters.base import AdapterError
from ..crypto import CryptoError
from ..db import new_id
from ..home import iso
from ..state import Hub
from .deps import Caller, caller, error, hub, need, require_confirm
from .home import perform

router = APIRouter(prefix="/v1", tags=["media"])

STREAM_AUD = "plex-stream"


def _rating_ok(h: Hub, c: Caller, item: dict[str, Any]) -> bool:
    if c.subject.role in ("admin", "adult", "tablet"):
        return True
    return item.get("rating") in h.config.child_policy.ratings


def _filter(h: Hub, c: Caller, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if _rating_ok(h, c, i)]


def _watch_cap(h: Hub, c: Caller) -> None:
    if not (h.policy.has(c.subject, "media.all") or h.policy.has(c.subject, "media.own_tv")):
        raise error(403, "forbidden", "Not available", ask_parent=c.subject.is_child_like)


@router.get("/media/continue")
async def continue_watching(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _watch_cap(h, c)
    try:
        return {"items": _filter(h, c, await h.plex.continue_watching())}
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None


@router.get("/media/search")
async def search(q: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _watch_cap(h, c)
    q = q.strip()[:100]
    if not q:
        return {"items": []}
    try:
        items = _filter(h, c, await h.plex.search(q))
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    out: dict[str, Any] = {"items": items}
    # Not in the library yet? Offer things that can be requested.
    if h.policy.has(c.subject, "media.request") and h.jellyseerr.enabled:
        from .media_requests import _allowed_for_child, _childlike, _result

        try:
            have = {(i["title"] or "").lower() for i in items}
            found = [_result(r) for r in await h.jellyseerr.search_raw(q) if r.get("title") or r.get("name")]
            found = [f for f in found if f["availability"] != "ready" and (f["title"] or "").lower() not in have]
            out["requestable"] = (await _allowed_for_child(h, found)) if _childlike(c) else found[:12]
        except AdapterError:
            pass
    return out


@router.get("/media/library")
async def library(rated: bool = False, kind: str | None = None, limit: int = 200, c: Caller = Depends(caller),
                  h: Hub = Depends(hub)) -> dict[str, Any]:
    """Browse the library. Children always get the rated list; rated=1 asks for it explicitly."""
    _watch_cap(h, c)
    try:
        items = await h.plex.library(kind=kind, limit=max(1, min(limit, 500)))
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if rated or c.subject.role not in ("admin", "adult", "tablet"):
        items = [i for i in items if i.get("rating") in h.config.child_policy.ratings]
    return {"items": items}


@router.get("/media/art")
async def art(path: str, width: int = 400, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    try:
        data, ctype = await h.plex.art(path, max(64, min(width, 1200)))
    except (ValueError, AdapterError):
        raise error(404, "not_found", "No artwork") from None
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/media/player-art/{entity_id}")
async def player_art(entity_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    if h.home.device(entity_id, c.subject) is None:
        raise error(404, "not_found", "No artwork")
    pic = h.home.states.get(entity_id, {}).get("attributes", {}).get("entity_picture")
    if not pic or not pic.startswith("/api/"):
        raise error(404, "not_found", "No artwork")
    try:
        resp = await h.ha.request("GET", pic)
    except AdapterError:
        raise error(404, "not_found", "No artwork") from None
    return Response(resp.content, media_type=resp.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=300"})


class PlayBody(BaseModel):
    item_id: str = Field(max_length=40)
    target_device_id: str = Field(max_length=200)


@router.post("/media/play")
async def play(body: PlayBody, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    try:
        item = await h.plex.metadata(body.item_id)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if item is None or not _rating_ok(h, c, item):
        raise error(404, "not_found", "That isn't in the library")
    ctype, cid = h.plex.ha_play_payload(item)
    return await perform(request, c, h, "media.play", body.target_device_id,
                         {"item_id": body.item_id, "_content_type": ctype, "_content_id": cid})


# -- the kids' built-in player: HLS through the hub, so no Plex token on the iPad
@router.get("/media/{item_id}/stream")
async def stream(item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _watch_cap(h, c)
    try:
        item = await h.plex.metadata(item_id)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    if item is None or not _rating_ok(h, c, item):
        raise error(404, "not_found", "That isn't in the library")
    pass_token, exp = h.tokens.issue_scoped({"aud": STREAM_AUD, "item": item_id, "dev": c.device["id"]}, 4 * 3600)
    h.db.log("media.stream", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=item_id,
             params={"title": item["title"]})
    return {"hls_url": f"/v1/media/hls/{pass_token}/start.m3u8", "expires_at": iso(exp)}


@router.get("/media/hls/{pass_token}/{path:path}")
async def hls(pass_token: str, path: str, request: Request, h: Hub = Depends(hub)) -> Response:
    try:
        claims = h.tokens.verify(pass_token, audience=STREAM_AUD)
    except CryptoError:
        raise error(401, "expired", "Playback pass expired") from None
    session = f"tc-{claims['dev'][-12:]}-{claims['item']}"
    if path == "start.m3u8":
        params = {
            "path": f"/library/metadata/{claims['item']}", "protocol": "hls", "directPlay": 0, "directStream": 1,
            "videoResolution": "1280x720", "maxVideoBitrate": 4000, "session": session, "mediaIndex": 0,
            "fastSeek": 1, "X-Plex-Platform": "iOS",
        }
        upstream = "/video/:/transcode/universal/start.m3u8"
    elif path.startswith("session/") and ".." not in path:
        params = dict(request.query_params)
        upstream = f"/video/:/transcode/universal/{path}"
    else:
        raise error(404, "not_found", "Not part of this stream")
    try:
        resp = await h.plex.request("GET", upstream, params=params)
    except AdapterError:
        raise error(502, "unavailable", "The movie server didn't answer") from None
    return Response(resp.content, media_type=resp.headers.get("content-type", "application/vnd.apple.mpegurl"))


# -- downloads for offline viewing ---------------------------------------------
FILE_AUD = "media-file"
FILE_TTL_S = 24 * 3600


async def _downloadable(h: Hub, c: Caller, item_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The title and its file info, if this person may watch it. Children: rated titles only, no asking."""
    _watch_cap(h, c)
    if item_id.startswith("yt-"):
        # A YouTube video the hub saved (hub/youtube.py): the whole family may watch what the kids added.
        v = h.youtube.video(item_id[3:])
        path = h.youtube.file(v) if v else None
        if v is None or path is None:
            raise error(404, "not_found", "That video isn't ready yet")
        media = {"local_path": str(path), "size": v.get("bytes"), "duration_ms": int((v.get("duration_s") or 0) * 1000),
                 "video_codec": "h264", "container": "mp4", "resolution": f"{v['height']}p" if v.get("height") else None}
        return h.youtube.item(v), media
    try:
        item = await h.plex.metadata(item_id)
        media = await h.plex.media_info(item_id) if item else None
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    # A grown-up sent it to this person's iPad (hub/sends.py): that's the permission, whatever its rating.
    sent = item is not None and not _rating_ok(h, c, item) and await h.sends.allowed(c.subject.person_id, item_id)
    if item is None or media is None or not (_rating_ok(h, c, item) or sent):
        raise error(404, "not_found", "That isn't in the library")
    if item["kind"] not in ("movie", "episode"):
        raise error(400, "bad_request", "Pick a film or an episode to download")
    return item, media


def _job_out(h: Hub, job: Any) -> dict[str, Any]:
    out = job.out()
    if job.status == "ready":
        token, exp = h.tokens.issue_scoped({"aud": FILE_AUD, "job": job.id, "dev": job.device_id}, FILE_TTL_S)
        out["url"] = f"/v1/media/file/{token}"
        out["expires_at"] = iso(exp)
    return out


@router.get("/media/{item_id}/episodes")
async def episodes(item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A show's episodes, to pick which to download. Children: only shows with an allowed rating."""
    _watch_cap(h, c)
    try:
        show = await h.plex.metadata(item_id)
        if show is None or not _rating_ok(h, c, show):
            raise error(404, "not_found", "That isn't in the library")
        items = await h.plex.episodes(item_id)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    # Episodes often carry no rating of their own; they inherit the show's.
    for i in items:
        i["rating"] = i.get("rating") or show.get("rating")
    return {"items": _filter(h, c, items)}


@router.get("/media/{item_id}/download")
async def download_options(item_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    from ..downloads import options

    item, media = await _downloadable(h, c, item_id)
    return {"item": item, "options": options(media)}


class DownloadBody(BaseModel):
    quality: str = Field(default="original", pattern="^(original|high|small)$")


@router.post("/media/{item_id}/download")
async def start_download(item_id: str, body: DownloadBody, c: Caller = Depends(caller),
                         h: Hub = Depends(hub)) -> dict[str, Any]:
    from ..downloads import original_mode

    item, media = await _downloadable(h, c, item_id)
    quality = body.quality
    if (quality == "original" and not media.get("local_path") and original_mode(media) is None
            and item_id in h.sends.cached_ids(c.subject.person_id)):
        quality = "high"  # a send asked for the original, which an iPad can't play as it is
    job = await h.downloads.start(item_id, quality, media, item, c.device["id"], c.subject.person_id)
    h.db.log("media.download", "ok" if job.status != "failed" else "error", person_id=c.subject.person_id,
             device_id=c.device["id"], target=item_id, params={"title": item["title"], "quality": quality})
    return _job_out(h, job)


@router.get("/media/downloads/{job_id}")
async def download_status(job_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    job = h.downloads.get(job_id)
    if job is None or job.device_id != c.device["id"]:
        raise error(404, "not_found", "That download has expired. Start it again.")
    return _job_out(h, job)


@router.get("/media/file/{pass_token}")
async def download_file(pass_token: str, request: Request, h: Hub = Depends(hub)) -> Response:
    try:
        claims = h.tokens.verify(pass_token, audience=FILE_AUD)
    except CryptoError:
        raise error(401, "expired", "Download link expired") from None
    job = h.downloads.get(claims["job"])
    if job is None or job.device_id != claims["dev"] or job.status != "ready":
        raise error(404, "not_found", "That download has expired. Start it again.")
    name = f"{job.item_id}.mp4"
    if job.path is not None:
        return FileResponse(job.path, media_type="video/mp4", filename=name)
    # Original MP4 straight from Plex, with Range so iOS can resume.
    plex = h.plex
    headers = plex.auth_headers()
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        client = plex.client()
        upstream = await client.send(client.build_request("GET", job.passthrough, headers=headers), stream=True)
    except Exception:  # noqa: BLE001
        raise error(502, "unavailable", "The movie server didn't answer") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise error(502, "unavailable", "The movie server didn't answer")
    keep = {k: upstream.headers[k] for k in ("content-length", "content-range", "last-modified", "etag")
            if k in upstream.headers}
    keep["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, media_type="video/mp4", headers=keep)


class ProgressBody(BaseModel):
    offset_s: float = Field(ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    watched: bool = False


@router.post("/media/{item_id}/progress")
async def report_progress(item_id: str, body: ProgressBody, c: Caller = Depends(caller),
                          h: Hub = Depends(hub)) -> dict[str, Any]:
    """Where a downloaded title was stopped (sent when the phone is back online)."""
    _watch_cap(h, c)
    if body.watched and c.subject.person_id and h.sends.all(c.subject.person_id):
        h.sends.watched(c.subject.person_id, item_id)  # moves "next episodes" sends along
    if item_id.startswith("yt-"):
        if h.youtube.video(item_id[3:]):
            h.youtube.played(item_id[3:], body.offset_s, body.watched)
        return {"ok": True}
    try:
        await h.plex.report_progress(item_id, int(body.offset_s * 1000),
                                     int(body.duration_s * 1000) if body.duration_s else None, body.watched)
    except AdapterError as exc:
        raise error(503, "unavailable", str(exc)) from None
    return {"ok": True}


# -- requests and approvals -----------------------------------------------
class RequestBody(BaseModel):
    kind: str = Field(default="media", pattern="^(media|internet|reward|other)$")
    media_id: str | None = Field(default=None, max_length=60)
    text: str = Field(default="", max_length=300)
    minutes: int | None = Field(default=None, ge=5, le=240)
    reward_id: str | None = Field(default=None, max_length=60)
    title: str | None = Field(default=None, max_length=200)
    poster_url: str | None = Field(default=None, max_length=400)


def _req_out(r: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    out = {"id": r["id"], "kind": r["kind"], "title": r["title"], "poster_url": r.get("poster_url"),
           "who": names.get(r["requester_id"]), "status": r["status"], "created_at": iso(r["created_at"])}
    if r["payload"].get("progress") is not None:
        out["progress"] = r["payload"]["progress"]
    return out


@router.get("/requests")
async def list_requests(mine: bool = False, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    s = c.subject
    if not s.person_id:
        return {"items": []}
    approver = not mine and any(h.policy.has(s, cap) for cap in ("media.approve", "chores.manage", "network.manage"))
    if approver:
        rows = h.db.all("SELECT * FROM requests ORDER BY created_at DESC LIMIT 100")
    else:
        rows = h.db.all("SELECT * FROM requests WHERE requester_id=? ORDER BY created_at DESC LIMIT 50", [s.person_id])
    await _refresh_media_status(h, rows)
    names = {p["id"]: p["name"] for p in h.db.people()}
    return {"items": [_req_out(r, names) for r in rows]}


async def _refresh_media_status(h: Hub, rows: list[dict[str, Any]]) -> None:
    if not h.jellyseerr.enabled:
        return
    for r in rows:
        if r["kind"] != "media" or r["status"] not in ("approved", "downloading") or not r.get("external_id"):
            continue
        try:
            data = await h.jellyseerr.request_status(r["external_id"])
        except AdapterError:
            return
        media_status = (data.get("media") or {}).get("status")
        new = {5: "ready", 4: "downloading", 3: "downloading"}.get(media_status, r["status"])
        if new != r["status"]:
            h.db.update("requests", "id", r["id"], {"status": new})
            r["status"] = new
            # The "ready to watch" push is sent by media_requests.push_ready, once per request.


@router.post("/requests")
async def create_request(body: RequestBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    s = c.subject
    if not s.person_id:
        raise error(403, "pending", "Waiting for Dylan to say whose device this is")
    if body.kind == "media":
        need(h, c, "media.request")
        if not body.media_id:
            raise error(400, "bad_params", "media_id is required")
    if body.kind == "internet":
        need(h, c, "network.ask")
    title = body.title or {"internet": f"More internet time ({body.minutes or 30} min)", "reward": "A reward",
                           "other": body.text[:60] or "Something"}.get(body.kind, body.text[:60] or "A movie")
    rid = new_id("req")
    payload = {"media_id": body.media_id, "text": body.text, "minutes": body.minutes, "reward_id": body.reward_id}
    h.db.insert("requests", {"id": rid, "created_at": time.time(), "kind": body.kind, "requester_id": s.person_id,
                             "title": title, "payload": payload, "poster_url": body.poster_url, "status": "waiting"})
    h.db.log("request.create", "ok", person_id=s.person_id, device_id=c.device["id"], target=rid,
             params={"kind": body.kind, "title": title})
    # Nobody needs approval for a movie or show (Dylan, 2026-11-12).
    if body.kind == "media":
        await _approve(h, c, h.db.one("SELECT * FROM requests WHERE id=?", [rid]))  # type: ignore[arg-type]
    else:
        who = h.db.person(s.person_id)["name"]  # type: ignore[index]
        await h.notify.send(
            roles=("admin", "adult"), level="decision", cat="requests", title=f"{who} asked for {title}",
            body=body.text or None, actions=[{"id": "approve", "title": "Approve"}, {"id": "deny", "title": "Not now", "destructive": True}],
            ref=f"thomascreek://requests/{rid}", group_key=f"request:{rid}", push_category="REQUEST",
        )
    names = {p["id"]: p["name"] for p in h.db.people()}
    return _req_out(h.db.one("SELECT * FROM requests WHERE id=?", [rid]), names)  # type: ignore[arg-type]


@router.post("/requests/{req_id}/{decision}")
async def decide_route(req_id: str, decision: str, request: Request, c: Caller = Depends(caller),
                       h: Hub = Depends(hub)) -> dict[str, Any]:
    return await decide_request(req_id, decision, request, c, h)


async def decide_request(req_id: str, decision: str, request: Request, c: Caller, h: Hub) -> dict[str, Any]:
    if decision not in ("approve", "deny"):
        raise error(404, "not_found", "Unknown decision")
    r = h.db.one("SELECT * FROM requests WHERE id=?", [req_id])
    if r is None:
        raise error(404, "not_found", "No such request")
    cap = {"media": "media.approve", "internet": "network.manage", "reward": "chores.manage"}.get(r["kind"], "media.approve")
    need(h, c, cap)
    if r["status"] != "waiting":
        raise error(409, "already_decided", "Someone already answered this")
    if decision == "deny":
        h.db.update("requests", "id", req_id, {"status": "denied", "decided_by": c.subject.person_id, "decided_at": time.time()})
        h.db.log("request.deny", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=req_id)
        await h.notify.send(person_ids=[r["requester_id"]], level="fyi", cat="requests",
                            title=f"Not this time: {r['title']}", body=None, ref=f"thomascreek://requests/{req_id}")
    else:
        if r["kind"] == "internet":
            await require_confirm(request, h, c, "secure")
        await _approve(h, c, r)
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), f"request:{req_id}"])
    names = {p["id"]: p["name"] for p in h.db.people()}
    return _req_out(h.db.one("SELECT * FROM requests WHERE id=?", [req_id]), names)  # type: ignore[arg-type]


async def _approve(h: Hub, c: Caller, r: dict[str, Any]) -> None:
    live = h.actions.is_live
    status, external = "approved", None
    payload = r["payload"]
    try:
        if r["kind"] == "media" and h.jellyseerr.enabled:
            if live("media.approve"):
                res = await h.jellyseerr.create_request(payload["media_id"])
                external = str(res.get("id")) if res else None
        elif r["kind"] == "internet":
            plan = h.actions.plan(c.subject, "network.grant", r["requester_id"], {"minutes": payload.get("minutes") or 30})
            if plan.decision.allowed:
                await h.actions.run(c.subject, plan, source="approval")
        elif r["kind"] == "reward" and h.family_board.enabled and payload.get("reward_id"):
            person = h.db.person(r["requester_id"])
            if live("chores.redeem") and person and person.get("family_board_id"):
                await h.family_board.redeem(payload["reward_id"], person["family_board_id"])
    except Exception as exc:  # noqa: BLE001
        h.db.log("request.approve", "error", person_id=c.subject.person_id, target=r["id"], detail=str(exc)[:300])
        raise error(502, "failed", f"Approved, but it didn't go through: {exc}") from None
    h.db.update("requests", "id", r["id"], {"status": status, "decided_by": c.subject.person_id,
                                            "decided_at": time.time(), "external_id": external})
    h.db.log("request.approve", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=r["id"])
    if r["requester_id"] != c.subject.person_id:
        await h.notify.send(person_ids=[r["requester_id"]], level="fyi", cat="requests",
                            title=f"Yes! {r['title']}", body=None, ref=f"thomascreek://requests/{r['id']}")


# -- pipeline (admin) ------------------------------------------------------
@router.get("/pipeline")
async def pipeline(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Requests -> downloads -> recently added, plus what's coming this week.

    Each section is filled independently; a service that fails is listed in
    `failures` instead of emptying the whole screen."""
    import asyncio

    need(h, c, "pipeline")
    downloads: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []

    async def safe(source: str, coro):
        try:
            return await coro
        except AdapterError as exc:
            failures.append({"source": source, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            failures.append({"source": source, "error": f"{type(exc).__name__}"})
        return None

    jobs = []
    for arr in (h.sonarr, h.radarr):
        if arr.enabled:
            jobs += [safe(arr.name, arr.queue()), safe(arr.name, arr.upcoming()), safe(arr.name, arr.recent())]
    if h.jellyseerr.enabled:
        jobs.append(safe("jellyseerr", h.jellyseerr.recent_requests()))
    results = await asyncio.gather(*jobs)
    i = 0
    for arr in (h.sonarr, h.radarr):
        if not arr.enabled:
            continue
        q, up, rec = results[i:i + 3]
        i += 3
        for row in q or []:
            if row.get("error"):
                failures.append(row)
            downloads.append(row)
        upcoming += up or []
        recent += rec or []
    if h.jellyseerr.enabled:
        requests = results[i] or []
    upcoming.sort(key=lambda r: r.get("at") or "")
    recent.sort(key=lambda r: r.get("at") or "", reverse=True)
    configured = [a.name for a in (h.jellyseerr, h.sonarr, h.radarr) if a.enabled]
    return {"requests": requests, "downloads": downloads, "recent": recent[:20], "upcoming": upcoming[:30],
            "failures": failures, "services": configured}
