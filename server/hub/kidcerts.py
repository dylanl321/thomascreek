"""Age certifications for the kids' titles, so a child's level means something for the provider's films and series.

The provider's `rating` is a star score ("7.8"), not an age rating, so `kidratings` could only judge a provider
title by the kids' category it sits in: a five-year-old on "Little" saw the same PG films and TV-Y7 shows as a
nine-year-old on "Kid". This module looks a title up once on TMDB (US certification: G/PG/PG-13 for a film,
TV-Y/TV-Y7/TV-G/TV-PG/TV-14 for a show), keeps the answer for the whole house, and `kidratings` judges by it.

It is filled lazily: a kids' card the store doesn't know is queued when it is served, and a background task
looks a few up at a time. Until a title is known it is judged as before (by its kids' category). The cache lives in
the hub's own settings (`hub_kv`), apart from the catalog and its rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger("hub.kidcerts")

KV = "kids.certs"
TMDB = "https://api.themoviedb.org/3"
SECRET = "tmdb.api_key"
HIT_TTL_S = 180 * 86400
MISS_TTL_S = 14 * 86400
MAX_PENDING = 5000
BATCH = 8
SAVE_EVERY_S = 30.0
START_QUIET_S = 300.0  # the hub's own start-up reads come first; the warm scan waits this long after start
_STARTED = time.monotonic()
US = "US"


def kind_of(item: dict[str, Any]) -> str | None:
    """"movie" or "tv" for a film or series card (by its kind or its id), None for a channel, a video or Plex."""
    kind = str(item.get("kind") or item.get("type") or "").lower()
    ident = str(item.get("id") or "")
    if kind in ("series", "tv", "show") or ident.startswith("series:"):
        return "tv"
    if kind in ("movie", "film", "vod") or ident.startswith("vod:"):
        return "movie"
    return None


def key_of(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The cache key for a card and what a lookup needs: its TMDB id when it has one, else its cleaned name."""
    kind = kind_of(item)
    if kind is None:
        return None
    tmdb = str(item.get("tmdb") or item.get("tmdb_id") or "").strip()
    if tmdb.isdigit():
        return f"{kind}:{tmdb}", {"kind": kind, "tmdb": tmdb}
    from . import tvnames

    name, named_year = tvnames.polish(item.get("name") or item.get("title"))
    name = name.strip()
    if not name:
        return None
    year = _year(item.get("year")) or named_year
    return f"{kind}:n:{name.casefold()}|{year or ''}", {"kind": kind, "name": name, "year": year}


def _year(value: object) -> int | None:
    text = str(value or "").strip()[:4]
    return int(text) if text.isdigit() and 1900 < int(text) < 2100 else None


class Certs:
    """The house's certification cache with its lookup queue. One per hub (`store`)."""

    def __init__(self, h: Any):
        self.h = h
        self.known: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.task: asyncio.Task[None] | None = None
        self.version = 0
        self.warmed = 0.0
        self.ids: dict[str, str] = {}  # catalog id -> cache key, from the warm scan of the kids' library
        self.specs: dict[str, dict[str, Any]] = {}
        doc = h.db.kv_get(KV)
        if isinstance(doc, dict):
            self.known = {str(k): v for k, v in doc.items() if isinstance(v, dict)}

    def cert(self, item: dict[str, Any]) -> str | None:
        """The title's US age certification when known ("PG", "TV-Y7"). An unknown kids' title is queued."""
        known_key = self.ids.get(str(item.get("id") or ""))
        found = key_of(item)
        if found is None:
            return None
        key, spec = found
        if known_key and known_key != key and not (item.get("tmdb") or item.get("tmdb_id")):
            key, spec = known_key, self.specs.get(known_key, spec)  # a page card: its catalog row's TMDB id
        entry = self.known.get(key)
        fresh = entry is not None and time.time() - float(entry.get("t") or 0) < (
            HIT_TTL_S if entry.get("c") else MISS_TTL_S)
        if not fresh and key not in self.pending and len(self.pending) < MAX_PENDING:
            self.pending[key] = spec
        if self.pending:
            self.kick()
        return str(entry["c"]) if entry and entry.get("c") else None

    def blocked(self, bucket: str, allowed: set[str]) -> set[str]:
        """Catalog ids ("vod:..." in `movies`, "series:..." in `series`) whose known certification this level
        leaves out, so a list can skip them before it is paged."""
        from .kidratings import CONTENT

        prefix = "vod:" if bucket == "movies" else "series:"
        out = set()
        for ident, key in self.ids.items():
            cert = str((self.known.get(key) or {}).get("c") or "")
            if ident.startswith(prefix) and cert in CONTENT and cert not in allowed:
                out.add(ident)
        return out

    def status(self) -> dict[str, Any]:
        """Counts for the admin check (`GET /v1/kids/certs`): what is known, mapped, waiting and left out per level."""
        from .kidratings import LEVELS, allowed_at

        hits = sum(1 for v in self.known.values() if v.get("c"))
        mapped_known = sum(1 for k in self.ids.values() if k in self.known)
        by_cert: dict[str, int] = {}
        for k in set(self.ids.values()):
            cert = str((self.known.get(k) or {}).get("c") or "")
            by_cert[cert or "none"] = by_cert.get(cert or "none", 0) + 1
        return {
            "known": len(self.known), "with_certification": hits, "pending": len(self.pending),
            "mapped": len(self.ids), "mapped_known": mapped_known, "by_certification": by_cert,
            "warmed_at": self.warmed or None, "warm_due": self.warm_due(),
            "looking_up": self.task is not None and not self.task.done(),
            "blocked": {name: {b: len(self.blocked(b, allowed_at(name))) for b in ("movies", "series")}
                        for name, _label, _adds in LEVELS},
            "saved": len((self.h.db.kv_get(KV) or {})),
        }

    def warm_due(self) -> bool:
        if time.monotonic() - _STARTED < START_QUIET_S:
            return False
        return time.time() - self.warmed >= 6 * 3600

    def warm(self) -> None:
        """Every kids' film and series queued at most every 6 hours, so a level narrows a page the first time a
        child opens it. Read from the catalog index (SQLite), never the big snapshot; run it off the loop."""
        if not self.warm_due():
            return
        self.warmed = time.time()
        catalog = getattr(self.h, "tv_catalog", None)
        try:
            if catalog is None or not catalog.index_ready():
                self.warmed = 0.0  # try again on the next page, once the index is there
                return
            kids = catalog._kid_names()
            for bucket, kind in (("movies", "movie"), ("series", "series")):
                rows, _total, _names = catalog.index.titles(bucket, needle="", category=None, kids=kids,
                                                            limit=100_000, offset=0, sort="name")
                for row in rows:
                    found = key_of({**row, "kind": kind}) if isinstance(row, dict) else None
                    if found is None:
                        continue
                    key, spec = found
                    self.ids[str(row.get("id"))] = key
                    self.specs[key] = spec
                    self.cert({**row, "kind": kind})
        except Exception as exc:  # noqa: BLE001 - a catalog still loading: the pages queue their own titles
            log.info("kidcerts: warm skipped (%s)", type(exc).__name__)

    def kick(self) -> None:
        if self.task is not None and not self.task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # a worker thread (a page built off the loop): the next call on the loop starts it
        self.task = loop.create_task(self.drain())

    async def drain(self) -> None:
        vault = self.h.vault
        key = vault.get(SECRET) if vault.has(SECRET) else None
        if not key:
            self.pending.clear()  # without a TMDB key titles stay judged by their kids' category
            return
        done = 0
        saved = time.monotonic()
        async with httpx.AsyncClient(timeout=15) as client:
            while self.pending:
                batch = list(self.pending.items())[:BATCH]
                answers = await asyncio.gather(*(lookup(client, str(key), spec) for _k, spec in batch),
                                               return_exceptions=True)
                now = time.time()
                for (k, _spec), answer in zip(batch, answers, strict=True):
                    self.pending.pop(k, None)
                    if isinstance(answer, BaseException):
                        log.info("kidcerts: lookup failed (%s)", _safe(answer))
                        continue  # not stored: tried again the next time the title is served
                    self.known[k] = {"c": answer or "", "t": now}
                    done += 1
                self.version += 1
                if done and time.monotonic() - saved >= SAVE_EVERY_S:
                    self._save()
                    done, saved = 0, time.monotonic()
                await asyncio.sleep(0.2)  # well under TMDB's limit, and the loop stays free
        if done:
            self._save()

    def _save(self) -> None:
        self.h.db.kv_set(KV, self.known)


def store(h: Any) -> Certs:
    found = getattr(h, "_kid_certs", None)
    if found is None:
        found = Certs(h)
        h._kid_certs = found
    return found


async def lookup(client: httpx.AsyncClient, key: str, spec: dict[str, Any]) -> str | None:
    """The US certification on TMDB, "" when TMDB has none, None when the title isn't found."""
    kind = spec["kind"]
    params = {"api_key": key}
    tmdb = spec.get("tmdb")
    if not tmdb:
        query: dict[str, Any] = {**params, "query": spec["name"]}
        if spec.get("year"):
            query["first_air_date_year" if kind == "tv" else "year"] = spec["year"]
        found = await _get(client, f"/search/{kind}", query)
        tmdb = _pick(found.get("results") or [], spec, kind)
        if tmdb is None:
            return None
    if kind == "tv":
        body = await _get(client, f"/tv/{tmdb}/content_ratings", params)
        return next((str(r.get("rating") or "").strip().upper() for r in body.get("results") or []
                     if isinstance(r, dict) and r.get("iso_3166_1") == US), "")
    body = await _get(client, f"/movie/{tmdb}/release_dates", params)
    for country in body.get("results") or []:
        if isinstance(country, dict) and country.get("iso_3166_1") == US:
            for release in country.get("release_dates") or []:
                cert = str((release or {}).get("certification") or "").strip().upper()
                if cert:
                    return cert
    return ""


def _pick(rows: list[Any], spec: dict[str, Any], kind: str) -> str | None:
    """The search hit for this title: the same name (and year when the card has one). A near miss is no answer, so
    a wrong film's certification never lands on a kids' title."""
    name = str(spec["name"]).casefold()
    year = spec.get("year")
    field, date = ("name", "first_air_date") if kind == "tv" else ("title", "release_date")
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        same = name in (str(row.get(field) or "").casefold(), str(row.get("original_" + field) or "").casefold())
        if same and (not year or str(row.get(date) or "").startswith(str(year))):
            return str(row["id"])
    return None


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(TMDB + path, params=params)
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def _safe(exc: BaseException) -> str:
    """An error for the log without the request address, which carries the TMDB key."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
