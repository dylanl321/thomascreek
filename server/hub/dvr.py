"""Record a live channel onto the NAS when the provider has no catch-up.

The stream address is built the same way the lineup names a channel: the
Xtream stream id from the catalog, with the household login. That address is
never returned and never logged. ffmpeg copies it into one MPEG-TS file.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .artwork import programme_key
from .db import new_id
from .home import iso
from .tvlineup import valid_key

log = logging.getLogger("hub.dvr")

KV_RECORDINGS = "dvr.recordings"
KV_RULES = "dvr.rules"
_PROG = re.compile(r"^prog:[a-f0-9]{20}$")
_ID = re.compile(r"^dvr_[a-f0-9]{20}$")
PUBLIC = ("id", "channel_key", "title", "start", "end", "status", "size_bytes", "duration_s",
          "poster_url", "error", "stream_url")
ACTIVE = ("scheduled", "recording")


class DvrError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def parse_time(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        raise DvrError(400, "bad_time", "start and end are ISO-8601 times")
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise DvrError(400, "bad_time", "start and end are ISO-8601 times") from None
    if moment.tzinfo is None:
        raise DvrError(400, "bad_time", "start and end need a timezone")
    return moment.timestamp()


class Dvr:
    def __init__(self, hub: Any, directory: Path):
        self.hub = hub
        self.dir = Path(directory)
        self.max_connections = int(hub.config.dvr.max_connections)
        self.quota_bytes = int(hub.config.dvr.quota_bytes)
        self.now: Callable[[], float] = time.time
        self.inline = False
        self._task: asyncio.Task[None] | None = None
        self._jobs: dict[str, asyncio.Task[None]] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("dvr: %s", exc)
            await asyncio.sleep(5)

    # -- records -----------------------------------------------------------------------------------------------
    def recordings(self) -> list[dict[str, Any]]:
        raw = self.hub.db.kv_get(KV_RECORDINGS) or []
        return [row for row in raw if isinstance(row, dict)]

    def rules(self) -> list[dict[str, Any]]:
        raw = self.hub.db.kv_get(KV_RULES) or []
        return [row for row in raw if isinstance(row, dict)]

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.hub.db.kv_set(KV_RECORDINGS, rows)

    def _save_rules(self, rows: list[dict[str, Any]]) -> None:
        self.hub.db.kv_set(KV_RULES, rows)

    def get(self, recording_id: str) -> dict[str, Any]:
        for row in self.recordings():
            if row.get("id") == recording_id:
                return row
        raise DvrError(404, "not_found", "That recording isn't on the hub")

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {key: row.get(key) for key in PUBLIC}
        out["stream_url"] = f"/v1/dvr/recordings/{row['id']}/stream"
        return out

    def visible(self, row: dict[str, Any], *, kids: bool) -> bool:
        return not kids or bool(row.get("kids"))

    # -- schedule ----------------------------------------------------------------------------------------------
    async def schedule(self, *, channel_key: str, start: float, end: float, title: str | None,
                       programme: str | None, pad_before_s: int, pad_after_s: int, kids_only: bool) -> dict[str, Any]:
        if end <= start:
            raise DvrError(400, "bad_time", "The recording has to end after it starts")
        if end + pad_after_s <= self.now():
            raise DvrError(400, "bad_time", "That programme has already ended")
        channel = self._channel(channel_key)
        if kids_only and not channel.get("kids"):
            raise DvrError(403, "forbidden", "That channel isn't for this profile")
        row = self._row(channel, channel_key, start, end, title, programme, pad_before_s, pad_after_s)
        rows = self.recordings()
        self._conflict(row, rows)
        rows.append(row)
        self._save(rows)
        self._publish(row)
        if row["capture_start"] <= self.now():
            await self._begin(row["id"])
            row = self.get(row["id"])
        return self.public(row)

    async def record_now(self, *, channel_key: str, duration_s: int, title: str | None, kids_only: bool) -> dict[str, Any]:
        start = self.now()
        return await self.schedule(channel_key=channel_key, start=start, end=start + duration_s, title=title,
                                   programme=None, pad_before_s=0, pad_after_s=0, kids_only=kids_only)

    def add_rule(self, *, channel_key: str | None, title_match: str, new_only: bool) -> dict[str, Any]:
        text = title_match.strip()
        if len(text) < 2:
            raise DvrError(400, "bad_request", "Type at least two letters of the title")
        if channel_key is not None:
            self._channel(channel_key)
        rule = {"id": new_id("dvrule"), "channel_key": channel_key, "title_match": text[:80], "new_only": bool(new_only)}
        rows = self.rules()
        rows.append(rule)
        self._save_rules(rows)
        return rule

    async def stop(self, recording_id: str) -> dict[str, Any]:
        row = self.get(recording_id)
        if row["status"] in ("done", "failed", "cancelled"):
            return self.public(row)
        await self._halt(recording_id)
        row = self.get(recording_id)
        row["status"] = "cancelled"
        row["size_bytes"] = self._size(row)
        row["duration_s"] = self._duration(row)
        self._replace(row)
        self._publish(row)
        return self.public(row)

    async def delete(self, recording_id: str) -> None:
        self.get(recording_id)
        await self._halt(recording_id)
        path = self._file(recording_id)
        if path.is_file():
            path.unlink()
        folder = path.parent
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        self._save([row for row in self.recordings() if row.get("id") != recording_id])

    def file_for(self, recording_id: str) -> Path:
        if not _ID.fullmatch(recording_id or ""):
            raise DvrError(404, "not_found", "That recording isn't on the hub")
        self.get(recording_id)
        path = self._file(recording_id)
        if not path.is_file() or path.stat().st_size <= 0:
            raise DvrError(409, "not_ready", "That recording has no video yet")
        return path

    # -- clock -----------------------------------------------------------------------------------------------
    async def tick(self) -> None:
        self.recover()
        await self._apply_rules()
        now = self.now()
        for row in self.recordings():
            if row["status"] == "scheduled" and row["capture_start"] <= now < row["capture_end"]:
                await self._begin(row["id"])
            elif row["status"] == "recording" and now >= row["capture_end"] and row["id"] not in self._jobs:
                self._finish(row["id"], "done")
        self.cleanup()

    def recover(self) -> None:
        """A restart keeps future recordings. One that was mid-file starts again if it is still due."""
        now = self.now()
        changed = False
        rows = self.recordings()
        for row in rows:
            if row["status"] == "recording" and row["id"] not in self._jobs:
                if now >= row["capture_end"]:
                    row["status"] = "done" if self._size(row) else "failed"
                    row["error"] = None if row["status"] == "done" else "The hub restarted after this recording ended"
                    row["size_bytes"] = self._size(row)
                    row["duration_s"] = self._duration(row)
                    changed = True
                else:
                    row["status"] = "scheduled"
                    changed = True
            elif row["status"] == "scheduled" and now >= row["capture_end"]:
                row["status"] = "failed"
                row["error"] = "The hub was down for this recording"
                changed = True
        if changed:
            self._save(rows)
            for row in rows:
                if row["status"] in ("failed", "done"):
                    self._publish(row)

    async def _apply_rules(self) -> None:
        guide = (self.hub.tv_catalog.load().get("guide") or {}) if self.hub.tv_catalog else {}
        now = self.now()
        existing = {(row.get("channel_key"), row.get("start")) for row in self.recordings()}
        for rule in self.rules():
            needle = rule["title_match"].casefold()
            for channel_key, items in guide.items():
                if rule.get("channel_key") and rule["channel_key"] != channel_key:
                    continue
                for item in items or []:
                    title = str(item.get("title") or "")
                    start = float(item.get("start_ts") or 0)
                    end = float(item.get("end_ts") or 0)
                    if not title or needle not in title.casefold() or end <= now:
                        continue
                    stamp = iso(start)
                    if (channel_key, stamp) in existing:
                        continue
                    if rule.get("new_only") and any(row.get("title") == title for row in self.recordings()):
                        continue
                    try:
                        made = await self.schedule(channel_key=channel_key, start=start, end=end, title=title,
                                                   programme=None, pad_before_s=self.hub.config.dvr.pad_before_s,
                                                   pad_after_s=self.hub.config.dvr.pad_after_s, kids_only=False)
                    except DvrError:
                        continue
                    existing.add((channel_key, made["start"]))

    # -- capture -----------------------------------------------------------------------------------------------
    async def _begin(self, recording_id: str) -> None:
        if recording_id in self._jobs:
            return
        row = self.get(recording_id)
        if row["status"] not in ACTIVE:
            return
        try:
            self._conflict(row, [other for other in self.recordings() if other["id"] != recording_id], live_only=True)
        except DvrError as exc:
            row["status"] = "failed"
            row["error"] = exc.message
            self._replace(row)
            self._publish(row)
            return
        row["status"] = "recording"
        row["error"] = None
        self._replace(row)
        self._publish(row)
        if self.inline:
            await self._capture(recording_id)
        else:
            self._jobs[recording_id] = asyncio.create_task(self._capture(recording_id))

    async def _capture(self, recording_id: str) -> None:
        try:
            row = self.get(recording_id)
            url = self.live_url(row.get("stream_key") or row["channel_key"])
            path = self._file(recording_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            attempts = 0
            while self.now() < row["capture_end"] and attempts < 3:
                attempts += 1
                try:
                    await self.write_stream(url, path, row["capture_end"])
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("dvr %s dropped (%s)", recording_id, _safe_error(exc))
                    if attempts >= 3 or self.now() >= row["capture_end"]:
                        self._finish(recording_id, "failed", "The stream dropped")
                        return
                    await asyncio.sleep(0 if self.inline else 2)
                    row = self.get(recording_id)
            current = self.get(recording_id)
            if current["status"] == "cancelled":
                return
            self._finish(recording_id, "done")
        finally:
            self._jobs.pop(recording_id, None)

    async def write_stream(self, url: str, path: Path, until: float) -> None:
        """Copy the live stream until `until`. Tests replace this."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed")
        args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-rw_timeout", "15000000",
                "-i", url, "-c", "copy", "-f", "mpegts", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert proc.stdout is not None
        try:
            with path.open("ab") as out:
                while self.now() < until:
                    try:
                        chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=5)
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if not chunk:
                        break
                    out.write(chunk)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if self.now() < until - 1:
            raise RuntimeError("stream dropped")

    def _finish(self, recording_id: str, status: str, error: str | None = None) -> None:
        try:
            row = self.get(recording_id)
        except DvrError:
            return
        if row["status"] == "cancelled":
            return
        row["status"] = status
        row["error"] = error
        row["size_bytes"] = self._size(row)
        row["duration_s"] = self._duration(row)
        self._replace(row)
        self._publish(row)
        self.cleanup()

    async def _halt(self, recording_id: str) -> None:
        job = self._jobs.pop(recording_id, None)
        if job is not None:
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass

    def cleanup(self) -> None:
        """Drop the oldest finished recordings until the folder is under the quota."""
        rows = self.recordings()
        done = sorted((row for row in rows if row["status"] == "done"), key=lambda row: row.get("capture_end") or 0)
        while sum(self._size(row) for row in rows) > self.quota_bytes and done:
            oldest = done.pop(0)
            path = self._file(oldest["id"])
            if path.is_file():
                path.unlink()
            shutil.rmtree(path.parent, ignore_errors=True)
            rows = [row for row in rows if row["id"] != oldest["id"]]
        self._save(rows)

    # -- stream address ----------------------------------------------------------------------------------------
    def live_url(self, stream_key: str) -> str:
        parts = str(stream_key).split(":")
        if len(parts) != 3 or parts[0] != "xc" or not parts[2]:
            raise DvrError(404, "not_found", "That channel has no live stream")
        server, user, password = self.hub.tv_catalog._login()
        return f"{server}/live/{user}/{password}/{parts[2]}.ts"

    def _channel(self, channel_key: str) -> dict[str, Any]:
        if not valid_key(channel_key or ""):
            raise DvrError(400, "bad_key", "channel_key must be an iptv-org, Xtream or tvg id")
        catalog = self.hub.tv_catalog
        found = catalog.channel(channel_key)
        if found:
            return found
        want = channel_key.split(":", 1)[1] if channel_key.startswith("iptv-org:") else (
            channel_key.split(":", 2)[-1] if channel_key.startswith("tvg:") else None)
        if want:
            for row in catalog.load().get("channels") or []:
                if row.get("epg_id") == want:
                    shown = catalog._present_channel(row)
                    if shown:
                        return shown
        raise DvrError(404, "not_found", "That channel isn't in the lineup")

    def _row(self, channel: dict[str, Any], requested_key: str, start: float, end: float, title: str | None,
             programme: str | None, pad_before_s: int, pad_after_s: int) -> dict[str, Any]:
        name = (title or channel.get("name") or "Recording").strip()[:120]
        art_key = programme if programme and _PROG.fullmatch(programme) else programme_key(name)
        recording_id = new_id("dvr")
        return {
            "id": recording_id,
            "channel_key": requested_key,
            "stream_key": channel["key"],
            "title": name,
            "start": iso(start),
            "end": iso(end),
            "capture_start": start - pad_before_s,
            "capture_end": end + pad_after_s,
            "status": "scheduled",
            "size_bytes": 0,
            "duration_s": 0,
            "poster_url": f"/v1/art/poster/{art_key}",
            "error": None,
            "kids": bool(channel.get("kids")),
        }

    def _conflict(self, row: dict[str, Any], others: list[dict[str, Any]], *, live_only: bool = False) -> None:
        """Refuse a recording that would open more streams than the provider allows."""
        windows = [(other["capture_start"], other["capture_end"]) for other in others if other["status"] in ACTIVE]
        if not live_only:
            windows.append((row["capture_start"], row["capture_end"]))
        else:
            windows.append((self.now(), row["capture_end"]))
        points = sorted({edge for window in windows for edge in window})
        for moment in points:
            open_streams = sum(1 for begin, finish in windows if begin <= moment < finish)
            if open_streams > self.max_connections:
                raise DvrError(409, "conflict", f"That would use more than {self.max_connections} streams at once")

    def _replace(self, row: dict[str, Any]) -> None:
        rows = self.recordings()
        self._save([row if item.get("id") == row["id"] else item for item in rows])

    def _publish(self, row: dict[str, Any]) -> None:
        self.hub.bus.publish({"type": "dvr_update", "data": {**self.public(row), "kids": bool(row.get("kids"))}})

    def _file(self, recording_id: str) -> Path:
        path = (self.dir / recording_id / "recording.ts").resolve()
        root = self.dir.resolve()
        if root != path.parent.parent:
            raise DvrError(404, "not_found", "That recording isn't on the hub")
        return path

    def _size(self, row: dict[str, Any]) -> int:
        path = self._file(row["id"])
        return path.stat().st_size if path.is_file() else int(row.get("size_bytes") or 0)

    def _duration(self, row: dict[str, Any]) -> int:
        begin = float(row.get("capture_start") or 0)
        end = min(self.now(), float(row.get("capture_end") or 0))
        if end <= begin:
            return 0
        return int(end - begin)


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    if "://" in text or "password" in text.casefold():
        return "stream dropped"
    return text[:160] or "stream dropped"
