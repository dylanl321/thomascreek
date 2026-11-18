"""Add a device away from home with a code from a signed-in admin's phone.

Being on home Wi-Fi is the usual proof of belonging (`/v1/enroll`). A travel phone may never be home, so an
admin's signed-in iPhone makes a one-time join code (Face ID), names whose device it will be, and shows it as a
QR code with the hub's outside address. The new device enrolls with that code from anywhere and starts active
for that person, with no second tap.

A code is 20 random characters, kept only as a SHA-256 hash, good for 10 minutes and for one device. A wrong
code is counted against the caller's address like any other away sign-in (main.away_rate_limit).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from .db import new_id

KV = "device.join_codes"
TTL_S = 10 * 60
MAX_LIVE = 5
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _live(db: Any, now: float) -> list[dict[str, Any]]:
    rows = db.kv_get(KV) or []
    return [r for r in rows if isinstance(r, dict) and not r.get("used_at") and float(r.get("expires_at") or 0) > now]


def make(db: Any, person_id: str, by_person: str | None, label: str | None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    code = "".join(secrets.choice(ALPHABET) for _ in range(20))
    row = {"id": new_id("join"), "hash": _hash(code), "person_id": person_id, "by": by_person,
           "label": (label or "").strip()[:80] or None, "created_at": now, "expires_at": now + TTL_S}
    rows = _live(db, now)[-(MAX_LIVE - 1):] + [row]
    db.kv_set(KV, rows)
    return {"id": row["id"], "code": code, "expires_at": row["expires_at"]}


def take(db: Any, code: str, now: float | None = None) -> dict[str, Any] | None:
    """The code's row, marked used, or None when it is unknown, used or expired."""
    now = time.time() if now is None else now
    if not code or len(code.strip()) != 20:
        return None
    want = _hash(code)
    rows = db.kv_get(KV) or []
    found = None
    for row in rows:
        if (isinstance(row, dict) and secrets.compare_digest(str(row.get("hash") or ""), want)
                and not row.get("used_at") and float(row.get("expires_at") or 0) > now):
            row["used_at"] = now
            found = row
            break
    if found is None:
        return None
    db.kv_set(KV, [r for r in rows if isinstance(r, dict) and float(r.get("expires_at") or 0) > now - 86400])
    return found
