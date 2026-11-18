"""Personal rows and a personal hero for Explore, built on the hub for the person who is watching.

The shared feed (tvexplore) is the same for every grown-up: Top 10, new, popular, acclaimed. This adds the rows
that belong to one person or one TV, from what they actually watch (the `tv.watch` buckets upnext keeps):

  because   "Because you watched X": titles that share X's genres
  picks     "Picked for you": the titles that best fit everything they watch
  moment    the time of day: Saturday morning cartoons, movie night, late night
  season    the time of year: holiday films, Halloween, summer
  anniversary  films turning 10, 20, 25... this year

Every row carries a one-line `reason`. Plain Python builds every row and every reason, so the feed is whole with
no AI key. With the house brain on, Claude (brain.explore_tier, Haiku by default) words the reasons, chooses
"Picked for you" and the hero from candidates the hub already allowed, and says which seasonal titles really
fit. It only ever chooses among titles plain Python offered, so it cannot add a title a profile may not see.
Nothing from a child's profile is sent to the model: kids get the plain rows.

A request never waits for the model. It gets the last answer for the same situation, or the plain rows, and a
fresh answer is asked for in the background (at most once per brain.explore_cooldown_min per person or TV).
Every call is costed in `ai_runs` (job "explore") and stops when the brain is off, paused, capped or keyless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime
from typing import Any

log = logging.getLogger("hub.explore.picks")

JOB = "explore"
AI_KV = "tv.explore.ai"
POOL_LIMIT = 2500
GENRE_DEPTH = 40
ROW_MIN = 3
ROW_LIMIT = 12
SEEN_LIMIT = 30
AI_MAX_AGE_S = 12 * 3600
ANNIVERSARIES = (50, 40, 30, 25, 20, 10)

# Rows that are the same for everyone, with the line each one carries when the model hasn't worded it.
BASE_REASONS = {
    "top10": "What the most people are watching today",
    "new": "Just out, and ready in the house",
    "popular": "Popular this week",
    "acclaimed": "Critics' favourites you can play now",
    "library": "Newest in the house's library",
    "rewatch": "Comfort shows to put on shuffle",
}
# Where each row sits. Personal rows sit high, the way a streaming home puts "Because you watched" near the top.
ORDER = {"top10": 0, "new": 1, "because": 2, "picks": 3, "popular": 4, "moment": 5, "season": 6, "acclaimed": 7,
         "anniversary": 8, "library": 9, "rewatch": 10}

_SPLIT = re.compile(r"\s*(?:,|/|&|\||;|\band\b)\s*", re.I)
_GENRE_ALIASES = {
    "sci-fi": "science fiction", "sci fi": "science fiction", "scifi": "science fiction", "sf": "science fiction",
    "animated": "animation", "cartoon": "animation", "cartoons": "animation", "children": "kids", "kid": "kids",
    "family movie": "family", "doc": "documentary", "documentaries": "documentary", "romcom": "romance",
    "suspense": "thriller", "musical": "music", "war politics": "war", "politics": "war", "tv movie": "",
    "news": "", "talk": "", "soap": "drama", "reality": "reality", "action adventure": "action",
}
# TMDB's own genre ids (movie and TV lists share most of them), so trending cards have genres too.
TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary", 18: "Drama",
    10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery",
    10749: "Romance", 878: "Science Fiction", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action", 10762: "Kids", 10764: "Reality", 10765: "Science Fiction", 10768: "War",
}

# The time of day: (title, genres it leans on, films only).
MOMENTS: dict[str, dict[str, tuple[str, tuple[str, ...], bool]]] = {
    "adult": {
        "weekend_morning": ("Saturday morning", ("animation", "family", "comedy", "adventure"), False),
        "morning": ("Something light", ("comedy", "family", "documentary", "animation"), False),
        "afternoon": ("Afternoon adventure", ("adventure", "action", "fantasy", "family"), True),
        "evening": ("Movie night", ("drama", "thriller", "action", "comedy", "science fiction"), True),
        "night": ("Late night", ("thriller", "horror", "mystery", "crime"), True),
    },
    "kids": {
        "weekend_morning": ("Saturday morning cartoons", ("animation", "kids", "family"), False),
        "morning": ("Breakfast shows", ("animation", "kids", "family"), False),
        "afternoon": ("After school", ("animation", "kids", "adventure", "comedy"), False),
        "evening": ("Family movie night", ("family", "animation", "adventure", "comedy"), True),
        "night": ("Before bed", ("family", "animation", "kids"), False),
    },
}

# The time of year: id -> (title, words in the title or story, genres that count when nothing names it).
SEASONS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "holiday": ("Holiday movies", ("christmas", "xmas", "santa", "holiday", "holidays", "grinch", "elf", "noel",
                                   "sleigh", "snowman", "reindeer", "nutcracker", "scrooge", "christmas carol",
                                   "home alone", "polar express", "mistletoe", "north pole", "thanksgiving"), ()),
    "halloween": ("Halloween night", ("halloween", "haunted", "haunting", "ghost", "ghosts", "witch", "witches",
                                      "vampire", "zombie", "monster", "spooky", "pumpkin", "hocus pocus",
                                      "goosebumps", "addams"), ("horror",)),
    "valentine": ("Date night", ("love", "valentine", "wedding", "romance"), ("romance",)),
    "summer": ("Summer movies", ("summer", "beach", "camp", "vacation", "surf", "shark", "island", "pool"), ()),
    "winter": ("Snowed in", ("snow", "winter", "ice", "frozen", "blizzard", "avalanche", "arctic"), ()),
}


def season_of(day: date) -> str | None:
    if day.month == 10:
        return "halloween"
    if day.month in (11, 12):
        return "holiday"
    if day.month == 2 and day.day <= 14:
        return "valentine"
    if day.month == 1:
        return "winter"
    if day.month in (6, 7, 8):
        return "summer"
    return None


def moment_of(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "weekend_morning" if now.weekday() >= 5 else "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"


def genres_of(values: Any) -> list[str]:
    """Provider, Plex and TMDB genres as one plain lower-case list: "Action & Adventure, Sci-Fi" -> action,
    adventure, science fiction."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values or []:
        if isinstance(value, int) and not isinstance(value, bool):
            value = TMDB_GENRES.get(value, "")
        for part in _SPLIT.split(str(value or "")):
            word = part.strip().casefold().replace("-", " ") if part else ""
            word = _GENRE_ALIASES.get(word, _GENRE_ALIASES.get(word.replace(" ", "-"), word))
            if word and len(word) <= 30 and word not in out:
                out.append(word)
    return out


def key_of(card: dict[str, Any]) -> str:
    return str(card.get("tmdb_id") or card.get("movie_id") or card.get("series_id") or card.get("recording_id")
               or card.get("title"))


def _name(title: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(title or "").casefold()).split())


# -- the pool -----------------------------------------------------------------------------------------------------
def build_pool(library: list[dict[str, Any]], *, today: date) -> list[dict[str, Any]]:
    """The titles personal rows may draw from: playable, in the house's language, with a picture. Built when the
    feed is published (every few hours) from the profile's already-filtered library, so a kids' pool only holds
    what the kids' feed may show. Kept small: the best few per genre, per anniversary year and per season."""
    from .tvexplore import _foreign, _has_art, _view

    usable = [row for row in library
              if not row.get("match_only") and (row.get("availability") or "play") == "play"
              and row.get("title") and not _foreign(row) and _has_art(row)]
    usable.sort(key=lambda row: (not row.get("backdrop_url"), -(row.get("year") or 0), str(row.get("title")).casefold()))
    chosen: dict[str, dict[str, Any]] = {}

    def take(rows: list[dict[str, Any]], depth: int) -> None:
        for row in rows[:depth]:
            card = _view(row, reason="library", badges=[])
            card["genres"] = list(row.get("genres") or [])
            chosen.setdefault(key_of(card), card)

    by_genre: dict[str, list[dict[str, Any]]] = {}
    for row in usable:
        for genre in genres_of(row.get("genres")):
            by_genre.setdefault(genre, []).append(row)
    for rows in by_genre.values():
        take(rows, GENRE_DEPTH)
    for years in ANNIVERSARIES:
        take([row for row in usable if row.get("year") == today.year - years and row.get("kind") == "movie"], 24)
    for season in SEASONS:
        take(sorted((row for row in usable if _season_score(row, season) > 0),
                    key=lambda row: -_season_score(row, season)), 30)
    return list(chosen.values())[:POOL_LIMIT]


def _season_score(card: dict[str, Any], season: str) -> int:
    _title, words, genres = SEASONS[season]
    title = f" {_name(card.get('title'))} "
    story = f" {_name(card.get('overview'))} "
    score = 0
    for word in words:
        if f" {word} " in title:
            score += 3
        elif f" {word} " in story:
            score += 1
    if score == 0 and genres and set(genres) & set(genres_of(card.get("genres"))):
        score = 1
    return score


# -- what this person watches ---------------------------------------------------------------------------------------
def seen(h: Any, history: dict[str, Any], pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The titles in a history bucket, newest first, with their genres: {key, title, genres, n, last}."""
    entries: list[tuple[str, str, dict[str, Any]]] = []
    for movie_id, row in (history.get("movies") or {}).items():
        if isinstance(row, dict):
            entries.append(("movie", str(movie_id), row))
    for series_id, row in (history.get("series") or {}).items():
        if isinstance(row, dict):
            entries.append(("series", str(series_id), row))
    entries.sort(key=lambda e: float(e[2].get("last") or 0), reverse=True)
    entries = entries[:SEEN_LIMIT]
    if not entries:
        return []
    by_id = {str(card.get("movie_id") or card.get("series_id")): card for card in pool
             if card.get("movie_id") or card.get("series_id")}
    catalog_rows: dict[str, dict[str, Any]] | None = None
    out = []
    for kind, ident, row in entries:
        card = by_id.get(ident)
        title, genres = (card.get("title"), card.get("genres")) if card else (None, None)
        if not title:
            if catalog_rows is None:
                catalog_rows = _catalog_rows(h)
            found = catalog_rows.get(ident) or {}
            title = found.get("name") or (row.get("title") if kind == "movie" else None)
            genres = genres or found.get("genre")
        if not title:
            continue
        out.append({"key": ident, "kind": kind, "title": str(title), "genres": genres_of(genres),
                    "n": int(row.get("n") or 1), "last": float(row.get("last") or 0)})
    return out


def _catalog_rows(h: Any) -> dict[str, dict[str, Any]]:
    catalog = getattr(h, "tv_catalog", None)
    if catalog is None:
        return {}
    try:
        doc = catalog.load()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for bucket in ("movies", "series"):
        for row in doc.get(bucket) or []:
            if isinstance(row, dict) and row.get("id"):
                out[str(row["id"])] = row
    return out


def taste(watched: list[dict[str, Any]], now: float) -> dict[str, float]:
    """Genre weights: plays count, recent plays count more."""
    weights: dict[str, float] = {}
    for item in watched:
        age_days = max(0.0, (now - item["last"]) / 86400) if item["last"] else 60.0
        weight = min(item["n"], 10) * (1.0 if age_days < 14 else 0.6 if age_days < 60 else 0.3)
        for genre in item["genres"]:
            if genre in ("kids", "family") and len(item["genres"]) > 1:
                weight_here = weight * 0.5  # nearly everything a child watches says "kids"; it tells little
            else:
                weight_here = weight
            weights[genre] = weights.get(genre, 0.0) + weight_here
    return weights


def _affinity(card: dict[str, Any], weights: dict[str, float]) -> float:
    return sum(weights.get(genre, 0.0) for genre in genres_of(card.get("genres")))


# -- the rows -------------------------------------------------------------------------------------------------------
def plain_rows(pool: list[dict[str, Any]], watched: list[dict[str, Any]], *, kids: bool, now: datetime,
               taken: set[str]) -> dict[str, dict[str, Any]]:
    """Every personal row, built without the model. `taken` holds titles already in the shared rows; personal
    rows don't repeat them, or each other."""
    used = set(taken)
    watched_names = {_name(item["title"]) for item in watched}
    watched_keys = {item["key"] for item in watched}

    def fresh(card: dict[str, Any]) -> bool:
        return (key_of(card) not in used and _name(card.get("title")) not in watched_names
                and str(card.get("movie_id") or card.get("series_id") or "") not in watched_keys)

    def row(row_id: str, title: str, cards: list[dict[str, Any]], reason: str, **extra: Any) -> dict[str, Any] | None:
        out = []
        for card in cards:
            if not fresh(card):
                continue
            out.append(dict(card))
            if len(out) >= ROW_LIMIT:
                break
        if len(out) < ROW_MIN:
            return None
        for card in out:
            used.add(key_of(card))
            card["reason"] = reason
        return {"id": row_id, "title": title, "reason": reason, "items": out, **extra}

    rows: dict[str, dict[str, Any]] = {}
    weights = taste(watched, now.timestamp())
    # The season goes first: in December the Christmas films belong in the holiday row, not "Because you watched".
    season = season_of(now.date())
    if season:
        ranked = sorted((card for card in pool if _season_score(card, season) > 0),
                        key=lambda card: (-_season_score(card, season), not card.get("backdrop_url"),
                                          -(card.get("year") or 0)))
        built = row("season", SEASONS[season][0], ranked, _season_reason(season), season=season)
        if built:
            rows["season"] = built
    # Because you watched: the newest title with genres that has enough company in the pool.
    for item in watched[:5]:
        mine = set(item["genres"]) - {"kids", "family"} or set(item["genres"])
        if not mine:
            continue
        ranked = sorted((card for card in pool if mine & set(genres_of(card.get("genres")))),
                        key=lambda card: (-len(mine & set(genres_of(card.get("genres")))),
                                          not card.get("backdrop_url"), -(card.get("year") or 0)))
        built = row("because", f"Because you watched {item['title']}", ranked,
                    f"More {_listing(sorted(mine)[:2])}, like {item['title']}", source_title=item["title"])
        if built:
            rows["because"] = built
            break
    if weights:
        ranked = sorted((card for card in pool if _affinity(card, weights) > 0),
                        key=lambda card: (-_affinity(card, weights), not card.get("backdrop_url"),
                                          -(card.get("year") or 0)))
        top = [g for g, _w in sorted(weights.items(), key=lambda kv: -kv[1]) if g not in ("kids", "family")][:2]
        built = row("picks", "Picked for you", ranked,
                    f"Lots of {_listing(top)} in what you watch" if top else "From what you watch")
        if built:
            rows["picks"] = built
    moment = moment_of(now)
    title, leans, films = MOMENTS["kids" if kids else "adult"][moment]
    ranked = sorted((card for card in pool if set(leans) & set(genres_of(card.get("genres")))
                     and (not films or card.get("kind") == "movie")),
                    key=lambda card: (-(_affinity(card, weights) + 2 * len(set(leans) & set(genres_of(card.get("genres"))))),
                                      not card.get("backdrop_url"), -(card.get("year") or 0)))
    built = row("moment", title, ranked, _moment_reason(moment, kids), part=moment)
    if built:
        rows["moment"] = built
    if not kids:
        built = _anniversary(pool, now, weights, row)
        if built:
            rows["anniversary"] = built
    return rows


def _anniversary(pool, now: datetime, weights, row) -> dict[str, Any] | None:
    week = now.isocalendar()[1]
    order = ANNIVERSARIES[week % len(ANNIVERSARIES):] + ANNIVERSARIES[:week % len(ANNIVERSARIES)]
    for years in order:
        year = now.year - years
        cards = sorted((card for card in pool if card.get("year") == year and card.get("kind") == "movie"),
                       key=lambda card: (-_affinity(card, weights), not card.get("backdrop_url"),
                                         str(card.get("title")).casefold()))
        built = row("anniversary", f"Turning {years} this year", cards, f"Released in {year}", years=years)
        if built:
            return built
    return None


def _moment_reason(moment: str, kids: bool) -> str:
    return {
        "weekend_morning": "Easy watching for a slow weekend morning",
        "morning": "Short and light to start the day",
        "afternoon": "Something with a bit of adventure for the afternoon",
        "evening": "Films for tonight" if not kids else "Films the whole family can watch tonight",
        "night": "Something gripping for late night" if not kids else "Gentle shows to wind down",
    }[moment]


def _season_reason(season: str) -> str:
    return {"holiday": "It's the holiday season", "halloween": "Halloween is coming",
            "valentine": "For Valentine's week", "summer": "Summer favourites",
            "winter": "Cold outside, cosy inside"}[season]


def _listing(words: list[str]) -> str:
    words = [w for w in words if w]
    if not words:
        return "the same"
    return words[0] if len(words) == 1 else f"{words[0]} and {words[1]}"


def plain_hero(base: dict[str, Any], watched: list[dict[str, Any]], now: float) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The hero, new and trending first, leaning toward what this person likes; and the candidates it came from."""
    candidates = hero_candidates(base, watched)
    if not candidates:
        return base.get("hero"), []
    weights = taste(watched, now)
    if not weights:
        return candidates[0], candidates
    best = max(enumerate(candidates), key=lambda pair: (_affinity(pair[1], weights), -pair[0]))[1]
    return best, candidates


def _hero_line(hero: dict[str, Any], watched: list[dict[str, Any]], now: float) -> str | None:
    weights = taste(watched, now)
    liked = [g for g in genres_of(hero.get("genres")) if weights.get(g) and g not in ("kids", "family")]
    if not liked:
        return None
    lead = "New this week" if "New" in (hero.get("badges") or []) else "Trending now"
    return f"{lead}, and you like {liked[0]}"


def hero_candidates(base: dict[str, Any], watched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    watched_names = {_name(item["title"]) for item in watched}
    rows = {row.get("id"): row.get("items") or [] for row in base.get("rows") or []}
    out, keys = [], set()
    ordered = [base.get("hero")] if isinstance(base.get("hero"), dict) else []
    for row_id in ("new", "top10", "popular"):
        ordered.extend(rows.get(row_id) or [])
    for card in ordered:
        if not card.get("backdrop_url") or _name(card.get("title")) in watched_names:
            continue
        k = key_of(card)
        if k in keys:
            continue
        keys.add(k)
        out.append(card)
        if len(out) == 10:
            break
    return out


# -- the model ------------------------------------------------------------------------------------------------------
SYSTEM = """You curate the TV home screen of one person in a family's house, like a streaming service would.

You get what they've watched lately (titles and genres), the time of day and season, the rows their screen has, and candidates the house already checked (every one is playable and allowed for them). Answer with:
- reasons: for each row id you were given, one short line (under 60 characters) saying why that row is there for this person now. Warm and plain, no exclamation marks, no names of people, no made-up facts.
- picks: up to 12 candidate numbers from pick_from for "Picked for you", best fit first. Prefer variety and things that are new or well loved; don't repeat what they just watched.
- hero: one number from hero_from to feature at the top. New and trending titles come first; choose the one this person is most likely to want.
- hero_reason: one short line (under 60 characters) on why it's featured for them.
- season_keep: the numbers from season_from that really belong to this time of year (a film that only mentions a word doesn't).
- unsure: true only if you can't tell.

Use only numbers you were given."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasons": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string"}, "line": {"type": "string"}},
            "required": ["id", "line"], "additionalProperties": False}},
        "picks": {"type": "array", "items": {"type": "integer"}},
        "hero": {"type": "integer"},
        "hero_reason": {"type": "string"},
        "season_keep": {"type": "array", "items": {"type": "integer"}},
        "unsure": {"type": "boolean"},
    },
    "required": ["reasons", "picks", "hero", "hero_reason", "season_keep", "unsure"], "additionalProperties": False,
}


def _brief(card: dict[str, Any], n: int) -> dict[str, Any]:
    out: dict[str, Any] = {"n": n, "title": card.get("title"), "kind": card.get("kind")}
    if card.get("year"):
        out["year"] = card["year"]
    genres = genres_of(card.get("genres"))
    if genres:
        out["genres"] = genres[:4]
    if card.get("badges"):
        out["badges"] = card["badges"]
    return out


def situation(*, watched: list[dict[str, Any]], rows: list[dict[str, Any]], pick_pool: list[dict[str, Any]],
              heroes: list[dict[str, Any]], season_pool: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    return {
        "time": moment_of(now), "day": "weekend" if now.weekday() >= 5 else "weekday",
        "season": season_of(now.date()) or "none",
        "watched": [{"title": w["title"], "genres": w["genres"][:4], "plays": w["n"]} for w in watched[:10]],
        "rows": [{"id": r["id"], "title": r["title"], "sample": [c.get("title") for c in (r.get("items") or [])[:4]]}
                 for r in rows],
        "pick_from": [_brief(card, i) for i, card in enumerate(pick_pool)],
        "hero_from": [_brief(card, i) for i, card in enumerate(heroes)],
        "season_from": [_brief(card, i) for i, card in enumerate(season_pool)],
    }


def situation_key(sit: dict[str, Any], day: date) -> str:
    """A new answer is worth asking for when what they watch, the time of day or the candidates change."""
    keyed = {
        "day": day.isoformat(), "time": sit["time"], "season": sit["season"],
        "watched": [w["title"] for w in sit["watched"]],
        "rows": [r["id"] for r in sit["rows"]],
        "pick": [c["title"] for c in sit["pick_from"]], "hero": [c["title"] for c in sit["hero_from"]],
    }
    return hashlib.sha256(json.dumps(keyed, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Curator:
    """Asks the model for one person's (or TV's) rows and keeps the answer. One per hub."""

    def __init__(self, hub: Any):
        self.hub = hub
        self._inflight: set[str] = set()
        self._last_call: dict[str, float] = {}

    @property
    def cfg(self) -> Any:
        return self.hub.config.brain

    def ready(self) -> bool:
        brain = getattr(self.hub, "brain", None)
        if brain is None or not getattr(self.cfg, "explore_enabled", True):
            return False
        try:
            return bool(brain.on and brain.has_key() and not brain.capped())
        except Exception:  # noqa: BLE001
            return False

    def answer(self, who: str, key: str) -> dict[str, Any] | None:
        stored = (self.hub.db.kv_get(AI_KV) or {}).get(who)
        if not isinstance(stored, dict) or stored.get("key") != key:
            return None
        if time.time() - float(stored.get("at") or 0) > AI_MAX_AGE_S:
            return None
        return stored

    def maybe_ask(self, who: str, sit: dict[str, Any], key: str) -> None:
        if not self.ready():
            return
        now = time.time()
        cool = float(getattr(self.cfg, "explore_cooldown_min", 60)) * 60
        if who in self._inflight or now - self._last_call.get(who, 0) < cool:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._inflight.add(who)
        self._last_call[who] = now
        loop.create_task(self.ask(who, sit, key))

    async def ask(self, who: str, sit: dict[str, Any], key: str) -> dict[str, Any] | None:
        brain = self.hub.brain
        try:
            tier = "smart" if getattr(self.cfg, "explore_tier", "cheap") == "smart" else "cheap"
            thought = await brain.router.think(system=SYSTEM, payload=sit, schema=SCHEMA, tier=tier,
                                               escalate=False, max_tokens=900)
            words = clean_answer(thought.data, sit)
            brain.log_run(job=JOB, trigger=f"explore:{who}", input_summary=json.dumps(sit, default=str)[:600],
                          input_hash=key, model=thought.model or "template", tier=thought.tier,
                          tokens_in=thought.tokens_in, tokens_out=thought.tokens_out, tokens_cached=thought.cached,
                          cost_usd=thought.cost_usd, latency_ms=thought.latency_ms,
                          decision={"answer": words, **({"fallback": thought.error} if thought.error else {})},
                          action="explore", outcome="curated" if words else "fallback")
            if not words:
                self._last_call[who] = time.time() - max(0.0, float(getattr(self.cfg, "explore_cooldown_min", 60)) * 60 - 120)
                return None
            stored = {"key": key, "at": time.time(), **words}
            raw = self.hub.db.kv_get(AI_KV) or {}
            raw = raw if isinstance(raw, dict) else {}
            raw[who] = stored
            if len(raw) > 60:
                for old in sorted(raw, key=lambda k: float((raw[k] or {}).get("at") or 0))[:len(raw) - 60]:
                    raw.pop(old, None)
            self.hub.db.kv_set(AI_KV, raw)
            return stored
        except Exception as exc:  # noqa: BLE001
            log.warning("explore model run failed: %s", type(exc).__name__)
            return None
        finally:
            self._inflight.discard(who)


def clean_answer(data: Any, sit: dict[str, Any]) -> dict[str, Any] | None:
    """Only what the plain rows allowed: known row ids, numbers in range, short lines."""
    if not isinstance(data, dict):
        return None
    row_ids = {r["id"] for r in sit["rows"]}
    reasons = {}
    for item in data.get("reasons") or []:
        if isinstance(item, dict) and item.get("id") in row_ids and isinstance(item.get("line"), str):
            line = " ".join(item["line"].split())[:80]
            if line:
                reasons[item["id"]] = line

    def numbers(values: Any, size: int) -> list[int]:
        out = []
        for value in values or []:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < size and value not in out:
                out.append(value)
        return out

    picks = numbers(data.get("picks"), len(sit["pick_from"]))[:ROW_LIMIT]
    hero = data.get("hero")
    hero = hero if isinstance(hero, int) and not isinstance(hero, bool) and 0 <= hero < len(sit["hero_from"]) else None
    hero_reason = " ".join(str(data.get("hero_reason") or "").split())[:80] or None
    keep = numbers(data.get("season_keep"), len(sit["season_from"]))
    if not reasons and not picks and hero is None:
        return None
    return {"reasons": reasons, "picks": picks, "hero": hero, "hero_reason": hero_reason, "season_keep": keep}


# -- the feed -------------------------------------------------------------------------------------------------------
def personal_feed(h: Any, profile: str, who: str | None, *, fallback_history: bool = True) -> dict[str, Any]:
    """The shared feed for this profile with this person's (or TV's) rows and hero added. Never raises."""
    from .tvexplore import feed

    base = feed(h, profile)
    try:
        return _personal(h, base, profile, who, fallback_history=fallback_history)
    except Exception as exc:  # noqa: BLE001 - a personal row must never cost the whole page
        log.warning("personal rows failed: %s", type(exc).__name__)
        return base


def _personal(h: Any, base: dict[str, Any], profile: str, who: str | None, *, fallback_history: bool) -> dict[str, Any]:
    from .tvexplore import pool
    from .upnext import _history

    kids = profile == "kids"
    now = h.policy.now()
    cards = pool(h, profile)
    history = (_history(h, who) if who else {}) or (_history(h, profile) if fallback_history else {})
    watched = seen(h, history, cards)
    taken = {key_of(card) for row in base.get("rows") or [] for card in row.get("items") or []}
    extra = plain_rows(cards, watched, kids=kids, now=now, taken=taken)
    hero, heroes = plain_hero(base, watched, now.timestamp())
    hero_reason = _hero_line(hero, watched, now.timestamp()) if watched and hero else None
    source = "rules"

    curator: Curator | None = getattr(h, "explore_curator", None)
    if curator is not None and not kids and who:
        pick_pool = _pick_pool(cards, watched, taken, extra)
        season_pool = (extra.get("season") or {}).get("items") or []
        rows_for_model = [r for r in base.get("rows") or [] if r.get("id") in BASE_REASONS] + list(extra.values())
        sit = situation(watched=watched, rows=rows_for_model, pick_pool=pick_pool, heroes=heroes,
                        season_pool=season_pool, now=now)
        key = situation_key(sit, now.date())
        got = curator.answer(who, key)
        if got is None:
            curator.maybe_ask(who, sit, key)
        else:
            source = "ai"
            hero, ai_line = _apply(got, extra, base, pick_pool, heroes, season_pool, hero)
            hero_reason = ai_line or (_hero_line(hero, watched, now.timestamp()) if watched and hero else None)
    rows = []
    for row in base.get("rows") or []:
        shown = dict(row)
        shown.setdefault("reason", BASE_REASONS.get(str(row.get("id")), ""))
        rows.append(shown)
    if source == "ai":
        reasons = got.get("reasons") or {}  # type: ignore[union-attr]
        for row in rows:
            if row.get("id") in reasons:
                row["reason"] = reasons[row["id"]]
    rows.extend(extra.values())
    rows.sort(key=lambda row: ORDER.get(str(row.get("id")), 50))
    if hero is not None:
        hero = dict(hero)
        if hero_reason:
            hero["reason"] = hero_reason
    out = {"hero": hero, "rows": rows, "curated_by": source}
    steps = hero_steps(hero, heroes)
    if len(steps) > 1:
        out["heroes"] = steps  # the hand-scrollable hero; one hero needs no list (older apps read "hero" only)
    return out


HERO_STEPS = 6


def hero_steps(hero: dict[str, Any] | None, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hero first, then the next new and trending titles it was chosen from: what a screen steps through when
    the hero moves on by itself or by hand. Each says why it is there."""
    if hero is None:
        return []
    out, keys = [hero], {key_of(hero)}
    for card in candidates:
        if len(out) >= HERO_STEPS:
            break
        k = key_of(card)
        if k in keys:
            continue
        keys.add(k)
        step = dict(card)
        step.setdefault("reason", "New this week" if "New" in (card.get("badges") or []) else "Trending now")
        out.append(step)
    return out


def _pick_pool(cards, watched, taken, extra) -> list[dict[str, Any]]:
    """Up to 40 candidates for "Picked for you": the plain picks first, then other good fits."""
    weights = taste(watched, time.time())
    out = list((extra.get("picks") or {}).get("items") or [])
    keys = {key_of(card) for card in out} | set(taken)
    for row_id in ("because", "moment", "season", "anniversary"):
        keys |= {key_of(card) for card in (extra.get(row_id) or {}).get("items") or []}
    watched_names = {_name(w["title"]) for w in watched}
    for card in sorted(cards, key=lambda card: (-_affinity(card, weights), not card.get("backdrop_url"),
                                                -(card.get("year") or 0))):
        if len(out) >= 40:
            break
        if key_of(card) in keys or _name(card.get("title")) in watched_names:
            continue
        keys.add(key_of(card))
        out.append(card)
    return out


def _apply(got: dict[str, Any], extra: dict[str, dict[str, Any]], base: dict[str, Any], pick_pool, heroes,
           season_pool, hero) -> tuple[dict[str, Any] | None, str | None]:
    reasons = got.get("reasons") or {}
    for row_id, row in extra.items():
        if row_id in reasons:
            row["reason"] = reasons[row_id]
            for card in row["items"]:
                card["reason"] = reasons[row_id]
    picks = [pick_pool[i] for i in got.get("picks") or [] if i < len(pick_pool)]
    if len(picks) >= ROW_MIN:
        line = reasons.get("picks") or (extra.get("picks") or {}).get("reason") or "From what you watch"
        extra["picks"] = {"id": "picks", "title": "Picked for you", "reason": line,
                          "items": [{**card, "reason": line} for card in picks]}
    if "season" in extra and season_pool:
        keep = [season_pool[i] for i in got.get("season_keep") or [] if i < len(season_pool)]
        if len(keep) >= ROW_MIN:
            extra["season"]["items"] = keep
        elif got.get("season_keep") is not None and not keep:
            extra.pop("season")
    if got.get("hero") is not None and got["hero"] < len(heroes):
        hero = heroes[got["hero"]]
    return hero, got.get("hero_reason")
