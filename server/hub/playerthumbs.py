"""Small pictures for the player's scrub bar, made by the hub at night.

Dragging the bar shows where you are landing. Plex has such pictures; an IPTV film or episode has none, so the hub
makes them (Dylan, Nov 19 2026: overnight, for Continue watching and the next episode of the shows people watch).
One picture every `STEP_S` seconds, `COLS` x `ROWS` of them on a sheet, so a player fetches one small file per
1000 seconds of film.

The hub reads the title like a player would, so it takes a login from the lease pool (`hub/iptvplay.py`) as the
holder `hub:thumbs`, and only then:

- in the night hours of the house, with nobody playing anything;
- when another play of that title would still start (a spare login), so the job never makes a person wait;
- one title at a time, and it stops and frees the login at once when anyone starts to play.

The provider address is handed to ffmpeg and nowhere else. ffmpeg's own words are never logged or kept: an error
line can carry the address."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HOLDER = "hub:thumbs"
KV = "player.thumbs"
WIDTH, HEIGHT = 160, 90
STEP_S = 10
COLS, ROWS = 10, 10
MAX_SHEETS = 15                      # 15000 seconds: longer films get pictures for their first four hours
NIGHT = (2, 6)                       # house time, from 02:00 up to 06:00
PER_NIGHT = 12
KEEP_TITLES = 400
RETRY_FAILED_S = 7 * 86400
TITLE_LIMIT_S = 30 * 60
CHECK_S = 3
LOOP_S = 600
_KINDS = ("vod", "ep")


def slug(item_id: str) -> str:
    """The folder name of a title: nothing of the id's own text reaches the file system."""
    return hashlib.sha256(item_id.encode()).hexdigest()[:20]


def view(h: Any, item_id: str, *, base: str, query: str) -> dict[str, Any]:
    """What a player needs to draw the picture for a second of the film; `sheets` is empty when none are made.

    Picture `n = second // interval_s` is on sheet `n // (columns * rows)`, in cell `n % (columns * rows)`,
    counted along the rows from the top left. Each sheet's address names the title again (`query`), because the
    file is only handed out after the same kids' rule."""
    count = _sheets(h, item_id)
    out: dict[str, Any] = {"interval_s": STEP_S, "width": WIDTH, "height": HEIGHT, "columns": COLS, "rows": ROWS,
                           "sheets": [f"{base}/{slug(item_id)}/{n}.jpg?{query}" for n in range(1, count + 1)]}
    return out


def sheet(h: Any, folder: Path, item_id: str, name: str, number: int) -> Path | None:
    """The file of one sheet of this title, or None. `name` must be this title's own folder."""
    count = _sheets(h, item_id)
    if name != slug(item_id) or not 1 <= number <= count:
        return None
    path = folder / name / f"{number:02d}.jpg"
    return path if path.is_file() else None


def wanted(h: Any, now: float | None = None) -> list[str]:
    """The IPTV films and episodes worth the night's work, most recently watched first: what anyone is partway
    through, and the episode after it. Titles that have pictures, or failed lately, are left out."""
    from .playernext import next_episode

    now = time.time() if now is None else now
    cat = getattr(h, "tv_catalog", None)
    if cat is None:
        return []
    doc = _doc(h)
    done, failed = doc.get("done") or {}, doc.get("failed") or {}
    ranked: dict[str, float] = {}

    def add(item_id: object, last: object) -> None:
        key = str(item_id or "")
        if key.split(":")[0] not in _KINDS or key in done:
            return
        if now - float(failed.get(key) or 0) < RETRY_FAILED_S:
            return
        ranked[key] = max(ranked.get(key, 0.0), float(last) if isinstance(last, (int, float)) else 0.0)

    for bucket in h.db.watch_buckets().values():
        for movie_id, row in (bucket.get("movies") or {}).items():
            position = row.get("position_s")
            if isinstance(position, (int, float)) and position >= 30 and cat.movie(str(movie_id)) is not None:
                add(movie_id, row.get("last"))
        for row in (bucket.get("series") or {}).values():
            episode_id = str(row.get("episode_id") or "")
            if not episode_id or cat.episode(episode_id) is None:
                continue
            add(episode_id, row.get("last"))
            after = next_episode(cat, episode_id)
            if after:
                add(after.get("episode_id"), (float(row.get("last") or 0) or 1.0) - 1.0)
    return sorted(ranked, key=lambda key: ranked[key], reverse=True)


def at_night(h: Any, now: float | None = None) -> bool:
    moment = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(h.config.hub.timezone))
    return NIGHT[0] <= moment.hour < NIGHT[1]


def quiet(h: Any) -> bool:
    """Nobody but this job holds a login."""
    from .iptvplay import accounts

    return all(row.get("tv") == HOLDER for row in accounts(h).get("sessions") or [])


def _spare(h: Any, item_id: str) -> bool:
    """Another play of this title would still start while the job holds its login."""
    from .playnow import availability

    return availability(h, item_id, "hub:thumbs-check").get("state") == "ready"


def command(ffmpeg: str, source: str, folder: Path) -> list[str]:
    """Key pictures only (cheap to decode), one every STEP_S seconds, tiled onto sheets."""
    box = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2"
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "quiet", "-skip_frame", "nokey", "-i", source,
            "-an", "-sn", "-dn", "-vf", f"fps=1/{STEP_S},{box},tile={COLS}x{ROWS}", "-q:v", "6",
            "-frames:v", str(MAX_SHEETS), "-start_number", "1", "-y", str(folder / "%02d.jpg")]


async def _run(args: list[str], keep_going: Any) -> bool:
    """Run ffmpeg to its end. False when it failed, ran too long or `keep_going()` said stop. Its output is
    thrown away unread (see the module note)."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    started = time.monotonic()
    try:
        while True:
            try:
                return await asyncio.wait_for(proc.wait(), CHECK_S) == 0
            except TimeoutError:
                if time.monotonic() - started > TITLE_LIMIT_S or not await keep_going():
                    return False
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def make(h: Any, folder: Path, item_id: str) -> str:
    """Make the sheets of one title: "done", "failed", "skip" when no login is spare for it, or "wait" when
    someone is playing."""
    from .iptvplay import direct, release_tv, touch

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "failed"
    if not await asyncio.to_thread(quiet, h):
        return "wait"
    source = await asyncio.to_thread(direct, h, item_id, HOLDER)
    if not source:
        return "wait"
    work = folder / f"{slug(item_id)}.part"
    final = folder / slug(item_id)
    stopped = False

    async def keep_going() -> bool:
        nonlocal stopped
        await asyncio.to_thread(touch, h, HOLDER)
        stopped = not await asyncio.to_thread(quiet, h)
        return not stopped

    try:
        if not await asyncio.to_thread(_spare, h, item_id):
            return "skip"                 # only one login carries it: left for a night with a spare
        await asyncio.to_thread(_fresh, work)
        ok = await _run(command(ffmpeg, source, work), keep_going)
    finally:
        await asyncio.to_thread(release_tv, h, HOLDER)
    if stopped:
        await asyncio.to_thread(shutil.rmtree, work, True)
        return "wait"
    count = await asyncio.to_thread(_finish, work, final) if ok else 0
    if not count:
        await asyncio.to_thread(shutil.rmtree, work, True)
    await asyncio.to_thread(_note, h, folder, item_id, count)
    await asyncio.to_thread(_log, h, "ok" if count else "failed", item_id, {"sheets": count})
    return "done" if count else "failed"


async def night(h: Any, folder: Path, *, limit: int = PER_NIGHT, now: float | None = None) -> int:
    """One pass: up to `limit` titles while it is night and the house is quiet. How many got pictures."""
    made = 0
    item_id: str
    for item_id in (await asyncio.to_thread(wanted, h, now))[:limit]:
        if not at_night(h, now):
            break
        result = await make(h, folder, item_id)
        if result == "wait":
            break
        made += result == "done"
    return made


async def loop(h: Any, folder: Path) -> None:
    """The hub's background task: look every ten minutes, work only at night."""
    while True:
        await asyncio.sleep(LOOP_S)
        try:
            if at_night(h):
                await night(h, folder)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad night never stops the hub; the type says where to look
            await asyncio.to_thread(_log, h, "failed", None, {"error": type(exc).__name__})


def _sheets(h: Any, item_id: str) -> int:
    done = (_doc(h).get("done") or {}).get(item_id) or {}
    found = done.get("sheets")
    return found if isinstance(found, int) and not isinstance(found, bool) else 0


def _log(h: Any, outcome: str, target: str | None, params: dict[str, Any]) -> None:
    h.db.log("player.thumbs", outcome, target=target, params=params, source="hub")


def _fresh(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)


def _finish(work: Path, final: Path) -> int:
    count = len([p for p in work.glob("*.jpg") if p.stat().st_size > 0])
    if not count:
        return 0
    shutil.rmtree(final, ignore_errors=True)
    work.replace(final)
    return count


def _note(h: Any, folder: Path, item_id: str, count: int) -> None:
    doc = _doc(h)
    done, failed = dict(doc.get("done") or {}), dict(doc.get("failed") or {})
    now = time.time()
    if count:
        done[item_id] = {"sheets": count, "at": now}
        failed.pop(item_id, None)
    else:
        failed[item_id] = now
    for old in sorted(done, key=lambda key: float(done[key].get("at") or 0), reverse=True)[KEEP_TITLES:]:
        shutil.rmtree(folder / slug(old), ignore_errors=True)
        done.pop(old)
    failed = {key: at for key, at in failed.items() if now - float(at or 0) < RETRY_FAILED_S}
    h.db.kv_set(KV, {"done": done, "failed": failed})


def _doc(h: Any) -> dict[str, Any]:
    doc = h.db.kv_get(KV)
    return doc if isinstance(doc, dict) else {}
