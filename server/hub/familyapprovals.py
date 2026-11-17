"""Parent approvals from the family board, answered from the iPhone (#212).

A chore a child reported ("I did it", on a chore that needs a parent's OK) and a reward that needs
approval wait on the board. The hub lists them for adults, answers them with the board's admin key
(family_board.admin_token), and tells the adults with a push that has Approve and Not now buttons.
Children never see or answer them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .adapters.base import AdapterError

log = logging.getLogger("hub.familyapprovals")

KV_SEEN = "family.approvals.seen"
CHECK_EVERY_S = 60
KINDS = ("chore", "reward")


class NeedsKey(Exception):
    """The board's admin key isn't saved on the hub, so approvals can't be read or answered."""


def ref(kind: str, item_id: str) -> str:
    return f"thomascreek://family/approvals/{kind}/{item_id}"


def group(kind: str, item_id: str) -> str:
    return f"board:{kind}:{item_id}"


def _names(h: Any, board_people: list[dict[str, Any]]) -> dict[str, str]:
    """Board person id -> the name the house uses (the hub's own name when linked)."""
    out = {str(p.get("id")): str(p.get("name") or "") for p in board_people if isinstance(p, dict)}
    for p in h.db.people():
        if p.get("family_board_id"):
            out[str(p["family_board_id"])] = p["name"]
    return out


def shape(h: Any, snap: dict[str, Any]) -> list[dict[str, Any]]:
    names = _names(h, snap.get("people") or [])
    items = {str(i.get("id")): i for i in snap.get("items") or [] if isinstance(i, dict)}
    rewards = {str(r.get("id")): r for r in snap.get("rewards") or [] if isinstance(r, dict)}
    out = []
    for r in snap.get("reports") or []:
        if not isinstance(r, dict) or r.get("status") != "pending" or not r.get("id"):
            continue
        item = items.get(str(r.get("item_id"))) or {}
        out.append({
            "kind": "chore", "id": str(r["id"]), "title": item.get("title") or "A chore",
            "person": names.get(str(r.get("person_id") or ""), "Someone"), "date": r.get("date"),
            "note": r.get("note") or None, "points": int(item.get("points") or 0) or None,
            "at": r.get("created_at") or r.get("reported_at"),
        })
    for r in snap.get("redemptions") or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if str(r.get("state") or r.get("status") or "") != "pending" or r.get("approved") is True:
            continue
        reward = rewards.get(str(r.get("reward_id"))) or {}
        out.append({
            "kind": "reward", "id": str(r["id"]), "title": reward.get("title") or r.get("reward_title") or "A reward",
            "person": names.get(str(r.get("person_id") or ""), "Someone"), "date": None, "note": None,
            "points": int(r.get("cost") or reward.get("cost") or 0) or None,
            "at": r.get("created_at") or r.get("redeemed_at"),
        })
    out.sort(key=lambda row: str(row.get("at") or ""))
    return out


async def pending(h: Any) -> list[dict[str, Any]]:
    board = h.family_board
    if not board.can_approve():
        raise NeedsKey()
    return shape(h, await board.admin_snapshot())


async def answer(h: Any, kind: str, item_id: str, approve: bool) -> dict[str, Any]:
    board = h.family_board
    if not board.can_approve():
        raise NeedsKey()
    result = await board.review(kind, item_id, approve)
    h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), group(kind, item_id)])
    return result


def _line(row: dict[str, Any]) -> tuple[str, str | None]:
    if row["kind"] == "chore":
        title = f"{row['person']} says they did {row['title']}"
    else:
        title = f"{row['person']} wants {row['title']}"
    bits = []
    if row.get("points"):
        bits.append(f"{row['points']} points")
    if row.get("note"):
        bits.append(f"“{row['note']}”")
    return title, " · ".join(bits) or None


async def check(h: Any) -> int:
    """Push the adults about approvals that are new since the last look, and clear answered ones.

    The first look after the key is saved only remembers what is waiting (it is all on the Family tab),
    so saving the key doesn't set off a burst of pushes."""
    cfg = getattr(getattr(h, "config", None), "family_board", None)
    if cfg is None or not cfg.enabled or not h.family_board.can_approve():
        return 0
    try:
        rows = await pending(h)
    except (AdapterError, NeedsKey) as exc:
        log.info("approvals not read: %s", type(exc).__name__)
        return 0
    now_ids = {group(r["kind"], r["id"]) for r in rows}
    seen = h.db.kv_get(KV_SEEN)
    first = not isinstance(seen, list)
    seen_ids = set(seen or [])
    sent = 0
    if not first:
        for row in rows:
            key = group(row["kind"], row["id"])
            if key in seen_ids:
                continue
            title, body = _line(row)
            await h.notify.send(
                roles=("admin", "adult"), level="decision", cat="requests", title=title, body=body,
                actions=[{"id": "approve", "title": "Approve"}, {"id": "deny", "title": "Not now", "destructive": True}],
                ref=ref(row["kind"], row["id"]), group_key=key, push_category="APPROVAL", sort=False,
            )
            sent += 1
    # Answered on the board itself: clear the notice from every adult's inbox.
    for key in seen_ids - now_ids:
        h.db.execute("UPDATE inbox SET acked_at=? WHERE group_key=? AND acked_at IS NULL", [time.time(), key])
    h.db.kv_set(KV_SEEN, sorted(now_ids))
    return sent
