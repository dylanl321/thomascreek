"""What every provider's playlist holds, and what the hub's cleaners make of it (read-only).

Nov 20 2026, measured on the house's own lists: one provider's films carried 74 different lead tags and a third
of them were left in the shown name; the cleaner for display and the cleaner for folding gave different names for
four films in ten; one channel in ten had a guide id. None of that was visible anywhere. This module counts it
once per provider read and keeps the answer in `tvapp/playlists.json`, so the admin page (and a later rules page)
can show each provider side by side without walking 350,000 rows on a request.

It changes nothing a TV or phone sees: it reads the stored lists and runs today's cleaners over them. It runs in
the provider-read child (or a worker thread in tests), never on a request. The report holds counts, provider ids,
tags, category names and a few title names as examples; never a login or an address.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from typing import Any, Iterable

from . import iptvnorm, tvgroups, tvnames

VERSION = 1
MAIN = "thomas-creek"
MAX_TAGS = 80
MAX_CATEGORIES = 400
MAX_EXAMPLES = 24
MIN_TAG_ROWS = 3

# A lead tag as providers write it: "|EN| ", "[US] ", "NF - ", "TOP | ", and for channels "US: ". The bare forms
# count only when the tag is written in capitals, so "Batman: Year One" and "Thor - 2011" are titles, not tags.
_BOXED = re.compile(r"^\s*(?:\|\s*([^|\]\[]{1,12}?)\s*\||\[\s*([^\]\[|]{1,12}?)\s*\])")
_BARE = re.compile(r"^\s*([A-Z0-9][A-Z0-9+&/.\-]{0,9})\s*(?:[|–—]|-(?=\s))\s*(?=\S)")
_BARE_COLON = re.compile(r"^\s*([A-Z0-9][A-Z0-9+&/.\-]{0,9})\s*:\s*(?=\S)")
_EVENT = re.compile(r"\bvs\.?\s|\s@\s|\b\d{1,2}[:.]\d{2}\s*(?:am|pm|et|ct|pt)?\b|\(\d{4}-\d\d-\d\d|\bno event\b|"
                    r"^\s*(?:ended?|next)\b", re.I)
_WORDS = re.compile(r"[^\w]+", re.UNICODE)
_ADULT_TAGS = {"X", "XX", "XXX", "18+", "18", "ADULT", "ADULTS"}

Kinds = ("live", "films", "series")
_BUCKET = {"live": "channels", "films": "movies", "series": "series"}
_CATEGORY_KIND = {"live": "live", "films": "vod", "series": "series"}


def lead_tag(name: str, *, live: bool) -> str | None:
    """The tag a provider put in front of a name, upper-cased, or None for a plain name."""
    found = _BOXED.match(name)
    if found:
        return (found.group(1) or found.group(2) or "").strip().upper() or None
    found = _BARE.match(name) or (_BARE_COLON.match(name) if live else None)
    return found.group(1).upper() if found else None


def shown_name(name: str, kind: str) -> str:
    """The name a screen shows today for a provider row of this kind."""
    if kind == "live":
        return tvnames.channel_name(name)
    return str(tvnames.title(name)["name"] or name)


def _plain_key(text: str) -> str:
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return _WORDS.sub(" ", text).casefold().strip()


def _category_of(doc: dict[str, Any], kind: str, row: dict[str, Any]) -> str | None:
    """A row's raw category: the main list keeps the name on the row, other providers keep an id and a table."""
    if row.get("category"):
        return str(row["category"])
    table = (doc.get("categories") or {}).get(_CATEGORY_KIND[kind])
    if isinstance(table, dict) and row.get("cat") is not None:
        name = table.get(str(row["cat"]))
        return str(name) if name else None
    return None


def _provider(pid: str, doc: dict[str, Any], tags: dict[str, dict[str, dict[str, Any]]],
              examples: list[dict[str, str]]
              ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    guide: dict[str, Any] = doc.get("guide") if isinstance(doc.get("guide"), dict) else {}  # type: ignore[assignment]
    out: dict[str, Any] = {"id": pid, "main": pid == MAIN, "fetched_at": doc.get("fetched_at")}
    categories: dict[str, list[dict[str, Any]]] = {}
    ids: dict[str, set[str]] = {}
    for kind in Kinds:
        rows = [row for row in doc.get(_BUCKET[kind]) or [] if isinstance(row, dict) and row.get("name")]
        live = kind == "live"
        have: Counter[str] = Counter()
        cats: Counter[str] = Counter()
        left = differ = dividers = events = 0
        keys: set[str] = set()
        for row in rows:
            name = str(row["name"])
            if live and tvnames.is_divider(name):
                dividers += 1
                continue
            for field, label in (("tmdb", "tmdb"), ("epg_id", "guide_id"), ("logo", "art"), ("cover", "art"),
                                 ("year", "year")):
                if row.get(field):
                    have[label] += 1
            category = _category_of(doc, kind, row)
            if category:
                have["category"] += 1
                cats[category] += 1
            if live:
                if guide.get(str(row.get("key") or "")):
                    have["programmes"] += 1
                if _EVENT.search(name):
                    events += 1
                keys.add(iptvnorm.parse(name).key)
            elif row.get("tmdb"):
                keys.add(str(row["tmdb"]))
            shown = shown_name(name, kind)
            tag = lead_tag(name, live=live)
            if tag:
                # A grown-up section's tag is listed like any other, without one of its titles as the example.
                entry = tags[kind].setdefault(tag, {"tag": tag, "rows": Counter(), "left": Counter(),
                                                    "example": "" if tag in _ADULT_TAGS else name[:80]})
                entry["rows"][pid] += 1
                if lead_tag(shown, live=live) or shown == name:
                    entry["left"][pid] += 1
                    left += 1
            if not live:
                folded = iptvnorm.parse(name, row.get("year"), titles=True)
                if _plain_key(shown) != _WORDS.sub(" ", folded.key).strip():
                    differ += 1
                    if len(examples) < MAX_EXAMPLES and differ % 97 == 1:
                        examples.append({"provider": pid, "kind": kind, "raw": name[:80], "shown": shown[:80],
                                         "folded": folded.display[:80]})
        block: dict[str, Any] = {"rows": len(rows) - dividers, "tag_left": left, "with": dict(have)}
        if live:
            block["dividers"] = dividers
            block["events"] = events
        else:
            block["cleaners_differ"] = differ
        out[kind] = block
        ids[kind] = keys
        table = []
        for raw, count in cats.most_common(MAX_CATEGORIES):
            place = tvgroups.country_of(raw) if live else None
            table.append({"raw": raw[:80], "label": tvnames.category(raw), "country": place[0] if place else None,
                          "rows": count})
        categories[kind] = table
    return out, categories, ids


def _overlap(ids: dict[str, dict[str, set[str]]], kind: str) -> list[dict[str, Any]]:
    """How many titles (by TMDB id) or channels (by folded name) each combination of providers carries."""
    where: dict[str, list[str]] = {}
    for pid in sorted(ids):
        for key in ids[pid].get(kind) or ():
            where.setdefault(key, []).append(pid)
    counts: Counter[tuple[str, ...]] = Counter(tuple(pids) for pids in where.values())
    return [{"providers": list(pids), "count": count}
            for pids, count in sorted(counts.items(), key=lambda item: (-len(item[0]), -item[1]))]


def build(docs: Iterable[tuple[str, dict[str, Any]]], now: float | None = None) -> dict[str, Any]:
    """The report for these providers' stored lists, `(provider id, snapshot)` each, the main provider first."""
    started = time.monotonic()
    tags: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in Kinds}
    examples: list[dict[str, str]] = []
    providers, categories, ids = [], {}, {}
    for pid, doc in docs:
        if not isinstance(doc, dict) or not any(doc.get(bucket) for bucket in _BUCKET.values()):
            continue
        summary, cats, keys = _provider(pid, doc, tags, examples)
        providers.append(summary)
        categories[pid] = cats
        ids[pid] = keys
    tag_rows: dict[str, list[dict[str, Any]]] = {}
    for kind in Kinds:
        rows = [{"tag": entry["tag"], "rows": dict(entry["rows"]), "left": dict(entry["left"]),
                 "example": entry["example"]}
                for entry in tags[kind].values() if sum(entry["rows"].values()) >= MIN_TAG_ROWS]
        rows.sort(key=lambda row: (-sum(row["left"].values()), -sum(row["rows"].values()), row["tag"]))
        tag_rows[kind] = rows[:MAX_TAGS]
    return {
        "version": VERSION, "built_at": time.time() if now is None else now,
        "took_s": round(time.monotonic() - started, 1),
        "providers": providers, "tags": tag_rows, "categories": categories,
        "overlap": {kind: _overlap(ids, kind) for kind in Kinds}, "examples": examples,
    }


def stamp(docs: Iterable[tuple[str, dict[str, Any]]]) -> str:
    """Which lists a report was built from: it is stale when any provider was read again or one came or went."""
    return "|".join(f"{pid}:{int(float(doc.get('fetched_at') or 0))}" for pid, doc in docs if doc) + f"|v{VERSION}"
