"""The Now playing Live Activity, started, updated and ended by the hub (#204).

Each phone sends its push-to-start token for NowPlayingAttributes, and each activity's own update token
once it starts. When a TV the person may control starts playing, the hub starts the activity on their
phones; when what's on changes it updates it; when nothing is on any more it ends it. The card lists
every TV that is on. A person can switch it off ("now_playing" in their alert settings).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

log = logging.getLogger("hub.nowplaying")

KV_START = "la_np_start."   # + device id: {"token", "env"}
KV_LIVE = "la_np."          # + device id: {"tokens": [{"id", "token", "env"}], "sig", "started_at"}
# A start push whose update token never came back (the phone was off): start again after this.
START_WAIT_S = 10 * 60
MAX_TVS = 6
ROLES = ("admin", "adult", "child", "guest")


def set_start_token(h: Any, device_id: str, token: str | None, env: str) -> None:
    h.db.kv_set(KV_START + device_id, {"token": token, "env": env} if token else None)


def add_activity(h: Any, device_id: str, activity_id: str, token: str, env: str) -> None:
    st = _live(h, device_id)
    rows = [r for r in st.get("tokens") or [] if r.get("id") != activity_id and r.get("token") != token]
    rows.append({"id": activity_id, "token": token, "env": env})
    st["tokens"] = rows[-3:]
    h.db.kv_set(KV_LIVE + device_id, st)


def _live(h: Any, device_id: str) -> dict[str, Any]:
    st = h.db.kv_get(KV_LIVE + device_id)
    return dict(st) if isinstance(st, dict) else {}


def tvs_for(h: Any, person: dict[str, Any]) -> list[dict[str, Any]]:
    """TVs that are on and that this person may control, as the activity shows them."""
    from .tvhandoff import devices

    home = True
    try:
        seen = h.household.presence(person).get("home")
        home = True if seen is None else bool(seen)
    except Exception:  # noqa: BLE001 - presence is a nicety here
        pass
    subject = h.subject_for_person(person, home)
    out = []
    for row in devices(h, subject):
        playing = row.get("playing") if isinstance(row.get("playing"), dict) else None
        title = str((playing or {}).get("title") or (playing or {}).get("name") or "").strip()
        if not row.get("online") or not title:
            continue
        out.append({"id": str(row["id"]), "name": str(row.get("name") or "TV"), "title": title[:80],
                    "kind": str((playing or {}).get("kind") or "")})
    return out[:MAX_TVS]


def state(tvs: list[dict[str, Any]]) -> dict[str, Any]:
    """The activity's content-state. `title` and `room` stay for app builds that only know those."""
    first = tvs[0] if tvs else {"title": "Nothing on", "name": ""}
    return {"title": first["title"], "room": first["name"], "tvs": tvs, "count": len(tvs)}


def _sig(tvs: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(tvs, sort_keys=True).encode()).hexdigest()[:16]


async def _send(h: Any, token: str, env: str, payload: dict[str, Any]) -> Any:
    return await h.apns.send(token, payload, env=env or "production", push_type="liveactivity",
                             topic_suffix=".push-type.liveactivity", priority=10)


def wants(h: Any, person_id: str) -> bool:
    try:
        return bool(h.alerts.prefs(person_id).get("now_playing", True))
    except Exception:  # noqa: BLE001
        return True


async def sync(h: Any, now: float | None = None) -> dict[str, int]:
    """Bring every phone's Now playing activity in line with the TVs. Pushes only on a change."""
    counts = {"started": 0, "updated": 0, "ended": 0}
    apns = getattr(h, "apns", None)
    if apns is None or not apns.enabled:
        return counts
    now = time.time() if now is None else now
    stamp = int(now)
    for person in h.db.people_with_role(*ROLES):
        tvs = tvs_for(h, person) if wants(h, person["id"]) else []
        sig = _sig(tvs)
        content = state(tvs)
        for device in h.db.devices(person["id"]):
            if device.get("status") != "active":
                continue
            did = device["id"]
            st = _live(h, did)
            tokens = st.get("tokens") or []
            if not tvs:
                if tokens or st.get("started_at"):
                    for t in tokens:
                        await _send(h, t["token"], t.get("env"), {"aps": {
                            "timestamp": stamp, "event": "end", "content-state": content, "dismissal-date": stamp}})
                        counts["ended"] += 1
                    h.db.kv_set(KV_LIVE + did, None)
                continue
            if tokens:
                if st.get("sig") == sig:
                    continue
                for t in tokens:
                    res = await _send(h, t["token"], t.get("env"), {"aps": {
                        "timestamp": stamp, "event": "update", "content-state": content}})
                    if getattr(res, "token_dead", False):
                        st["tokens"] = [x for x in st.get("tokens") or [] if x is not t]
                    counts["updated"] += 1
                st["sig"] = sig
                h.db.kv_set(KV_LIVE + did, st)
                continue
            # Nothing running on this phone yet: start it by push, once (the phone answers with its token).
            if st.get("started_at") and now - float(st["started_at"]) < START_WAIT_S:
                continue
            start = h.db.kv_get(KV_START + did)
            if not isinstance(start, dict) or not start.get("token"):
                continue
            first = tvs[0]
            more = f" and {len(tvs) - 1} more" if len(tvs) > 1 else ""
            res = await _send(h, start["token"], start.get("env"), {"aps": {
                "timestamp": stamp, "event": "start", "content-state": content,
                "attributes-type": "NowPlayingAttributes", "attributes": {"deviceId": "house"},
                "alert": {"title": first["title"], "body": f"On {first['name']}{more}"},
            }})
            if getattr(res, "token_dead", False):
                set_start_token(h, did, None, "production")
                continue
            if getattr(res, "ok", False):
                counts["started"] += 1
                h.db.kv_set(KV_LIVE + did, {"tokens": [], "sig": sig, "started_at": now})
    return counts


def kick(h: Any) -> None:
    """A TV reported what's on: sync soon, once, off the request."""
    if getattr(h, "_np_pending", False):
        return

    async def run() -> None:
        try:
            await asyncio.sleep(2)
            await sync(h)
        except Exception as exc:  # noqa: BLE001
            log.info("now playing sync failed: %s", type(exc).__name__)
        finally:
            h._np_pending = False

    try:
        asyncio.get_running_loop().create_task(run())
        h._np_pending = True
    except RuntimeError:
        pass
