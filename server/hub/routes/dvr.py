"""Recordings made on the hub for channels that have no catch-up.

POST   /v1/dvr/recordings                 schedule one programme
POST   /v1/dvr/recordings/now             record the channel from now
POST   /v1/dvr/rules                      record matching titles when they air
GET    /v1/dvr/recordings                 list
GET    /v1/dvr/recordings/{id}
DELETE /v1/dvr/recordings/{id}
POST   /v1/dvr/recordings/{id}/stop
GET    /v1/dvr/recordings/{id}/stream     the file, with HTTP Range
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..dvr import DvrError, parse_time
from ..state import Hub
from .deps import Caller, caller, error, hub
from .tvguide import _media

router = APIRouter(prefix="/v1/dvr", tags=["dvr"])


class ScheduleIn(BaseModel):
    channel_key: str = Field(max_length=140)
    start: str = Field(max_length=40)
    end: str = Field(max_length=40)
    title: str | None = Field(default=None, max_length=120)
    programme_key: str | None = Field(default=None, max_length=40)
    pad_before_s: int = 60
    pad_after_s: int = 180


class NowIn(BaseModel):
    channel_key: str = Field(max_length=140)
    duration_s: int = 3600
    title: str | None = Field(default=None, max_length=120)


class RuleIn(BaseModel):
    channel_key: str | None = Field(default=None, max_length=140)
    title_match: str = Field(max_length=80)
    new_only: bool = False


def _err(exc: DvrError):
    return error(exc.status, exc.code, exc.message)


def _pads(before: int, after: int) -> tuple[int, int]:
    if not 0 <= before <= 600 or not 0 <= after <= 1800:
        raise error(400, "bad_request", "pad_before_s is 0 to 600 and pad_after_s is 0 to 1800")
    return before, after


@router.post("/recordings", status_code=201)
async def schedule(body: ScheduleIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> JSONResponse:
    kids = _media(h, c)
    before, after = _pads(body.pad_before_s, body.pad_after_s)
    try:
        row = await h.dvr.schedule(channel_key=body.channel_key, start=parse_time(body.start), end=parse_time(body.end),
                                   title=body.title, programme=body.programme_key, pad_before_s=before,
                                   pad_after_s=after, kids_only=kids)
    except DvrError as exc:
        raise _err(exc) from None
    return JSONResponse(row, status_code=201)


@router.post("/recordings/now", status_code=201)
async def record_now(body: NowIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> JSONResponse:
    kids = _media(h, c)
    if not 60 <= body.duration_s <= 8 * 3600:
        raise error(400, "bad_request", "duration_s is 60 to 28800")
    try:
        row = await h.dvr.record_now(channel_key=body.channel_key, duration_s=body.duration_s, title=body.title,
                                     kids_only=kids)
    except DvrError as exc:
        raise _err(exc) from None
    return JSONResponse(row, status_code=201)


@router.post("/rules", status_code=201)
async def add_rule(body: RuleIn, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> JSONResponse:
    _media(h, c)
    try:
        rule = h.dvr.add_rule(channel_key=body.channel_key, title_match=body.title_match, new_only=body.new_only)
    except DvrError as exc:
        raise _err(exc) from None
    return JSONResponse(rule, status_code=201)


@router.get("/recordings")
async def list_recordings(status: str | None = None, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict:
    kids = _media(h, c)
    rows = [h.dvr.public(row) for row in h.dvr.recordings() if h.dvr.visible(row, kids=kids)
            and (status is None or row.get("status") == status)]
    rows.sort(key=lambda row: row.get("start") or "", reverse=True)
    return {"recordings": rows}


@router.get("/recordings/{recording_id}")
async def one_recording(recording_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict:
    kids = _media(h, c)
    try:
        row = h.dvr.get(recording_id)
    except DvrError as exc:
        raise _err(exc) from None
    if not h.dvr.visible(row, kids=kids):
        raise error(404, "not_found", "That recording isn't on the hub")
    return h.dvr.public(row)


@router.delete("/recordings/{recording_id}")
async def delete_recording(recording_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict:
    kids = _media(h, c)
    try:
        row = h.dvr.get(recording_id)
        if not h.dvr.visible(row, kids=kids):
            raise DvrError(404, "not_found", "That recording isn't on the hub")
        await h.dvr.delete(recording_id)
    except DvrError as exc:
        raise _err(exc) from None
    return {"ok": True}


@router.post("/recordings/{recording_id}/stop")
async def stop_recording(recording_id: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict:
    kids = _media(h, c)
    try:
        row = h.dvr.get(recording_id)
        if not h.dvr.visible(row, kids=kids):
            raise DvrError(404, "not_found", "That recording isn't on the hub")
        return await h.dvr.stop(recording_id)
    except DvrError as exc:
        raise _err(exc) from None


@router.get("/recordings/{recording_id}/stream")
async def stream(recording_id: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    kids = _media(h, c)
    try:
        row = h.dvr.get(recording_id)
        if not h.dvr.visible(row, kids=kids):
            raise DvrError(404, "not_found", "That recording isn't on the hub")
        path = h.dvr.file_for(recording_id)
    except DvrError as exc:
        raise _err(exc) from None
    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200
    header = request.headers.get("range")
    if header:
        if not header.startswith("bytes=") or "," in header:
            raise error(416, "bad_range", "Ask for a single byte range")
        piece = header[6:]
        left, _, right = piece.partition("-")
        try:
            start = int(left) if left else 0
            end = int(right) if right else size - 1
        except ValueError:
            raise error(416, "bad_range", "Ask for a single byte range") from None
        if start < 0 or start >= size or end < start:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        status = 206
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(end - start + 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(len(data)),
        "Content-Type": "video/mp2t",
        "Cache-Control": "private, no-cache",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(data, status_code=status, headers=headers, media_type="video/mp2t")
