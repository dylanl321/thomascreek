"""The cloud media worker: lighter streams for slow connections, made on the house's cloud server (Nov 23 2026).

The worker (`cloud/worker` in this repository) runs on the cloud server and reaches the hub over the house's
private network. It joins once: it makes its own secret, sends only that secret's hash with a short code, and
Dylan approves the code on the hub web page (Watching, Cloud media). After that every call it makes carries the
secret, and the hub keeps only the hash.

While the worker is up, a phone's play answer carries `lite_url`: the same pass the hub relay takes, on the
worker's public address. The worker asks the hub for that pass's provider addresses (`/v1/cloud/open`), on the
login already leased to that phone, turns the stream into a small ladder of sizes with ffmpeg and serves it as
HLS. The provider address and login stay inside the worker: they are never logged and never reach the phone.
Without a worker nothing changes: there is no `lite_url`, and every stream plays as before.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

KV_WORKER = "cloud.worker"
KV_JOINS = "cloud.joins"
KV_ENABLED = "cloud.lite_enabled"
JOIN_TTL_S = 30 * 60
MAX_JOINS = 5
BEAT_FRESH_S = 90
CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY3479"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
LITE_NOTE = "A lighter picture for a slow connection, made on the cloud server."


class CloudError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _clean_name(value: object) -> str:
    name = re.sub(r"[^\w .-]", "", str(value or ""))[:40].strip()
    return name or "Cloud worker"


def _public_url(value: object) -> str | None:
    """The worker's public address: https only, no path, user or query. Kept out of the repository and the log."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query \
            or parts.fragment or parts.path not in ("", "/"):
        return None
    return f"https://{parts.netloc}"


def _joins(h: Any, now: float) -> list[dict[str, Any]]:
    rows = h.db.kv_get(KV_JOINS) or []
    return [r for r in rows if isinstance(r, dict) and now - float(r.get("at") or 0) < JOIN_TTL_S]


def join(h: Any, ip: str | None, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """A worker asks to join. It sends the hash of a secret it made itself; the secret never leaves it."""
    now = time.time() if now is None else now
    digest = str(body.get("secret_hash") or "").lower()
    if not _HEX64.match(digest):
        raise CloudError(400, "bad_params", "Send the hash of the worker's secret")
    worker = h.db.kv_get(KV_WORKER) or {}
    if worker.get("hash") and hmac.compare_digest(str(worker["hash"]), digest):
        return {"status": "approved"}
    rows = _joins(h, now)
    same = next((r for r in rows if r.get("hash") == digest), None)
    if same is None:
        if len(rows) >= MAX_JOINS:
            raise CloudError(429, "too_many", "Too many workers are waiting. Approve or turn some away first.")
        same = {"id": secrets.token_urlsafe(9), "hash": digest, "at": now, "ip": ip,
                "code": "".join(secrets.choice(CODE_ALPHABET) for _ in range(6)),
                "name": _clean_name(body.get("name"))}
        rows.append(same)
        h.db.kv_set(KV_JOINS, rows)
        h.db.log("cloud.join", "pending", params={"name": same["name"], "code": same["code"]}, source="cloud")
    return {"status": "pending", "join_id": same["id"], "code": same["code"],
            "message": "Approve this code on the hub web page: Watching, Cloud media."}


def poll(h: Any, join_id: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    worker = h.db.kv_get(KV_WORKER) or {}
    if worker.get("join_id") == join_id:
        return {"status": "approved"}
    row = next((r for r in _joins(h, now) if r.get("id") == join_id), None)
    if row is None:
        return {"status": "gone", "message": "That request ended. The worker asks again."}
    return {"status": "pending", "code": row["code"]}


def pending(h: Any, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    return [{"id": r["id"], "code": r["code"], "name": r["name"], "at": r["at"]} for r in _joins(h, now)]


def approve(h: Any, join_id: str, now: float | None = None) -> dict[str, Any]:
    """An admin's explicit approval on the web page. One worker at a time: approving a new one replaces the old."""
    now = time.time() if now is None else now
    rows = _joins(h, now)
    row = next((r for r in rows if r.get("id") == join_id), None)
    if row is None:
        raise CloudError(404, "not_found", "That request ended. Restart the worker to ask again.")
    worker = {"hash": row["hash"], "join_id": row["id"], "name": row["name"], "approved_at": now}
    h.db.kv_set(KV_WORKER, worker)
    h.db.kv_set(KV_JOINS, [r for r in rows if r.get("id") != join_id])
    h.db.log("cloud.join", "approved", params={"name": row["name"], "code": row["code"]}, source="admin")
    return worker


def deny(h: Any, join_id: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    h.db.kv_set(KV_JOINS, [r for r in _joins(h, now) if r.get("id") != join_id])
    h.db.log("cloud.join", "denied", target=join_id, source="admin")


def forget(h: Any) -> None:
    h.db.kv_set(KV_WORKER, {})
    h.db.log("cloud.worker", "removed", source="admin")


def check(h: Any, secret: str) -> bool:
    worker = h.db.kv_get(KV_WORKER) or {}
    digest = worker.get("hash")
    return bool(secret and digest) and hmac.compare_digest(str(digest), secret_hash(secret))


def beat(h: Any, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """The worker says it is up, where phones reach it, and how busy it is."""
    now = time.time() if now is None else now
    worker = dict(h.db.kv_get(KV_WORKER) or {})
    public = _public_url(body.get("public_url"))
    sessions = body.get("sessions")
    capacity = body.get("capacity")
    worker.update({
        "beat_at": now,
        "version": str(body.get("version") or "")[:40],
        "public_url": public,
        "sessions": sessions if isinstance(sessions, int) and 0 <= sessions < 1000 else 0,
        "capacity": capacity if isinstance(capacity, int) and 0 < capacity < 1000 else None,
        "load": round(float(body["load"]), 2) if isinstance(body.get("load"), (int, float)) else None,
    })
    h.db.kv_set(KV_WORKER, worker)
    out: dict[str, Any] = {"ok": True, "enabled": enabled(h)}
    if public is None:
        out["note"] = "Set PUBLIC_URL on the worker to an https address so phones can reach it."
    return out


def enabled(h: Any) -> bool:
    value = h.db.kv_get(KV_ENABLED)
    return value is not False


def set_enabled(h: Any, on: bool) -> None:
    h.db.kv_set(KV_ENABLED, bool(on))


def status(h: Any, now: float | None = None) -> dict[str, Any]:
    """For the web page: whether a worker is approved, up and offering lighter streams."""
    now = time.time() if now is None else now
    worker = h.db.kv_get(KV_WORKER) or {}
    beat_at = worker.get("beat_at")
    up = bool(worker.get("hash")) and isinstance(beat_at, (int, float)) and now - beat_at < BEAT_FRESH_S
    return {"approved": bool(worker.get("hash")), "up": up, "name": worker.get("name"),
            "version": worker.get("version"), "public_url": worker.get("public_url"),
            "sessions": worker.get("sessions"), "capacity": worker.get("capacity"), "load": worker.get("load"),
            "beat_at": beat_at, "enabled": enabled(h)}


def base(h: Any, now: float | None = None) -> str | None:
    """The worker's public address when it can take a stream now: approved, beating, switched on, not full."""
    s = status(h, now)
    if not (s["up"] and s["enabled"] and s["public_url"]):
        return None
    if isinstance(s["capacity"], int) and isinstance(s["sessions"], int) and s["sessions"] >= s["capacity"]:
        return None
    return str(s["public_url"])


def pass_of(path: str) -> str | None:
    """The pass inside a hub relay path (`/v1/tv/phone/file/<pass>` or `/v1/tv/phone/live/<pass>/index.m3u8`)."""
    m = re.match(r"^/v1/tv/phone/(?:file|live)/([^/?#]+)", path or "")
    return m.group(1) if m else None


def lite_url(h: Any, relay_path: str | None, now: float | None = None) -> str | None:
    """The lighter stream for a hub relay pass, on the worker's public address, or None without a worker."""
    token = pass_of(relay_path or "")
    root = base(h, now) if token else None
    return f"{root}/v/{token}/index.m3u8" if root else None


def offer(h: Any, out: dict[str, Any], relay_path: str | None, *, data_saver: bool) -> None:
    """Add the optional lighter-stream fields to a play answer.

    `lite_url` is always offered while the worker is up. With `data_saver` (the phone's own switch, or the
    player saw a slow connection and asked again) it becomes the `url` itself; the full stream stays as
    `full_url`, and the provider's own addresses stay in `alternates` / `ts_urls` as before."""
    lite = lite_url(h, relay_path)
    if not lite:
        return
    out["lite_url"] = lite
    out["lite_note"] = LITE_NOTE
    if data_saver:
        out["full_url"] = out.get("url")
        out["url"] = lite
        out["lite"] = True
        out.pop("hls", None)  # the lighter stream is always HLS
