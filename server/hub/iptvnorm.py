"""Clean a provider's title and decide whether two rows are the same title.

Quality tags, bracketed notes and country prefixes come off. ESPN 2, a
Spanish feed, an East feed and a +1 timeshift stay their own channels.
A pair is folded together only above a high score. Anything less stays apart.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit

AUTO_MERGE = 0.9
UNSURE = 0.62

_QUALITY = (
    (re.compile(r"\b(?:4k|uhd|2160p)\b", re.I), "2160p"),
    (re.compile(r"\b(?:fhd|1080p)\b", re.I), "1080p"),
    (re.compile(r"\b(?:hd|720p)\b", re.I), "720p"),
    (re.compile(r"\bsd\b", re.I), "sd"),
)
_DROP_WORDS = re.compile(r"\b(?:hevc|h\.?265|h\.?264|hdr|x264|x265)\b", re.I)
_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_PREFIX = re.compile(r"^(?:(?P<tag>USA|US|UK|GB|CA|AU|EN|ES|FR|DE|IT|PT|NL))\s*[\|:/\-]\s*", re.I)
# Streaming-service tags some providers put before film and series titles ("NF - Kaos", "D+ - Moana 2").
_SERVICE = re.compile(
    r"^\s*[\[(|]?\s*(?:NF|NETFLIX|D\+|DSNP|DISNEY\+?|AMZ|AMZN|PRIME|HBO|HMAX|HBOMAX|ATV\+?|ATVP|APPLE\s?TV\+?|HULU|PCOK|PEACOCK"
    r"|PMNT|P\+|PARAMOUNT\+?|STAN|CRAVE|SHO|STARZ|AMC\+|MUBI)\s*[\])|]?\s*[-\u2013|:]\s*(?=\S)", re.I)
_TIMESHIFT = re.compile(r"(?:\+|plus\s*)1\b", re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PUNCT = re.compile(r"[^\w\s+]", re.UNICODE)
_SPACE = re.compile(r"\s+")
_REGIONS = {"us": "US", "usa": "US", "uk": "UK", "gb": "UK", "ca": "CA", "au": "AU"}
_LANGS = {"en": "en", "es": "es", "fr": "fr", "de": "de", "it": "it", "pt": "pt", "nl": "nl"}
_VARIANTS = {"deportes", "east", "west", "pacific", "mountain", "central"}
_NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5"}


@dataclass(frozen=True)
class Parsed:
    key: str
    display: str
    variant: str | None
    region: str | None
    language: str | None
    timeshift: int
    quality: str | None
    year: int | None


def parse(name: str, year: object = None, language: str | None = None, titles: bool = False) -> Parsed:
    """The part of a title that is safe to compare, and the part that must stay distinct.

    `titles` is for films and series: a streaming-service tag in front ("NF - ") is not part of the title.
    """
    if titles:
        return _parse_title(name or "", year, language)
    original = unicodedata.normalize("NFKC", name or "")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", original) if not unicodedata.combining(ch))
    region = None
    lang = (language or "").strip().lower() or None
    while True:
        found = _PREFIX.match(text.strip())
        if not found and titles:
            found = _SERVICE.match(text.strip())
            if found:
                text = text.strip()[found.end():]
                continue
        if not found:
            break
        tag = found.group("tag").lower()
        if tag in _REGIONS and region is None:
            region = _REGIONS[tag]
        if tag in _LANGS and lang is None:
            lang = _LANGS[tag]
        text = text[found.end():]
    # A dated show filed as a film ("WWE Raw (26/08/2024)"): the date tells one episode from the next, so it stays
    # as the variant and two dates never fold into one title (Nov 19 2026: 27 Raw episodes were one "film").
    dated = None
    found_date = _DATE.search(text)
    if found_date:
        day, month, year4 = _date_parts(found_date)
        if day and month:
            dated = f"{year4:04d}-{month:02d}-{day:02d}"
            text = text[:found_date.start()] + " " + text[found_date.end():]
    quality = None
    for pattern, label in _QUALITY:
        if quality is None and pattern.search(text):
            quality = label
    year_in = _YEAR.search(text)
    parsed_year = int(year_in.group(0)) if year_in else _as_year(year)
    text = _BRACKETS.sub(" ", text)
    timeshift = 1 if _TIMESHIFT.search(text) else 0
    text = _TIMESHIFT.sub(" ", text)
    for pattern, _label in _QUALITY:
        text = pattern.sub(" ", text)
    text = _DROP_WORDS.sub(" ", text)
    text = _YEAR.sub(" ", text)
    paren_region = re.search(r"\b(US|UK|CA|AU)\b", text, re.I)
    if paren_region and region is None:
        region = paren_region.group(1).upper()
        text = paren_region.re.sub(" ", text, count=1)
    words = [word for word in _PUNCT.sub(" ", text).split() if word]
    variant = dated
    kept = []
    for word in words:
        low = word.casefold()
        if low in _VARIANTS and variant is None:
            variant = low
            continue
        if low in _NUMBERS and variant is None and kept:
            variant = _NUMBERS[low]
            continue
        if low.isdigit() and variant is None and kept and len(low) <= 2:
            variant = str(int(low))
            continue
        kept.append(word)
    display = _SPACE.sub(" ", " ".join(kept)).strip() or (name or "").strip()
    key = display.casefold()
    if dated and found_date:
        display = f"{display} ({found_date.group(0)})"  # the episode's own date, so each card says which one
    return Parsed(key, display, variant, region, lang, timeshift, quality, parsed_year)


def title_key(name: str) -> str:
    """A cleaned title as it is compared: no accents, no punctuation, one case. "Carême" and "Careme" are one."""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))
    return _SPACE.sub(" ", _PUNCT.sub(" ", text)).strip().casefold()


def _parse_title(name: str, year: object, language: str | None) -> Parsed:
    """A film or series. The name is the one the card shows (`tvnames.name_year`), so matching and display agree.
    Channel rules don't apply: a number is part of a title ("District 9", "Force 2"), and so are the words "us"
    and "east". Only a dated show's date is a variant."""
    from . import tvnames

    original = unicodedata.normalize("NFKC", name).strip()
    region = None
    lang = (language or "").strip().lower() or None
    lead = original
    while True:  # the place or language a provider wrote in front, for the copy's language; the cleaner drops it
        found = _PREFIX.match(lead)
        if not found:
            break
        tag = found.group("tag").lower()
        region = region or _REGIONS.get(tag)
        lang = lang or _LANGS.get(tag)
        lead = lead[found.end():].strip()
    text = original
    dated = None
    found_date = _DATE.search(text)
    if found_date:
        day, month, year4 = _date_parts(found_date)
        if day and month:
            dated = f"{year4:04d}-{month:02d}-{day:02d}"
            text = (text[:found_date.start()].rstrip(" ([") + " " + text[found_date.end():].lstrip(" )]")).strip()
    quality = next((label for pattern, label in _QUALITY if pattern.search(original)), None)
    display, shown_year = tvnames.name_year(text)
    display = _SPACE.sub(" ", display).strip() or original
    # A bare number on the end ("Blade Runner 2049", "First Day Out 2023") stays on the card, where it may be the
    # title. For matching it is read as the year, so it never folds with a film of the same name from another year
    # and still meets its copy that was written "First Day Out (2023)".
    # The key is read off the card name itself, so a saved title read back gives the same key.
    again = tvnames.title(display)
    found_year = shown_year or again["year"]
    key = title_key(str(again["name"] or display))
    if dated and found_date:
        display = f"{display} ({found_date.group(0)})"  # the episode's own date, so each card says which one
    return Parsed(key, display, dated, region, lang, 0, quality, found_year or _as_year(year))


_DATE = re.compile(r"\b(?:(\d{1,2})[/.](\d{1,2})[/.]((?:19|20)\d{2})|((?:19|20)\d{2})-(\d{1,2})-(\d{1,2}))\b")


def _date_parts(found: re.Match[str]) -> tuple[int, int, int]:
    """(day, month, year) of a dd/mm/yyyy or yyyy-mm-dd date, or zeros when it isn't a real date."""
    if found.group(1):
        day, month, year = int(found.group(1)), int(found.group(2)), int(found.group(3))
        if month > 12 and day <= 12:
            day, month = month, day  # an mm/dd/yyyy date
    else:
        year, month, day = int(found.group(4)), int(found.group(5)), int(found.group(6))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return 0, 0, year
    return day, month, year


def logo_key(url: object) -> str | None:
    """The file name of a logo. A URL with a username or password is ignored."""
    if not isinstance(url, str) or "://" not in url:
        return None
    parts = urlsplit(url)
    if parts.username or parts.password:
        return None
    base = parts.path.rsplit("/", 1)[-1].lower()
    base = re.sub(r"\.(png|jpe?g|webp|gif)$", "", base)
    return base or None


def score(left: Parsed, right: Parsed, *, kind: str, left_ids: dict[str, str], right_ids: dict[str, str],
          left_logo: str | None, right_logo: str | None, left_season: int | None = None,
          right_season: int | None = None, left_episode: int | None = None, right_episode: int | None = None) -> float:
    """1 is the same title. 0 is not. Between the two cutoffs the hub asks, or leaves them apart."""
    hard = _hard(left_ids, right_ids)
    if hard == 0:
        return 0.0
    if hard == 1:
        return 1.0
    if left.variant != right.variant or left.timeshift != right.timeshift:
        return 0.0
    if left.region and right.region and left.region != right.region:
        return 0.0
    if kind == "series" and _numbers_differ(left_season, right_season):
        return 0.0
    if kind == "series" and _numbers_differ(left_episode, right_episode):
        return 0.0
    if kind == "movie" and left.year and right.year and left.year != right.year:
        return 0.0
    if not left.key or not right.key:
        return 0.0
    if left.key == right.key:
        both_years = kind == "movie" and left.year and right.year
        base = 0.95 if (kind != "movie" or both_years) else 0.72
    else:
        ratio = SequenceMatcher(None, left.key, right.key).ratio()
        if ratio < 0.9:
            return 0.0
        if kind == "movie" and left.year and right.year and left.year != right.year:
            return 0.0
        base = 0.7
    # Channel logos are shared across feeds, so a different one is a hint. Movie and show posters
    # differ between copies of the same film all the time, so they say nothing.
    if kind == "live" and left_logo and right_logo and left_logo != right_logo:
        base -= 0.2
    return max(0.0, min(1.0, base))


def _hard(left: dict[str, str], right: dict[str, str]) -> int | None:
    seen = False
    for field in ("tmdb", "imdb", "tvg_id"):
        a, b = left.get(field), right.get(field)
        if not a or not b:
            continue
        seen = True
        if str(a).lower() != str(b).lower():
            return 0
    return 1 if seen else None


def _numbers_differ(left: int | None, right: int | None) -> bool:
    return left is not None and right is not None and left != right


def _as_year(value: object) -> int | None:
    match = _YEAR.search(str(value or ""))
    return int(match.group(0)) if match else None
