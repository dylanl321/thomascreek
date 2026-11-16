"""Clean provider film, series and category names for every screen.

The provider writes "NF - Athlete A 4K (2020)" and "|EN| ACTORS & DIRECTORS". The screens show
"Athlete A" (2020, 4K) and "Actors & Directors". Cleaning lives here, once, so the TVs and the
iPhone draw the same names and match the same TMDB titles.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

# Lead tags: "EN - ", "NF - ", "SPT - ", "WC – ", "|EN| ", "[MULTI] ", "EN | ". Only known tags, so "FBI - ..." stays.
_TAGS = (r"EN|ENG|US|UK|GB|CA|AU|FR|DE|ES|IT|PT|NL|KR|JP|TR|IN|AR|MULTI|SUB|SPT|WC|VOD|4K|UHD|FHD|HD"
         r"|NF|NETFLIX|D\+|DSNP|DISNEY\+|AMZ|AMZN|PRIME|HBO|HMAX|ATV\+?|ATVP|HULU|PCOK|PMNT|P\+")
_LEAD = re.compile(rf"^\s*(?:\|\s*(?:{_TAGS})\s*\||\[\s*(?:{_TAGS})\s*\]|(?:{_TAGS})\s*(?:[-\u2013\u2014|]))\s*(?=\S)", re.I)
_SERVICES = {
    "NF": "Netflix", "NETFLIX": "Netflix", "D+": "Disney+", "DSNP": "Disney+", "DISNEY+": "Disney+",
    "AMZ": "Prime Video", "AMZN": "Prime Video", "PRIME": "Prime Video", "HBO": "HBO Max", "HMAX": "HBO Max",
    "ATV": "Apple TV+", "ATV+": "Apple TV+", "ATVP": "Apple TV+", "HULU": "Hulu", "PCOK": "Peacock",
    "PMNT": "Paramount+", "P+": "Paramount+",
}
_YEAR = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]|\b((?:19|20)\d{2})\s*$")
_QUALITY = re.compile(r"\b(4K|UHD|2160p|FHD|1080p|HD|720p|HEVC|H\.?26[45]|x26[45]|HDR10?\+?|DV)\b", re.I)
_LANG_NOTE = re.compile(r"\b(?:MULTI(?:[-\s]?SUBS?)?|(?:ENG?[-\s]?)?SUBS?|SUBBED|DUBBED|DUAL(?:[-\s]AUDIO)?|VOSTFR)\b", re.I)
_BRACKETS = re.compile(r"\s*[(\[]([^)\]]*)[)\]]")
_SPACE = re.compile(r"\s+")
_COUNTRY = re.compile(r"^(?:US|USA|UK|GB|KR|JP|TR|FR|DE|ES|IT|IN|CA|AU|CN|MX|BR|SE|DK|NO)$", re.I)
_LANGS = {"FRENCH", "ENGLISH", "SPANISH", "GERMAN", "ITALIAN", "JAPANESE", "KOREAN", "TURKISH", "HINDI",
          "CHINESE", "PORTUGUESE", "ARABIC", "RUSSIAN", "DUTCH", "POLISH", "THAI", "SWEDISH"}
# Words kept upper case when a shouting category name is title-cased.
_UPPER = {"4K", "HD", "UHD", "TV", "UFC", "NFL", "NBA", "MLB", "NHL", "WWE", "F1", "HBO", "BBC", "AMC", "MTV",
          "USA", "UK", "US", "ESPN", "PPV", "DC", "MMA", "TNT", "TBS", "CW", "ABC", "CBS", "NBC", "FX", "PBS", "AMC+",
          "NCAA", "MLS", "WNBA", "PGA", "LGBTQ", "LGBTQ+", "XXX", "24/7", "3D", "II", "III", "IV"}
_PROPER = {"NETFLIX": "Netflix", "DISNEY+": "Disney+", "HULU": "Hulu", "PEACOCK": "Peacock", "PARAMOUNT+": "Paramount+",
           "APPLE": "Apple", "AMAZON": "Amazon", "PRIME": "Prime", "MAX": "Max", "WORLDCUP": "World Cup",
           "MARVEL": "Marvel", "PIXAR": "Pixar", "STARZ": "Starz", "SHOWTIME": "Showtime", "CRUNCHYROLL": "Crunchyroll"}
_SMALL = {"and", "or", "of", "the", "a", "an", "in", "on", "for", "to", "with", "vs", "at", "by"}
_ALL_TAGS = re.compile(r"^\s*(?:\|[^|]{1,8}\||\[[^\]]{1,8}\])\s*")
_MAX = 120


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace(" ", " ")


def title(raw: object) -> dict[str, Any]:
    """{"name", "year", "service", "quality", "subs"} for a provider film or series name."""
    text = _plain(str(raw or "")).strip()
    original = text
    service = None
    quality = None
    subs = False
    for _ in range(3):
        found = _LEAD.match(text)
        if not found:
            break
        tag = re.sub(r"[\s|\[\]\-–—:]", "", found.group(0)).upper()
        service = service or _SERVICES.get(tag)
        text = text[found.end():]
    year = None
    # Everything after the year is notes (cast lists, "UNCREDIT" ...): "Blonde 4K (2022) Marilyn Monroe".
    found = re.search(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]", text)
    if found:
        year = int(found.group(1))
        tail = text[found.end():]
        text = text[:found.start()]
        if _QUALITY.search(tail):
            quality = quality or _quality(_QUALITY.search(tail).group(1))
        if _LANG_NOTE.search(tail) or any(word in tail.upper() for word in _LANGS):
            subs = True
    else:
        bare = re.search(r"\s((?:19|20)\d{2})\s*$", text)
        if bare and len(text[:bare.start()].strip()) > 1:
            year = int(bare.group(1))
            text = text[:bare.start()]

    def bracket(match: re.Match[str]) -> str:
        nonlocal quality, subs
        inner = match.group(1).strip()
        words = re.split(r"[\s,/-]+", inner.upper())
        if _QUALITY.search(inner):
            quality = quality or _quality(_QUALITY.search(inner).group(1))
        if _LANG_NOTE.search(inner) or any(word in _LANGS for word in words):
            subs = True
        if not inner or _COUNTRY.match(inner) or _QUALITY.fullmatch(inner) or _LANG_NOTE.search(inner) \
                or all(word in _LANGS or _QUALITY.fullmatch(word) or _LANG_NOTE.fullmatch(word) or not word for word in words) \
                or inner.upper() in {"X", "XXX"}:
            return ""
        return match.group(0)

    text = _BRACKETS.sub(bracket, text)
    for match in _QUALITY.finditer(text):
        quality = quality or _quality(match.group(1))
    text = _QUALITY.sub(" ", text)
    if _LANG_NOTE.search(text):
        subs = True
        text = _LANG_NOTE.sub(" ", text)
    text = _SPACE.sub(" ", text).strip(" -–|:.,")
    if not text:
        text = original
    if text.isupper() and len(text) > 4:
        text = _title_case(text)
    return {"name": text[:_MAX], "year": year, "service": service, "quality": quality, "subs": subs}


def _quality(tag: str) -> str | None:
    tag = tag.upper()
    if tag in {"4K", "UHD", "2160P"}:
        return "4K"
    if tag in {"FHD", "1080P", "HD", "720P"}:
        return "HD"
    return None


def category(raw: object) -> str:
    """"|EN| ACTORS & DIRECTORS" as "Actors & Directors"; "|MULTI| NETFLIX SERIES 4K" as "Netflix Series 4K"."""
    text = _plain(str(raw or "")).strip()
    original = text
    for _ in range(3):
        found = _ALL_TAGS.match(text) or re.match(r"^\s*(?:EN|MULTI|SPT|US|UK|NF|WC)\s*[|\-–:]\s*(?=\S)", text, re.I)
        if not found:
            break
        text = text[found.end():]
    text = re.sub(r"\bWORLDCUP\b", "World Cup", text, flags=re.I)
    text = _SPACE.sub(" ", text).strip(" -–|:")
    if not text:
        return original
    letters = [ch for ch in text if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.8:
        text = _title_case(text)
    return text[:60]


def _title_case(text: str) -> str:
    out = []
    for index, word in enumerate(text.split(" ")):
        upper = word.upper()
        bare = upper.strip("()[]&,.:!?")
        if bare in _UPPER:
            out.append(word.upper())
        elif bare in _PROPER:
            out.append(word.upper().replace(bare, _PROPER[bare]))
        elif index and word.lower() in _SMALL:
            out.append(word.lower())
        elif "/" in word:
            out.append("/".join(part[:1].upper() + part[1:].lower() for part in word.split("/")))
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def names(doc: dict[str, Any], *, kids_only: bool, is_kids: Any) -> dict[str, Any]:
    """Clean names for the catalog the TVs play from, keyed by the provider's own ids."""
    def ident(row: dict[str, Any]) -> str | None:
        key = str(row.get("id") or "")
        return key.rsplit(":", 1)[-1] if ":" in key else None

    def rows(kind: str) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for row in doc.get(kind) or []:
            if kids_only and not is_kids(row.get("category")):
                continue
            key = ident(row)
            if not key:
                continue
            clean = title(row.get("name"))
            out[key] = [clean["name"], clean["year"] or row.get("year"), clean["quality"], clean["service"]]
        return out

    def labels(kind: str) -> dict[str, str]:
        shown = doc.get("categories", {}).get(kind) or []
        return {name: category(name) for name in shown if not kids_only or is_kids(name)}

    body = {"movies": rows("movies"), "series": rows("series"),
            "categories": {"live": labels("live"), "vod": labels("vod"), "series": labels("series")}}
    body["version"] = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    body["fields"] = ["name", "year", "quality", "service"]
    return body
