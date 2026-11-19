"""Clean provider film, series and category names for every screen.

The provider writes "NF - Athlete A 4K (2020)" and "|EN| ACTORS & DIRECTORS". The screens show
"Athlete A" (2020, 4K) and "Actors & Directors". Cleaning lives here, once, so the TVs and the
iPhone draw the same names and match the same TMDB titles.
"""

from __future__ import annotations

import functools
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
_UPPER = {"8K", "16K",
          "4K", "HD", "UHD", "TV", "UFC", "NFL", "NBA", "MLB", "NHL", "WWE", "F1", "HBO", "BBC", "AMC", "MTV",
          "USA", "UK", "US", "ESPN", "PPV", "DC", "MMA", "TNT", "TBS", "CW", "ABC", "CBS", "NBC", "FX", "PBS", "AMC+",
          "NCAA", "MLS", "WNBA", "PGA", "LGBTQ", "LGBTQ+", "XXX", "24/7", "3D", "II", "III", "IV"}
_PROPER = {"NETFLIX": "Netflix", "DISNEY+": "Disney+", "HULU": "Hulu", "PEACOCK": "Peacock", "PARAMOUNT+": "Paramount+",
           "APPLE": "Apple", "AMAZON": "Amazon", "PRIME": "Prime", "MAX": "Max", "WORLDCUP": "World Cup",
           "IPLAYER": "iPlayer",
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
    episode = None
    # "Show (2025) S01 E01" or "Show - S01E02 - Pilot": the episode mark and anything after it are not the title.
    mark = re.search(r"[\s\-\u2013(\[]*\bS(\d{1,2})\s*[.\-]?\s*E(\d{1,4})\b", text, re.I)
    if mark:
        episode = f"S{int(mark.group(1))}E{int(mark.group(2))}"
        text = text[:mark.start()]
    # A cut-off name ("Show (", "Show (2025", "Show [EN"): an opening bracket that never closes is a lost note.
    cut = re.search(r"\s*[(\[]([^)\]]*)$", text)
    if cut:
        bare_year = re.fullmatch(r"\s*((?:19|20)\d{2})\s*", cut.group(1))
        if _LANG_NOTE.search(cut.group(1)):
            subs = True
        text = text[:cut.start()] + (f" ({bare_year.group(1)})" if bare_year else "")
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
    return {"name": text[:_MAX], "year": year, "service": service, "quality": quality, "subs": subs,
            "episode": episode}


def _quality(tag: str) -> str | None:
    tag = tag.upper()
    if tag in {"4K", "UHD", "2160P"}:
        return "4K"
    if tag in {"FHD", "1080P", "HD", "720P"}:
        return "HD"
    return None


# Place codes a provider puts in front of a live group ("UK | SPORTS", "NL: NEWS", "UK I SKY STORE").
PLACE_CODES = ("US|USA|UK|GB|IE|CA|AU|NZ|NL|BE|DE|AT|CH|FR|ES|PT|IT|SE|NO|DK|FI|PL|TR|GR|RO|IN|PK|AR|AE|MX|BR|LAT"
               "|LATAM|CN|JP|KR|PH|ZA|CARIB|AF|RU|UA|HU|CZ|IL|WS|INT|WW")
_GROUP_LEAD = re.compile(rf"^\s*\|?\s*(?:EN|MULTI|SPT|NF|WC|{PLACE_CODES})\s*(?:[|\-–:]|\s+I\s+)\s*(?=\S)", re.I)
# Superscript badges ("ᵛᴵᴾ", "ᵁᴴᴰ") and the VIP tag they spell: decoration, not part of the name.
_BADGE = re.compile(r"[\u02b0-\u02ff\u1d2c-\u1dbf\u2070-\u209f]+")
_VIP = re.compile(r"\s*\bVIP\b\s*", re.I)


def category(raw: object) -> str:
    """"|EN| ACTORS & DIRECTORS" as "Actors & Directors"; "|MULTI| NETFLIX SERIES 4K" as "Netflix Series 4K";
    "UK | SKY SPORTS+ ᵛᴵᴾ" as "Sky Sports+" (the place is kept apart, see tvgroups.country_of)."""
    return _category(str(raw or ""))


@functools.lru_cache(maxsize=16384)  # a few thousand provider categories, asked for on every list
def _category(raw: str) -> str:
    text = _plain(_BADGE.sub(" ", str(raw or ""))).strip()
    original = text
    text = _VIP.sub(" ", text).strip()
    for _ in range(3):
        found = _ALL_TAGS.match(text) or _GROUP_LEAD.match(text)
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
    else:
        text = _acronyms(text)
    return text[:60]


def _acronyms(text: str) -> str:
    """Known acronyms in a mixed-case label keep their capitals: "Espn+ PPV" -> "ESPN+ PPV"."""
    out = []
    for word in text.split(" "):
        bare = word.upper().strip("()[]&,.:!?")
        out.append(word.upper() if (bare in _UPPER or bare.rstrip("+") in _UPPER) and len(bare) > 1 else word)
    return " ".join(out)


_PAREN_YEAR = re.compile(r"[(\[]\s*(?:19|20)\d{2}\s*[)\]]")
_DIVIDER = re.compile(r"^[\W_]*[#=*~\-]{3,}")


def is_divider(name: object) -> bool:
    """A provider's section header listed as a channel ("###### ARABIC ######", "===== UK ====")."""
    return bool(_DIVIDER.match(_plain(str(name or ""))))


def polish(raw: object) -> tuple[str, int | None]:
    """A card title as people read it, and its year if the name carried one: "EN - Rolling Loud (2026)" ->
    ("Rolling Loud", 2026). A plain title is left alone, so "Blade Runner 2049" keeps its number."""
    return _polish(str(raw or "").strip())


@functools.lru_cache(maxsize=262144)  # the films and series lists sort on it, every row, every page
def _polish(text: str) -> tuple[str, int | None]:
    if not text or not (_LEAD.match(text) or _PAREN_YEAR.search(text) or _ALL_TAGS.match(text)):
        return text, None
    clean = title(text)
    name, year = clean["name"] or text, clean["year"]
    if year and not _PAREN_YEAR.search(text):
        # The year came off the end of a plain name ("EN - Blade Runner 2049"): it's part of the title.
        name, year = f"{name} {year}", None
    return name, year


def _title_case(text: str) -> str:
    out = []
    for index, word in enumerate(text.split(" ")):
        upper = word.upper()
        bare = upper.strip("()[]&,.:!?")
        if bare in _UPPER or bare.rstrip("+") in _UPPER:
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
            entry = [clean["name"], clean["year"] or row.get("year"), clean["quality"], clean["service"]]
            if clean["episode"]:
                entry.append(clean["episode"])  # loose episodes listed as films group under `name`
            out[key] = entry
        return out

    def labels(kind: str) -> dict[str, str]:
        shown = doc.get("categories", {}).get(kind) or []
        return {name: category(name) for name in shown if not kids_only or is_kids(name)}

    body = {"movies": rows("movies"), "series": rows("series"),
            "categories": {"live": labels("live"), "vod": labels("vod"), "series": labels("series")}}
    body["version"] = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    body["fields"] = ["name", "year", "quality", "service", "episode"]
    return body


# -- channel names ---------------------------------------------------------------------------------------------
# A provider writes "US| THE OFFICE FHD", "UK: BBC One HD", "|EN| ESPN 2 4K", "DE: ZDF ◉". People read "The Office",
# "BBC One", "ESPN 2", "ZDF". Films and series go through `title`, which suits those ("DE: ZDF" would become
# "De: Zdf"), so channels get their own, gentler cleaner: only known lead tags, only trailing quality or feed tags.
_CH_CODES = (r"EN|ENG|US|USA|UK|GB|CA|AU|NZ|IE|FR|DE|AT|CH|ES|IT|PT|NL|BE|KR|JP|TR|IN|AR|MX|BR|SE|DK|NO|FI|PL"
             r"|LAT|LATAM|EU|INT|MULTI|SPT|VIP|24/7")
_CH_LEAD = re.compile(rf"^\s*(?:\|\s*[A-Z0-9+/]{{1,6}}\s*\||\[\s*[A-Z0-9+/]{{1,6}}\s*\]|(?:{_CH_CODES})\s*[:|\-–—])\s*",
                      re.I)
_CH_TAIL = re.compile(r"(?:\s+|\s*[|\-]\s*)(?:F?HD|UHD|SD|4K|8K|HEVC|H\.?26[45]|\d{2,3}\s?FPS|RAW|BACKUP|VIP|ALT|"
                      r"\(?(?:FHD|HD|UHD|SD|4K|RAW|BACKUP)\)?)\s*$", re.I)
_CH_SYMBOLS = re.compile(r"[^\w+)!?.]+$")
_COMMON = {"THE", "AND", "ONE", "TWO", "SIX", "TEN", "NEW", "OLD", "FOX", "BIG", "RED", "SKY", "BOX", "GOD", "ART",
           "CAR", "DAY", "FUN", "JOY", "KID", "MAX", "NOW", "OUT", "TOP", "ALL", "HOT", "PET", "WAR", "FOOD", "LIVE",
           "OF", "IN", "ON", "TO", "AT", "BY", "OR", "MY", "GO", "UP", "NO", "SO"}


_CALL_SIGN = re.compile(r"[KW][A-Z]{2,3}(?:-(?:TV|DT|LD|CD))?")


def channel_name(raw: object) -> str:
    """A live channel's name as people read it. Anything it can't make sense of comes back as given."""
    return _channel_name(str(raw or "").strip())


@functools.lru_cache(maxsize=65536)
def _channel_name(text: str) -> str:
    original = text
    text = _SPACE.sub(" ", _plain(text)).strip()
    for _ in range(3):  # "|US| USA: ESPN" carries two
        stripped = _CH_LEAD.sub("", text, count=1)
        if stripped == text or not stripped.strip():
            break
        text = stripped
    for _ in range(4):  # "THE OFFICE AU FHD RAW"
        stripped = _CH_TAIL.sub("", text)
        if stripped == text or not stripped.strip():
            break
        text = stripped
    if is_divider(text):
        return original
    text = _CH_SYMBOLS.sub("", text).strip()
    if not text:
        return original
    letters = [ch for ch in text if ch.isalpha()]
    if letters and not any(ch.islower() for ch in letters):
        text = " ".join(_channel_word(word, index) for index, word in enumerate(text.split(" ")))
    return text[:_MAX]


def _channel_word(word: str, index: int) -> str:
    bare = word.strip("()[]&,.:!?")
    if bare in _UPPER or bare.rstrip("+") in _UPPER:
        return word
    if bare in _PROPER:
        return word.replace(bare, _PROPER[bare])
    letters = [ch for ch in bare if ch.isalpha()]
    if word.startswith("(") or _CALL_SIGN.fullmatch(bare):
        return word  # a US call sign: (WABC), KTLA
    short = len(bare) <= 3 and bare not in _COMMON
    if not letters or short or any(ch.isdigit() for ch in bare) or not any(ch in "AEIOUY" for ch in bare):
        return word  # an acronym or a number: ZDF, CNN, AU, 5USA
    if index and word.lower() in _SMALL:
        return word.lower()
    return word[:1].upper() + word[1:].lower()
