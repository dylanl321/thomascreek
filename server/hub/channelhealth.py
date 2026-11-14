"""Which copy of a channel is worth playing.

The provider has tens of thousands of channels. This never asks all of them.
Plays the TVs already made are the score. A short check, overnight, only touches
favorites, channels watched in the last 30 days, and the few hundred played most.
A source that keeps failing is left off the default, and it is never deleted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .home import iso
from .tvapp import TvAppError
from .tvlineup import valid_key

log = logging.getLogger("hub.channelhealth")

KV = "tv.source_health"
WATCH = "tv.watch"
RECENT_S = 30 * 86400
TOP = 200
WINDOW = 12
DEAD_STREAK = 5
MAX_CHANNELS = 500
MAX_EVENTS = 40
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")


class SourceHealth:
    def __init__(self, hub: Any):
        self.hub = hub
        self._task: asyncio.Task[None] | None = None
        self.fetch = self._fetch

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def accept(self, events: list[dict[str, Any]], now: float | None = None) -> int:
        """Fold real plays into the score. Anything with a URL in it is ignored."""
        now = time.time() if now is None else now
        doc = self._load()
        taken = 0
        for event in events[:MAX_EVENTS]:
            if not isinstance(event, dict):
                continue
            key = str(event.get("channel_key") or "")
            source = str(event.get("source") or "")
            if not valid_key(key) or not _SOURCE.fullmatch(source):
                continue
            if "://" in source or "://" in str(event.get("error") or ""):
                continue
            ok = event.get("ok")
            if not isinstance(ok, bool):
                continue
            when = _when(event.get("ts"), now)
            if when is None:
                continue
            startup = event.get("startup_ms")
            if startup is not None and (isinstance(startup, bool) or not isinstance(startup, (int, float)) or not 0 <= startup <= 60000):
                startup = None
            row = doc.setdefault(key, {}).setdefault(source, {"bits": [], "startup_ms": None, "last_ok": None, "last": 0})
            bits = list(row.get("bits") or [])
            bits.append(1 if ok else 0)
            row["bits"] = bits[-WINDOW:]
            row["last"] = when
            if ok:
                row["last_ok"] = when
                if isinstance(startup, (int, float)):
                    prev = row.get("startup_ms")
                    row["startup_ms"] = int(startup if prev is None else prev * 0.7 + startup * 0.3)
            doc[key][source] = row
            taken += 1
        self._save(_trim(doc))
        return taken

    def annotate(self, channels: list[dict[str, Any]]) -> None:
        """Add health and a ranked source list. Channels with no plays are left unchanged."""
        doc = self._load()
        for channel in channels:
            block = self._block(doc.get(channel.get("key")) or {})
            if block is None:
                continue
            channel["health"] = {"score": block["score"], "last_ok": block["last_ok"]}
            channel["sources"] = block["sources"]

    def worst(self, limit: int = 40) -> dict[str, Any]:
        doc = self._load()
        names = self._names(set(doc))
        rows = []
        for key, sources in doc.items():
            name = names.get(key) or key
            for source, row in (sources or {}).items():
                score = _score(row)
                if score is None:
                    continue
                rows.append({
                    "channel_key": key, "name": name, "source": source, "score": score,
                    "last_ok": iso(row.get("last_ok")), "default": _default_source(sources) == source,
                })
        rows.sort(key=lambda row: (row["score"], str(row["name"]).casefold(), row["source"]))
        return {"sources": rows[: max(1, min(limit, 100))]}

    def candidates(self, now: float | None = None) -> list[str]:
        """Favorites, the last 30 days, and the top plays. Never the whole catalog."""
        now = time.time() if now is None else now
        ranked: dict[str, tuple[int, float]] = {}
        watched = self.hub.db.kv_get(WATCH) or {}
        if isinstance(watched, dict):
            for bucket in watched.values():
                if not isinstance(bucket, dict):
                    continue
                for key, row in (bucket.get("channels") or {}).items():
                    if not valid_key(str(key)) or not isinstance(row, dict):
                        continue
                    last = float(row.get("last") or 0)
                    if now - last > RECENT_S:
                        continue
                    prev = ranked.get(str(key))
                    plays = int(row.get("n") or 0)
                    if prev is None or plays > prev[0]:
                        ranked[str(key)] = (plays, last)
        favorites = []
        for key in self._favorite_keys():
            if key not in favorites:
                favorites.append(key)
            ranked.setdefault(key, (0, now))
        rest = sorted((key for key in ranked if key not in favorites), key=lambda key: ranked[key], reverse=True)
        return (favorites + rest)[:TOP]

    def overnight(self, now: float | None = None) -> bool:
        moment = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(self.hub.config.hub.timezone))
        return 1 <= moment.hour < 6

    def concurrency(self) -> int:
        """Stay under the provider's stream cap. Two is the ceiling either way."""
        dvr = getattr(self.hub.config, "dvr", None)
        cap = int(getattr(dvr, "max_connections", 2) or 2)
        return max(1, min(2, cap))

    async def probe_some(self, now: float | None = None) -> int:
        """Check a handful of the candidate channels. A miss is a score, not an error."""
        if not self.overnight(now):
            return 0
        keys = [key for key in self.candidates(now) if key.startswith("xc:")][: self.concurrency()]
        done = 0
        for key in keys:
            try:
                ok = await self.fetch(key)
            except Exception as exc:  # noqa: BLE001
                log.info("channel check skipped: %s", _safe(exc))
                ok = False
            source = key.split(":")[1]
            self.accept([{"channel_key": key, "source": source, "ok": ok, "startup_ms": 500 if ok else None}], now)
            done += 1
            await asyncio.sleep(0)
        return done

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self.probe_some()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("channel check: %s", _safe(exc))
            await asyncio.sleep(30 * 60)

    async def _fetch(self, channel_key: str) -> bool:
        url = self._url(channel_key)
        if not url:
            return False
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, read=4.0), follow_redirects=False) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return False
                    async for _chunk in resp.aiter_bytes():
                        return True
                    return False
        except httpx.HTTPError:
            return False

    def _url(self, channel_key: str) -> str | None:
        parts = channel_key.split(":")
        if len(parts) != 3 or parts[0] != "xc" or not parts[2]:
            return None
        try:
            server, user, password = self.hub.tv_catalog._login()
        except TvAppError:
            return None
        if not server or not user or not password:
            return None
        return f"{server}/live/{user}/{password}/{parts[2]}.ts"

    def _block(self, sources: dict[str, Any]) -> dict[str, Any] | None:
        ranked = []
        for name, row in sources.items():
            if not isinstance(row, dict):
                continue
            score = _score(row)
            if score is None:
                continue
            ranked.append({"source": name, "score": score, "last_ok": iso(row.get("last_ok")), "default": False,
                           "_dead": _dead(row)})
        if not ranked:
            return None
        ranked.sort(key=lambda row: (row["_dead"], -row["score"], row["source"]))
        chosen = next((row for row in ranked if not row["_dead"]), None)
        if chosen is not None:
            chosen["default"] = True
        for row in ranked:
            row.pop("_dead", None)
        best = chosen or ranked[0]
        return {"score": best["score"], "last_ok": best["last_ok"], "sources": ranked}

    def _names(self, keys: set[str]) -> dict[str, str]:
        """Names for channels we have already scored. One pass, not a check of every channel."""
        names: dict[str, str] = {}
        doc = self.hub.tv_catalog.load() if self.hub.tv_catalog else {}
        for row in doc.get("channels") or []:
            if row.get("key") in keys and row.get("name"):
                names[str(row["key"])] = str(row["name"])
        for row in self.hub.tv_app._lineup().get("channels") or []:
            if row.get("key") in keys and row.get("name") and not row.get("hidden"):
                names[str(row["key"])] = str(row["name"])
        return names

    def _favorite_keys(self) -> list[str]:
        keys = []
        try:
            rows = self.hub.db.all("SELECT value FROM person_settings WHERE key=?", ["fav.media"])
        except Exception:  # noqa: BLE001
            return keys
        for row in rows:
            value = row.get("value")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    continue
            if not isinstance(value, list):
                continue
            for item in value:
                ident = item.get("id") if isinstance(item, dict) else None
                if isinstance(ident, str) and valid_key(ident):
                    keys.append(ident)
        return keys

    def _load(self) -> dict[str, Any]:
        raw = self.hub.db.kv_get(KV) or {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, doc: dict[str, Any]) -> None:
        self.hub.db.kv_set(KV, doc)

    def _default_of(self, sources: dict[str, Any]) -> str | None:
        return _default_source(sources)


def _default_source(sources: dict[str, Any]) -> str | None:
    best = None
    best_score = -1.0
    for name, row in sources.items():
        if not isinstance(row, dict) or _dead(row):
            continue
        score = _score(row)
        if score is None:
            continue
        if score > best_score:
            best, best_score = name, score
    return best


def _score(row: dict[str, Any]) -> float | None:
    bits = [bit for bit in (row.get("bits") or []) if bit in (0, 1)]
    if not bits:
        return None
    rate = sum(bits) / len(bits)
    startup = row.get("startup_ms")
    speed = 1.0 if not isinstance(startup, (int, float)) else max(0.3, 1 - min(float(startup), 10000) / 10000)
    return round(rate * speed, 2)


def _dead(row: dict[str, Any]) -> bool:
    bits = [bit for bit in (row.get("bits") or []) if bit in (0, 1)]
    return len(bits) >= DEAD_STREAK and all(bit == 0 for bit in bits[-DEAD_STREAK:])


def _trim(doc: dict[str, Any]) -> dict[str, Any]:
    if len(doc) <= MAX_CHANNELS:
        return doc
    def last(item: tuple[str, Any]) -> float:
        sources = item[1] if isinstance(item[1], dict) else {}
        return max((float((row or {}).get("last") or 0) for row in sources.values() if isinstance(row, dict)), default=0)
    kept = sorted(doc.items(), key=last, reverse=True)[:MAX_CHANNELS]
    return dict(kept)


def _when(value: object, now: float) -> float | None:
    if value is None:
        return now
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    when = float(value)
    if when > now + 120 or now - when > 14 * 86400:
        return None
    return when


def _safe(exc: Exception) -> str:
    text = str(exc)
    if "://" in text or "password" in text.casefold():
        return "the check didn't finish"
    return text[:120] or "the check didn't finish"
