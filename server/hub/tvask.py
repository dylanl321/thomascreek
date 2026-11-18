"""Ask for something to watch in plain words, and "Pick for me": what the phone (and later the TVs) asks the hub.

Both answer from the caller's own library: a child or a kids' screen only ever gets the kids' library, so the
words can't widen what they see. Ask uses the house brain (cheap tier) to read the words and choose from candidates
the hub already narrowed; without the brain, or for a child, it falls back to plain matching on titles, genres,
years and length. Pick for me is the person's own hero and picks (tvpicks), stepping on with each press.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger("hub.tvask")

JOB = "tv_ask"
LIMIT = 12
CANDIDATES = 120

# Plain words people use for a kind of film or show, mapped to the catalog's genres.
MOODS: dict[str, tuple[str, ...]] = {
    "funny": ("Comedy",), "comedy": ("Comedy",), "laugh": ("Comedy",), "silly": ("Comedy", "Family"),
    "scary": ("Horror", "Thriller"), "horror": ("Horror",), "spooky": ("Horror", "Family"),
    "action": ("Action", "Adventure"), "exciting": ("Action", "Adventure", "Thriller"),
    "adventure": ("Adventure",), "romantic": ("Romance",), "romance": ("Romance",), "love": ("Romance",),
    "cartoon": ("Animation",), "animated": ("Animation",), "anime": ("Animation",),
    "kids": ("Family", "Animation", "Kids"), "family": ("Family",), "children": ("Family", "Animation", "Kids"),
    "documentary": ("Documentary",), "true": ("Documentary", "History"), "history": ("History",),
    "crime": ("Crime",), "mystery": ("Mystery",), "thriller": ("Thriller",), "drama": ("Drama",),
    "sad": ("Drama",), "space": ("Science Fiction",), "sci-fi": ("Science Fiction",), "scifi": ("Science Fiction",),
    "fantasy": ("Fantasy",), "magic": ("Fantasy",), "western": ("Western",), "war": ("War",),
    "music": ("Music",), "musical": ("Music",), "christmas": ("Family",), "holiday": ("Family",),
}
STOP = {"a", "an", "the", "and", "or", "for", "with", "to", "of", "in", "on", "me", "us", "my", "our", "something",
        "anything", "some", "show", "shows", "movie", "movies", "film", "films", "watch", "want", "like", "good",
        "please", "find", "put", "play", "tonight", "now", "i", "we", "is", "it", "that", "this", "under", "about",
        "minutes", "mins", "min", "hour", "hours", "series", "episode", "episodes", "new", "old"}

SYSTEM = """You help one person in a family's house find something to watch from their own library.

You get what they asked in their own words, the time of day, and numbered candidates \
(every one is playable and allowed for them). Answer with:
- picks: up to 12 candidate numbers that fit what they asked, best fit first. Only real fits; fewer is fine.
- reply: one short, warm line (under 90 characters) saying what you found, or that nothing fits and \
what to try instead. No exclamation marks, no made-up facts.

Use only numbers you were given."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {"type": "integer"}}, "reply": {"type": "string"}},
    "required": ["picks", "reply"], "additionalProperties": False,
}


def _cards(h: Any, kids: bool) -> list[dict[str, Any]]:
    from .tvexplore import _library, _view

    out, keys = [], set()
    for row in _library(h, kids):
        if row.get("match_only") or (row.get("availability") or "play") != "play" or not row.get("title"):
            continue
        card = _view(row, reason="library", badges=[])
        card.pop("reason", None)
        if row.get("duration_s"):
            card["duration_s"] = row["duration_s"]
        key = (card["kind"], str(card["title"]).casefold(), card.get("year"))
        if key in keys:
            continue
        keys.add(key)
        out.append(card)
    return out


def words_of(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'-]*", text.casefold()) if w not in STOP]


def plain_match(cards: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """The deterministic answer: title words first, then mood words as genres, a year or decade, a length limit."""
    low = text.casefold()
    words = words_of(text)
    genres = {g for w in words for g in MOODS.get(w, ())}
    kind = "series" if re.search(r"\b(show|shows|series|episodes?)\b", low) else (
        "movie" if re.search(r"\b(movie|movies|film|films)\b", low) else None)
    decade = re.search(r"\b(19|20)?([0-9])0s\b", low)
    years = None
    if decade:
        century = decade.group(1) or ("19" if decade.group(2) >= "3" else "20")
        start = int(f"{century}{decade.group(2)}0")
        years = (start, start + 9)
    limit = re.search(r"under (\d+) ?(min|minutes|hour|hours|h)\b", low)
    max_s = None
    if limit:
        n = int(limit.group(1))
        max_s = n * 3600 if limit.group(2).startswith("h") else n * 60
    title_words = [w for w in words if not re.fullmatch(r"\d+s?", w)]
    plain_words = [w for w in title_words if w not in MOODS]

    scored = []
    for card in cards:
        if kind and card["kind"] != kind:
            continue
        if years and not (card.get("year") and years[0] <= int(card["year"]) <= years[1]):
            continue
        if max_s and card.get("duration_s") and int(card["duration_s"]) > max_s:
            continue
        title = str(card.get("title") or "").casefold()
        hits = sum(1 for w in title_words if w in title)
        mood = len(genres & set(card.get("genres") or []))
        if not hits and not mood and (plain_words or genres):
            continue
        if not title_words and not years and not kind:
            continue
        scored.append((-(hits * 10 + mood * 3), not card.get("backdrop_url"), -(card.get("year") or 0),
                       str(card.get("title")).casefold(), card))
    scored.sort(key=lambda row: row[:4])
    return [row[4] for row in scored[:LIMIT]]


def _plain_reply(text: str, found: list[dict[str, Any]]) -> str:
    if not found:
        return "Nothing in the library fits that. Try a title, a mood like funny or scary, or a decade."
    return f"{len(found)} {'title fits' if len(found) == 1 else 'titles fit'} “{text.strip()[:60]}”."


def _candidates(cards: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """What the model may choose from: the plain matches first, then a spread of the library for loose asks."""
    first = plain_match(cards, text)
    keys = {id(c) for c in first}
    rest = sorted((c for c in cards if id(c) not in keys),
                  key=lambda c: (not c.get("backdrop_url"), -(c.get("year") or 0)))
    by_genre: dict[str, int] = {}
    spread = []
    for card in rest:
        g = (card.get("genres") or ["?"])[0]
        if by_genre.get(g, 0) >= 8:
            continue
        by_genre[g] = by_genre.get(g, 0) + 1
        spread.append(card)
    return (first + spread)[:CANDIDATES]


async def ask(h: Any, text: str, *, kids: bool, who: str | None) -> dict[str, Any]:
    """{reply, items, by: ai|rules}. Never raises for a model problem; the plain answer stands in."""
    text = " ".join(text.split())[:300]
    cards = _cards(h, kids)
    brain = getattr(h, "brain", None)
    ready = False
    if brain is not None and not kids:
        try:
            ready = bool(brain.on and brain.has_key() and not brain.capped())
        except Exception:  # noqa: BLE001
            ready = False
    if ready and cards and brain is not None:
        pool = _candidates(cards, text)
        payload = {"asked": text, "time": time.strftime("%A %H:%M", time.localtime(h.policy.now().timestamp())),
                   "candidates": [_brief(c, i) for i, c in enumerate(pool)]}
        try:
            thought = await brain.router.think(system=SYSTEM, payload=payload, schema=SCHEMA, tier="cheap",
                                               escalate=False, max_tokens=500)
            data = thought.data if isinstance(thought.data, dict) else {}
            picks = []
            for n in data.get("picks") or []:
                if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(pool) and pool[n] not in picks:
                    picks.append(pool[n])
            reply = str(data.get("reply") or "").strip()[:140]
            brain.log_run(job=JOB, trigger=f"ask:{who or 'house'}", input_summary=text[:200],
                          input_hash=None, model=thought.model or "template", tier=thought.tier,
                          tokens_in=thought.tokens_in, tokens_out=thought.tokens_out, tokens_cached=thought.cached,
                          cost_usd=thought.cost_usd, latency_ms=thought.latency_ms,
                          decision={"picks": len(picks), **({"fallback": thought.error} if thought.error else {})},
                          action="ask", outcome="answered" if reply else "fallback")
            if reply and (picks or not plain_match(cards, text)):
                return {"reply": reply, "items": picks[:LIMIT], "by": "ai"}
        except Exception as exc:  # noqa: BLE001  the plain answer stands in
            log.warning("tv ask model run failed: %s", type(exc).__name__)
    found = plain_match(cards, text)
    return {"reply": _plain_reply(text, found), "items": found, "by": "rules"}


def _brief(card: dict[str, Any], n: int) -> dict[str, Any]:
    out: dict[str, Any] = {"n": n, "title": card.get("title"), "kind": card.get("kind")}
    for key in ("year", "rating"):
        if card.get(key):
            out[key] = card[key]
    if card.get("genres"):
        out["genres"] = list(card["genres"])[:4]
    if card.get("duration_s"):
        out["minutes"] = int(card["duration_s"]) // 60
    if card.get("overview"):
        out["about"] = str(card["overview"])[:140]
    return out


def pick(h: Any, *, kids: bool, who: str | None, skip: list[str]) -> dict[str, Any]:
    """One title for right now, with its reason. `skip` holds keys already shown, so "Another" steps on."""
    from .tvpicks import key_of, personal_feed

    feed = personal_feed(h, "kids" if kids else "adult", who)
    order: list[dict[str, Any]] = []
    for card in feed.get("heroes") or ([feed["hero"]] if feed.get("hero") else []):
        order.append(card)
    rows = {str(r.get("id")): r for r in feed.get("rows") or []}
    for row_id in ("picks", "because", "moment", "season", "anniversary", "rewatch", "new", "popular", "acclaimed"):
        row = rows.get(row_id) or {}
        for card in row.get("items") or []:
            order.append({**card, "reason": card.get("reason") or row.get("reason") or row.get("title")})
    seen: set[str] = set()
    skipped = set(skip)
    choices = []
    for card in order:
        if (card.get("availability") or "play") != "play":
            continue
        k = key_of(card)
        if k in seen:
            continue
        seen.add(k)
        choices.append((k, card))
    fresh = [c for c in choices if c[0] not in skipped]
    if not fresh and choices:
        fresh = choices  # went round once: start again
    if not fresh:
        return {"item": None, "reason": "Nothing to pick yet. The library is still loading.", "more": False}
    key, card = fresh[0]
    return {"item": {**card, "key": key}, "key": key, "reason": card.get("reason") or "Picked for you",
            "more": len(fresh) > 1, "curated_by": feed.get("curated_by") or "rules"}

