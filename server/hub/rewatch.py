"""The "Shuffle a favourite" Home row: comfort shows people put on again and again.

The hub decides which shows belong. Claude is asked once a month for a long list of rewatchable shows
(sitcoms, procedurals, cartoons, cooking and reality comfort TV); without a key or on any miss, a plain list of
classics is used. Only shows the house can play make the row, and a child's row only ever sees the kids
library. A press on one of these cards starts a shuffle of the whole show (`play: "shuffle"`).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import date
from typing import Any

from .tvnames import title as clean_title

log = logging.getLogger("hub.rewatch")

KV = "tv.rewatch.titles"
ASK_EVERY_S = 30 * 86400
RETRY_S = 86400
ROW_LIMIT = 30
MAX_TITLES = 400

# Plain fallback: shows people famously rewatch. Order does not matter; the row is shuffled daily.
CLASSICS = (
    "Seinfeld", "Friends", "The Office", "Parks and Recreation", "Brooklyn Nine-Nine", "Frasier", "Cheers",
    "Scrubs", "How I Met Your Mother", "The Simpsons", "Futurama", "Family Guy", "King of the Hill",
    "Bob's Burgers", "American Dad", "South Park", "Arrested Development", "30 Rock", "Community",
    "Modern Family", "The Big Bang Theory", "Young Sheldon", "Malcolm in the Middle", "That '70s Show",
    "Everybody Loves Raymond", "The King of Queens", "Two and a Half Men", "It's Always Sunny in Philadelphia",
    "Curb Your Enthusiasm", "New Girl", "Superstore", "Schitt's Creek", "The Golden Girls", "M*A*S*H",
    "Home Improvement", "Full House", "The Fresh Prince of Bel-Air", "Boy Meets World", "Family Matters",
    "Martin", "Living Single", "A Different World", "Married... with Children", "Roseanne", "The Nanny",
    "Will & Grace", "Gilmore Girls", "The Good Place", "Abbott Elementary", "Ted Lasso", "What We Do in the Shadows",
    "Veep", "Silicon Valley", "Reno 911!", "The Middle", "Last Man Standing", "Mom", "Ghosts",
    "The Andy Griffith Show", "I Love Lucy", "The Wonder Years", "Grace and Frankie", "Unbreakable Kimmy Schmidt",
    "Whose Line Is It Anyway?", "Impractical Jokers", "Rick and Morty", "The Cleveland Show", "Archer",
    "Law & Order", "Law & Order: Special Victims Unit", "NCIS", "CSI: Crime Scene Investigation", "Criminal Minds",
    "Bones", "Castle", "Monk", "Psych", "House", "Columbo", "Murder, She Wrote", "The X-Files",
    "Star Trek: The Next Generation", "Grey's Anatomy", "Blue Bloods", "Forensic Files", "Dateline NBC",
    "Diners, Drive-Ins and Dives", "Chopped", "Hell's Kitchen", "Kitchen Nightmares", "The Great British Bake Off",
    "Top Gear", "MythBusters", "Pawn Stars", "American Pickers", "House Hunters", "Property Brothers",
    "Fixer Upper", "Bar Rescue", "Cops", "Bob Ross: The Joy of Painting", "Survivor", "The Amazing Race",
    "Bluey", "SpongeBob SquarePants", "Avatar: The Last Airbender", "Phineas and Ferb", "Gravity Falls",
    "Paw Patrol", "Peppa Pig", "Adventure Time", "Teen Titans Go!", "The Loud House", "Hey Arnold!",
    "Rugrats", "Arthur", "Curious George", "Daniel Tiger's Neighborhood", "Scooby-Doo, Where Are You!",
    "Tom and Jerry", "Looney Tunes", "DuckTales", "Kim Possible", "Hannah Montana", "iCarly",
    "The Suite Life of Zack & Cody", "Wizards of Waverly Place", "Lizzie McGuire", "Good Luck Charlie",
)

SYSTEM = (
    "You pick TV shows for a family's 'Shuffle a favourite' row: shows people happily rewatch in any order or "
    "leave on in the background. Think classic and modern sitcoms, animated comedies, long-running procedurals, "
    "comfort cooking, home and reality shows, and kids' cartoons. Skip serialized dramas that must be watched in "
    "order. Answer with JSON only: {\"titles\": [\"Show name\", ...]} with 200 to 300 English show titles, "
    "as they are usually written."
)


def norm(name: str) -> str:
    """A show name compared loosely: no tags, case, punctuation, a leading "the", or a year."""
    text = clean_title(name)["name"].casefold().replace("&", " and ")
    words = [word for word in re.sub(r"[^a-z0-9]+", " ", text).split() if word not in ("the", "a", "an")]
    return " ".join(words)


def titles(db: Any) -> list[str]:
    """The model's list when there is one, the classics otherwise; both, so a thin answer still fills the row."""
    stored = db.kv_get(KV) or {}
    asked = [str(t) for t in stored.get("titles") or [] if isinstance(t, str)] if isinstance(stored, dict) else []
    return list(dict.fromkeys(asked + list(CLASSICS)))


def row(library: list[dict[str, Any]], names: list[str], *, day: date | None = None,
        limit: int = ROW_LIMIT) -> list[dict[str, Any]]:
    """Library shows on the list, one copy each, in an order that changes once a day."""
    wanted = {norm(name) for name in names if name}
    wanted.discard("")
    best: dict[str, dict[str, Any]] = {}
    for item in library:
        if item.get("kind") != "series" or not item.get("series_id") or not item.get("title"):
            continue
        key = norm(str(item["title"]))
        if key not in wanted:
            continue
        if key not in best or _score(item) > _score(best[key]):
            best[key] = item
    picked = sorted(best.values(), key=lambda item: norm(str(item["title"])))
    random.Random((day or date.today()).toordinal()).shuffle(picked)
    return picked[:limit]


def _score(item: dict[str, Any]) -> tuple[int, int, int]:
    """Prefer a copy the feed really shows, with a picture, then a clean name without provider tags."""
    raw = str(item.get("title") or "")
    return (0 if item.get("match_only") else 1, 1 if item.get("poster_url") else 0, 1 if clean_title(raw)["name"] == raw else 0)


async def refresh_titles(h: Any, *, ask: Any = None, now: float | None = None) -> bool:
    """Ask Claude for rewatchable shows about once a month. Any miss keeps the last list and tries again tomorrow."""
    now = time.time() if now is None else now
    stored = h.db.kv_get(KV) or {}
    if not isinstance(stored, dict):
        stored = {}
    due = float(stored.get("at") or 0) + (ASK_EVERY_S if stored.get("titles") else RETRY_S)
    if now < due:
        return False
    got = await (ask or _ask_model)(h)
    if got:
        h.db.kv_set(KV, {"at": now, "titles": got[:MAX_TITLES]})
        log.info("rewatch: %d titles from the model", len(got))
        return True
    h.db.kv_set(KV, {**stored, "at": now - (ASK_EVERY_S - RETRY_S) if stored.get("titles") else now})
    return False


async def _ask_model(h: Any) -> list[str]:
    config = getattr(h.config, "assistant", None)
    secret = getattr(config, "key_secret", "anthropic.api_key")
    key = h.vault.get(secret) if h.vault.has(secret) else None
    if not key:
        return []
    model = getattr(getattr(h.config, "iptv_match", None), "model", None) or "claude-haiku-5-5"
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=str(key), max_retries=1, timeout=90)
        response = await client.messages.create(
            model=model, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": "List the shows."}],
        )
    except Exception as exc:  # noqa: BLE001
        log.info("rewatch: %s", type(exc).__name__)
        return []
    return read_titles("".join(getattr(block, "text", "") for block in response.content))


def read_titles(text: str) -> list[str]:
    """The model's JSON answer as a clean list of names; anything else is an empty list."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        doc = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for name in doc.get("titles") or [] if isinstance(doc, dict) else []:
        if isinstance(name, str) and 0 < len(name.strip()) <= 80:
            out.append(name.strip())
    return list(dict.fromkeys(out))
