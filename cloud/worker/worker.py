"""Thomas Creek cloud media worker: lighter streams for slow connections (hub/cloudmedia.py has the whole story).

It runs on the house's cloud server, reaches the hub over the house's private network, and serves phones on its
public address:

* `GET /v/<pass>/index.m3u8`: the lighter stream for a hub playback pass. The first ask opens the stream (the hub
  hands this worker the provider address for the login leased to that phone). A live channel answers once every
  size has its first segment; a film answers with its whole list as soon as its length is known, so the player
  resumes and seeks as usual.
* `GET /s/<key>/<n>/index.m3u8` and `.../segNNNNN.ts`: the sizes and their segments.
* `GET /health`.

Settings (environment): HUB_URL (the hub on the private network), PUBLIC_URL (this worker's https address),
LADDER (sizes, default `720,360`), MAX_SESSIONS (default 6), THREADS_PER_SESSION (default 2), STATE_DIR (default
/data, keeps the worker's secret and the hub's certificate print), WORK_DIR (default /tmp/tc-media).

Nothing here logs a pass, a provider address or a login.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import ssl
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, Fingerprint, web

import ladder

VERSION = "1.0"
log = logging.getLogger("worker")

HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/data"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/tc-media"))
RUNGS = ladder.parse(os.environ.get("LADDER", "720,360"))
CAPACITY = max(1, int(os.environ.get("MAX_SESSIONS", "6")))
THREADS = max(1, int(os.environ.get("THREADS_PER_SESSION", "2")))
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
IDLE_LIVE_S = 40
IDLE_VOD_S = 15 * 60
# A film is read up to this many times faster than it plays: well ahead of the viewer, without one film taking
# the whole server.
FILM_READRATE = float(os.environ.get("FILM_READRATE", "4"))
READY_S = 25
TOUCH_S = int(os.environ.get("TOUCH_S", "20"))
BEAT_S = 30
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type"}


class HubLink:
    """The worker's side of joining and talking to the hub. The hub's certificate is pinned on first contact."""

    def __init__(self) -> None:
        self.secret: str | None = None
        self.print: str | None = None
        self.http: ClientSession | None = None
        self.approved = asyncio.Event()

    @property
    def _state_file(self) -> Path:
        return STATE_DIR / "worker.json"

    def load(self) -> None:
        try:
            data = json.loads(self._state_file.read_text())
            self.secret, self.print = data.get("secret"), data.get("hub_print")
        except (OSError, ValueError):
            self.secret, self.print = None, None

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"secret": self.secret, "hub_print": self.print}))
        os.chmod(tmp, 0o600)
        tmp.replace(self._state_file)

    def _hub_print(self) -> str:
        parts = urlsplit(HUB_URL)
        pem = ssl.get_server_certificate((parts.hostname or "", parts.port or 443), timeout=10)
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()

    def _ssl(self) -> Any:
        if not HUB_URL.startswith("https://"):
            return None
        return Fingerprint(bytes.fromhex(self.print)) if self.print else False

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None,
                   *, signed: bool = True) -> tuple[int, dict[str, Any]]:
        assert self.http is not None
        headers = {"x-tc-worker": self.secret} if signed and self.secret else {}
        try:
            async with self.http.request(method, HUB_URL + path, json=body, headers=headers, ssl=self._ssl()) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = {}
                return r.status, data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 - every failure is a short line and a retry
            return 0, {"error": "unreachable", "message": type(exc).__name__}

    async def join(self) -> None:
        """Make a secret, send its hash with a request, and wait for Dylan to approve the code on the hub page."""
        while True:
            if not self.secret:
                self.secret = secrets.token_urlsafe(32)
            if HUB_URL.startswith("https://") and not self.print:
                try:
                    self.print = await asyncio.to_thread(self._hub_print)
                except OSError as exc:
                    log.warning("can't reach the hub yet (%s); trying again in 30 s", type(exc).__name__)
                    await asyncio.sleep(30)
                    continue
            status, body = await self.call("POST", "/v1/cloud/join", {
                "secret_hash": hashlib.sha256(self.secret.encode()).hexdigest(),
                "name": os.environ.get("WORKER_NAME", "Cloud server")}, signed=False)
            if status == 200 and body.get("status") == "approved":
                self.save()
                self.approved.set()
                log.info("joined the hub")
                return
            if status != 200:
                log.warning("join refused (%s %s); trying again in 30 s", status, body.get("error"))
                await asyncio.sleep(30)
                continue
            self.save()
            join_id, code = body.get("join_id"), body.get("code")
            log.warning("JOIN CODE %s: approve it on the hub web page, Watching, Cloud media", code)
            while True:
                await asyncio.sleep(5)
                status, state = await self.call("GET", f"/v1/cloud/join/{join_id}", signed=False)
                if state.get("status") == "approved":
                    self.approved.set()
                    log.info("approved: joined the hub")
                    return
                if state.get("status") == "gone":
                    log.warning("the join request ended; asking again")
                    break

    async def beat_forever(self, sessions: Sessions) -> None:
        while True:
            await self.approved.wait()
            load = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
            status, body = await self.call("POST", "/v1/cloud/beat", {
                "version": VERSION, "public_url": PUBLIC_URL, "sessions": len(sessions.live()),
                "capacity": CAPACITY, "load": round(load, 2)})
            if status == 401:
                log.warning("the hub no longer knows this worker; asking to join again")
                self.approved.clear()
                self.secret = None
                self.save()
                await self.join()
                continue
            if body.get("note"):
                log.warning("%s", body["note"])
            await asyncio.sleep(BEAT_S)


class Stream:
    """One phone's lighter stream: ffmpeg writing every size into its own folder.

    A live channel runs one ffmpeg from the moment it is opened, with a short rolling list. A film or an episode
    gets its whole list up front (`ladder.film_playlist`, from the length ffprobe reads), and ffmpeg starts at the
    segment the player asks for; a jump far ahead or back starts it again there. Either way the provider is read
    once, on the login the phone holds."""

    def __init__(self, key: str, token: str) -> None:
        self.key, self.token = key, token
        self.dir = WORK_DIR / key
        self.live = False
        self.audio = True
        self.src: str | None = None
        self.duration: float | None = None
        self.start_seg = 0
        self.proc: asyncio.subprocess.Process | None = None
        self.seen = time.monotonic()
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self.errors: deque[str] = deque(maxlen=12)
        self.tasks: list[asyncio.Task[Any]] = []
        self.lock = asyncio.Lock()
        self.hub: HubLink | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def open(self, hub: HubLink) -> None:
        self.hub = hub
        status, body = await hub.call("POST", "/v1/cloud/open", {"pass": self.token})
        if status != 200:
            self.failed = str(body.get("message") or "The hub didn't open this stream")
            self.ready.set()
            return
        self.live = bool(body.get("live"))
        sources = [str(s) for s in body.get("sources") or []]
        if body.get("relay"):
            sources.append(HUB_URL + str(body["relay"]))  # through the hub, when the provider turns this server away
        if self.live:
            await self._open_live(sources)
        else:
            await self._open_film(sources)
        self.ready.set()

    async def _open_live(self, sources: list[str]) -> None:
        for n, src in enumerate(sources):
            for audio in (True, False):
                self.audio = audio
                if await self._start(src, 0):
                    log.info("stream %s: live on source %d (%d sizes%s)", self.key[:8], n, len(RUNGS),
                             "" if audio else ", no sound")
                    return
                if not any("matches no streams" in e or "Stream map" in e for e in self.errors):
                    break  # it wasn't the missing sound: the next source
        log.warning("stream %s: no source played (%s)", self.key[:8], " | ".join(list(self.errors)[-2:]))
        self.failed = "This channel didn't start on the cloud server"

    async def _open_film(self, sources: list[str]) -> None:
        for n, src in enumerate(sources):
            found = await probe(src)
            if found is not None:
                self.src, (self.duration, self.audio) = src, found
                log.info("stream %s: film on source %d, %d min%s", self.key[:8], n, int(self.duration // 60),
                         "" if self.audio else ", no sound")
                return
        self.failed = "This title didn't open on the cloud server"
        log.warning("stream %s: no source answered", self.key[:8])

    async def _start(self, src: str, segment: int) -> bool:
        """Start ffmpeg; for a live channel wait for the first segment of every size."""
        await self.stop_proc()
        if self.live or segment == 0:
            shutil.rmtree(self.dir, ignore_errors=True)
        for i in range(len(RUNGS)):
            (self.dir / str(i)).mkdir(parents=True, exist_ok=True)
        self.errors.clear()
        self.start_seg = segment
        args = ladder.ffmpeg_args(FFMPEG, src, str(self.dir), RUNGS, live=self.live,
                                  start_s=segment * ladder.SEGMENT_S, start_number=segment, threads=THREADS,
                                  audio=self.audio, user_agent=os.environ.get("USER_AGENT"),
                                  readrate=None if self.live else FILM_READRATE)
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE)
        self.tasks.append(asyncio.create_task(self._read_errors(self.proc)))
        self.tasks.append(asyncio.create_task(self._keep()))
        if not self.live:
            return True
        deadline = time.monotonic() + READY_S
        while time.monotonic() < deadline:
            if all(ladder.segments_in(_read(self.dir / str(i) / "index.m3u8")) >= 1 for i in range(len(RUNGS))):
                return True
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.25)
        await self.stop_proc()
        return False

    def _head(self) -> int:
        """The newest segment of the smallest size written since ffmpeg last started."""
        last = len(RUNGS) - 1
        n = self.start_seg
        while (self.dir / str(last) / f"seg{n:05d}.ts").exists():
            n += 1
        return n - 1

    async def segment(self, rung: int, number: int) -> Path | None:
        """A film's segment: made already, coming soon from the running ffmpeg, or ffmpeg moves there."""
        path = self.dir / str(rung) / f"seg{number:05d}.ts"
        if path.exists():
            return path
        async with self.lock:
            if not path.exists():
                near = self.running() and self.start_seg <= number <= self._head() + 3
                if not near and self.src:
                    log.info("stream %s: jumping to %d:%02d", self.key[:8], number * ladder.SEGMENT_S // 60,
                             number * ladder.SEGMENT_S % 60)
                    await self._start(self.src, number)
        deadline = time.monotonic() + READY_S
        while time.monotonic() < deadline:
            if path.exists():
                return path
            if not self.running():
                break
            await asyncio.sleep(0.2)
        return path if path.exists() else None

    async def _read_errors(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = ladder.redact(raw.decode(errors="replace").strip())
            if line:
                self.errors.append(line[:300])

    async def _keep(self) -> None:
        """Keep the phone's lease while ffmpeg still reads the provider; stop when the hub says it moved on."""
        proc = self.proc
        while proc is not None and proc.returncode is None:
            await asyncio.sleep(TOUCH_S)
            if proc.returncode is not None or self.hub is None:
                break
            status, _body = await self.hub.call("POST", "/v1/cloud/touch", {"pass": self.token})
            if status in (401, 409):
                log.info("stream %s: the hub moved the login on; stopping", self.key[:8])
                self.failed = "This stream moved on. Play it again."
                await self.stop_proc()
                break

    async def stop_proc(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    async def close(self) -> None:
        await self.stop_proc()
        for task in self.tasks:
            task.cancel()
        shutil.rmtree(self.dir, ignore_errors=True)

    def idle_for(self) -> float:
        return time.monotonic() - self.seen


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


async def probe(src: str) -> tuple[float, bool] | None:
    """A film's length and whether it has sound, or None when this address gives nothing."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    args = [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json"]
    if src.startswith(("http://", "https://")):
        args += ["-rw_timeout", "15000000"]
    proc = await asyncio.create_subprocess_exec(*args, src, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    try:
        data = json.loads(out or b"{}")
        duration = float(data.get("format", {}).get("duration") or 0)
    except (ValueError, TypeError):
        return None
    kinds = {s.get("codec_type") for s in data.get("streams") or [] if isinstance(s, dict)}
    if duration <= 0 or "video" not in kinds:
        return None
    return duration, "audio" in kinds


class Sessions:
    def __init__(self, hub: HubLink) -> None:
        self.hub = hub
        self.by_key: dict[str, Stream] = {}

    def live(self) -> list[Stream]:
        return [s for s in self.by_key.values() if s.running()]

    async def get(self, token: str) -> Stream:
        key = ladder.session_key(token, 0)
        found = self.by_key.get(key)
        if found is not None and found.failed:
            await found.close()
            self.by_key.pop(key, None)
            found = None
        if found is None:
            if len(self.live()) >= CAPACITY:
                raise web.HTTPServiceUnavailable(text=json.dumps({
                    "error": "full", "message": "The lighter stream is full right now. Play the full picture."}),
                    content_type="application/json", headers=CORS)
            found = Stream(key, token)
            self.by_key[key] = found
            found.tasks.append(asyncio.create_task(found.open(self.hub)))
        found.seen = time.monotonic()
        return found

    async def reap_forever(self) -> None:
        while True:
            await asyncio.sleep(5)
            for key, s in list(self.by_key.items()):
                limit = IDLE_LIVE_S if s.live else IDLE_VOD_S
                if s.idle_for() > limit or (s.failed and s.idle_for() > 30):
                    log.info("stream %s closed after %d s without a viewer", key[:8], int(s.idle_for()))
                    self.by_key.pop(key, None)
                    await s.close()
                elif s.live and not s.running() and s.ready.is_set() and not s.failed:
                    # A live run that ended on its own (the provider dropped it): the next ask starts it again.
                    self.by_key.pop(key, None)
                    await s.close()

    async def close_all(self) -> None:
        for s in list(self.by_key.values()):
            await s.close()
        self.by_key.clear()


async def master(request: web.Request) -> web.StreamResponse:
    sessions: Sessions = request.app["sessions"]
    token = request.match_info["token"]
    if len(token) > 2000 or not token.replace(".", "").replace("-", "").replace("_", "").isalnum():
        raise web.HTTPNotFound(headers=CORS)
    stream = await sessions.get(token)
    try:
        await asyncio.wait_for(stream.ready.wait(), READY_S + 10)
    except asyncio.TimeoutError:
        raise web.HTTPGatewayTimeout(text=json.dumps({"error": "slow", "message": "The lighter stream took too "
                                                     "long to start. Play the full picture."}),
                                     content_type="application/json", headers=CORS) from None
    if stream.failed:
        raise web.HTTPBadGateway(text=json.dumps({"error": "unavailable", "message": stream.failed}),
                                 content_type="application/json", headers=CORS)
    body = ladder.master_playlist(stream.key, RUNGS, audio=stream.audio)
    return web.Response(text=body, content_type="application/vnd.apple.mpegurl",
                        headers={**CORS, "Cache-Control": "no-cache"})


async def part(request: web.Request) -> web.StreamResponse:
    sessions: Sessions = request.app["sessions"]
    key, rung, name = request.match_info["key"], request.match_info["rung"], request.match_info["name"]
    stream = sessions.by_key.get(key)
    if stream is None or not rung.isdigit() or int(rung) >= len(RUNGS) or not ladder.SAFE_NAME.match(name):
        raise web.HTTPNotFound(headers=CORS)
    stream.seen = time.monotonic()
    if name.endswith(".m3u8"):
        if stream.live:
            text = _read(stream.dir / rung / name)
        else:
            text = ladder.film_playlist(stream.duration or 0) if stream.duration else ""
        if not text:
            raise web.HTTPNotFound(headers=CORS)
        return web.Response(text=text, content_type="application/vnd.apple.mpegurl",
                            headers={**CORS, "Cache-Control": "no-cache"})
    number = ladder.segment_number(name)
    if stream.live:
        path: Path | None = stream.dir / rung / name
    elif number is None:
        path = None
    else:
        path = await stream.segment(int(rung), number)
    if path is None or not path.is_file():
        raise web.HTTPNotFound(headers=CORS)
    return web.FileResponse(path, headers={**CORS, "Content-Type": "video/mp2t", "Cache-Control": "max-age=3600"})


async def health(request: web.Request) -> web.Response:
    sessions: Sessions = request.app["sessions"]
    hub: HubLink = request.app["hub"]
    return web.json_response({"ok": True, "joined": hub.approved.is_set(), "sessions": len(sessions.live()),
                              "capacity": CAPACITY, "version": VERSION})


async def options(_request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS)


async def on_start(app: web.Application) -> None:
    hub: HubLink = app["hub"]
    hub.http = ClientSession(timeout=ClientTimeout(total=20))
    hub.load()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for old in WORK_DIR.iterdir():
        shutil.rmtree(old, ignore_errors=True)
    if hub.secret:
        hub.approved.set()  # the first beat says whether the hub still knows it
    else:
        app["join"] = asyncio.create_task(hub.join())
    app["beat"] = asyncio.create_task(hub.beat_forever(app["sessions"]))
    app["reap"] = asyncio.create_task(app["sessions"].reap_forever())


async def on_stop(app: web.Application) -> None:
    for name in ("join", "beat", "reap"):
        task = app.get(name)
        if task is not None:
            task.cancel()
    await app["sessions"].close_all()
    hub: HubLink = app["hub"]
    if hub.http is not None:
        await hub.http.close()


def make_app() -> web.Application:
    hub = HubLink()
    app = web.Application()
    app["hub"] = hub
    app["sessions"] = Sessions(hub)
    app.router.add_get("/health", health)
    app.router.add_get("/v/{token}/index.m3u8", master)
    app.router.add_get("/s/{key}/{rung}/{name}", part)
    app.router.add_route("OPTIONS", "/{tail:.*}", options)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)  # access lines would carry passes
    if not HUB_URL:
        raise SystemExit("Set HUB_URL to the hub's address on the private network")
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=None)
