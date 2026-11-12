"""Request authentication and the secure-tier check."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..crypto import CryptoError, confirm_message, new_nonce, verify_signature
from ..policy import Subject
from ..state import Hub

NONCE_TTL_S = 120


def hub(request: Request) -> Hub:
    return request.app.state.hub


class HubHTTPError(HTTPException):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(status_code=status, detail=body)


def error(status: int, error: str, message: str | None = None, **extra: Any) -> HubHTTPError:
    body: dict[str, Any] = {"error": error}
    if message:
        body["message"] = message
    body.update(extra)
    return HubHTTPError(status, body)


def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    body = exc.detail if isinstance(exc.detail, dict) else {"error": "error", "message": str(exc.detail)}
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def caller_ip(request: Request, h: Hub) -> str | None:
    peer = request.client.host if request.client else None
    return h.client_ip(peer, request.headers.get("x-forwarded-for"))


@dataclass
class Caller:
    subject: Subject
    device: dict[str, Any]
    ip: str | None


PAGE_COOKIE = "tc_page"


def _token_from(request: Request, allow_cookie: bool) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # The page cookie only works for the holiday-lights pages, never the rest of the API.
    return request.cookies.get(PAGE_COOKIE) if allow_cookie else None


def caller(request: Request, h: Hub = Depends(hub), *, allow_cookie: bool = False) -> Caller:
    token = _token_from(request, allow_cookie)
    if not token:
        raise error(401, "unauthorized", "Sign in from the app")
    try:
        claims = h.tokens.verify(token)
    except CryptoError:
        raise error(401, "unauthorized", "Token expired") from None
    device = h.db.device(claims["sub"])
    if device is None or device["status"] == "revoked":
        raise error(401, "revoked", "This device was removed from the house")
    ip = caller_ip(request, h)
    h.db.update("devices", "id", device["id"], {"last_seen_at": time.time()})
    return Caller(h.subject_for(device, h.is_home(ip)), device, ip)


def issue_nonce(h: Hub, device_id: str) -> str:
    nonce = new_nonce()
    now = time.time()
    h.db.execute("DELETE FROM nonces WHERE expires_at < ?", [now])
    h.db.insert("nonces", {"nonce": nonce, "device_id": device_id, "expires_at": now + NONCE_TTL_S, "used": 0})
    return nonce


def consume_nonce(h: Hub, device_id: str, nonce: str) -> bool:
    cur = h.db.execute(
        "UPDATE nonces SET used=1 WHERE nonce=? AND device_id=? AND used=0 AND expires_at>=?",
        [nonce, device_id, time.time()],
    )
    return cur.rowcount == 1


async def require_confirm(request: Request, h: Hub, c: Caller, tier: str) -> None:
    """Secure and admin tiers need a signature from the biometric-bound key.

    The header is `X-TC-Confirm: <nonce>.<base64 ES256 signature>` where the
    signature covers `<nonce>.<hex sha256 of the raw request body>`. Without it
    the hub answers 428 with a fresh nonce.
    """
    if tier not in ("secure", "admin"):
        return
    secure_key = c.device.get("secure_public_key")
    if not secure_key:
        raise error(403, "forbidden", "This device can't confirm with Face ID", ask_parent=False)
    header = request.headers.get("x-tc-confirm")
    if not header or "." not in header:
        raise error(428, "confirm_required", "Confirm with Face ID", nonce=issue_nonce(h, c.device["id"]))
    nonce, sig = header.split(".", 1)
    body = await request.body()
    if not consume_nonce(h, c.device["id"], nonce) or not verify_signature(secure_key, confirm_message(nonce, body), sig):
        raise error(428, "confirm_required", "Face ID check failed, try again", nonce=issue_nonce(h, c.device["id"]))


def need(h: Hub, c: Caller, cap: str) -> None:
    if not h.policy.has(c.subject, cap):
        raise error(403, "forbidden", "Not available for this person", ask_parent=c.subject.is_child_like)
