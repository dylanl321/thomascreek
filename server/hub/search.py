"""One search across the whole house, run as the person asking.

`run(hub, subject, q)` answers from what the hub already holds (rooms, devices,
scenes, cameras, ovens, people, app screens) at once, and asks the slower
sources (Plex and Jellyseerr, Mealie, Frigate events, the family board's
calendar and chores, TREK, alert history, Homebox, the kids' YouTube) at the
same time with a short budget. A source that is slower than the budget is
listed in `pending` and the app can ask again for just those types.

Role limits come from the same policy the actions use: a source the person has
no capability for is never asked, devices come through `HomeModel.device`
(own room / own TV for children), films are filtered by rating, and every
result's one-tap `action` is only offered if `Policy.decide` allows it.

The assistant uses this as its lookup: each item carries the id, the entity or
source id, a deep link and an action that `POST /v1/actions` accepts as-is.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .home import kind_for
from .policy import Subject, Target

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.search")

BUDGET_S = 1.2
MAX_Q = 100

# type id -> heading. The order is only the tie-break; groups sort by their best match.
TYPES: dict[str, str] = {
    "room": "Rooms",
    "light": "Lights and switches",
    "scene": "Scenes",
    "door": "Doors and garage",
    "climate": "Climate",
    "player": "TVs and speakers",
    "appliance": "Kitchen and appliances",
    "camera": "Cameras",
    "event": "Camera events",
    "media": "Movies and shows",
    "youtube": "YouTube",
    "recipe": "Recipes",
    "chore": "Chores",
    "calendar": "Calendar",
    "trip": "Trips",
    "person": "People",
    "inventory": "Where things are",
    "alert": "Alerts",
    "screen": "Settings and screens",
}

# Frigate labels people search for, with the words they use.
LABEL_WORDS = {
    "person": ("person", "people", "someone", "somebody", "man", "woman", "visitor"),
    "car": ("car", "cars", "vehicle", "truck"),
    "dog": ("dog", "dogs"),
    "cat": ("cat", "cats"),
    "package": ("package", "packages", "delivery", "parcel", "box"),
    "bird": ("bird", "birds"),
    "bicycle": ("bike", "bicycle"),
    "deer": ("deer",),
}
LABEL_TITLE = {"person": "Person", "car": "Car", "dog": "Dog", "cat": "Cat", "package": "Package", "bird": "Bird",
               "bicycle": "Bike", "deer": "Deer"}

# App screens: (id, title, subtitle, deep link, keywords, capability or None, roles or None)
SCREENS: list[tuple[str, str, str, str, str, str | None, tuple[str, ...] | None]] = [
    ("profile", "Profile", "Your name, this phone, sign out", "thomascreek://profile", "me account settings phone",
     None, None),
    ("vpn", "Away from home", "Off, when I'm away, or always on", "thomascreek://profile",
     "vpn wireguard remote tunnel away from home connect", None, ("admin", "adult")),
    ("alert-settings", "Alert settings", "Choose what pings you", "thomascreek://profile",
     "alerts notifications push settings quiet hours", None, ("admin", "adult", "child")),
    ("inbox", "Inbox and alert history", "Everything from the last 30 days", "thomascreek://inbox",
     "inbox alerts history notifications requests", None, None),
    ("downloads", "Downloads and storage", "Films and shows saved on this phone", "thomascreek://downloads",
     "downloads offline storage space saved", None, ("admin", "adult", "child")),
    ("kids-devices", "Kids' devices", "Pause, more time, schedules", "thomascreek://kids",
     "kids devices internet wifi pause bedtime screen time firewalla network", "network.manage", None),
    ("doors", "Doors and garage", "Garage doors and locks", "thomascreek://doors", "doors garage locks lock unlock",
     "doors", None),
    ("cameras", "Cameras", "All cameras and recent events", "thomascreek://camera", "cameras frigate live clips events",
     "cameras", None),
    ("climate", "Climate", "Thermostats", "thomascreek://climate", "climate thermostat heat cool temperature hvac",
     "climate.all", None),
    ("kitchen", "Kitchen", "Ovens and timers", "thomascreek://kitchen", "kitchen oven ovens preheat timer bake",
     "appliances", None),
    ("recipes", "Recipes", "Every recipe in Mealie", "thomascreek://recipes", "recipes cookbook mealie cooking",
     "family.edit", None),
    ("dinners", "Dinners", "This week's meal plan", "thomascreek://dinners", "dinner meals meal plan week tonight",
     "family.edit", None),
    ("shopping", "Shopping list", "Add and tick off", "thomascreek://shopping", "shopping list groceries grocery",
     "shopping.add", ("admin", "adult", "tablet")),
    ("calendar", "Calendar", "The family calendar", "thomascreek://calendar", "calendar schedule events today week",
     "family.view", ("admin", "adult", "tablet")),
    ("chores", "Chores", "Jobs, stars and rewards", "thomascreek://chores", "chores jobs stars rewards points",
     None, ("admin", "adult", "child")),
    ("trips", "Trips", "Plans, places and packing", "thomascreek://trips", "trips travel vacation packing holiday",
     "trips.view", None),
    ("inventory", "Where things are", "Search the house inventory", "thomascreek://inventory",
     "inventory homebox where things stuff find", "inventory.search", ("admin", "adult", "tablet")),
    ("music", "Music", "Speakers around the house", "thomascreek://music", "music speakers sonos audio",
     "media.all", None),
    ("requests", "Requests", "Films and shows asked for", "thomascreek://requests", "requests request jellyseerr ask",
     "media.request", None),
    ("pipeline", "Downloads in progress", "Sonarr and Radarr queue", "thomascreek://pipeline",
     "pipeline downloading sonarr radarr queue", "pipeline", None),
    ("holiday", "Holiday lights", "Show, looks and schedule", "thomascreek://holiday",
     "holiday christmas lights show xlights looks", None, ("admin", "adult", "child", "tablet")),
    ("controllers", "Show controllers", "Light controllers and health", "thomascreek://controllers",
     "controllers falcon fpp holiday lights health", "holiday.full", None),
    ("house3d", "House in 3D", "The house model", "thomascreek://house3d", "3d model house", None, ("admin", "adult")),
    ("systems", "Systems", "NAS, apps and services", "thomascreek://systems", "systems nas truenas apps services",
     "systems", None),
    ("storage", "Storage", "NAS pools and space", "thomascreek://storage", "storage nas disk pool space", "systems",
     None),
    ("activity", "Activity", "Who did what", "thomascreek://activity", "activity log history audit", "activity",
     None),
    ("people", "People and phones", "Roles, rooms and devices", "thomascreek://people",
     "people phones devices roles permissions family", "people", None),
    ("vpn-pool", "VPN configs", "WireGuard configs for phones", "thomascreek://vpnpool",
     "vpn wireguard pool configs firewalla", "people", None),
    ("hub-web", "Hub web page", "Sign in on a computer to add keys", "thomascreek://hubweb",
     "hub web page admin keys secrets computer", "systems", None),
]

QUESTION_WORDS = ("is", "are", "was", "what", "whats", "when", "where", "who", "how", "did", "do", "does", "can",
                  "why", "which", "should", "will", "tell", "show", "turn", "make", "set", "please")


# -- matching -----------------------------------------------------------------
def norm(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def words(q: str) -> list[str]:
    return [w for w in norm(q).split() if w]


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def score(ws: list[str], title: str, extra: str = "") -> float:
    """0 when any word is missing; higher for title starts and whole words."""
    if not ws:
        return 0.0
    t, k = norm(title), norm(extra)
    tw, kw = t.split(), k.split()
    total = 0.0
    for w in ws:
        sw = _stem(w)
        if any(x == w or _stem(x) == sw for x in tw):
            total += 4
        elif any(x.startswith(w) for x in tw):
            total += 3
        elif any(x == w or _stem(x) == sw for x in kw):
            total += 2
        elif any(x.startswith(w) for x in kw):
            total += 1.5
        elif len(w) >= 3 and (w in t or w in k):
            total += 0.75
        else:
            return 0.0
    if t.startswith(" ".join(ws)):
        total += 2
    if t == " ".join(ws):
        total += 3
    return total


def looks_like_question(q: str) -> bool:
    ws = words(q)
    return len(ws) >= 5 or (len(ws) >= 2 and ws[0] in QUESTION_WORDS) or q.strip().endswith("?")


# -- result shape ---------------------------------------------------------------
def item(type_: str, id_: str, title: str, subtitle: str | None, sc: float, *, source: str, ref: str | None = None,
         link: str | None = None, action: dict[str, Any] | None = None, verb: str | None = None,
         image: str | None = None, symbol: str | None = None, **extra: Any) -> dict[str, Any]:
    out = {"id": f"{type_}:{id_}", "type": type_, "title": title, "subtitle": subtitle, "score": round(sc, 2),
           "source": source, "ref": ref, "deep_link": link, "action": action, "verb": verb or ("Open" if link else None),
           "image_url": image, "symbol": symbol}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _act(h: "Hub", s: Subject, action: str, target: str | None, params: dict[str, Any], label: str,
         t: Target | None = None) -> dict[str, Any] | None:
    """An action the person may run, with its tier (secure ones ask for Face ID in the app)."""
    d = h.policy.decide(s, action, t or Target(entity_id=target), params)
    if not d.allowed:
        return None
    return {"action": action, "target": target, "params": params, "tier": d.tier, "label": label}


SEGMENT = re.compile(r"\b(segment \d+|main)$", re.I)
JUNK_NAME = re.compile(r"^(0x[0-9a-f]+|[a-z0-9]{2,6}_[0-9a-f]{4}|light|interior light|googletv\d+)$", re.I)


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    m = int((time.time() - ts) / 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 60 * 24:
        return f"{m // 60} h ago"
    d = m // (60 * 24)
    return "yesterday" if d == 1 else f"{d} days ago"


# -- instant sources (in memory) ---------------------------------------------------
DEVICE_TYPE = {"light": "light", "fan": "light", "switch": "light", "garage": "door", "lock": "door",
               "climate": "climate", "media": "player", "speaker": "player", "oven": "appliance",
               "vacuum": "appliance", "valve": "appliance", "shower": "appliance"}
SYMBOL = {"light": "lightbulb.fill", "fan": "fanblades.fill", "switch": "switch.2", "garage": "door.garage.closed",
          "lock": "lock.fill", "climate": "thermometer.medium", "media": "tv", "speaker": "hifispeaker.fill",
          "oven": "oven.fill", "vacuum": "fan.floor", "valve": "drop.fill", "shower": "shower.fill"}


def _device_hits(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    home = h.home
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for eid in list(home.states):
        st = home.states[eid]
        kind = kind_for(st)
        typ = DEVICE_TYPE.get(kind or "")
        if not typ:
            continue
        name = (st.get("attributes", {}).get("friendly_name") or "").strip()
        if not name or JUNK_NAME.match(name) or name.startswith("Plex ("):
            continue
        if kind == "light" and SEGMENT.search(name):
            continue
        area = home.entity_area.get(eid)
        available = st.get("state") not in ("unavailable", "unknown")
        if not available and not area:
            continue  # ghosts: cast targets, old integrations
        room = (home.areas.get(area) or {}).get("name") if area else None
        sc = score(ws, name, f"{room or ''} {kind} {'tv television' if kind == 'media' else ''}")
        if sc <= 0:
            continue
        d = home.device(eid, s)
        if d is None:
            continue
        key = (norm(name), typ)
        prev = seen.get(key)
        if prev is not None and (prev["_available"] or not available):
            continue  # one row per name (HA makes several players per TV)
        stt = d["state"]
        sub, action, verb, link = room or "", None, None, f"thomascreek://room/{area}" if area else None
        if kind in ("light", "fan", "switch"):
            on = bool(stt.get("on"))
            word = "on" + (f", {stt['brightness']}%" if on and stt.get("brightness") is not None else "") if on else "off"
            sub = " · ".join(x for x in (room, word) if x)
            action = _act(h, s, f"{kind}.set", eid, {"on": not on}, "Turn off" if on else "Turn on",
                          Target(entity_id=eid, area=area, kind=kind))
            if action:
                action["toggle"] = True
                action["on"] = on
        elif kind == "garage":
            is_open = bool(stt.get("open"))
            sub = " · ".join(x for x in (room, "open" if is_open else "closed") if x)
            action = _act(h, s, "garage.set", eid, {"open": not is_open}, "Close" if is_open else "Open",
                          Target(entity_id=eid, area=area, kind=kind))
            link = "thomascreek://doors"
        elif kind == "lock":
            locked = bool(stt.get("locked"))
            sub = " · ".join(x for x in (room, "locked" if locked else "unlocked") if x)
            action = _act(h, s, "lock.set", eid, {"locked": not locked}, "Unlock" if locked else "Lock",
                          Target(entity_id=eid, area=area, kind=kind))
            link = "thomascreek://doors"
        elif kind in ("media", "speaker"):
            what = "playing" if stt.get("playing") else ("on" if stt.get("on") else "off")
            if st.get("attributes", {}).get("media_title") and stt.get("on"):
                what += f" · {st['attributes']['media_title']}"
            sub = " · ".join(x for x in (room, what) if x)
            link, verb = f"thomascreek://remote/{eid}", "Remote"
        elif kind == "climate":
            cur = stt.get("current_temp")
            sub = " · ".join(x for x in (room, f"{round(cur)}°" if cur is not None else None, stt.get("hvac")) if x)
            link = "thomascreek://climate"
        elif kind == "oven":
            sub = "On" if stt.get("on") else "Off"
            if stt.get("on") and stt.get("target_temp"):
                sub += f" · {round(stt['target_temp'])}°"
            label = h.kitchen.label(eid) if hasattr(h, "kitchen") else name
            name = label or name
            link, verb = f"thomascreek://kitchen/{eid}", "Preheat" if not stt.get("on") else "Open"
        if not available:
            sub = " · ".join(x for x in (room, "offline") if x)
            action = None
        row = item(typ, eid, name, sub, sc + (0.5 if available else -1), source="home_assistant", ref=eid, link=link,
                   action=action, verb=verb if not action else action["label"], symbol=SYMBOL.get(kind or ""),
                   room_id=area, kind=kind)
        row["_available"] = available
        if prev is not None:
            out.remove(prev)
        seen[key] = row
        out.append(row)
    for r in out:
        r.pop("_available", None)
    return out


def _room_hits(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    home = h.home
    vis = home.visible_areas(s)
    out = []
    for a in home.areas.values():
        if vis is not None and a["id"] not in vis:
            continue
        sc = score(ws, a["name"], f"{a['id'].replace('_', ' ')} room")
        if sc <= 0:
            continue
        eids = [e for e, ar in home.entity_area.items() if ar == a["id"]]
        devs = [d for e in eids if (d := home.device(e, s)) is not None]
        if not devs:
            continue
        lights = [d for d in devs if d["kind"] in ("light", "fan", "switch") and d["available"]]
        on = [d for d in lights if d["state"].get("on")]
        sub = f"{len(on)} of {len(lights)} lights on" if lights else "Room"
        acts = []
        target = on if on else lights
        for d in target:
            a2 = _act(h, s, f"{d['kind']}.set", d["id"], {"on": not on}, "",
                      Target(entity_id=d["id"], area=a["id"], kind=d["kind"]))
            if a2:
                acts.append({k: v for k, v in a2.items() if k != "label"})
        action = None
        if acts:
            action = {"action": "batch", "target": a["id"], "params": {}, "tier": "everyday",
                      "label": "All off" if on else "Lights on", "steps": acts}
        out.append(item("room", a["id"], a["name"], sub, sc + 1.5, source="home_assistant", ref=a["id"],
                        link=f"thomascreek://room/{a['id']}", action=action,
                        verb=action["label"] if action else "Open", symbol=a.get("symbol")))
    return out


def _scene_hits(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    home = h.home
    out = []
    for eid, st in home.states.items():
        if not eid.startswith("scene.") or st.get("state") == "unavailable":
            continue
        name = (st.get("attributes", {}).get("friendly_name") or "").strip()
        if not name:
            continue
        area = home.entity_area.get(eid)
        room = (home.areas.get(area) or {}).get("name") if area else None
        sc = score(ws, name, f"{room or ''} scene")
        if sc <= 0:
            continue
        action = _act(h, s, "scene.run", eid, {}, "Run", Target(entity_id=eid, area=area, kind="scene"))
        if action is None:
            continue
        out.append(item("scene", eid, name, f"Scene · {room}" if room else "Scene", sc, source="home_assistant",
                        ref=eid, link=f"thomascreek://room/{area}" if area else None, action=action,
                        verb="Run", symbol="sparkles", room_id=area))
    # Modes from the hub config (Goodnight, Away...).
    for m in h.config.modes:
        if s.role not in m.roles:
            continue
        sc = score(ws, m.name, "mode scene")
        if sc > 0:
            action = _act(h, s, "mode.run", m.id, {}, "Run")
            if action:
                out.append(item("scene", f"mode.{m.id}", m.name, "Mode", sc + 0.5, source="hub", ref=m.id,
                                action=action, verb="Run", symbol=m.symbol or "sparkles"))
    return out


def _camera_hits(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    if not h.policy.has(s, "cameras") or not h.frigate.enabled:
        return []
    from .routes.cameras import camera_list

    out = []
    for c in camera_list(h):
        sc = score(ws, c["name"], f"{c['id'].replace('_', ' ')} camera cam")
        if sc <= 0:
            continue
        last = c.get("last_event")
        sub = "Live"
        if last and last.get("start"):
            sub += f" · {last.get('label') or 'motion'} {_ago(_iso_ts(last['start']))}"
        if c.get("online") is False:
            sub = "Offline"
        out.append(item("camera", c["id"], c["name"], sub, sc, source="frigate", ref=c["id"],
                        link=f"thomascreek://camera/{c['id']}", verb="Live", image=c.get("snapshot_url"),
                        symbol="video.fill"))
    return out


def _iso_ts(v: Any) -> float | None:
    from datetime import datetime

    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return None


def _person_hits(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    out = []
    admin = h.policy.has(s, "people")
    for p in h.home.people(s):
        sc = score(ws, p["name"], f"{p['role']} person family")
        if sc <= 0:
            continue
        where = {True: "home", False: "away"}.get(p.get("home"))  # type: ignore[arg-type]
        sub = " · ".join(x for x in (p["role"].capitalize(), where) if x)
        out.append(item("person", p["id"], p["name"], sub, sc, source="hub", ref=p["id"],
                        link="thomascreek://people" if admin else None, symbol="person.crop.circle",
                        verb="Devices" if admin else None))
    return out


def _screen_hits(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    caps = set(h.policy.capabilities_for(s))
    out = []
    for sid, title, sub, link, kw, cap, roles in SCREENS:
        if cap and cap not in caps:
            continue
        if roles and s.role not in roles:
            continue
        sc = score(ws, title, f"{sub} {kw}")
        if sc > 0:
            out.append(item("screen", sid, title, sub, sc - 0.5, source="app", link=link, symbol="gearshape"))
    return out


# -- slower sources (network) -----------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, ttl: float, fn: Callable[[], Awaitable[Any]]) -> Any:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val


def _childlike(s: Subject) -> bool:
    return s.role not in ("admin", "adult", "tablet")


async def _media(h: "Hub", s: Subject, q: str, ws: list[str]) -> list[dict[str, Any]]:
    if not (h.policy.has(s, "media.all") or h.policy.has(s, "media.own_tv")) or not h.plex.enabled:
        return []
    ratings = set(h.config.child_policy.ratings)
    rows = await h.plex.search(q, limit=20)
    out: list[dict[str, Any]] = []
    have: set[str] = set()
    for m in rows:
        if _childlike(s) and m.get("rating") not in ratings:
            continue
        sc = score(ws, m["title"], f"{m.get('show') or ''} {m.get('library') or ''} {m['kind']}") or 1.0
        bits = [{"movie": "Movie", "show": "Show", "episode": "Episode"}.get(m["kind"], "Video")]
        if m.get("year"):
            bits.append(str(m["year"]))
        if m.get("rating"):
            bits.append(m["rating"])
        bits.append("in Plex")
        have.add(norm(m["title"]))
        out.append(item("media", m["id"], m["title"], " · ".join(bits), sc + 0.5, source="plex", ref=m["id"],
                        link="thomascreek://media", verb="Play", image=m.get("poster_url"), symbol="film",
                        media=m))
    if (h.policy.has(s, "media.request") and h.jellyseerr.enabled and s.role in ("admin", "adult")
            and len(out) < 6):
        try:
            for r in (await h.jellyseerr.search(q))[:6]:
                if norm(r.get("title")) in have:
                    continue
                sc = score(ws, r.get("title") or "", r.get("kind") or "")
                if sc <= 0:
                    continue
                sub = " · ".join(x for x in ("Movie" if r["kind"] == "movie" else "Show", r.get("year"),
                                             "not in Plex") if x)
                out.append(item("media", f"req-{r['id']}", r["title"], sub, sc - 1, source="jellyseerr",
                                ref=r["id"], verb="Request", image=r.get("poster_url"), symbol="plus.circle",
                                request={"media_id": r["id"], "title": r["title"]}))
        except Exception as exc:  # noqa: BLE001
            log.debug("jellyseerr search: %s", exc)
    return out


async def _youtube(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    yt = getattr(h, "youtube", None)
    if yt is None or not getattr(yt, "enabled", False):
        return []
    if not (h.policy.has(s, "media.all") or h.policy.has(s, "media.own_tv")):
        return []
    out = []
    for src in h.db.all("SELECT id, title, channel FROM yt_sources"):
        sc = score(ws, src.get("title") or "", f"{src.get('channel') or ''} youtube channel")
        if sc > 0:
            out.append(item("youtube", src["id"], src.get("title") or src.get("channel") or "YouTube",
                            "Channel", sc, source="youtube", ref=src["id"], link="thomascreek://media",
                            verb="Watch", symbol="play.rectangle.fill"))
    for v in h.db.all("SELECT * FROM yt_videos WHERE status='ready' ORDER BY COALESCE(ready_at, added_at) DESC "
                      "LIMIT 500"):
        sc = score(ws, v.get("title") or "", f"{v.get('channel') or ''} youtube video")
        if sc > 0:
            m = yt.item(v)
            mins = f"{int(v['duration_s']) // 60} min" if v.get("duration_s") else None
            out.append(item("youtube", v["id"], v.get("title") or "Video",
                            " · ".join(x for x in ("Video", v.get("channel"), mins) if x), sc, source="youtube",
                            ref=v["id"], link="thomascreek://media", verb="Watch", image=m.get("poster_url"),
                            symbol="play.rectangle.fill", media=m))
    return out


async def _recipes(h: "Hub", s: Subject, q: str, ws: list[str]) -> list[dict[str, Any]]:
    if not h.policy.has(s, "family.view") or not h.mealie.enabled or _childlike(s):
        return []
    data = await h.mealie.recipes(q[:80], None, 1, per_page=12)
    out = []
    for r in data.get("items", []):
        sc = score(ws, r.get("title") or "", " ".join(r.get("tags") or []) + " recipe") or 1.0
        bits = ["Recipe"]
        if r.get("total_time"):
            bits.append(str(r["total_time"]))
        if r.get("favorite"):
            bits.append("favorite")
        out.append(item("recipe", r.get("slug") or r["id"], r.get("title") or "Recipe", " · ".join(bits), sc,
                        source="mealie", ref=r.get("slug"), link=f"thomascreek://recipe/{r.get('slug')}",
                        image=r.get("image_url"), symbol="fork.knife",
                        verb="Preheat" if r.get("oven") and h.policy.has(s, "appliances") else "Open"))
    return out


async def _events(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    if not h.policy.has(s, "cameras") or not h.frigate.enabled:
        return []
    from .routes.cameras import camera_name, event_item

    label_words = {w for syn in LABEL_WORDS.values() for w in syn}
    labels = [lab for lab, syn in LABEL_WORDS.items() if any(w in syn for w in ws)]
    rest = [w for w in ws if w not in label_words]
    cams = [cam for cam in h.frigate._info if rest and score(rest, camera_name(h, cam), cam.replace("_", " ")) > 0]
    if rest and not cams:
        return []  # the other words aren't a camera
    if not labels and not (cams and len(cams) <= 3):
        return []
    rows = await h.frigate.events(cams[0] if len(cams) == 1 else None, 12,
                                  label=",".join(labels) if labels else None)
    out = []
    for e in rows:
        if cams and e.get("camera") not in cams:
            continue
        ev = event_item(h, e)
        title = f"{LABEL_TITLE.get(ev['label'] or '', (ev['label'] or 'Motion').capitalize())} at {ev['camera_name']}"
        when = _ago(e.get("start_time"))
        out.append(item("event", ev["id"], title, " · ".join(x for x in (when, ev.get("sub_label")) if x),
                        3.0 + (1 if labels else 0), source="frigate", ref=ev["id"],
                        link=f"thomascreek://event/{ev['id']}", verb="Play" if ev.get("clip_url") else "Open",
                        image=ev.get("thumb_url"), symbol="person.fill.viewfinder", start=ev.get("start")))
        if len(out) >= 6:
            break
    return out


async def _calendar(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    if not h.policy.has(s, "family.view") or not h.family_board.enabled:
        return []
    data = await _cached("calendar", 300, lambda: h.family_board.calendar(45))
    out = []
    from datetime import datetime

    for e in data.get("events", []):
        sc = score(ws, e.get("title") or "", f"{e.get('who') or ''} {e.get('location') or ''} calendar event")
        if sc <= 0:
            continue
        when = e.get("start") or ""
        try:
            dt = datetime.fromisoformat(when)
            when = dt.strftime("%a %b %-d") + ("" if e.get("all_day") else dt.strftime(" %-I:%M %p").lower())
        except ValueError:
            pass
        sub = " · ".join(x for x in (when, e.get("who")) if x)
        out.append(item("calendar", str(e.get("id") or e.get("title")), e.get("title") or "Event", sub, sc,
                        source="family_board", ref=e.get("id"), link="thomascreek://calendar", symbol="calendar",
                        start=e.get("start")))
    return out[:8]


async def _chores(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    manage = h.policy.has(s, "chores.manage")
    if not (manage or h.policy.has(s, "chores.do")) or not h.family_board.enabled:
        return []
    from .routes.family import link_board_people

    await link_board_people(h)
    people = [p for p in h.db.people() if p.get("family_board_id")
              and (p["id"] == s.person_id or (manage and p["role"] == "child"))]
    out = []

    async def one(p: dict[str, Any]) -> None:
        raw = await _cached(f"chores:{p['family_board_id']}", 60,
                            lambda: h.family_board.today(p["family_board_id"]))
        for c in h.family_board.normalize_today(raw)["items"]:
            sc = score(ws, c.get("title") or "", f"{p['name']} chore job")
            if sc <= 0:
                continue
            mine = p["id"] == s.person_id
            state = "done" if c["done"] else ("waiting for a parent" if c.get("waiting") else "to do")
            sub = " · ".join(x for x in (None if mine else p["name"], state,
                                         f"{c['points']} stars" if c.get("points") else None) if x)
            verb = None if c["done"] or c.get("waiting") else "Done"
            out.append(item("chore", f"{p['id']}:{c['id']}", c.get("title") or "Chore", sub, sc + (1 if mine else 0),
                            source="family_board", ref=str(c["id"]), link="thomascreek://chores", verb=verb or "Open",
                            symbol="star.fill", chore={"id": c["id"], "person": p["id"], "done": c["done"]}))

    await asyncio.gather(*(one(p) for p in people), return_exceptions=True)
    return out


async def _trips(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    if not h.policy.has(s, "trips.view") or not h.trek.enabled or s.role == "tablet":
        return []
    from .adapters import trips as shape

    raw = await h.trek.trips()
    if _childlike(s):
        hidden = set(h.config.trek.hidden_from_children)
        raw = [t for t in raw if t.get("id") not in hidden]
    today = h.policy.now().date()
    out = []
    for t in raw:
        sm = shape.summary(t, today)
        sc = score(ws, sm["title"], f"{sm.get('description') or ''} trip vacation travel")
        if sc <= 0:
            continue
        when = {"upcoming": f"in {sm['days_until']} days" if sm.get("days_until") else "soon",
                "now": f"day {sm.get('day_number')} of {sm.get('day_count')}", "past": "past",
                "someday": "no dates yet"}.get(sm["status"], "")
        dates = " – ".join(x for x in (sm.get("start_date"), sm.get("end_date")) if x)
        out.append(item("trip", str(sm["id"]), sm["title"], " · ".join(x for x in ("Trip", dates, when) if x),
                        sc + (0 if sm["status"] == "past" else 0.5), source="trek", ref=str(sm["id"]),
                        link=f"thomascreek://trip/{sm['id']}", symbol="airplane"))
    return out


async def _alerts(h: "Hub", s: Subject, ws: list[str]) -> list[dict[str, Any]]:
    if not s.person_id:
        return []
    from .routes.alerts import _history_kinds

    kinds = _history_kinds(s.role)
    rows = h.alerts.history(s.person_id, ",".join(kinds), None, 200)
    out = []
    for r in rows:
        sc = score(ws, r.get("title") or "", f"{r.get('body') or ''} {r.get('kind')} alert")
        if sc <= 0:
            continue
        out.append(item("alert", r["id"], r.get("title") or "Alert",
                        " · ".join(x for x in (_ago(r.get("ts")), r.get("body")) if x), sc - 0.5, source="alerts",
                        ref=r.get("ref"), link=f"thomascreek://event/{r['ref']}" if r.get("kind") in (
                            "camera", "package", "doorbell") and r.get("ref") else "thomascreek://inbox",
                        symbol="bell.fill", ts=r.get("ts")))
        if len(out) >= 6:
            break
    return out


async def _inventory(h: "Hub", s: Subject, q: str, ws: list[str]) -> list[dict[str, Any]]:
    if not h.policy.has(s, "inventory.search") or not h.homebox.enabled or _childlike(s):
        return []
    out = []
    for i in (await h.homebox.search(q))[:8]:
        sc = score(ws, i.get("name") or "", f"{i.get('location') or ''} {i.get('description') or ''}") or 1.0
        sub = " · ".join(x for x in (i.get("location"), f"×{i['quantity']}" if (i.get("quantity") or 1) > 1 else None)
                         if x)
        out.append(item("inventory", str(i["id"]), i.get("name") or "Item", sub or "Inventory", sc,
                        source="homebox", ref=str(i["id"]), link="thomascreek://inventory", symbol="shippingbox"))
    return out


# -- commands ---------------------------------------------------------------------
def command(h: "Hub", s: Subject, q: str) -> dict[str, Any] | None:
    """'kitchen off', 'turn on basement', 'preheat 350'. Returns a one-tap action for the top of the list."""
    m = norm(q)
    r = re.match(r"^(?:turn |switch )?(?:the )?(.+?) (?:lights? )?(off|on)$", m) or \
        re.match(r"^(?:turn |switch )?(off|on) (?:the )?(.+?)(?: lights?)?$", m)
    if r:
        place, onoff = (r.group(1), r.group(2)) if r.group(2) in ("on", "off") else (r.group(2), r.group(1))
        pw = words(place)
        cands = [a for a in _room_hits(h, s, pw)]
        best = max(((score(pw, a["title"]), a) for a in cands), key=lambda x: x[0], default=(0, None))
        room = best[1]
        if room and best[0] > 0:
            home = h.home
            want = onoff == "on"
            steps = []
            for eid, ar in home.entity_area.items():
                if ar != room["ref"]:
                    continue
                d = home.device(eid, s)
                if not d or d["kind"] not in ("light", "fan", "switch") or not d["available"]:
                    continue
                if bool(d["state"].get("on")) == want:
                    continue
                a = _act(h, s, f"{d['kind']}.set", eid, {"on": want}, "",
                         Target(entity_id=eid, area=room["ref"], kind=d["kind"]))
                if a:
                    steps.append({k: v for k, v in a.items() if k != "label"})
            sub = f"{len(steps)} light{'s' if len(steps) != 1 else ''}" if steps else f"Already {onoff}"
            return {"title": f"Turn {onoff} {room['title']}", "subtitle": sub,
                    "action": {"action": "batch", "target": room["ref"], "params": {}, "tier": "everyday",
                               "label": f"Turn {onoff}", "steps": steps} if steps else None}
    r = re.match(r"^preheat(?: (?:the )?(upper|lower|top|bottom))?(?: oven)?(?: (?:to )?(\d{3}))?$", m)
    if r and h.policy.has(s, "appliances"):
        ovens = h.kitchen.ovens()
        if ovens:
            which = r.group(1)
            eid = ovens[0]
            if which:
                for o in ovens:
                    lab = norm(h.kitchen.label(o))
                    if which in lab or (which == "top" and "upper" in lab) or (which == "bottom" and "lower" in lab):
                        eid = o
            temp = int(r.group(2)) if r.group(2) else None
            label = h.kitchen.label(eid)
            return {"title": f"Preheat {label.lower()}" + (f" to {temp}°" if temp else ""),
                    "subtitle": "Bake · asks for Face ID",
                    "deep_link": f"thomascreek://kitchen/{eid}",
                    "action": _act(h, s, "oven.set", eid, {"mode": "bake", "temp": temp or 350}, "Preheat",
                                   Target(entity_id=eid, kind="oven")) if temp else None}
    return None


# -- run ----------------------------------------------------------------------------
LOCAL = ("room", "light", "scene", "door", "climate", "player", "appliance", "camera", "person", "screen")
REMOTE: dict[str, tuple[str, ...]] = {
    "plex": ("media",), "youtube": ("youtube",), "mealie": ("recipe",), "frigate": ("event",),
    "calendar": ("calendar",), "chores": ("chore",), "trek": ("trip",), "alerts": ("alert",),
    "homebox": ("inventory",),
}


async def run(h: "Hub", s: Subject, q: str, types: set[str] | None = None, limit: int = 8,
              budget: float | None = None, assistant: bool = False) -> dict[str, Any]:
    q = (q or "").strip()[:MAX_Q]
    ws = words(q)
    if not ws:
        return {"query": q, "groups": [], "top": None, "command": None, "pending": [], "ask": False}
    want = (lambda t: types is None or t in types)

    hits: list[dict[str, Any]] = []
    rooms = _room_hits(h, s, ws)
    for fn, ts in ((lambda: rooms, ("room",)), (lambda: _device_hits(h, s, ws), ("light", "door", "climate",
                                                                               "player", "appliance")),
                   (lambda: _scene_hits(h, s, ws), ("scene",)), (lambda: _camera_hits(h, s, ws), ("camera",)),
                   (lambda: _person_hits(h, s, ws), ("person",)), (lambda: _screen_hits(h, s, ws), ("screen",))):
        if any(want(t) for t in ts):
            try:
                hits += [x for x in fn() if want(x["type"])]
            except Exception as exc:  # noqa: BLE001
                log.warning("search local source failed: %s", exc)

    makers: dict[str, Callable[[], Awaitable[list[dict[str, Any]]]]] = {
        "plex": lambda: _media(h, s, q, ws), "youtube": lambda: _youtube(h, s, ws),
        "mealie": lambda: _recipes(h, s, q, ws), "frigate": lambda: _events(h, s, ws),
        "calendar": lambda: _calendar(h, s, ws), "chores": lambda: _chores(h, s, ws),
        "trek": lambda: _trips(h, s, ws), "alerts": lambda: _alerts(h, s, ws),
        "homebox": lambda: _inventory(h, s, q, ws),
    }
    who = f"{s.role}:{s.person_id}:{','.join(sorted(s.areas))}:{s.tv_device}"
    jobs = {name: _cached(f"q:{name}:{who}:{norm(q)}", 45, mk) for name, mk in makers.items()
            if any(want(t) for t in REMOTE[name])}

    pending: list[str] = []
    failed: list[str] = []
    if jobs:
        tasks = {name: asyncio.ensure_future(coro) for name, coro in jobs.items()}
        done, slow = await asyncio.wait(tasks.values(), timeout=budget if budget is not None else BUDGET_S)
        for name, t in tasks.items():
            if t in slow:
                pending += REMOTE[name]
                t.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)  # finishes into the cache
                continue
            exc = t.exception()
            if exc is not None:
                log.info("search source %s failed: %s", name, exc)
                failed.append(name)
                continue
            hits += t.result()

    cmd = command(h, s, q) if types is None or "command" in types else None

    groups: dict[str, list[dict[str, Any]]] = {}
    for x in sorted(hits, key=lambda x: -x["score"]):
        groups.setdefault(x["type"], []).append(x)
    order = list(TYPES)
    out_groups = [{"type": t, "title": TYPES[t], "total": len(rows), "items": rows[:limit]}
                  for t, rows in sorted(groups.items(), key=lambda kv: (-kv[1][0]["score"], order.index(kv[0])))]
    best = sorted(hits, key=lambda x: -x["score"])
    top = None
    if best and best[0]["score"] >= 6 and (len(best) == 1 or best[0]["score"] > best[1]["score"]):
        top = best[0]
    return {"query": q, "groups": out_groups, "top": top, "command": cmd, "pending": sorted(set(pending)),
            "failed": failed, "ask": assistant and (looks_like_question(q) or not hits)}
