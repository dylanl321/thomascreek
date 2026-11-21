"""Who is watching each TV.

The hub keeps one answer per TV so Continue watching, history and the Home rows follow a person from screen to
screen. The TV's "Who's watching?" picker opens on the hub's guess, so most of the time it is a confirmation:

1. A launcher TV pinned to one person is that person.
2. A recent claim: someone picked themselves on the TV, pressed "I'm watching" on their phone, or sent a show
   to the TV from their phone. A claim lasts while the TV keeps reporting and fades after CLAIM_S of quiet.
3. On a grown-up TV, the one adult who is home. On a kids' TV, the one child who is home.
4. Otherwise nobody, and the TV keeps its own history (the fallback every TV had before).

Kids first: a kids' TV is only ever a child's. A child can claim a TV for themselves only when that does not
take over a grown-up's show, and never names anyone else. Naming a child on a grown-up TV only narrows it.
"""

from __future__ import annotations

import time
from typing import Any

from .tvlauncher import view as launcher_view

CLAIM_S = 4 * 3600
REASONS = {
    "pinned": "This TV is set up for {name}",
    "claimed": "{name} is watching",
    "phone": "{name} sent something to this TV",
    "home": "{name} is the only one home",
}
_ROLES = ("admin", "adult", "child")


class WhoError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def people(h: Any) -> list[dict[str, Any]]:
    """The household (no guests, tablets or pending), with whether each is home when the hub can tell."""
    out = []
    try:
        rows = h.db.people()
    except Exception:  # noqa: BLE001 - a TV must not fail on the people table
        return out
    household = getattr(h, "household", None)
    for p in rows:
        if p.get("role") not in _ROLES:
            continue
        if p.get("expires_at") and p["expires_at"] < time.time():
            continue
        first = str(p.get("name") or "").strip().split(" ")[0][:40]
        if not first:
            continue
        home = None
        if household is not None:
            try:
                home = household.presence(p)["home"]
            except Exception:  # noqa: BLE001 - presence is a hint, never a failure
                home = None
        out.append({"id": str(p["id"]), "name": first, "role": p["role"], "avatar": p.get("avatar"), "home": home})
    return out


def choices(h: Any, tv: dict[str, Any]) -> list[dict[str, Any]]:
    """Who the picker offers on this TV. A kids' TV offers children only."""
    rows = people(h)
    if tv.get("profile") == "kids":
        rows = [p for p in rows if p["role"] == "child"]
    order = {"admin": 0, "adult": 0, "child": 1}
    rows.sort(key=lambda p: (p["home"] is False, order.get(p["role"], 2), p["name"].casefold()))
    return rows


def allowed_on(tv: dict[str, Any], person: dict[str, Any] | None) -> bool:
    """A kids' TV is only a child's. Anyone in the household may be named on a grown-up TV (a child narrows it)."""
    if person is None:
        return True
    if person.get("role") not in _ROLES:
        return False
    return tv.get("profile") != "kids" or person.get("role") == "child"


def current(h: Any, install_id: str, now: float | None = None,
            household: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """{person_id, name, role, reason, line, since} for one TV. person_id is None when the hub can't tell.
    `household` is `people(h)` read once by a caller asking for many TVs (the TV list read it once per TV)."""
    now = time.time() if now is None else now
    tv = h.tv_app.tvs().get(install_id) or {}
    everyone = {p["id"]: p for p in (people(h) if household is None else household)}
    found = _guess(tv, everyone, now)
    if found is None:
        return {"person_id": None, "name": None, "role": None, "reason": None, "line": None, "since": None}
    person, reason, since = found
    return {
        "person_id": person["id"], "name": person["name"], "role": person["role"], "reason": reason,
        "line": REASONS[reason].format(name=person["name"]), "since": since,
    }


def _guess(tv: dict[str, Any], everyone: dict[str, dict[str, Any]], now: float):
    launcher = launcher_view(tv)
    pinned = everyone.get(str(launcher.get("person_id") or "")) if launcher.get("on") else None
    if pinned is not None and allowed_on(tv, pinned):
        return pinned, "pinned", None
    claim = tv.get("watching")
    if isinstance(claim, dict) and claim.get("person_id"):
        fresh = now - float(claim.get("at") or 0) < CLAIM_S
        person = everyone.get(str(claim["person_id"]))
        if fresh and person is not None and allowed_on(tv, person):
            return person, ("phone" if claim.get("by") == "play" else "claimed"), claim.get("since") or claim.get("at")
    if isinstance(claim, dict) and claim.get("person_id") is None and claim.get("by") in ("tv", "phone") \
            and now - float(claim.get("at") or 0) < CLAIM_S:
        # Someone said "nobody" on purpose: keep the TV's own history rather than guess.
        return None
    kids = tv.get("profile") == "kids"
    home = [p for p in everyone.values() if p["home"] is True and (p["role"] == "child") == kids]
    if len(home) == 1:
        return home[0], "home", None
    return None


def claim(h: Any, install_id: str, person_id: str | None, *, by: str, caller: dict[str, Any] | None = None,
          now: float | None = None) -> dict[str, Any]:
    """Name who is watching. `by` is tv (the picker), phone ("I'm watching") or play (a phone sent a show).

    `caller` is the phone's person when the phone asks; None for the TV itself. Raises WhoError when the
    person can't be named here."""
    now = time.time() if now is None else now
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    if tv is None or tv.get("status") != "approved":
        raise WhoError(404, "not_found", "That TV isn't registered")
    everyone = {p["id"]: p for p in people(h)}
    person = None
    if person_id:
        person = everyone.get(str(person_id))
        if person is None:
            raise WhoError(404, "not_found", "That person isn't in the household")
        if not allowed_on(tv, person):
            raise WhoError(403, "kids_tv", f"{tv.get('name') or 'This TV'} is a kids' TV")
    if caller is not None:
        _check_caller(tv, everyone, caller, person, now)
    old = tv.get("watching") if isinstance(tv.get("watching"), dict) else {}
    same = old.get("person_id") == (person["id"] if person else None)
    tv["watching"] = {
        "person_id": person["id"] if person else None,
        "by": by,
        "at": now,
        "since": old.get("since") if same and old.get("since") else now,
    }
    tvs[install_id] = tv
    h.tv_app._save_tvs(tvs)
    shown = current(h, install_id, now)
    if not same:
        h.db.log("tv.watching", "ok", person_id=(caller or {}).get("id"), device_id=f"tv:{install_id}",
                 target=install_id, params={"person": shown["person_id"], "by": by}, source="tv" if by == "tv" else "app")
        announce(h, install_id, shown, by=by)
    return shown


def refresh(h: Any, install_id: str, person_id: str | None, now: float | None = None) -> None:
    """The TV's heartbeat says who it is playing for. Keep that claim fresh; ignore anyone not allowed here."""
    if not person_id:
        return
    now = time.time() if now is None else now
    tvs = h.tv_app.tvs()
    tv = tvs.get(install_id)
    if tv is None:
        return
    everyone = {p["id"]: p for p in people(h)}
    person = everyone.get(str(person_id))
    if person is None or not allowed_on(tv, person):
        return
    old = tv.get("watching") if isinstance(tv.get("watching"), dict) else {}
    if old.get("person_id") == person["id"]:
        old["at"] = now
        tv["watching"] = old
        tvs[install_id] = tv
        h.tv_app._save_tvs(tvs)
        return
    try:
        claim(h, install_id, person["id"], by="tv", now=now)
    except WhoError:
        return


def claim_for_play(h: Any, install_id: str, person_id: str | None, role: str | None) -> None:
    """A phone sent a show to a TV: that person is probably the one watching. Never raises."""
    if not person_id or role not in _ROLES:
        return
    try:
        claim(h, install_id, person_id, by="play", caller={"id": person_id, "role": role})
    except WhoError:
        return


def _check_caller(tv: dict[str, Any], everyone: dict[str, dict[str, Any]], caller: dict[str, Any],
                  person: dict[str, Any] | None, now: float) -> None:
    role = caller.get("role")
    if role in ("admin", "adult"):
        return
    if role != "child":
        raise WhoError(403, "forbidden", "Only the family can say who's watching")
    if person is None or person["id"] != caller.get("id"):
        raise WhoError(403, "forbidden", "You can only say you're watching")
    if tv.get("profile") == "kids":
        return
    claim = tv.get("watching")
    other = everyone.get(str((claim or {}).get("person_id") or ""))
    busy = other is not None and other["role"] != "child" and now - float((claim or {}).get("at") or 0) < CLAIM_S \
        and isinstance(tv.get("playing"), dict)
    if busy:
        raise WhoError(409, "busy", f"{other['name']} is watching. Ask them first.")


def announce(h: Any, install_id: str, shown: dict[str, Any], *, by: str) -> None:
    """Tell the TV (so its picker and token follow) and the phones (so the Now playing card does)."""
    data = {"install_id": install_id, "by": by, **{k: shown[k] for k in ("person_id", "name", "role", "reason", "line")}}
    h.bus.publish({"type": "tv_watching", "data": data}, device_id=f"tv:{install_id}")
    try:
        h.bus.publish({"type": "tv", "data": {**h.tv_app.watch_event(install_id), "watching": shown}})
    except Exception:  # noqa: BLE001 - the phones' card is a nicety
        pass


def public(h: Any, install_id: str, now: float | None = None) -> dict[str, Any]:
    """The TV's answer for GET /v1/tv/watching: the guess and who the picker offers."""
    tv = h.tv_app.tvs().get(install_id) or {}
    return {"watching": current(h, install_id, now), "choices": choices(h, tv), "claim_s": CLAIM_S}
