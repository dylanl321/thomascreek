"""What the cloud worker asks ffmpeg for, and the playlists it hands out. No outside packages: the hub's test suite
imports this file directly (server/tests/test_cloud_worker.py)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rung:
    height: int
    video_kbps: int
    max_kbps: int
    buffer_kbps: int
    audio_kbps: int = 96

    @property
    def bandwidth(self) -> int:
        """Peak bits per second for the HLS master list (video peak plus audio, with room for the container)."""
        return int((self.max_kbps + self.audio_kbps) * 1000 * 1.1)

    @property
    def average(self) -> int:
        return int((self.video_kbps + self.audio_kbps) * 1000 * 1.1)


RUNGS = {
    1080: Rung(1080, 4500, 5000, 9000, 128),
    720: Rung(720, 2500, 2800, 5000),
    480: Rung(480, 1200, 1400, 2500),
    360: Rung(360, 700, 800, 1400, 80),
    240: Rung(240, 400, 450, 800, 64),
}
SEGMENT_S = 4
URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)


def parse(text: str) -> list[Rung]:
    """`LADDER=720,360` -> those rungs, biggest first. Unknown sizes are dropped; nothing left means 720 and 360."""
    out = []
    for part in (text or "").split(","):
        part = part.strip().lower().removesuffix("p")
        if part.isdigit() and int(part) in RUNGS and RUNGS[int(part)] not in out:
            out.append(RUNGS[int(part)])
    return sorted(out, key=lambda r: -r.height) or [RUNGS[720], RUNGS[360]]


def session_key(token: str, start_s: int) -> str:
    """A name for one stream's folder and addresses. It never holds the pass itself."""
    return hashlib.sha256(f"{token}|{start_s}".encode()).hexdigest()[:24]


def redact(text: str) -> str:
    """ffmpeg's messages can quote its input address, which carries the provider login. Never log that."""
    return URL.sub("<address>", text or "")


def ffmpeg_args(ffmpeg: str, src: str, out_dir: str, rungs: list[Rung], *, live: bool, start_s: int = 0,
                threads: int = 2, audio: bool = True, user_agent: str | None = None,
                start_number: int = 0, readrate: float | None = None) -> list[str]:
    """One ffmpeg run that reads the stream once and writes every size as HLS, segments cut at the same moments
    so a player can step between sizes.

    A film starts at segment `start_number` (`start_s` is that segment's second), so the worker can jump to where
    the player asks inside the full-length list it hands out (`film_playlist`). ffmpeg's own list for a film is
    `ff.m3u8`, unused; segments appear under their final name only once complete (`temp_file`)."""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if src.startswith(("http://", "https://")):
        args += ["-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_on_network_error", "1",
                 "-reconnect_delay_max", "4"]
        if live:
            args += ["-reconnect_streamed", "1"]
        if user_agent:
            args += ["-user_agent", user_agent]
    if readrate and not live:
        args += ["-readrate", str(readrate)]
    if start_s > 0 and not live:
        args += ["-ss", str(int(start_s))]
    args += ["-i", src]
    n = len(rungs)
    split = f"[0:v:0]split={n}" + "".join(f"[v{i}]" for i in range(n))
    scales = ";".join(f"[v{i}]scale=-2:'min({r.height},ih)',format=yuv420p[o{i}]" for i, r in enumerate(rungs))
    args += ["-filter_complex", f"{split};{scales}"]
    for i in range(n):
        args += ["-map", f"[o{i}]"]
        if audio:
            args += ["-map", "0:a:0"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-sc_threshold", "0",
             "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_S})", "-threads", str(max(1, threads))]
    for i, r in enumerate(rungs):
        args += [f"-b:v:{i}", f"{r.video_kbps}k", f"-maxrate:v:{i}", f"{r.max_kbps}k",
                 f"-bufsize:v:{i}", f"{r.buffer_kbps}k"]
    if audio:
        args += ["-c:a", "aac", "-ac", "2", "-ar", "48000"]
        for i, r in enumerate(rungs):
            args += [f"-b:a:{i}", f"{r.audio_kbps}k"]
    args += ["-f", "hls", "-hls_time", str(SEGMENT_S), "-hls_segment_type", "mpegts",
             "-hls_segment_filename", f"{out_dir}/%v/seg%05d.ts"]
    if live:
        args += ["-hls_list_size", "8", "-hls_flags", "delete_segments+independent_segments+temp_file"]
    else:
        args += ["-hls_list_size", "0", "-start_number", str(int(start_number)),
                 "-hls_flags", "independent_segments+temp_file"]
    streams = " ".join(f"v:{i},a:{i}" if audio else f"v:{i}" for i in range(n))
    args += ["-var_stream_map", streams, f"{out_dir}/%v/{'index' if live else 'ff'}.m3u8"]
    return args


def film_playlist(duration_s: float) -> str:
    """A film's whole list up front (VOD, every segment named), so a player resumes and seeks as on any film;
    the worker makes each segment when it is asked for (see worker.Film)."""
    count = max(1, int(-(-float(duration_s) // SEGMENT_S)))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEGMENT_S}", "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for n in range(count):
        length = min(SEGMENT_S, float(duration_s) - n * SEGMENT_S) if n == count - 1 else SEGMENT_S
        lines += [f"#EXTINF:{max(0.1, length):.3f},", f"seg{n:05d}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def segment_number(name: str) -> int | None:
    m = re.match(r"^seg(\d{5})\.ts$", name)
    return int(m.group(1)) if m else None


def master_playlist(key: str, rungs: list[Rung], *, audio: bool = True) -> str:
    """The list a player opens first. The smallest size comes first, so a player on a slow connection starts
    there and steps up as the connection allows."""
    codecs = "avc1.4d401f,mp4a.40.2" if audio else "avc1.4d401f"
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for i, r in sorted(enumerate(rungs), key=lambda pair: pair[1].height):
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={r.bandwidth},AVERAGE-BANDWIDTH={r.average},CODECS="{codecs}"')
        lines.append(f"/s/{key}/{i}/index.m3u8")
    return "\n".join(lines) + "\n"


def segments_in(playlist: str) -> int:
    return sum(1 for line in (playlist or "").splitlines() if line and not line.startswith("#"))


SAFE_NAME = re.compile(r"^(index\.m3u8|seg\d{5}\.ts)$")
