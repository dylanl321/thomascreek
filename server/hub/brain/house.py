"""The house brain's jobs. Detection is plain Python over what the hub already knows; the model only words it.

1. plain_cards   - appliances that finished, lights on for a day, outside doors left open (inside ones after a day)
2. left_on       - media and windows left on/open when everyone's out
3. health        - devices gone unavailable, the Home Assistant link down, NAS alerts, failing services
4. briefing      - a morning card: weather, today, chores, trash, packages, what finished downloading overnight
5. catch_up      - "while you were out", for the person who just got home
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .jobs import CARDS_SCHEMA, NOTE_SCHEMA, Finding, Job, Trigger

if TYPE_CHECKING:
    from . import Brain

DEAD = ("unavailable", "unknown", None, "")
APPLIANCE = re.compile(r"wash|dryer|dishwasher|laundry", re.I)
RUNNING = {"on", "running", "run", "wash", "washing", "rinse", "rinsing", "spin", "spinning", "dry", "drying",
           "drain", "cleaning", "in_use", "active", "heating", "cooling_down"}
WATCHED_DOMAINS = ("light", "switch", "lock", "cover", "climate", "media_player", "fan")
OCCUPANCY = ("motion", "occupancy", "presence")
OPENINGS = ("door", "window", "opening")
LIGHT_DAY_S = 24 * 3600      # a light counts as "left on" after a full day
OUTSIDE_OPEN_S = 30 * 60     # an outside door left open
INSIDE_OPEN_S = 24 * 3600    # closets, inside doors, windows: only when it has been a day

VOICE = ("You write for the family's home app, Thomas Creek. Short, warm, plain words, like a thoughtful housemate: "
         "no exclamation marks, no emoji, no 'I noticed'. Use the names given. Never invent facts that aren't in the "
         "input. Set unsure=true only if the input is contradictory or you can't tell what matters.")

CARD_SYSTEM = VOICE + (" For each input item return one entry with the same key: a title of at most 7 words, a "
                       "detail of at most 18 words (or empty), and priority 30-80 (higher = more worth seeing now; "
                       "something running up a bill or left open while away is higher than an FYI).")


# -- helpers ------------------------------------------------------------------------------------------------------
def _ts(v: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(v)).timestamp() if v else None
    except ValueError:
        return None


def _name(b: "Brain", eid: str) -> str:
    return ((b.hub.home.states.get(eid) or {}).get("attributes") or {}).get("friendly_name") or eid


def _room(b: "Brain", eid: str) -> str | None:
    aid = b.hub.home.entity_area.get(eid)
    return (b.hub.home.areas.get(aid) or {}).get("name") if aid else None


def _clock(b: "Brain", ts: float) -> str:
    d = datetime.fromtimestamp(ts, b.hub.policy.tz)
    return d.strftime("%-I:%M %p").lower().replace(":00 ", " ")


def _kid_areas(b: "Brain") -> set[str]:
    out: set[str] = set()
    for p in b.hub.db.people_with_role("child"):
        a = p.get("areas")
        out |= set(json.loads(a) if isinstance(a, str) else a or [])
    return out


def _words(words: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out = {}
    for w in (words or {}).get("items") or []:
        if isinstance(w, dict) and w.get("key") and w.get("title"):
            out[str(w["key"])] = w
    return out


def _cards_from(b: "Brain", job: str, run_id: str, finding: Finding, words: dict[str, Any] | None,
                ttl_s: float, look: dict[str, tuple[str, str, str]]) -> dict[str, Any]:
    """One card per item: the model's words when it gave them, the template's otherwise."""
    w = _words(words)
    made = []
    today = b.now().date().isoformat()
    for it in finding.items:
        said = w.get(it["key"]) or {}
        tint, symbol, eyebrow = look.get(it["kind"], ("accent", "sparkles", "House"))
        try:
            prio = max(30, min(80, int(said.get("priority", it.get("priority", 55)))))
        except (TypeError, ValueError):
            prio = 55
        key = it["key"] if len(it["key"]) <= 60 else it["key"].split(":", 1)[0] + ":" + _short(it["key"])
        card = {"id": f"brain_{job}:{key}:{today}", "priority": prio, "tint": tint, "symbol": symbol,
                "eyebrow": eyebrow, "title": (said.get("title") or it["say"])[:80],
                "why": f"House brain · {it.get('why') or finding.summary}"[:160]}
        detail = said.get("detail") if said else it.get("detail")
        if detail:
            card["detail"] = str(detail)[:160]
        if it.get("ref"):
            card["ref"] = it["ref"]
        cid = b.add_card(job=job, run_id=run_id, card=card, audience=finding.audience, ttl_s=ttl_s,
                         buttons=[x for x in it.get("buttons") or [] if x] or [{"kind": "done", "title": "Got it"}])
        made.append(cid)
    b.drop_cards(job, keep=set(made))
    return {"cards": made, "outcome": "suggested" if made else "nothing"}


def _short(s: str) -> str:
    import hashlib

    return hashlib.sha1(s.encode()).hexdigest()[:10]


def _off_steps(eids: list[str]) -> list[dict[str, Any]]:
    out = []
    for e in eids:
        if e.startswith("light."):
            out.append({"action": "light.set", "target": e, "params": {"on": False}})
        elif e.startswith("switch."):
            out.append({"action": "switch.set", "target": e, "params": {"on": False}})
        elif e.startswith("fan."):
            out.append({"action": "fan.set", "target": e, "params": {"on": False}})
        elif e.startswith("media_player."):
            out.append({"action": "media.command", "target": e, "params": {"command": "power", "on": False}})
    return out


def _off_button(eids: list[str], label: str = "Turn off") -> dict[str, Any] | None:
    steps = _off_steps(eids)
    if not steps:
        return None
    return {"kind": "action", "title": label, "action": "steps.run",
            "params": {"steps": steps, "label": label}, "done": "Off"}


def _running(st: dict[str, Any] | None) -> bool:
    if not st:
        return False
    s = str(st.get("state") or "").lower()
    unit = ((st.get("attributes") or {}).get("unit_of_measurement") or "").lower()
    if unit in ("w", "kw"):
        try:
            return float(s) * (1000 if unit == "kw" else 1) > 5
        except ValueError:
            return False
    return s in RUNNING


# -- 1. plain-language cards --------------------------------------------------------------------------------------
def plain_on_state(b: "Brain", eid: str, old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    if not new or not APPLIANCE.search(f"{eid} {(new.get('attributes') or {}).get('friendly_name', '')}"):
        return False
    if new.get("state") in DEAD:
        return False
    if _running(old) and not _running(new):
        name = (new.get("attributes") or {}).get("friendly_name") or eid
        name = re.sub(r"\s+(power|status|state|operation|run state|machine state)$", "", name, flags=re.I)
        b.memory.setdefault("appliance_done", {})[eid] = {"at": time.time(), "name": name}
        return True
    if _running(new) and not _running(old):
        b.memory.setdefault("appliance_done", {}).pop(eid, None)  # started again: the old "done" is moot
    return False


async def plain_detect(b: "Brain", trig: Trigger) -> Finding | None:
    now = time.time()
    states = b.hub.home.states
    items: list[dict[str, Any]] = []
    done = b.memory.get("appliance_done") or {}
    for eid, d in sorted(done.items()):
        mins = int((now - d["at"]) / 60)
        if mins > 120:
            continue
        items.append({"key": f"done:{eid}", "kind": "appliance_done", "name": d["name"], "minutes_ago": mins,
                      "say": f"{d['name']} finished", "detail": f"{mins} min ago" if mins else "Just now",
                      "why": "an appliance just finished", "priority": 62,
                      "buttons": [{"kind": "done", "title": "Got it", "done": "Thanks"}]})
    covered = {e for v in b.cards.values() if v["job"] == "left_on" for e in v.get("covers", [])}
    # Only lights left on for a day or more. A lamp on for a few hours is not worth a card (Dylan, Nov 15).
    long_on = []
    for eid, st in sorted(states.items()):
        if not eid.startswith("light.") or st.get("state") != "on" or eid in covered:
            continue
        if eid not in b.hub.home.entity_area:
            continue
        since = _ts(st.get("last_changed"))
        if since and now - since >= LIGHT_DAY_S:
            long_on.append((eid, since))
    if long_on:
        eids = [e for e, _ in long_on[:6]]
        first = min(t for _, t in long_on)
        names = [f"{_name(b, e)}" + (f" ({_room(b, e)})" if _room(b, e) else "") for e in eids]
        span = _human_span(now - first)
        items.append({"key": "on_since:" + ",".join(eids), "kind": "light_on_long", "lights": names,
                      "on_for": span,
                      "say": (f"{_name(b, eids[0])} on for {span}" if len(eids) == 1
                              else f"{len(eids)} lights on for {span}"),
                      "detail": ", ".join(names[:4]), "why": "lights left on for a day or more", "priority": 48,
                      "buttons": [_off_button(eids), {"kind": "done", "title": "Leave them", "primary": False}]})
    # Outside doors left open are a security matter (30 min). Inside doors and windows only after a day.
    outside = set(b.hub.alerts.sources().get("outside_doors") or [])
    opened = 0
    for eid, st in sorted(states.items()):
        attrs = st.get("attributes") or {}
        if not eid.startswith("binary_sensor.") or attrs.get("device_class") not in OPENINGS:
            continue
        if st.get("state") != "on":
            continue
        since = _ts(st.get("last_changed"))
        if not since:
            continue
        is_out = _outside_door(eid, attrs, outside)
        if now - since < (OUTSIDE_OPEN_S if is_out else INSIDE_OPEN_S):
            continue
        mins = int((now - since) / 60)
        items.append({"key": f"open:{eid}", "kind": "open_long", "name": _name(b, eid), "minutes": mins,
                      "say": f"{_name(b, eid)} open for {_human_minutes(mins)}",
                      "why": ("an outside door has been open a while" if is_out
                              else "a door or window has been open for a day or more"),
                      "priority": 72 if is_out else 50})
        opened += 1
        if opened >= 3:
            break
    if not items:
        b.drop_cards("plain_cards")
        return None
    return Finding(items=_hashable(items), summary="; ".join(i["say"] for i in items))


def _human_minutes(m: int) -> str:
    if m < 90:
        return f"{m} min"
    if m < 36 * 60:
        return f"{round(m / 60)} hours"
    return f"{round(m / 1440)} days"


def _human_span(secs: float) -> str:
    return _human_minutes(int(secs / 60))


OUTSIDE_WORDS = re.compile(r"\b(front|back|side|patio|deck|porch|exterior|outside|entry|mudroom|slider|sliding)\b", re.I)


def _outside_door(eid: str, attrs: dict[str, Any], configured: set[str]) -> bool:
    """A door to the outside: listed in the alert settings, or named like one. Windows never count."""
    if eid in configured:
        return True
    if attrs.get("device_class") == "window":
        return False
    return bool(OUTSIDE_WORDS.search(f"{eid.replace('_', ' ')} {attrs.get('friendly_name') or ''}"))


def _hashable(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Minutes tick every run; round them so the same situation hashes the same (no repeat model calls)."""
    for i in items:
        for k in ("minutes_ago", "minutes"):
            if k in i:
                i[k] = (i[k] // 30) * 30
        if i.get("kind") == "appliance_done":
            i["detail"] = None
    return items


PLAIN_LOOK = {"appliance_done": ("accent", "washer.fill", "Just finished"),
              "light_on_long": ("warn", "lightbulb.fill", "Left on"),
              "open_long": ("warn", "door.left.hand.open", "Still open")}


async def plain_deliver(b: "Brain", trig: Trigger, finding: Finding, words: dict[str, Any] | None,
                        run_id: str) -> dict[str, Any]:
    return _cards_from(b, "plain_cards", run_id, finding, words, ttl_s=2 * 3600, look=PLAIN_LOOK)


# -- 2. left-on / left-open sweep -------------------------------------------------------------------------------------
async def left_detect(b: "Brain", trig: Trigger) -> Finding | None:
    now = time.time()
    states = b.hub.home.states
    area_of = b.hub.home.entity_area
    job = b.jobs["left_on"]
    away = trig.name == "last_left" or b.hub.alerts.nobody_home()
    items: list[dict[str, Any]] = []
    # At home, lights in a room nobody is in are not worth a card (Dylan, Nov 15). Only the "everyone's out" sweep.
    if not away:
        b.drop_cards("left_on")
        return None
    media = [e for e, st in sorted(states.items()) if e.startswith("media_player.") and e in area_of
             and st.get("state") in ("playing", "on", "paused") and not b.human_touched(e, job.human_window_s, now)]
    if media:
        items.append({"key": "away_media:" + ",".join(media), "kind": "away_media",
                      "players": [_name(b, e) for e in media],
                      "say": (f"{_name(b, media[0])} is still on" if len(media) == 1 else f"{len(media)} TVs still on"),
                      "detail": "Everyone's out.", "why": "a TV or speaker is on while everyone's out",
                      "priority": 66, "covers": media,
                      "buttons": [_off_button(media), {"kind": "done", "title": "Leave it", "primary": False}]})
    windows = [e for e, st in sorted(states.items()) if e.startswith("binary_sensor.") and st.get("state") == "on"
               and (st.get("attributes") or {}).get("device_class") == "window"]
    if windows:
        items.append({"key": "away_windows:" + ",".join(windows), "kind": "away_windows",
                      "windows": [_name(b, e) for e in windows],
                      "say": (f"{_name(b, windows[0])} is open" if len(windows) == 1 else f"{len(windows)} windows open"),
                      "detail": "Everyone's out.", "why": "windows open while everyone's out", "priority": 70})
    if not items:
        b.drop_cards("left_on")
        return None
    return Finding(items=items, summary=("away: " if away else "home: ") + "; ".join(i["say"] for i in items))


LEFT_LOOK = {"empty_room": ("warn", "lightbulb.fill", "Empty room"),
             "away_media": ("warn", "tv.fill", "Nobody's home"),
             "away_windows": ("warn", "window.casement", "Nobody's home")}


async def left_deliver(b: "Brain", trig: Trigger, finding: Finding, words: dict[str, Any] | None,
                       run_id: str) -> dict[str, Any]:
    out = _cards_from(b, "left_on", run_id, finding, words, ttl_s=90 * 60, look=LEFT_LOOK)
    for cid, it in zip(out["cards"], finding.items):
        if cid in b.cards:
            b.cards[cid]["covers"] = it.get("covers") or []
    return out


# -- 3. health watch ------------------------------------------------------------------------------------------------------
STARTUP_GRACE_S = 10 * 60    # right after the hub starts, states and service checks are still settling
RELINK_GRACE_S = 5 * 60      # same after the live Home Assistant link comes back
UNAVAILABLE_S = 15 * 60      # a device counts once Home Assistant has had it unavailable this long
BACK_FOR_S = 60 * 60         # a told device has to work this long before it can be told about again
RETELL_S = 24 * 3600         # a problem that is still there gets one reminder a day
TOLD_KV = "brain.health.told"
TOLD_ITEMS_KV = "brain.health.told_items"


def _unavailable_items(b: "Brain", now: float) -> list[dict[str, Any]]:
    """Devices that newly stopped responding. Each one is told about once (the list survives restarts), and
    devices that have been dead for ages (stale Home Assistant entities) aren't repeated on every restart."""
    raw = b.hub.db.kv_get(TOLD_KV)
    seeding = raw is None
    told: dict[str, dict[str, Any]] = dict(raw or {})
    bad: list[str] = []
    for eid, st in sorted(b.hub.home.states.items()):
        if eid.split(".", 1)[0] not in WATCHED_DOMAINS or eid not in b.hub.home.entity_area:
            continue
        if st.get("state") == "unavailable":
            if eid in told:
                told[eid]["ok"] = None
            since = _ts(st.get("last_changed"))
            if since and now - since >= UNAVAILABLE_S:
                bad.append(eid)
        elif eid in told and st.get("state") not in DEAD:
            ok = told[eid].get("ok") or now
            told[eid]["ok"] = ok
            if now - ok >= BACK_FOR_S:
                told.pop(eid)
    for eid in [e for e in told if e not in b.hub.home.states]:  # removed from Home Assistant
        told.pop(eid)
    if seeding:  # first run with this list: what's already dead was reported before, so start from here
        told = {e: {"t": now, "ok": None} for e in bad}
    b.hub.db.kv_set(TOLD_KV, told)
    new = [e for e in bad if e not in told]
    if not new:
        return []
    names = [_name(b, e) + (f" ({_room(b, e)})" if _room(b, e) else "") for e in new]
    return [{"key": "unavailable:" + ",".join(new[:20]), "kind": "unavailable", "count": len(new),
             "eids": new, "devices": names[:10],
             "say": f"{len(new)} device{'s' if len(new) != 1 else ''} stopped responding",
             "detail": ", ".join(names[:6]) + ("…" if len(names) > 6 else "")}]


def _told(b: "Brain", eids: list[str]) -> None:
    told = dict(b.hub.db.kv_get(TOLD_KV) or {})
    now = time.time()
    for e in eids:
        told[e] = {"t": now, "ok": None}
    b.hub.db.kv_set(TOLD_KV, told)


async def health_detect(b: "Brain", trig: Trigger) -> Finding | None:
    now = time.time()
    if trig.name != "ha_down" and now - b.hub.started_at < STARTUP_GRACE_S:
        return None
    items: list[dict[str, Any]] = []
    ws = b.hub.ha_ws
    down = b.ha_down_for(now)
    if ws.enabled and not ws.connected and (down >= 120 or trig.name == "ha_down"):
        items.append({"key": "ha_link", "kind": "ha_link", "error": ws.error,
                      "say": "The hub lost its live link to Home Assistant",
                      "detail": f"Down {_human_minutes(int(down / 60))}; using the slower backup poll."
                      + (f" Last error: {ws.error}" if ws.error else "")})
    elif not (ws.enabled and ws.connected and now - (ws.connected_at or 0) < RELINK_GRACE_S):
        items += _unavailable_items(b, now)
    sysinfo = b.hub.home.systems or {}
    for a in sysinfo.get("alerts") or []:
        level = str(a.get("level") or "").upper()
        if level in ("INFO", "NOTICE", ""):
            continue
        text = str(a.get("text") or "")[:300]
        items.append({"key": "nas:" + text[:80], "kind": "nas_alert", "level": level, "text": text,
                      "say": f"NAS {level.lower()}", "detail": text[:160]})
    crashed = (sysinfo.get("apps") or {}).get("crashed_names") or []
    if crashed:
        items.append({"key": "crashed:" + ",".join(sorted(crashed)), "kind": "nas_apps", "apps": crashed,
                      "say": f"{len(crashed)} NAS app{'s' if len(crashed) != 1 else ''} stopped", "detail": ", ".join(crashed)})
    for h in b.hub.health():
        if h.get("enabled") and not h.get("ok") and h.get("error") and h.get("id") != "home_assistant":
            items.append({"key": f"service:{h['id']}", "kind": "service", "service": h.get("title"),
                          "error": str(h["error"])[:200], "say": f"{h.get('title')} isn't answering",
                          "detail": str(h["error"])[:160]})
    b.memory["health_open"] = len(items)
    # Each problem is told once while it lasts (and again after a day if it still does); a hub restart or a
    # second problem showing up doesn't repeat the first. A problem that clears and comes back is told again.
    told = {k: float(t) for k, t in (b.hub.db.kv_get(TOLD_ITEMS_KV) or {}).items()}
    live = {i["key"] for i in items}
    told = {k: t for k, t in told.items() if k in live}
    b.hub.db.kv_set(TOLD_ITEMS_KV, told)
    items = [i for i in items if i["kind"] == "unavailable" or now - told.get(i["key"], 0) >= RETELL_S]
    if not items:
        return None
    return Finding(items=items, summary="; ".join(i["say"] for i in items), audience={"roles": ["admin"]})


HEALTH_SYSTEM = VOICE + (" You are writing one maintenance note for the person who runs the house's systems. Title: "
                         "at most 8 words naming the most important problem. Detail: one or two sentences with the "
                         "likely cause and the first thing to check. Items: one per problem, title = the thing, "
                         "value = a few words on its state.")


async def health_deliver(b: "Brain", trig: Trigger, finding: Finding, words: dict[str, Any] | None,
                         run_id: str) -> dict[str, Any]:
    n = len(finding.items)
    title = (words or {}).get("title") or (finding.items[0]["say"] if n == 1 else f"{n} things need a look")
    body = (words or {}).get("detail") or "; ".join(f"{i['say']}" + (f" ({i['detail']})" if i.get("detail") else "")
                                                     for i in finding.items[:5])
    ids = await b.hub.notify.send(title=str(title)[:120], body=str(body)[:600], cat="systems", level="maintenance",
                                  roles=["admin"], ref="thomascreek://systems", group_key="brain.health")
    _told(b, [e for i in finding.items for e in i.get("eids") or []])
    told = dict(b.hub.db.kv_get(TOLD_ITEMS_KV) or {})
    told.update({i["key"]: time.time() for i in finding.items if i["kind"] != "unavailable"})
    b.hub.db.kv_set(TOLD_ITEMS_KV, told)
    return {"notice": str(title)[:120], "outcome": "notified" if ids else "nobody", "action": "inbox"}


# -- 4. morning briefing ------------------------------------------------------------------------------------------------
async def brief_detect(b: "Brain", trig: Trigger) -> Finding | None:
    local = b.now()
    day = local.date().isoformat()
    if b.hub.db.kv_get("brain.briefing_day") == day:
        return None
    if not 5 <= local.hour < 11:
        return None
    now = time.time()
    h = b.hub
    items: list[dict[str, Any]] = []
    w = h.smart.weather.data or {}
    if w.get("temp") is not None:
        items.append({"key": "weather", "kind": "weather", "now": round(w["temp"]), "high": w.get("high"),
                      "low": w.get("tonight_low") or w.get("low"), "condition": w.get("condition"),
                      "rain_at": w.get("rain_at"),
                      "say": f"{w.get('condition') or 'Weather'}, {round(w['temp'])}°"
                      + (f", high {round(w['high'])}°" if w.get("high") is not None else "")})
    events = [{"title": e.get("title"), "start": e.get("start"), "all_day": e.get("all_day")}
              for e in (h.home.calendar or [])[:8]]
    if events:
        items.append({"key": "calendar", "kind": "calendar", "events": events,
                      "say": f"{len(events)} thing{'s' if len(events) != 1 else ''} on the calendar"})
    chores = []
    for p in h.db.people_with_role("child"):
        t = h.smart.chores.get(p["id"]) or {}
        left = [i for i in t.get("items", []) if not i.get("done")]
        if left:
            chores.append({"name": p["name"], "left": len(left)})
    if chores:
        items.append({"key": "chores", "kind": "chores", "kids": chores,
                      "say": ", ".join(f"{c['name']}: {c['left']} chores" for c in chores)})
    if (h.config.smart.trash_night or "").lower() == local.strftime("%A").lower():
        items.append({"key": "trash", "kind": "trash", "say": "Trash goes out tonight"})
    pk = h.db.all("SELECT title, body, ts FROM alert_log WHERE kind='package' AND ts>? ORDER BY ts DESC LIMIT 3",
                  [now - 14 * 3600])
    if pk:
        items.append({"key": "packages", "kind": "packages", "count": len(pk),
                      "say": f"{len(pk)} package{'s' if len(pk) != 1 else ''} seen at the door"})
    seen = h.db.kv_get("smart.media_ready") or {}
    fresh = [k for k, t in seen.items() if now - float(t) < 14 * 3600]
    ready = []
    if fresh:
        q = ",".join("?" * len(fresh))
        ready = [r["title"] for r in h.db.all(f"SELECT title FROM requests WHERE id IN ({q})", fresh)]
    since = now - 12 * 3600
    ready += [r["title"] for r in h.db.all("SELECT title FROM requests WHERE kind='media' AND status='ready' AND "
                                           "decided_at>? LIMIT 5", [since]) if r["title"] not in ready]
    if ready:
        items.append({"key": "downloads", "kind": "downloads", "titles": ready[:5],
                      "say": f"Ready overnight: {', '.join(ready[:3])}"})
    if b.memory.get("health_open"):
        items.append({"key": "health", "kind": "health", "open": b.memory["health_open"],
                      "say": f"{b.memory['health_open']} house system issue(s) in the inbox"})
    if not items:
        return None
    return Finding(items=items, summary="; ".join(i["say"] for i in items), scope=day,
                   context={"day": local.strftime("%A %B %-d")})


BRIEF_SYSTEM = VOICE + (" Write the family's morning briefing card. Title: at most 8 words, the one thing that most "
                        "shapes the day (weather that changes plans, a busy calendar, trash night), or a calm 'Quiet "
                        "day' style line. Detail: one sentence. Items: 3-5 rows, title = topic (e.g. 'Weather', "
                        "'Today', 'Packages'), value = a few words. Skip anything not in the input.")


async def brief_deliver(b: "Brain", trig: Trigger, finding: Finding, words: dict[str, Any] | None,
                        run_id: str) -> dict[str, Any]:
    local = b.now()
    day = local.date().isoformat()
    w = words or {}
    rows = [{"title": str(i.get("title"))[:30], "value": str(i.get("value"))[:60]}
            for i in (w.get("items") or []) if isinstance(i, dict) and i.get("title")][:5]
    if not rows:
        names = {"weather": "Weather", "calendar": "Today", "chores": "Chores", "trash": "Tonight",
                 "packages": "Packages", "downloads": "Plex", "health": "House"}
        rows = [{"title": names.get(i["kind"], i["kind"].title()), "value": i["say"][:60]} for i in finding.items[:5]]
    title = str(w.get("title") or "Good morning")[:80]
    card = {"id": f"brain_briefing:{day}", "priority": 64, "tint": "sun", "symbol": "sun.horizon.fill",
            "eyebrow": "Good morning", "title": title, "items": rows,
            "why": "House brain · the morning briefing"}
    if w.get("detail"):
        card["detail"] = str(w["detail"])[:160]
    end = local.replace(hour=11, minute=0, second=0, microsecond=0).timestamp()
    cid = b.add_card(job="briefing", run_id=run_id, card=card, audience=finding.audience,
                     ttl_s=max(600.0, end - time.time()), buttons=[{"kind": "done", "title": "Thanks"}])
    b.hub.db.kv_set("brain.briefing_day", day)
    if b.cfg.briefing_push:
        await b.hub.notify.send(title=title, body="; ".join(f"{r['title']}: {r['value']}" for r in rows),
                                cat="house", level="fyi", roles=["admin", "adult"], ref="thomascreek://home",
                                group_key="brain.briefing", sort=False)
    return {"cards": [cid], "outcome": "suggested"}


def _brief_at(b: "Brain") -> str:
    return b.cfg.briefing_time or "07:00"


# -- 5. "while you were out" ------------------------------------------------------------------------------------------------
async def catch_detect(b: "Brain", trig: Trigger) -> Finding | None:
    pid = trig.data.get("person_id")
    since = float(trig.data.get("away_since") or 0)
    now = time.time()
    p = b.hub.db.person(pid) if pid else None
    if not p or p["role"] not in ("admin", "adult") or not since or now - since < 60 * 60:
        return None
    h = b.hub
    items: list[dict[str, Any]] = []
    alerts = [r for r in h.alerts.history(pid, limit=50) if r["ts"] >= since]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in alerts:
        by_kind.setdefault(r["kind"], []).append(r)
    for kind, rows in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        items.append({"key": f"alert:{kind}", "kind": "alerts", "alert": kind, "count": len(rows),
                      "latest": rows[0]["title"], "say": f"{len(rows)} × {rows[0]['title']}" if len(rows) > 1
                      else rows[0]["title"]})
    acts = h.db.all("SELECT action, person_id, target FROM activity WHERE ts>? AND outcome='ok' AND "
                    "(person_id IS NULL OR person_id!=?) AND (action LIKE 'lock.%' OR action LIKE 'garage.%' OR "
                    "action LIKE 'mode.%' OR action LIKE 'media_requests.%' OR action LIKE 'people.%') "
                    "ORDER BY ts DESC LIMIT 20", [since, pid])
    if acts:
        names = {q["id"]: q["name"] for q in h.db.people()}
        rows = [f"{names.get(a['person_id'], 'The house')}: {a['action']} {a.get('target') or ''}".strip() for a in acts[:6]]
        items.append({"key": "activity", "kind": "activity", "changes": rows,
                      "say": f"{len(acts)} change{'s' if len(acts) != 1 else ''} around the house"})
    ready = h.db.all("SELECT title FROM requests WHERE requester_id=? AND status='ready' AND decided_at>? LIMIT 5",
                     [pid, since])
    if ready:
        items.append({"key": "ready", "kind": "ready", "titles": [r["title"] for r in ready],
                      "say": f"Ready to watch: {', '.join(r['title'] for r in ready[:3])}"})
    if not items:
        return None
    away_h = (now - since) / 3600
    return Finding(items=items, summary=f"{p['name']} back after {away_h:.1f} h: " + "; ".join(i["say"] for i in items),
                   audience={"person_ids": [pid]}, scope=f"{pid}:{int(since)}",
                   context={"person": p["name"], "away_hours": round(away_h, 1)})


CATCH_SYSTEM = VOICE + (" Someone just got home. Write their 'while you were out' card, addressed to them. Title: at "
                        "most 8 words, the most useful thing to know. Detail: one sentence or empty. Items: up to 5 "
                        "rows, title = topic, value = a few words. Leave out noise.")


async def catch_deliver(b: "Brain", trig: Trigger, finding: Finding, words: dict[str, Any] | None,
                        run_id: str) -> dict[str, Any]:
    w = words or {}
    rows = [{"title": str(i.get("title"))[:30], "value": str(i.get("value"))[:60]}
            for i in (w.get("items") or []) if isinstance(i, dict) and i.get("title")][:5]
    if not rows:
        rows = [{"title": {"alerts": "Alerts", "activity": "House", "ready": "Plex"}.get(i["kind"], "Note"),
                 "value": i["say"][:60]} for i in finding.items[:5]]
    pid = finding.audience["person_ids"][0]
    card = {"id": f"brain_catch_up:{pid}:{int(time.time())}", "priority": 72, "tint": "accent",
            "symbol": "house.fill", "eyebrow": "While you were out", "title": str(w.get("title") or "Welcome back")[:80],
            "items": rows, "why": "House brain · what happened while you were out"}
    if w.get("detail"):
        card["detail"] = str(w["detail"])[:160]
    for k, v in list(b.cards.items()):
        if v["job"] == "catch_up" and v["audience"].get("person_ids") == [pid]:
            b._close(k, "replaced")
            b.cards.pop(k, None)
    cid = b.add_card(job="catch_up", run_id=run_id, card=card, audience=finding.audience, ttl_s=2 * 3600,
                     buttons=[{"kind": "done", "title": "Got it"}])
    return {"cards": [cid], "outcome": "suggested"}


def build_jobs() -> list[Job]:
    return [
        Job(id="plain_cards", title="Plain-language cards",
            about="Appliances that finished, lights on all day, doors and windows left open.",
            detect=plain_detect, deliver=plain_deliver, system=CARD_SYSTEM, schema=CARDS_SCHEMA,
            every_s=15 * 60, on_state=plain_on_state, cooldown_s=120),
        Job(id="left_on", title="Left on, left open",
            about="Lights on in empty rooms; TVs and windows when the last person leaves.",
            detect=left_detect, deliver=left_deliver, system=CARD_SYSTEM, schema=CARDS_SCHEMA,
            every_s=10 * 60, hub_events=("last_left",), cooldown_s=300, dedupe_s=3 * 3600),
        Job(id="health", title="Health watch",
            about="Devices not responding, the Home Assistant link, NAS alerts and services, to the inbox.",
            detect=health_detect, deliver=health_deliver, system=HEALTH_SYSTEM, schema=NOTE_SCHEMA,
            every_s=5 * 60, hub_events=("ha_down",), dedupe_s=12 * 3600),
        Job(id="briefing", title="Morning briefing",
            about="Weather, today, chores, trash, packages and overnight downloads, once each morning.",
            detect=brief_detect, deliver=brief_deliver, system=BRIEF_SYSTEM, schema=NOTE_SCHEMA,
            at=_brief_at, hub_events=("first_open",), dedupe_s=20 * 3600),
        Job(id="catch_up", title="While you were out",
            about="What happened while you were away, when you get home.",
            detect=catch_detect, deliver=catch_deliver, system=CATCH_SYSTEM, schema=NOTE_SCHEMA,
            hub_events=("arrival",)),
    ]
