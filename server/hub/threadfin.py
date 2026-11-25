"""A live TV feed for Threadfin (and through it Plex Live TV): an M3U playlist, an XMLTV guide and a stream relay.

Dylan, Nov 25 2026: the cloud server's Plex and the home Plex take live TV from Threadfin, which reads this feed.

- `GET /v1/threadfin/<feed>.m3u?token=…` and `<feed>.xml`: one named feed (FEEDS: plex, kids, sports, news,
  everything), each a playlist and its guide; `group=` keeps only the named groups.
- `GET /v1/threadfin/playlist.m3u?token=…`: the first link, the same as `plex.m3u` (`list=all` is `everything`).
  The hub's curated lineup. `list=basic` (the default) is the basic
  cable lineup (hub/tvbasic.py: the home market's locals and one copy of each standard network, numbered, with
  the hub's cleaned names) in groups by kind; `list=all` is the whole lineup with the guide editor's edits, cleaned
  names and category labels, without the provider's grown-up section (`group=` keeps only the named labels).
- `GET /v1/threadfin/guide.xml?token=…`: XMLTV whose channel ids are the M3U's `tvg-id` (the hub's channel key).
- `GET /v1/threadfin/stream/<key>.ts?token=…`: the channel as one MPEG-TS stream, relayed by the hub. Each stream
  leases its own login from the house's pool (hub/iptvplay.py), the same pool the TVs and phones take theirs from,
  on the provider's best address with the backups after it; a login the provider refuses is set aside and the
  next one tried. The lease is freed the moment Threadfin hangs up.
- `GET /v1/threadfin/status?token=…`: the connection budget (how many streams every login allows, how many are
  open, how many are free and how many Threadfin holds), to size Threadfin's tuner count.

Threadfin can't send headers, so the token rides in the query string. It is its own secret in the vault
(`threadfin.token`), made on first use, shown on the hub web page (Watching, Threadfin) and replaced there. It
opens nothing else. Callers from the public internet are refused even with the token: the feed is for the home
network and the Tailscale link to the cloud server. Log lines never carry the token or a provider address.
"""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("hub.threadfin")

SECRET = "threadfin.token"
HOLDER_PREFIX = "threadfin:"
# The named feeds, each a playlist and a guide with the same channel ids (Dylan, Nov 25 2026: "a playlist
# specifically for Plex, then kids, then everything normalized"). Threadfin takes each as its own playlist.
FEEDS: dict[str, tuple[str, str]] = {
    "plex": ("Plex", "The basic lineup: the home market's locals and one copy of each network, numbered like "
                     "cable."),
    "kids": ("Kids", "Only the channels a kids' TV gets, with the hub's names and guide."),
    "sports": ("Sports", "The basic lineup's sports networks first, then every sports channel the provider has."),
    "news": ("News", "The basic lineup's news networks first, then every news channel the provider has."),
    "everything": ("Everything", "Every channel with the hub's cleaned names and groups, without the grown-up "
                                 "section."),
}
ALIASES = {"basic": "plex", "all": "everything"}  # the first links (?list=basic|all) keep working
LISTS = (*FEEDS, *ALIASES)
# Channels a combined feed adds after the basic lineup's are numbered from here, so no two share a number.
EXTRA_FROM = 1000
_TOPIC = {
    "sports": re.compile(r"\b(?:sports?|espn\w*|nfl|nba|mlb|nhl|mls|ncaa\w*|f1|formula\s*1|nascar|indycar|"
                         r"motor\s*sports?|racing|golf|tennis|soccer|football|futbol|rugby|cricket|ufc|boxing|wwe|fight\w*|ppv|"
                         r"bein|dazn|tsn|sky\s*sports?|game\s*pass|sunday\s*ticket)\b", re.I),
    "news": re.compile(r"\b(?:news\w*|cnn|msnbc|cnbc|bloomberg|c-?span|weather|newsmax|fox\s*business|bbc\s*world|"
                       r"sky\s*news|al\s*jazeera|euronews|france\s*24|dw)\b", re.I),
}
_TOPIC_GROUP = {"sports": "Sports", "news": "News"}
# Programmes kept in the XMLTV: from a little before now (Plex shows what is on) to as far as the guide goes.
GUIDE_BACK_S = 2 * 3600
_GZIP_FROM = 32 * 1024
_TOKEN_IN_QUERY = re.compile(r"(token=)[^&\s\"]+")
_ADULT = re.compile(r"\b(?:for adults|adults? only|adult|xxx|18\s*\+|porn\w*|erotic\w*)(?!\w)", re.I)

# The basic lineup's numbers say what kind of channel it is (hub/tvbasic.py NETWORKS); Plex shows these groups.
_BASIC_GROUPS = ((0, "Local"), (20, "News"), (30, "Sports"), (50, "Entertainment"), (90, "Documentary & Lifestyle"),
                 (110, "Kids"), (200, "Movies & Premium"))


# -- the token --------------------------------------------------------------------------------------------------
def token(h: Any) -> str:
    """The feed's token, made the first time it is asked for. Never logged."""
    if h.vault.has(SECRET):
        found = str(h.vault.get(SECRET) or "")
        if found:
            return found
    return renew(h)


def renew(h: Any) -> str:
    """A new token; the old links stop working at once."""
    fresh = "tf_" + secrets.token_urlsafe(24)
    h.vault.set(SECRET, fresh)
    _CACHE.clear()  # the playlist carries the token in its stream addresses
    log.info("threadfin: a new feed token was made")
    return fresh


def check(h: Any, given: str | None) -> bool:
    if not given or not h.vault.has(SECRET):
        return False
    return secrets.compare_digest(str(given), str(h.vault.get(SECRET) or ""))


TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def allowed_from(h: Any, ip: str | None) -> bool:
    """Home, the home VPN, the web page's extra ranges and Tailscale (100.64/10, the cloud server's link) may read the
    feed. A caller on the public internet (through the reverse proxy) may not, even with the token."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return h.is_home(ip) or h.can_open_web(ip) or addr in TAILSCALE


def trusted(h: Any, ip: str | None) -> bool:
    """A Tailscale address the config lets read the feed without the token (hub.feed_trusted). Tailscale proves
    which machine is calling; an address outside 100.64/10 never counts, whatever the config says."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    listed = getattr(h.config.hub, "feed_trusted", None) or []
    return addr in TAILSCALE and any(str(addr) == str(x).strip() for x in listed)


def redact(text: str) -> str:
    """A request line with the token taken out (uvicorn's access log)."""
    return _TOKEN_IN_QUERY.sub(r"\1…", text)


class RedactToken(logging.Filter):
    """Keeps the feed token out of uvicorn's access log (it logs every request's path and query)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, str) and "token=" in a for a in args):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in args)
        return True


# -- the lineup -------------------------------------------------------------------------------------------------
def basic_group(number: object) -> str:
    """The kind of channel a basic lineup number is (the ranges of tvbasic.NETWORKS; locals sit below 20)."""
    try:
        n = int(number)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "Entertainment"
    return next(name for start, name in reversed(_BASIC_GROUPS) if n >= start)


def feed_name(which: str) -> str | None:
    """The feed a name or an old `list=` value means, or None."""
    which = (which or "").strip().lower()
    which = ALIASES.get(which, which)
    return which if which in FEEDS else None


def _basic_rows(h: Any, home: tuple[float, float] | None) -> list[dict[str, Any]]:
    cat = h.tv_catalog
    shown = cat._shown_channels(cat.load())
    out: list[dict[str, Any]] = []
    for row in (cat.basic(home) or {}).get("channels") or []:
        key = str(row.get("key") or "")
        if key not in shown:
            continue
        out.append({"key": key, "name": str(row.get("name") or shown[key].get("name") or key),
                    "number": row.get("number"), "group": basic_group(row.get("number")),
                    "logo": bool(shown[key].get("logo"))})
    return out


def _lineup_rows(h: Any, kids: bool, groups: set[str] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in (h.tv_catalog.lineup(kids_only=kids) or {}).get("channels") or []:
        label = str(row.get("group_label") or row.get("group") or "Other")
        if _ADULT.search(f"{row.get('group') or ''} {label}"):
            continue  # the provider's grown-up section never reaches Plex
        if groups and label.casefold() not in groups:
            continue
        out.append({"key": str(row["key"]), "name": str(row.get("display_name") or row.get("name") or row["key"]),
                    "number": row.get("number"), "group": label, "logo": bool(row.get("logo"))})
    return out


def channels(h: Any, which: str, home: tuple[float, float] | None,
             groups: set[str] | None = None) -> list[dict[str, Any]]:
    """The feed's channels: {key, name, number, group, logo (has art)}, in lineup order."""
    feed = feed_name(which) or "plex"
    if feed == "plex":
        return [r for r in _basic_rows(h, home) if not groups or r["group"].casefold() in groups]
    if feed == "everything":
        return _lineup_rows(h, False, groups)
    if feed == "kids":
        return _lineup_rows(h, True, groups)
    # A topic feed: the basic lineup's networks of that kind with their cable numbers, then every other channel
    # whose group or name says so, numbered from EXTRA_FROM so no two share a number in Plex.
    topic, label = _TOPIC[feed], _TOPIC_GROUP[feed]
    out = [r for r in _basic_rows(h, home) if r["group"] == label]
    seen = {r["key"] for r in out}
    number = EXTRA_FROM
    for row in _lineup_rows(h, False, groups):
        if row["key"] in seen or not topic.search(f"{row['group']} {row['name']}"):
            continue
        seen.add(row["key"])
        out.append({**row, "number": number})
        number += 1
    return out


def _attr(value: object) -> str:
    """An M3U attribute value: no quotes or line breaks."""
    return re.sub(r"[\"\r\n]+", " ", str(value or "")).strip()


def _q(tok: str) -> str:
    """The query that carries the token, or nothing for a trusted caller's links."""
    return f"?token={tok}" if tok else ""


def playlist(rows: list[dict[str, Any]], base: str, tok: str, feed: str = "plex",
             groups: set[str] | None = None) -> str:
    """M3U with `tvg-id` = the channel key (the XMLTV ids), `tvg-chno`, `group-title`, a logo and the relay; its
    `url-tvg` is the same feed's guide. An empty `tok` makes links without a token (a trusted caller's)."""
    pick = f"group={quote(','.join(sorted(groups)))}" if groups else ""
    guide_q = _q(tok) + (("&" if tok else "?") + pick if pick else "")
    lines = [f'#EXTM3U url-tvg="{base}/v1/threadfin/{feed}.xml{guide_q}"']
    for row in rows:
        key = row["key"]
        number = f' tvg-chno="{_attr(row["number"])}"' if row.get("number") is not None else ""
        logo = f' tvg-logo="{base}/v1/threadfin/logo/{key}{_q(tok)}"' if row.get("logo") else ""
        lines.append(f'#EXTINF:-1 tvg-id="{_attr(key)}" tvg-name="{_attr(row["name"])}"{number}{logo} '
                     f'group-title="{_attr(row["group"])}",{_attr(row["name"])}')
        lines.append(f"{base}/v1/threadfin/stream/{key}.ts{_q(tok)}")
    return "\n".join(lines) + "\n"


def _xt(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp), timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _text(value: object) -> str:
    # XML 1.0 has no place for control characters a provider's guide sometimes carries.
    return escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value)))


def guide(h: Any, rows: list[dict[str, Any]], base: str, tok: str, now: float | None = None) -> str:
    """XMLTV for `rows`: one <channel> per row (id = its key) and every programme the hub's guide holds from a
    little before now."""
    now = time.time() if now is None else now
    doc = h.tv_catalog.load()
    listings = doc.get("guide") or {}
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Thomas Creek Hub">']
    for row in rows:
        out.append(f"<channel id={quoteattr(row['key'])}><display-name>{_text(row['name'])}</display-name>"
                   + (f"<display-name>{_text(row['number'])}</display-name>" if row.get("number") is not None else "")
                   + (f"<icon src={quoteattr(f'{base}/v1/threadfin/logo/{row['key']}{_q(tok)}')}/>"
                      if row.get("logo") else "")
                   + "</channel>")
    since = now - GUIDE_BACK_S
    for row in rows:
        ident = quoteattr(row["key"])
        for item in listings.get(row["key"]) or []:
            try:
                begins, ends = float(item["start_ts"]), float(item["end_ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if ends <= since or ends <= begins:
                continue
            parts = [f'<programme start="{_xt(begins)}" stop="{_xt(ends)}" channel={ident}>',
                     f"<title>{_text(item.get('title') or row['name'])}</title>"]
            if item.get("subtitle"):
                parts.append(f"<sub-title>{_text(item['subtitle'])}</sub-title>")
            if item.get("desc"):
                parts.append(f"<desc>{_text(item['desc'])}</desc>")
            if row.get("group"):
                parts.append(f"<category>{_text(row['group'])}</category>")
            icon = str(item.get("icon") or "")
            if icon.startswith("https://"):
                parts.append(f"<icon src={quoteattr(icon)}/>")
            parts.append("</programme>")
            out.append("".join(parts))
    out.append("</tv>")
    return "\n".join(out) + "\n"


# -- answers kept per lineup and guide --------------------------------------------------------------------------
class Reply:
    def __init__(self, raw: bytes, media: str):
        self.raw, self.media = raw, media
        self.tag = '"' + hashlib.sha256(raw).hexdigest()[:24] + '"'
        self.packed = gzip.compress(raw, compresslevel=5) if len(raw) >= _GZIP_FROM else None


_CACHE: dict[Any, Reply] = {}
_LOCK = threading.Lock()


def _stamp(h: Any, which: str, home: Any, groups: Any) -> Any:
    cat = h.tv_catalog
    doc = cat.load()
    return (which, home, groups, id(doc), doc.get("fetched_at"), doc.get("guide_at"),
            h.db.kv_stamp("tvapp.lineup"), cat.lineup_stamp(which == "kids"))


KV_WANTED = "threadfin.wanted"
_WANTED_MAX = 16


def reply(h: Any, kind: str, which: str, home: tuple[float, float] | None, groups: set[str] | None,
          base: str, keyless: bool = False) -> Reply:
    """The playlist or guide, built once per lineup, guide and caller address (run off the hub's loop). What was
    asked is remembered, so `rewarm` builds it again after a provider refresh before Threadfin asks (the whole
    guide took 2 s to build on the live hub, Nov 25 2026). `keyless`: links without the token, for a trusted
    caller (the token never reaches it)."""
    tok = "" if keyless else token(h)
    which = feed_name(which) or "plex"
    home = home if which in ("plex", "sports", "news") else None
    frozen = frozenset(groups) if groups else None
    key = (kind, base, keyless, _stamp(h, which, home, frozen))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        rows = channels(h, which, home, groups)
        if kind == "m3u":
            built = Reply(playlist(rows, base, tok, which, groups).encode(), "audio/x-mpegurl")
        else:
            built = Reply(guide(h, rows, base, tok).encode(), "application/xml")
        same = key[:3] + key[3][:3]  # the same answer for an older lineup or guide
        for old in [k for k in _CACHE if k[:3] + k[3][:3] == same]:
            _CACHE.pop(old, None)
        if len(_CACHE) > 32:
            _CACHE.clear()
        _CACHE[key] = built
    _remember(h, kind, which, home, groups, base, keyless)
    return built


def _remember(h: Any, kind: str, which: str, home: Any, groups: set[str] | None, base: str,
              keyless: bool = False) -> None:
    entry: dict[str, Any] = {"kind": kind, "list": which, "home": list(home) if home else None,
             "groups": sorted(groups) if groups else None, "base": base}
    if keyless:
        entry["keyless"] = True
    wanted = [w for w in (h.db.kv_get(KV_WANTED) or []) if isinstance(w, dict)]
    if wanted and wanted[0] == entry:
        return
    wanted = [entry, *[w for w in wanted if w != entry]][:_WANTED_MAX]
    h.db.kv_set(KV_WANTED, wanted)


def rewarm(h: Any) -> int:
    """Build every playlist and guide Threadfin asked for lately, for the catalog as it is now (hub/warm.py runs it
    after a refresh and at start, in a worker). How many were built."""
    if not h.vault.has(SECRET):
        return 0
    done = 0
    for w in list(h.db.kv_get(KV_WANTED) or []):
        try:
            home = tuple(w["home"]) if w.get("home") else None
            reply(h, str(w["kind"]), str(w["list"]), home, set(w["groups"]) if w.get("groups") else None,
                  str(w["base"]), bool(w.get("keyless")))
            done += 1
        except Exception as exc:  # noqa: BLE001 - the next ask builds it instead
            log.info("threadfin: rebuilding a feed answer failed (%s)", type(exc).__name__)
    return done


# -- the connection budget ----------------------------------------------------------------------------------------
def budget(h: Any, now: float | None = None) -> dict[str, Any]:
    """How many provider streams the house has, how many are open and how many are free, per provider and in all.

    `total` counts every login that can play now (not expired, not turned off); `in_use` is the larger of the
    provider's own fresh count and the hub's leases; `threadfin` is the streams this feed holds now.
    `suggested_tuners` is what Threadfin could open at once right now: the free ones plus its own."""
    from .iptvplay import accounts, logins, provider_names

    found = accounts(h, now)
    names = {lg["provider"]: str(lg.get("name") or lg["provider"]) for lg in logins(h)}
    names.update(provider_names(h))
    sessions = found.get("sessions") or []
    by_provider: dict[str, dict[str, Any]] = {}
    for row in found.get("logins") or []:
        entry = by_provider.setdefault(row["provider"], {"provider": names.get(row["provider"], row["provider"]),
                                                         "logins": 0, "total": 0, "in_use": 0, "free": 0})
        entry["logins"] += 1
        if row["status"] in ("expired", "disabled"):
            continue
        entry["total"] += row["max_connections"]
        entry["in_use"] += min(row["active"], row["max_connections"])
        entry["free"] += row["slots_left"]
    total = sum(p["total"] for p in by_provider.values())
    in_use = sum(p["in_use"] for p in by_provider.values())
    free = sum(p["free"] for p in by_provider.values())
    mine = sum(1 for s in sessions if str(s.get("tv") or "").startswith(HOLDER_PREFIX))
    return {"total": total, "in_use": in_use, "free": free, "threadfin": mine,
            "suggested_tuners": free + mine, "providers": sorted(by_provider.values(), key=lambda p: p["provider"])}
