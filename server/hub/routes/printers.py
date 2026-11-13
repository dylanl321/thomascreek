"""The workshop: Bambu printers, their camera, and the shared library of sliced models.

Controls go through POST /v1/actions (printer.control, printer.light, printer.speed,
printer.temp, printer.print) so the usual permission and Face ID checks apply.
Children can watch a print and ask for a model, but not control anything.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..bambu import MAX_UPLOAD, PrinterError
from ..state import Hub
from .deps import Caller, caller, error, hub

router = APIRouter(prefix="/v1/printers", tags=["printers"])

KID_FIELDS = ("id", "name", "model", "state", "online", "set_up", "job", "ams", "active_slot")


def _can_watch(h: Hub, c: Caller) -> bool:
    return h.policy.has(c.subject, "appliances") or c.subject.role == "child"


def _need_watch(h: Hub, c: Caller) -> None:
    if not _can_watch(h, c):
        raise error(403, "forbidden", "Not available for this person")


def _need_manage(h: Hub, c: Caller) -> None:
    if not h.policy.has(c.subject, "appliances"):
        raise error(403, "forbidden", "Ask a parent", ask_parent=c.subject.is_child_like)


def _wrap(exc: PrinterError):
    return error(exc.status, exc.error, exc.message)


def _view(h: Hub, c: Caller, s: dict[str, Any]) -> dict[str, Any]:
    if h.policy.has(c.subject, "appliances"):
        actions = [a for a in ("printer.control", "printer.light", "printer.speed", "printer.temp", "printer.print")]
        return {**s, "actions": actions}
    return {k: s.get(k) for k in KID_FIELDS} | {"actions": []}


@router.get("")
async def printers(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Every printer with its full live status (temperatures, AMS, job)."""
    _need_watch(h, c)
    return {"printers": [_view(h, c, p.status()) for p in h.workshop.printers.values()]}


# -- library (before /{pid} so "library" isn't read as a printer id) ------------
@router.get("/library")
async def library(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Sliced models saved on the hub, newest or most recently printed first."""
    _need_watch(h, c)
    return {"models": h.workshop.library()}


@router.post("/library")
async def add_model(file: UploadFile = File(...), title: str | None = Form(default=None),
                    c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Add a sliced .gcode.3mf (Bambu Studio: Export plate sliced file)."""
    _need_manage(h, c)
    data = await file.read(MAX_UPLOAD + 1)
    try:
        item = await asyncio.to_thread(h.workshop.add, file.filename or "model.gcode.3mf", data, c.subject.person_id, title)
    except PrinterError as exc:
        raise _wrap(exc) from None
    h.db.log("printer.library.add", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=item["id"],
             params={"title": item["title"]})
    return item


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=80)


@router.patch("/library/{mid}")
async def rename_model(mid: str, body: Rename, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _need_manage(h, c)
    try:
        return h.workshop.rename(mid, body.title)
    except PrinterError as exc:
        raise _wrap(exc) from None


@router.delete("/library/{mid}")
async def delete_model(mid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _need_manage(h, c)
    try:
        h.workshop.remove(mid)
    except PrinterError as exc:
        raise _wrap(exc) from None
    h.db.log("printer.library.remove", "ok", person_id=c.subject.person_id, device_id=c.device["id"], target=mid)
    return {"ok": True}


@router.get("/library/{mid}/thumb.png")
async def model_thumb(mid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    _need_watch(h, c)
    try:
        data = h.workshop.thumb(mid)
    except PrinterError as exc:
        raise _wrap(exc) from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/library/{mid}/ask")
async def ask_for_model(mid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A child asks a grown-up to print something."""
    _need_watch(h, c)
    try:
        item = h.workshop.item(mid)
    except PrinterError as exc:
        raise _wrap(exc) from None
    who = (h.db.person(c.subject.person_id) or {}).get("name") or "Someone"
    await h.notify.send(title=f"{who} would like a print", body=f"{item['title']}. Open the Workshop to print it.",
                        cat="printer", roles=["admin", "adult"], ref=f"model:{mid}", group_key=f"ask:{mid}")
    return {"ok": True, "message": "Sent to the grown-ups"}


# -- one printer -------------------------------------------------------------------
@router.get("/{pid}")
async def printer(pid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _need_watch(h, c)
    try:
        return _view(h, c, h.workshop.get(pid).status())
    except PrinterError as exc:
        raise _wrap(exc) from None


@router.get("/{pid}/snapshot.jpg")
async def printer_snapshot(pid: str, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> Response:
    """The chamber camera's latest frame (the camera starts when first asked, so the first one takes a few seconds)."""
    _need_watch(h, c)
    try:
        grab = h.workshop.get(pid).frames()
    except PrinterError as exc:
        raise _wrap(exc) from None
    frame = await grab.latest()
    if not frame:
        raise error(503, "camera_unavailable", grab.error or "The printer camera isn't sending pictures yet")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/{pid}/mjpeg")
async def printer_mjpeg(pid: str, request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> StreamingResponse:
    """The chamber camera as a multipart MJPEG stream, about 2 frames a second."""
    _need_watch(h, c)
    try:
        grab = h.workshop.get(pid).frames()
    except PrinterError as exc:
        raise _wrap(exc) from None

    async def body():
        last = 0.0
        while not await request.is_disconnected():
            frame = await grab.latest(wait=8)
            if frame is None:
                break
            if grab.frame_at != last:
                last = grab.frame_at
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            else:
                await asyncio.sleep(0.25)

    return StreamingResponse(body(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})
