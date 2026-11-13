"""Stable channel keys for the household lineup.

A key names a channel. It never names a stream URL. Xtream puts the provider
password in the URL path, so a hash of a "cleaned" URL would still be derived
from a secret. v1 therefore accepts only:

  iptv-org:<id>          the iptv-org channel id, e.g. CNN.us
  xc:<source>:<stream>   Xtream stream_id, source is a household label
  tvg:<source>:<tvg-id>  M3U tvg-id, same kind of source label

When a row carries more than one, the hub prefers iptv-org, then stream_id,
then tvg-id. `source` is a short name the household picks once per playlist
(`thomas-creek`). It is not the username and not a host.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

_SOURCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,80}$")
_KEY = re.compile(r"^(?:iptv-org:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}|xc:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80}|tvg:[a-z0-9][a-z0-9-]{0,40}:[A-Za-z0-9][A-Za-z0-9._+-]{0,80})$")
_SECRET_QUERY = ("password", "passwd", "token", "secret")


class LineupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def valid_key(key: str) -> bool:
    return bool(_KEY.fullmatch(key or ""))


def channel_key(*, iptv_org: str | None = None, source: str | None = None,
                stream_id: str | None = None, tvg_id: str | None = None) -> str:
    """Build a key. Preference: iptv-org, then Xtream stream_id, then tvg-id."""
    if iptv_org:
        if not _ID.fullmatch(str(iptv_org)):
            raise LineupError("bad_key", "iptv-org id must be the channel id, not a URL")
        return f"iptv-org:{iptv_org}"
    if stream_id:
        return f"xc:{_source(source)}:{_ident(stream_id, 'stream_id')}"
    if tvg_id:
        return f"tvg:{_source(source)}:{_ident(tvg_id, 'tvg_id')}"
    raise LineupError("bad_key", "A channel needs an iptv-org id, an Xtream stream_id, or a tvg-id")


def _source(source: str | None) -> str:
    if not source or not _SOURCE.fullmatch(source):
        raise LineupError("bad_key", "source is a short household name such as thomas-creek, not a URL or username")
    return source


def _ident(value: object, label: str) -> str:
    text = str(value)
    if not _ID.fullmatch(text):
        raise LineupError("bad_key", f"{label} must be the provider id, not a URL")
    return text


def clean_logo(url: str | None) -> str | None:
    if url is None:
        return None
    text = str(url).strip()
    if not text:
        return None
    if len(text) > 300:
        raise LineupError("bad_logo", "logo URL is too long")
    if text.startswith("/v1/"):
        if ".." in text or "@" in text or "://" in text:
            raise LineupError("bad_logo", "logo path is not a hub path")
        return text
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise LineupError("bad_logo", "logo must be an http(s) URL or a hub path")
    if parts.username or parts.password or "@" in parts.netloc:
        raise LineupError("bad_logo", "logo URL must not contain a username or password")
    query = parts.query.lower()
    if any(k in query for k in _SECRET_QUERY):
        raise LineupError("bad_logo", "logo URL must not carry a secret")
    return text


def normalize_channel(raw: dict[str, Any]) -> dict[str, Any]:
    """One stored channel. `hidden` stays in the vault copy and is left off the public list."""
    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        raise LineupError("bad_channel", "A channel needs a name")
    if raw.get("key"):
        key = str(raw["key"])
        if not valid_key(key):
            raise LineupError("bad_key", "channel key must be iptv-org, xc or tvg, and must not be a URL")
    else:
        key = channel_key(iptv_org=_blank(raw.get("iptv_org")), source=_blank(raw.get("source")),
                          stream_id=_blank(raw.get("stream_id")), tvg_id=_blank(raw.get("tvg_id")))
    number = raw.get("number")
    if number is not None and number != "":
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise LineupError("bad_channel", "number must be an integer") from None
        if not 0 <= number <= 99999:
            raise LineupError("bad_channel", "number is out of range")
    else:
        number = None
    group = str(raw.get("group") or "").strip()[:40] or None
    return {
        "key": key,
        "name": name,
        "number": number,
        "group": group,
        "logo": clean_logo(_blank(raw.get("logo"))),
        "kids": bool(raw.get("kids")),
        "hidden": bool(raw.get("hidden")),
    }


def public_channel(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": row["key"],
        "name": row["name"],
        "number": row.get("number"),
        "group": row.get("group"),
        "logo": row.get("logo"),
        "kids": bool(row.get("kids")),
    }


def _blank(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
