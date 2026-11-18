"""Live TV categories organised the way people look for them: by country, then by kind (sports, news, kids...).

A provider names its groups "UK | SPORTS", "|US| NEWS", "NL: SPORTS MIX 16K", "EN - KIDS". The hub reads the
country off the front, keeps the readable label (tvnames.category), and gives each group a kind. The phone (and
later the TVs) draws the result; it doesn't parse provider names itself.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from . import tvnames

COUNTRIES: dict[str, str] = {
    "US": "United States", "USA": "United States", "UK": "United Kingdom", "GB": "United Kingdom",
    "IE": "Ireland", "CA": "Canada", "AU": "Australia", "NZ": "New Zealand", "NL": "Netherlands", "BE": "Belgium",
    "DE": "Germany", "AT": "Austria", "CH": "Switzerland", "FR": "France", "ES": "Spain", "PT": "Portugal",
    "IT": "Italy", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "TR": "Turkey", "GR": "Greece", "RO": "Romania", "IN": "India", "PK": "Pakistan", "AR": "Arabic", "AE": "Arabic",
    "MX": "Mexico", "BR": "Brazil", "LAT": "Latin America", "LATAM": "Latin America", "CN": "China",
    "JP": "Japan", "KR": "Korea", "PH": "Philippines", "ZA": "South Africa", "CARIB": "Caribbean", "AF": "Africa",
    "RU": "Russia", "UA": "Ukraine", "HU": "Hungary", "CZ": "Czechia", "IL": "Israel",
}
WORLD = {"WS": "World", "INT": "International", "WW": "World"}
# Codes a provider puts in front that are languages or brands, not places.
NOT_PLACES = {"EN", "MULTI", "SPT", "NF", "WC", "VIP", "PPV", "HD", "4K", "UHD", "FHD", "VOD", "24/7"}
_LEAD = re.compile(r"^\s*[|\[(]?\s*([A-Za-z]{2,5})\s*(?:[|\]):\-–]|\s+I\s+)")

KINDS: list[tuple[str, str]] = [
    ("locals", r"\blocals?\b|affiliates?|\bnbc\b.*\bcbs\b"),
    ("sports", r"sport|espn|nfl|nba|mlb|nhl|soccer|football|f1|formula|racing|golf|tennis|boxing|ufc|wwe|cricket|"
               r"rugby|dazn|tnt sports|sky sports|bein|nascar|motor"),
    ("news", r"\bnews\b|cnn|bbc news|sky news|fox news|msnbc|cnbc|business"),
    ("kids", r"\bkids?\b|children|cartoon|disney|nick|junior|family"),
    ("movies", r"movie|cinema|film|hbo|showtime|starz|cinemax|sky cinema|rakuten"),
    ("documentary", r"document|discovery|history|nat ?geo|science|nature"),
    ("music", r"music|radio|mtv|vevo"),
    ("entertainment", r"entertain|general|variety|reality|comedy|drama|lifestyle|bbc iplayer|itv|channel 4"),
    ("events", r"\bppv\b|events?|replay|24/7|247"),
    ("religion", r"relig|faith|christian|islam"),
]
KIND_ORDER = [k for k, _ in KINDS] + ["other"]
KIND_LABELS = {"locals": "Locals", "sports": "Sports", "news": "News", "kids": "Kids", "movies": "Movies",
               "documentary": "Documentary", "music": "Music", "entertainment": "Entertainment",
               "events": "Events and replays", "religion": "Faith", "other": "More"}


def country_of(raw: object) -> tuple[str, str] | None:
    """("UK", "United Kingdom") for "UK | SPORTS"; None for "|EN| KIDS" or a group with no place."""
    text = str(raw or "")
    found = _LEAD.match(text)
    if not found:
        return None
    code = found.group(1).upper()
    if code in NOT_PLACES:
        return None
    if code in COUNTRIES:
        name = COUNTRIES[code]
        code = {"USA": "US", "GB": "UK", "LATAM": "LAT", "AE": "AR"}.get(code, code)
        return code, name
    if code in WORLD:
        return "WORLD", "World"
    return None


def kind_of(label: str) -> str:
    low = label.casefold()
    for kind, pattern in KINDS:
        if re.search(pattern, low):
            return kind
    return "other"


def groups(channels: Iterable[dict[str, Any]], home: str = "US") -> dict[str, Any]:
    """{countries: [{code, name, count, groups: [{group, label, kind, count}]}], kinds: {...}} with the home
    country first, then the rest by size; groups inside a country by kind, then size."""
    by_group: dict[str, dict[str, Any]] = {}
    for row in channels:
        raw = str(row.get("group") or "")
        if not raw:
            continue
        entry = by_group.get(raw)
        if entry is None:
            label = row.get("group_label") or tvnames.category(raw)
            place = country_of(raw)
            entry = {"group": raw, "label": label, "kind": kind_of(f"{label} {raw}"),
                     "country": place[0] if place else None, "country_name": place[1] if place else None,
                     "count": 0}
            by_group[raw] = entry
        entry["count"] += 1
    countries: dict[str, dict[str, Any]] = {}
    for entry in by_group.values():
        code = entry["country"] or "OTHER"
        box = countries.setdefault(code, {"code": code, "name": entry["country_name"] or "More channels",
                                          "count": 0, "groups": []})
        box["count"] += entry["count"]
        box["groups"].append({k: entry[k] for k in ("group", "label", "kind", "count")})
    for box in countries.values():
        box["groups"].sort(key=lambda g: (KIND_ORDER.index(g["kind"]), -g["count"], g["label"].casefold()))
    order = sorted(countries.values(),
                   key=lambda b: (b["code"] != home, b["code"] in ("OTHER", "WORLD"), -b["count"], b["name"]))
    return {"home": home, "countries": order, "kind_labels": KIND_LABELS}
