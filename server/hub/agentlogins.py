"""Agent logins: a narrow, logged admin login for Claude agents on the home network (Dylan, Nov 23 2026).

Routine admin steps (reading the admin pages, approving the house's cloud media worker) kept waiting for Dylan's
Face ID sign-in. An agent login lets a Claude session on the home network do just those steps, and nothing else:

  - It is made inside the hub container (`hub agent-login create NAME`; the key is printed once), or by whoever
    edits the hub's app on the NAS: `HUB_AGENT_LOGINS=name:sha256` in its environment adds that login at start
    (the key stays on the agent's machine, the NAS only holds its hash). Only the SHA-256 is kept. There is no
    route that makes one, so a key can't make another key. A login turned off is never brought back by the env.
  - It is sent as `Authorization: Bearer tcagent_...` and works only from where the web page opens (home network
    or home VPN), never from away.
  - Scopes: `admin.read` (GET of the /admin pages and their JSON status, never a form post), `cloud.approve`
    (approve or turn away a waiting cloud worker, by the code its page shows) and `test.pair` (let in a TV,
    emulator or phone that is waiting to join, a phone only as Dylan's test device; routes/agentpair.py, Dylan,
    Nov 24 2026). No secrets, no people, no keys.
  - A login the NAS env names is given any scope added since it was made (never one turned off).
  - Every use is written to the activity log with source "agent" and the login's name.
  - Dylan turns one off on the hub web page (Security, Agent logins) or with `hub agent-login revoke ID`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from .db import new_id

PREFIX = "tcagent_"
KV = "agent.logins"
SCOPES = ("admin.read", "cloud.approve", "test.pair")
DEFAULT_DAYS = 180


def _hash(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def logins(db: Any) -> list[dict[str, Any]]:
    """Every agent login, newest first, without the hash."""
    rows = [dict(r) for r in (db.kv_get(KV) or [])]
    for r in rows:
        r.pop("hash", None)
    return sorted(rows, key=lambda r: -float(r.get("created_at") or 0))


def create(db: Any, name: str, days: int | None = DEFAULT_DAYS, scopes: tuple[str, ...] = SCOPES,
           by: str = "cli") -> tuple[dict[str, Any], str]:
    """Make a login. Returns (row, the key); the key is never stored or shown again."""
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope: {', '.join(bad)}")
    key = PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    row = {"id": new_id("ag"), "name": (name.strip() or "Claude agent")[:60], "hash": _hash(key),
           "scopes": list(scopes), "created_at": now, "expires_at": now + days * 86400 if days else None,
           "last_used_at": None, "revoked_at": None}
    db.kv_set(KV, [*(db.kv_get(KV) or []), row])
    db.log("agent.login_create", "ok", target=row["id"], params={"name": row["name"], "scopes": row["scopes"],
                                                                  "days": days}, source=by)
    shown = dict(row)
    shown.pop("hash")
    return shown, key


def seed_from_env(db: Any, value: str | None = None) -> list[str]:
    """Add the logins named in HUB_AGENT_LOGINS ("name:sha256hex", comma separated) that the hub hasn't seen."""
    import os
    import re

    value = os.environ.get("HUB_AGENT_LOGINS", "") if value is None else value
    rows = list(db.kv_get(KV) or [])
    known = {str(r.get("hash")) for r in rows}
    made: list[str] = []
    grown = False
    for item in (v.strip() for v in value.split(",")):
        name, _, digest = item.rpartition(":")
        digest = digest.strip().lower()
        if not name.strip() or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        if digest in known:
            for r in rows:
                if r.get("hash") == digest and not r.get("revoked_at") and not set(SCOPES) <= set(r.get("scopes", [])):
                    r["scopes"] = [*r.get("scopes", []), *(s for s in SCOPES if s not in r.get("scopes", []))]
                    grown = True
                    db.log("agent.login_scopes", "ok", target=r["id"], params={"name": r["name"],
                                                                                "scopes": r["scopes"]}, source="nas")
            continue
        now = time.time()
        row = {"id": new_id("ag"), "name": name.strip()[:60], "hash": digest, "scopes": list(SCOPES),
               "created_at": now, "expires_at": None, "last_used_at": None, "revoked_at": None}
        rows.append(row)
        known.add(digest)
        made.append(str(row["id"]))
        db.log("agent.login_create", "ok", target=row["id"], params={"name": row["name"], "scopes": row["scopes"],
                                                                      "from": "env"}, source="nas")
    if made or grown:
        db.kv_set(KV, rows)
    return made


def revoke(db: Any, login_id: str, by: str = "web") -> bool:
    rows = list(db.kv_get(KV) or [])
    hit = False
    for r in rows:
        if r.get("id") == login_id and not r.get("revoked_at"):
            r["revoked_at"] = time.time()
            hit = True
    if hit:
        db.kv_set(KV, rows)
        db.log("agent.login_revoke", "ok", target=login_id, source=by)
    return hit


def check(db: Any, key: str, scope: str) -> dict[str, Any] | None:
    """The live login for this key if it holds `scope`, else None. Marks it used (at most once a minute)."""
    key = (key or "").strip()
    if not key.startswith(PREFIX):
        return None
    digest = _hash(key)
    rows = list(db.kv_get(KV) or [])
    now = time.time()
    for r in rows:
        if not hmac.compare_digest(str(r.get("hash") or ""), digest):
            continue
        if r.get("revoked_at") or (r.get("expires_at") and r["expires_at"] < now) or scope not in r.get("scopes", []):
            return None
        if now - float(r.get("last_used_at") or 0) > 60:
            r["last_used_at"] = now
            db.kv_set(KV, rows)
        out = dict(r)
        out.pop("hash", None)
        return out
    return None


def bearer(headers: Any) -> str:
    auth = str(headers.get("authorization") or "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""
