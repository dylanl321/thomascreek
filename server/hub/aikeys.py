"""AI connection keys: let an outside AI app (Claude Desktop, Claude Code, any MCP client) use the hub's MCP server.

Dylan makes a key on the hub web page (/admin, AI connections) for one person. It is shown once.
An AI app that sends it acts as that person, with exactly that person's rights, the same way
"Ask Thomas Creek" does (hub/mcp.py runs the assistant's own tools).

Each key gets its own device row (kind "ai", no Face ID key), so:
  - every change it makes is in the activity log under that device,
  - removing the device or turning the key off stops it straight away,
  - a Face ID step can never be satisfied by the key itself: it becomes an approval on the
    person's own iPhone (see hub/mcp.py).

Only the SHA-256 of a key is stored.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING, Any

from .db import new_id

if TYPE_CHECKING:
    from .state import Hub

PREFIX = "tcai_"


def _hash(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def create(h: "Hub", person_id: str, label: str, created_by: str | None) -> tuple[dict[str, Any], str]:
    """Make a key and its device. Returns (row, the key itself); the key is never stored or shown again."""
    person = h.db.person(person_id)
    if person is None:
        raise ValueError("no such person")
    key = PREFIX + secrets.token_urlsafe(32)
    kid = new_id("ak")
    did = new_id("dev")
    now = time.time()
    label = (label or "").strip()[:60] or "AI app"
    h.db.insert("devices", {
        "id": did, "person_id": person_id, "name": f"{label} (AI)", "model": "AI connection", "platform": "mcp",
        "public_key": f"ai-key:{kid}", "status": "active", "kind": "ai", "created_at": now,
    })
    h.db.insert("ai_keys", {
        "id": kid, "key_hash": _hash(key), "label": label, "person_id": person_id, "device_id": did,
        "created_by": created_by, "created_at": now,
    })
    h.db.log("admin.ai_key_create", "ok", person_id=created_by, target=kid, params={"for": person_id}, source="web")
    return h.db.one("SELECT * FROM ai_keys WHERE id=?", [kid]), key  # type: ignore[return-value]


def check(h: "Hub", key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(key row, device row) for a live key, or None (unknown, turned off, device removed, person gone)."""
    if not key or not key.strip().startswith(PREFIX):
        return None
    row = h.db.one("SELECT * FROM ai_keys WHERE key_hash=?", [_hash(key)])
    if row is None or row.get("revoked_at"):
        return None
    device = h.db.device(row["device_id"])
    if device is None or device.get("status") != "active" or device.get("person_id") != row["person_id"]:
        return None
    if h.db.person(row["person_id"]) is None:
        return None
    return row, device


def touch(h: "Hub", key_id: str) -> None:
    h.db.update("ai_keys", "id", key_id, {"last_used_at": time.time()})


def keys(h: "Hub") -> list[dict[str, Any]]:
    return h.db.all("SELECT * FROM ai_keys WHERE revoked_at IS NULL ORDER BY created_at")


def revoke(h: "Hub", key_id: str, by: str | None) -> bool:
    row = h.db.one("SELECT * FROM ai_keys WHERE id=?", [key_id])
    if row is None or row.get("revoked_at"):
        return False
    h.db.update("ai_keys", "id", key_id, {"revoked_at": time.time()})
    device = h.db.device(row["device_id"])
    if device is not None and device.get("status") != "revoked":
        h.revoke_device(device, reason="AI key turned off")
    h.db.log("admin.ai_key_revoke", "ok", person_id=by, target=key_id, source="web")
    return True
