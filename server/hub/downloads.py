"""Films and episodes saved to phones for offline viewing.

Two ways to get a title onto a phone:

* ``original``: the file as it sits in Plex. An MP4/M4V/MOV with codecs an
  iPhone plays is streamed straight from Plex (no copy, starts at once). Other
  containers (usually MKV) are rewrapped into MP4 without re-encoding, which
  is quick and keeps the full quality. Titles whose video an iPhone can't
  decode get no original option.
* ``high`` / ``small``: Plex's own transcoder converts it (720p or 480p), and
  ffmpeg stitches the result into one MP4 the phone can play offline.

Prepared files live under ``<data>/downloads`` and are removed a day after
they were made. The phone downloads through a bearer-free pass in the URL so
an iOS background download can resume without a fresh access token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("hub.downloads")

PLAYABLE_CONTAINERS = {"mp4", "m4v", "mov"}
PLAYABLE_VIDEO = {"h264", "hevc", "h265"}
PLAYABLE_AUDIO = {"aac", "ac3", "eac3", "mp3", "alac", "flac", "opus"}

QUALITIES: dict[str, dict[str, Any]] = {
    "high": {"label": "Good", "detail": "720p", "kbps": 4000, "res": "1280x720"},
    "small": {"label": "Small", "detail": "480p", "kbps": 1500, "res": "854x480"},
}
KEEP_S = 24 * 3600
MAX_JOBS = 2


@dataclass
class Job:
    id: str
    item_id: str
    quality: str
    device_id: str
    person_id: str | None
    item: dict[str, Any]
    status: str = "preparing"  # preparing | ready | failed
    progress: float = 0.0
    bytes: int | None = None
    error: str | None = None
    path: Path | None = None
    passthrough: str | None = None  # Plex part key streamed as is
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def out(self) -> dict[str, Any]:
        return {"id": self.id, "item_id": self.item_id, "quality": self.quality, "status": self.status,
                "progress": round(self.progress, 3), "bytes": self.bytes, "error": self.error,
                "item": self.item}


def options(media: dict[str, Any]) -> list[dict[str, Any]]:
    """What can be offered for one title, with rough sizes."""
    dur_s = (media.get("duration_ms") or 0) / 1000
    out: list[dict[str, Any]] = []
    if original_mode(media):
        mode = original_mode(media)
        out.append({"quality": "original", "label": "Original",
                    "detail": f"{media.get('resolution') or 'Full'} quality, fastest to get",
                    "bytes": media.get("size"), "instant": mode == "direct"})
    for q, spec in QUALITIES.items():
        out.append({"quality": q, "label": spec["label"], "detail": f"{spec['detail']}, converted by Plex",
                    "bytes": int(dur_s * (spec["kbps"] + 192) * 1000 / 8) if dur_s else None, "instant": False})
    return out


def original_mode(media: dict[str, Any]) -> str | None:
    """'direct' (stream the Plex file), 'remux' (rewrap into MP4) or None (an iPhone can't play it)."""
    video = (media.get("video_codec") or "").lower()
    audio = (media.get("audio_codec") or "").lower()
    if video not in PLAYABLE_VIDEO or (audio and audio not in PLAYABLE_AUDIO):
        return None
    container = (media.get("container") or "").lower()
    return "direct" if container in PLAYABLE_CONTAINERS else "remux"


class Downloads:
    def __init__(self, hub: Any, root: Path):
        self.hub = hub
        self.root = root
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(MAX_JOBS)

    # -- jobs ---------------------------------------------------------------
    async def start(self, item_id: str, quality: str, media: dict[str, Any], item: dict[str, Any],
                    device_id: str, person_id: str | None) -> Job:
        # One job per device, title and quality: asking again returns the same one.
        for j in self.jobs.values():
            if (j.device_id, j.item_id, j.quality) == (device_id, item_id, quality) and j.status != "failed":
                return j
        job = Job(id=secrets.token_hex(8), item_id=item_id, quality=quality, device_id=device_id,
                  person_id=person_id, item=item)
        self.jobs[job.id] = job
        mode = original_mode(media) if quality == "original" else "transcode"
        if mode == "direct":
            job.passthrough = media["part_key"]
            job.bytes = media.get("size")
            job.status, job.progress = "ready", 1.0
            return job
        if mode is None:
            job.status, job.error = "failed", "An iPhone can't play the original file. Pick Good or Small."
            return job
        job.task = asyncio.create_task(self._prepare(job, media, mode))
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def _prepare(self, job: Job, media: dict[str, Any], mode: str) -> None:
        async with self._slots:
            try:
                await self._run_ffmpeg(job, media, mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("download %s failed: %s", job.id, exc)
                job.status, job.error = "failed", "The hub couldn't prepare this title. Try another quality."
                if job.path:
                    job.path.unlink(missing_ok=True)

    def _input_for(self, job: Job, media: dict[str, Any], mode: str) -> tuple[str, list[str]]:
        plex = self.hub.plex
        base = str(plex.cfg.url).rstrip("/")
        token = plex.secret(plex.cfg.token_secret) or ""
        headers = f"X-Plex-Token: {token}\r\nX-Plex-Client-Identifier: thomascreek-hub-dl\r\nX-Plex-Product: Thomas Creek Hub\r\n"
        if mode == "remux":
            return f"{base}{media['part_key']}", ["-headers", headers]
        spec = QUALITIES[job.quality]
        from urllib.parse import urlencode

        q = urlencode({
            "path": f"/library/metadata/{job.item_id}", "protocol": "hls", "directPlay": 0, "directStream": 1,
            "videoResolution": spec["res"], "maxVideoBitrate": spec["kbps"], "mediaIndex": 0, "fastSeek": 1,
            "session": f"tc-dl-{job.id}", "X-Plex-Platform": "iOS", "X-Plex-Session-Identifier": f"tc-dl-{job.id}",
        })
        return f"{base}/video/:/transcode/universal/start.m3u8?{q}", ["-headers", headers]

    async def _run_ffmpeg(self, job: Job, media: dict[str, Any], mode: str) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f"{job.id}.part.mp4"
        final = self.root / f"{job.id}.mp4"
        job.path = tmp
        src, extra = self._input_for(job, media, mode)
        args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *extra, "-i", src,
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn",
                "-movflags", "+faststart", "-tag:v", "hvc1" if (media.get("video_codec") or "").lower() in ("hevc", "h265") and mode == "remux" else "avc1",
                "-progress", "pipe:1", "-y", str(tmp)]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        dur_us = (media.get("duration_ms") or 0) * 1000
        assert proc.stdout is not None
        async for line in proc.stdout:
            k, _, v = line.decode(errors="ignore").strip().partition("=")
            if k == "out_time_us" and dur_us and v.isdigit():
                job.progress = min(0.99, int(v) / dur_us)
        err = (await proc.stderr.read()).decode(errors="ignore") if proc.stderr else ""
        if await proc.wait() != 0 or not tmp.exists():
            raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else "ffmpeg failed")
        os.replace(tmp, final)
        job.path = final
        job.bytes = final.stat().st_size
        job.status, job.progress = "ready", 1.0

    # -- housekeeping --------------------------------------------------------
    def cleanup(self, now: float | None = None) -> int:
        now = now or time.time()
        gone = 0
        for jid, j in list(self.jobs.items()):
            if now - j.created_at > KEEP_S and j.status != "preparing":
                if j.path:
                    j.path.unlink(missing_ok=True)
                del self.jobs[jid]
                gone += 1
        if self.root.exists():
            known = {j.path for j in self.jobs.values() if j.path}
            for f in self.root.iterdir():
                if f not in known and now - f.stat().st_mtime > KEEP_S:
                    f.unlink(missing_ok=True)
        return gone
