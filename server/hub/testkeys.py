"""Test keys: let Simulator builds join the hub as a person without Dylan approving each one.

Dylan makes a key on the hub web page (/admin, Test simulators). It is shown once and
kept on his Mac at ~/.thomascreek/test-key. The app reads that file only in Simulator
builds (`#if targetEnvironment(simulator)`), so a real phone never sends it.

A device that enrolls with a valid key:
  - must still be on the home network (same rule as every phone),
  - must say it is a simulator,
  - is made active straight away for the key's person (Dylan), with kind "test",
  - shows a "Test" tag in People and on the web page,
  - can't take a VPN client from the Firewalla pool.

Only the SHA-256 of a key is stored. Turning a key off also removes every device it let in.
Real phones are unchanged: they still wait as "pending" until Dylan says whose they are.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING, Any

from .db import new_id

if TYPE_CHECKING:
    from .state import Hub

PREFIX = "tctest_"
MAX_ACTIVE_PER_KEY = 30
DEFAULT_DAYS = 365


def _hash(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def create(h: "Hub", person_id: str, label: str, created_by: str | None, days: int | None = DEFAULT_DAYS) -> tuple[dict[str, Any], str]:
    """Make a key. Returns (row, the key itself); the key is never stored or shown again."""
    key = PREFIX + secrets.token_urlsafe(32)
    kid = new_id("tk")
    h.db.insert("test_keys", {
        "id": kid, "key_hash": _hash(key), "label": label[:60] or "Test simulators", "person_id": person_id,
        "created_by": created_by, "created_at": time.time(),
        "expires_at": time.time() + days * 86400 if days else None,
    })
    h.db.log("admin.test_key_create", "ok", person_id=created_by, target=kid, params={"for": person_id, "days": days},
             source="web")
    return h.db.one("SELECT * FROM test_keys WHERE id=?", [kid]), key  # type: ignore[return-value]


def check(h: "Hub", key: str) -> dict[str, Any] | None:
    """The live key row for this key, or None (unknown, turned off, or expired)."""
    if not key or not key.strip().startswith(PREFIX):
        return None
    row = h.db.one("SELECT * FROM test_keys WHERE key_hash=?", [_hash(key)])
    if row is None or row.get("revoked_at"):
        return None
    if row.get("expires_at") and row["expires_at"] < time.time():
        return None
    if h.db.person(row["person_id"]) is None:
        return None
    return row


def active_devices(h: "Hub", key_id: str) -> list[dict[str, Any]]:
    return h.db.all("SELECT * FROM devices WHERE test_key_id=? AND status!='revoked' ORDER BY created_at", [key_id])


def keys(h: "Hub") -> list[dict[str, Any]]:
    return h.db.all("SELECT * FROM test_keys WHERE revoked_at IS NULL ORDER BY created_at")


def revoke(h: "Hub", key_id: str, by: str | None) -> int:
    """Turn a key off and remove the devices it let in. Returns how many devices were removed."""
    row = h.db.one("SELECT * FROM test_keys WHERE id=?", [key_id])
    if row is None:
        return 0
    h.db.update("test_keys", "id", key_id, {"revoked_at": time.time()})
    n = 0
    for d in active_devices(h, key_id):
        h.revoke_device(d, reason="test key turned off")
        n += 1
    h.db.log("admin.test_key_revoke", "ok", person_id=by, target=key_id, params={"devices": n}, source="web")
    return n
