"""An episode's real name when the provider only calls it "Episode 3".

Providers often send episodes with no name of their own, so the "Next episode" card and the queue said
"Episode 3". The hub asks TMDB once per season (by the series' TMDB id) and keeps the names for the house, so
every screen shows the same name and no screen calls an outside service (rules 1 and 2).

It is filled lazily: the first time a nameless episode of a season is served the season is queued and the
answer is still "Episode 3"; a background task reads the season, and the next ask gets the name. Without a TMDB
key, a TMDB id or an answer, the name stays as it was (rule 4: a plain fallback)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger("hub.episodenames")

KV = "tv.episode_names"
TMDB = "https://api.themoviedb.org/3"
SECRET = "tmdb.api_key"
HIT_TTL_S = 30 * 86400
MISS_TTL_S = 86400          # a season TMDB had nothing for: new seasons get their names within days
MAX_SEASONS = 4000
MAX_PENDING = 200
_PLACEHOLDER = re.compile(r"^(episode|folge|episodio|épisode|aflevering)\s*#?\d*$", re.I)


def nameless(title: object) -> bool:
    """True for a name that says nothing ("Episode", "Episode 3")."""
    return bool(_PLACEHOLDER.match(str(title or "").strip()))


def parse(body: object) -> dict[str, str]:
    """TMDB's season body as `{"<episode number>": name}`, without TMDB's own "Episode 3" placeholders."""
    out: dict[str, str] = {}
    rows = body.get("episodes") if isinstance(body, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        number, name = row.get("episode_number"), str(row.get("name") or "").strip()
        if isinstance(number, int) and not isinstance(number, bool) and number > 0 and name and not nameless(name):
            out[str(number)] = name[:120]
    return out


class Names:
    """The house's episode names with their lookup queue. One per catalog (`store`)."""

    def __init__(self, vault: Any, db: Any):
        self.vault, self.db = vault, db
        self.known: dict[str, dict[str, Any]] = {}
        self.pending: set[str] = set()
        self.task: asyncio.Task[None] | None = None
        doc = db.kv_get(KV) if db is not None else None
        if isinstance(doc, dict):
            self.known = {str(k): v for k, v in doc.items() if isinstance(v, dict)}

    def name(self, tmdb: object, season: object, number: object) -> str | None:
        """The episode's name when known. An unknown or stale season is queued (`kick` starts the lookup)."""
        if not (str(tmdb or "").isdigit() and isinstance(season, int) and isinstance(number, int)
                and not isinstance(season, bool) and not isinstance(number, bool) and season > 0 and number > 0):
            return None
        key = f"{tmdb}:{season}"
        entry = self.known.get(key)
        names = entry.get("n") if entry else None
        fresh = entry is not None and time.time() - float(entry.get("t") or 0) < (HIT_TTL_S if names else MISS_TTL_S)
        if not fresh and len(self.pending) < MAX_PENDING:
            self.pending.add(key)
        found = names.get(str(number)) if isinstance(names, dict) else None
        return str(found) if found else None

    def kick(self) -> None:
        """Start the lookups, from the event loop. From a worker thread it does nothing: the route kicks."""
        if not self.pending or (self.task is not None and not self.task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.task = loop.create_task(self.drain())

    async def drain(self) -> None:
        try:
            key = self.vault.get(SECRET) if self.vault is not None and self.vault.has(SECRET) else None
        except Exception:  # noqa: BLE001 - a vault that can't answer: names stay as they are
            key = None
        if not key:
            self.pending.clear()
            return
        done = False
        async with httpx.AsyncClient(timeout=10) as client:
            while self.pending:
                season_key = self.pending.pop()
                tmdb, _, season = season_key.partition(":")
                try:
                    names = await lookup(client, str(key), tmdb, season)
                except (httpx.HTTPError, ValueError) as exc:
                    log.info("episodenames: lookup failed (%s)", type(exc).__name__)
                    continue  # not stored: asked again the next time the season is served
                self.known[season_key] = {"n": names, "t": time.time()}
                done = True
                await asyncio.sleep(0.2)  # well under TMDB's limit
        if done:
            await asyncio.to_thread(self._save)

    def _save(self) -> None:
        if len(self.known) > MAX_SEASONS:
            by_age = sorted(self.known, key=lambda k: float(self.known[k].get("t") or 0))
            for old in by_age[:len(self.known) - MAX_SEASONS]:
                self.known.pop(old, None)
        self.db.kv_set(KV, self.known)


async def lookup(client: httpx.AsyncClient, key: str, tmdb: str, season: str) -> dict[str, str]:
    """One season's names from TMDB; empty when TMDB does not know the season."""
    bearer = key.startswith("eyJ")   # a v4 token goes in the header, a v3 key in the query
    answer = await client.get(f"{TMDB}/tv/{tmdb}/season/{season}",
                              params=None if bearer else {"api_key": key},
                              headers={"Authorization": f"Bearer {key}"} if bearer else None)
    if answer.status_code == 404:
        return {}
    answer.raise_for_status()
    return parse(answer.json())


def store(cat: Any) -> Names | None:
    """The names kept on the catalog `cat`; None for a catalog without a database (a test double)."""
    found = getattr(cat, "_episode_names", None)
    if found is None:
        db = getattr(cat, "db", None)
        if db is None:
            return None
        found = Names(getattr(cat, "vault", None), db)
        try:
            cat._episode_names = found
        except AttributeError:
            return None
    return found if isinstance(found, Names) else None


def better(cat: Any, row: dict[str, Any], title: str) -> str:
    """`title`, or the episode's real name when `title` says nothing and the house knows the name."""
    if not nameless(title):
        return title
    names = store(cat)
    known_row = getattr(cat, "known_row", None)
    if names is None or known_row is None or not row.get("series_id"):
        return title
    try:
        series = known_row("series", str(row["series_id"])) or {}
    except Exception:  # noqa: BLE001 - a catalog still loading: the plain name
        return title
    return names.name(series.get("tmdb"), row.get("season"), row.get("number")) or title


def kick(cat: Any) -> None:
    names = store(cat)
    if names is not None:
        names.kick()
