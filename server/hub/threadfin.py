"""A live TV feed for Threadfin (and through it Plex Live TV): an M3U playlist, an XMLTV guide and a stream relay.

Dylan, Nov 25 2026: the cloud server's Plex and the home Plex take live TV from Threadfin, which reads this feed.

- `GET /v1/threadfin/playlist.m3u?token=…`: the hub's curated lineup. `list=basic` (the default) is the basic
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
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("hub.threadfin")

SECRET = "threadfin.token"
HOLDER_PREFIX = "threadfin:"
LISTS = ("basic", "all")
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


def channels(h: Any, which: str, home: tuple[float, float] | None,
             groups: set[str] | None = None) -> list[dict[str, Any]]:
    """The feed's channels: {key, name, number, group, logo (has art)}, in lineup order."""
    cat = h.tv_catalog
    shown = cat._shown_channels(cat.load())
    out: list[dict[str, Any]] = []
    if which == "basic":
        body = cat.basic(home) or {"channels": []}
        for row in body.get("channels") or []:
            key = str(row.get("key") or "")
            if key not in shown:
                continue
            out.append({"key": key, "name": str(row.get("name") or shown[key].get("name") or key),
                        "number": row.get("number"), "group": basic_group(row.get("number")),
                        "logo": bool(shown[key].get("logo"))})
    else:
        lineup = cat.lineup(kids_only=False) or {"channels": []}
        for row in lineup.get("channels") or []:
            label = str(row.get("group_label") or row.get("group") or "Other")
            if _ADULT.search(f"{row.get('group') or ''} {label}"):
                continue  # the provider's grown-up section never reaches Plex
            if groups and label.casefold() not in groups:
                continue
            out.append({"key": str(row["key"]), "name": str(row.get("display_name") or row.get("name") or row["key"]),
                        "number": row.get("number"), "group": label, "logo": bool(row.get("logo"))})
    return out


def _attr(value: object) -> str:
    """An M3U attribute value: no quotes or line breaks."""
    return re.sub(r"[\"\r\n]+", " ", str(value or "")).strip()


def playlist(rows: list[dict[str, Any]], base: str, tok: str) -> str:
    """M3U with `tvg-id` = the channel key (the XMLTV ids), `tvg-chno`, `group-title`, a logo and the relay."""
    lines = [f'#EXTM3U url-tvg="{base}/v1/threadfin/guide.xml?token={tok}"']
    for row in rows:
        key = row["key"]
        number = f' tvg-chno="{_attr(row["number"])}"' if row.get("number") is not None else ""
        logo = f' tvg-logo="{base}/v1/threadfin/logo/{key}?token={tok}"' if row.get("logo") else ""
        lines.append(f'#EXTINF:-1 tvg-id="{_attr(key)}" tvg-name="{_attr(row["name"])}"{number}{logo} '
                     f'group-title="{_attr(row["group"])}",{_attr(row["name"])}')
        lines.append(f"{base}/v1/threadfin/stream/{key}.ts?token={tok}")
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
                   + (f"<icon src={quoteattr(f'{base}/v1/threadfin/logo/{row['key']}?token={tok}')}/>"
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
            h.db.kv_stamp("tvapp.lineup"), cat.lineup_stamp(False))


def reply(h: Any, kind: str, which: str, home: tuple[float, float] | None, groups: set[str] | None,
          base: str) -> Reply:
    """The playlist or guide, built once per lineup, guide and caller address (run off the hub's loop)."""
    tok = token(h)
    frozen = frozenset(groups) if groups else None
    key = (kind, base, _stamp(h, which, home, frozen))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        rows = channels(h, which, home, groups)
        if kind == "m3u":
            built = Reply(playlist(rows, base, tok).encode(), "audio/x-mpegurl")
        else:
            built = Reply(guide(h, rows, base, tok).encode(), "application/xml")
        for old in [k for k in _CACHE if k[0] == kind and k[1] == base]:
            _CACHE.pop(old, None)
        if len(_CACHE) > 16:
            _CACHE.clear()
        _CACHE[key] = built
        return built


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
