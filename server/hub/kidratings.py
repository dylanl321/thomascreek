"""How grown-up a title each child may watch: one level per child, set by a grown-up in the apps.

Dylan, Nov 19 2026: "Just make change rating per kid or in the app directly." The house-wide
`child_policy.ratings` let TV-PG sitcoms (Seinfeld, Friends) reach a nine-year-old. Each child now has a level.
A grown-up changes it from the phone, the iPhone or the admin page (`PUT /v1/people/{id}/ratings`). Every
screen's rows, search and play narrow to it on the hub (`narrow`, `allows`).

The levels build on each other, so a level adds ratings and never takes one away:

    little  G, TV-Y, TV-G                       (default for a "little" child: the five-year-old)
    kid     + TV-Y7, TV-Y7-FV, PG                (default for a "kid" child: the nine-year-old)
    older   + TV-PG                              (what the whole house allowed before)
    teen    + PG-13, TV-14

The kids' library and feeds are still built once for every child, from the loosest level any child has
(`house_allowed`). A child's own view is narrowed when it is served. A title whose rating isn't a content
rating (no rating, or a provider's star score) is judged by its US certification from TMDB once the hub has
looked it up (`kidcerts`), and until then as before: by the kids' categories the hub trusts.

Some grown-up cartoons sit in a provider's kids' groups ("24/7 HAPPY TREE FRIENDS"). `denied` names them; they
never reach a child, whatever group or rating they carry.
"""

from __future__ import annotations

import re
from typing import Any

KV = "kids.ratings.{person}"

LEVELS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("little", "Little kids: G, TV-Y and TV-G", ("G", "TV-Y", "TV-G")),
    ("kid", "Kids: adds TV-Y7 and PG films", ("TV-Y7", "TV-Y7-FV", "PG")),
    ("older", "Older kids: adds TV-PG", ("TV-PG",)),
    ("teen", "Teens: adds PG-13 and TV-14", ("PG-13", "TV-14")),
)
NAMES = tuple(level for level, _label, _adds in LEVELS)
# Every content rating any level knows, and the grown-up ones no level allows. Only these are judged by level.
CONTENT = {r for _level, _label, adds in LEVELS for r in adds} | {"R", "NC-17", "TV-MA", "X", "XXX", "NR", "UR"}
DEFAULT_BY_STYLE = {"little": "little", "kid": "kid"}
FALLBACK = "kid"


# Grown-up animation a provider files with the kids' channels. Matched on whole words in a channel or title name.
DENIED = (
    "happy tree friends", "south park", "family guy", "american dad", "rick and morty", "beavis and butt",
    "bojack horseman", "big mouth", "robot chicken", "adult swim", "aqua teen", "squidbillies", "the boondocks",
    "harley quinn", "f is for family", "solar opposites", "hazbin hotel", "helluva boss", "paradise pd",
    "brickleberry", "the cleveland show", "archer",
)
_DENIED = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in DENIED) + r")\b")


def denied(name: object) -> bool:
    """A channel or title no child gets, whichever kids' group the provider put it in."""
    text = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower())
    return bool(_DENIED.search(text))


def denied_channel(name: object) -> bool:
    """For the catalog's kids flag on a channel (lineup, search and guide)."""
    return denied(name)


def allowed_at(level: str) -> set[str]:
    """Every rating a level allows (its own and every level below it)."""
    out: set[str] = set()
    for name, _label, adds in LEVELS:
        out.update(adds)
        if name == level:
            return out
    return allowed_at(FALLBACK)


def level_of(h: Any, person: dict[str, Any]) -> str:
    """This child's level: the one a grown-up set, else the default for how the child's screens look."""
    saved = h.db.kv_get(KV.format(person=person["id"])) or {}
    level = saved.get("level") if isinstance(saved, dict) else None
    if level in NAMES:
        return str(level)
    return DEFAULT_BY_STYLE.get(str(person.get("ui_style") or ""), FALLBACK)


def set_level(h: Any, person_id: str, level: str, *, by: str | None = None) -> None:
    if level not in NAMES:
        raise ValueError(level)
    h.db.kv_set(KV.format(person=person_id), {"level": level, "by": by})


def children(h: Any) -> list[dict[str, Any]]:
    return [p for p in h.db.people() if p.get("role") == "child"]


def for_person(h: Any, person_id: str | None) -> set[str]:
    """What this child may watch. Nobody named (a kids' TV nobody picked on) gets the strictest child's level,
    so an unnamed kids' screen never shows more than the youngest may see."""
    kids = children(h)
    if person_id:
        found = next((p for p in kids if p["id"] == person_id), None)
        if found is not None:
            return allowed_at(level_of(h, found))
    if not kids:
        return allowed_at(FALLBACK)
    return min((allowed_at(level_of(h, p)) for p in kids), key=len)


def for_subject(h: Any, subject: Any) -> set[str]:
    """For a child's own phone, iPad or a kids' screen: that child's level (the strictest without a person)."""
    pid = getattr(subject, "person_id", None) if getattr(subject, "role", None) == "child" else None
    return for_person(h, pid)


def house_allowed(h: Any) -> set[str]:
    """The loosest level any child has, joined with the house setting: what the shared kids' library is built
    from before each child's view is narrowed."""
    out = {str(r) for r in (h.config.child_policy.ratings or [])}
    for p in children(h):
        out |= allowed_at(level_of(h, p))
    return out


def allows(allowed: set[str], rating: object) -> bool:
    """A content rating outside the level says no. Anything else (no rating, a star score) was already judged by
    the kids' categories, so it stays."""
    text = str(rating or "").strip().upper()
    return text not in CONTENT or text in allowed


def allows_item(allowed: set[str], item: dict[str, Any], certs: Any = None) -> bool:
    """A card or catalog row: its own content rating, else its certification from TMDB (`kidcerts`) once known.
    A denied grown-up cartoon never passes."""
    if denied(item.get("name") or item.get("title")):
        return False
    rating = str(item.get("rating") or "").strip().upper()
    if rating in CONTENT:
        return rating in allowed
    cert = certs.cert(item) if certs is not None else None
    return allows(allowed, cert)


def narrow(body: Any, allowed: set[str], certs: Any = None) -> Any:
    """A feed, row list or search answer with every card a level doesn't allow taken out. Rows left empty go.
    With `certs` (`kidcerts.store`), a provider title is judged by its age certification once the hub knows it."""
    if isinstance(body, list):
        out = []
        for item in body:
            if isinstance(item, dict) and not allows_item(allowed, item, certs):
                continue
            kept = narrow(item, allowed, certs)
            if isinstance(item, dict) and isinstance(item.get("items"), list) and item["items"] and not kept["items"]:
                continue  # a row with nothing left
            out.append(kept)
        return out
    if isinstance(body, dict):
        out_d: dict[str, Any] = {}
        for key, value in body.items():
            if key == "hero" and isinstance(value, dict) and not allows_item(allowed, value, certs):
                out_d[key] = None
            elif isinstance(value, (list, dict)):
                out_d[key] = narrow(value, allowed, certs)
            else:
                out_d[key] = value
        return out_d
    return body


def view(h: Any, person: dict[str, Any]) -> dict[str, Any]:
    level = level_of(h, person)
    return {"person": person["id"], "name": person.get("name"), "level": level,
            "ratings": sorted(allowed_at(level)),
            "levels": [{"id": name, "label": label} for name, label, _adds in LEVELS]}
