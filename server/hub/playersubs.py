"""Subtitles the hub fetches when a film or episode has none of its own.

The player asks for the list only when the person opens Subtitles and the stream has no track in their language
(Dylan, Nov 19 2026: fetched on request, English first). The hub searches OpenSubtitles with its own key, keeps
the list for a day, and downloads a file only when the person picks one (the service counts downloads per day).
A picked file is kept on the hub as WebVTT, so the next screen that plays the title gets it without a download.

No key, no match and a service that is down all give an empty list with a short `reason` the player can show."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import httpx

API = "https://api.opensubtitles.com/api/v1"
AGENT = "ThomasCreekHub v1"            # the service wants an app name and version
KEY = "opensubtitles.api_key"
KV = "player.subtitles"
KEEP_S = 86400
KEEP_FAILED_S = 300
MAX_TITLES = 500
MAX_HITS = 8
MAX_FILE = 2_000_000
_LANG = re.compile(r"[a-z]{2,3}(-[a-z]{2})?")
_STAMP = re.compile(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})")


def hits(body: object) -> list[dict[str, Any]]:
    """OpenSubtitles' search answer as our list: single-file subtitles made by people, most downloaded first,
    and plain ones before those for the hard of hearing."""
    rows = body.get("data") if isinstance(body, dict) else None
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        at = row.get("attributes") if isinstance(row, dict) else None
        files = at.get("files") if isinstance(at, dict) else None
        if not isinstance(at, dict) or not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            continue                      # a subtitle in several files does not fit one stream
        if at.get("machine_translated") or at.get("ai_translated"):
            continue
        file_id = files[0].get("file_id")
        lang = str(at.get("language") or "").lower()
        if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id <= 0 or not _LANG.fullmatch(lang):
            continue
        label = str(at.get("release") or files[0].get("file_name") or "").strip()[:80]
        downloads = at.get("download_count")
        out.append({"id": str(file_id), "lang": lang, "label": label or "Subtitles",
                    "hearing_impaired": bool(at.get("hearing_impaired")),
                    "downloads": downloads if isinstance(downloads, int) and not isinstance(downloads, bool) else 0})
    out.sort(key=lambda h: (h["hearing_impaired"], -h["downloads"]))
    return out[:MAX_HITS]


def to_vtt(data: bytes) -> str | None:
    """A subtitle file (SubRip or WebVTT, any common encoding) as WebVTT text, or None when it is neither."""
    if len(data) > MAX_FILE:
        return None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "-->" not in text:
        return None
    if text.startswith("WEBVTT"):
        return text + "\n"
    return "WEBVTT\n\n" + _STAMP.sub(r"\1.\2", text) + "\n"


def _doc(h: Any) -> dict[str, Any]:
    doc = h.db.kv_get(KV)
    return doc if isinstance(doc, dict) else {}


def _cached(h: Any, key: str, now: float) -> dict[str, Any] | None:
    row = _doc(h).get(key)
    return row if isinstance(row, dict) and float(row.get("until") or 0) > now else None


def _keep(h: Any, key: str, row: dict[str, Any]) -> None:
    doc = _doc(h)
    doc[key] = row
    if len(doc) > MAX_TITLES:
        for old in sorted(doc, key=lambda k: float((doc[k] or {}).get("until") or 0))[:len(doc) - MAX_TITLES]:
            doc.pop(old, None)
    h.db.kv_set(KV, doc)


def _headers(key: str) -> dict[str, str]:
    return {"Api-Key": key, "User-Agent": AGENT, "Accept": "application/json"}


async def search(h: Any, title_id: str, *, tmdb: str | None, name: str | None, year: str | None,
                 season: int | None, number: int | None, lang: str, client: httpx.AsyncClient,
                 now: float | None = None) -> dict[str, Any]:
    """`{"subtitles":[{id, lang, label, hearing_impaired, downloads}], "reason"?}` for one title and language."""
    lang = lang if _LANG.fullmatch(lang or "") else "en"
    now = time.time() if now is None else now
    cache_key = f"{title_id}|{lang}"
    cached = await asyncio.to_thread(_cached, h, cache_key, now)
    if cached is not None:
        return {k: cached[k] for k in ("subtitles", "reason") if k in cached}
    key = str(h.vault.get(KEY) or "").strip()
    if not key:
        return {"subtitles": [], "reason": "The hub has no subtitle service key yet"}
    episode = bool(season and number and season > 0 and number > 0)
    params: dict[str, Any] = {"languages": lang}
    if tmdb and str(tmdb).isdigit():
        params["parent_tmdb_id" if episode else "tmdb_id"] = tmdb
    elif name:
        params["query"] = name[:120]
        if year and not episode and str(year)[:4].isdigit():
            params["year"] = str(year)[:4]
    else:
        return {"subtitles": [], "reason": "The hub doesn't know this title well enough to search"}
    if episode:
        params.update(season_number=season, episode_number=number)
    out: dict[str, Any] = {"subtitles": []}
    keep = KEEP_FAILED_S
    try:
        answer = await client.get(f"{API}/subtitles", params=params, headers=_headers(key), timeout=8)
        if answer.status_code == 200:
            out["subtitles"] = hits(answer.json())
            keep = KEEP_S
            if not out["subtitles"]:
                out["reason"] = "No subtitles were found for this one"
        elif answer.status_code in (401, 403):
            out["reason"] = "The subtitle service refused the hub's key"
        elif answer.status_code == 429:
            out["reason"] = "The subtitle service is busy. Try again in a minute"
        else:
            out["reason"] = "The subtitle service didn't answer"
    except (httpx.HTTPError, ValueError):
        out["reason"] = "The subtitle service didn't answer"
    await asyncio.to_thread(_keep, h, cache_key, {**out, "until": now + keep})
    return out


def offered(h: Any, title_id: str, file_id: str, now: float | None = None) -> bool:
    """Whether the hub offered `file_id` for this title (in any language) and the list is still kept: the only
    files it will download, so nobody spends the day's downloads on an id of their own."""
    now = time.time() if now is None else now
    for key, row in _doc(h).items():
        if key.startswith(f"{title_id}|") and isinstance(row, dict) and float(row.get("until") or 0) > now:
            if any(isinstance(s, dict) and s.get("id") == file_id for s in row.get("subtitles") or []):
                return True
    return False


class SubtitleError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


async def fetch(h: Any, folder: Path, file_id: str, client: httpx.AsyncClient) -> str:
    """The WebVTT text of one offered file: from the hub's folder, or downloaded once and kept there."""
    if not file_id.isdigit() or len(file_id) > 12:
        raise SubtitleError(404, "not_found", "That subtitle isn't on offer")
    path = folder / f"{file_id}.vtt"
    if path.is_file():
        return await asyncio.to_thread(path.read_text, "utf-8")
    key = str(h.vault.get(KEY) or "").strip()
    if not key:
        raise SubtitleError(503, "no_key", "The hub has no subtitle service key yet")
    try:
        ticket = await client.post(f"{API}/download", json={"file_id": int(file_id)}, headers=_headers(key), timeout=10)
        if ticket.status_code == 406:
            raise SubtitleError(429, "limit", "Today's subtitle downloads are used up. Try again tomorrow")
        link = ticket.json().get("link") if ticket.status_code == 200 else None
        if not isinstance(link, str) or not link.startswith("https://"):
            raise SubtitleError(502, "unavailable", "The subtitle service didn't hand over the file")
        got = await client.get(link, headers={"User-Agent": AGENT}, timeout=15)
        text = to_vtt(got.content) if got.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        raise SubtitleError(502, "unavailable", "The subtitle service didn't answer") from None
    if text is None:
        raise SubtitleError(502, "unavailable", "That subtitle file couldn't be read")

    def save() -> None:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        files = sorted(folder.glob("*.vtt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[2000:]:
            old.unlink(missing_ok=True)
    await asyncio.to_thread(save)
    return text
