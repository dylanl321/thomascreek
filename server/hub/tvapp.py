"""Thomas Creek TV: the household's IPTV app (a fork of IPVIO Stream) kept installed and current on the Android TV boxes.

Where builds come from
  CI in github.com/dylanl321/thomascreek-tv publishes a GitHub release for every `tc-*` tag with two files:
  ThomasCreekTV.apk and update.json ({"versionCode", "versionName", "apk", "sha256", "notes"}). The repo is
  private, so the hub reads it with a fine-grained GitHub token saved in the vault as `github.tv_app_token`
  (read-only "Contents" access to that one repo).

What the hub checks before keeping a build
  1. the APK's SHA-256 matches update.json (catches a damaged download);
  2. the APK's signing certificate (APK Signature Scheme v2/v3 block) matches the pinned one. The first verified
     build pins it (kv "tvapp.signer"); a later build with a different certificate is refused and Dylan is told.
     Android itself checks the signature on install and refuses an update signed by another key, so the pin is
     about not *offering* a foreign build, not about replacing Android's check.

How it reaches the TVs
  Over ADB on the network (port 5555). Each box is added once by an admin: turn on Developer options and
  Network/Wireless debugging on the box, add it here, then press "Allow" on the TV the first time the hub connects
  (tick "Always allow from this computer"). The hub's ADB key lives under the data dir (adb/.android/adbkey).
  A box with auto-update on gets a new build within one check interval, but never while the app is on screen.

How a TV gets its source (no typing on a remote)
  On first start the app makes a P-256 key in the Android keystore and calls POST /v1/tv/enroll from the home
  network with an install id, its name and the public key. A TV whose IP is a box an admin already added is
  approved at once with that box's profile; any other TV waits for an admin (the hub sends a notice). The app then
  calls POST /v1/tv/config with a signature over "tvconfig:<install id>:<unix time>" and gets the IPTV source
  (vault keys iptv.*), the guide address and its profile (adult or kids, with the kids' category list). Changing a
  source on the hub reaches every TV on its next config call. Revoking a TV stops that at once.

  POST /v1/tv/token is the same kind of signature ("tvtoken:<id>:<unix time>" or with ":<person id>") and returns
  the same short-lived bearer the phones use. That bearer is limited to the TV's room and profile: a kids' TV is a
  child, an adult TV is room-scoped everyday control, and neither can ever lock, open the garage or view a camera.
  The bearer then calls the existing home, action and event routes. Revoking the TV rejects the bearer on the
  next request.

Nothing is installed on a box that an admin hasn't added. Downgrades aren't possible without losing the app's data
(Android only allows them for debuggable apps), so there is no automatic rollback: the previous APK is kept so a
fixed build can be re-tagged quickly.

Code layout
  TvApp (this file) holds the TV's state reports, the command queue and the background loop. Its other jobs are
  mixins: builds and boxes (tvapp_builds), enrolment and tokens (tvapp_enrol), source, config and lineup
  (tvapp_config). Shared errors, ADB, APK signatures and helpers are in tvapp_common and re-exported here.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .db import new_id
from .home import iso
from .tvlineup import valid_key
from .upnext import remember
from .tvapp_common import (  # noqa: F401  (re-exported: other modules import these from here)
    APK_NAME,
    Adb,
    AdbResult,
    COMMAND_TTL_S,
    CONFIG_SKEW_S,
    GITHUB,
    INSTALL_TTL_S,
    IPTV_SECRETS,
    KV_BOXES,
    KV_BUILDS,
    KV_COMMANDS,
    KV_CONFIG_VERSION,
    KV_INSTALL,
    KV_KIDS,
    KV_LATEST,
    KV_LINEUP,
    KV_SIGNER,
    KV_TVS,
    MAX_APK,
    MAX_CHANNELS,
    MAX_COMMANDS,
    MAX_TVS,
    METADATA_KEYS,
    PROFILES,
    Runner,
    SHUFFLE_MAX,
    TOKEN_SECRET,
    TV_KEEP,
    TvAppError,
    WATCHING_CLAIM_S,
    _CODE_ALPHABET,
    _COMMAND_FIELDS,
    _MAGIC,
    _MEDIA,
    _PERSON_ID,
    _ROOM,
    _SIG_V2,
    _SIG_V3,
    _SIG_V31,
    _UNSET,
    _URL,
    _clip,
    _command_result,
    _favorite_channels,
    _household,
    _lp,
    _person_layers,
    _play_id,
    _play_shuffle,
    _play_start,
    _position,
    _public_command,
    _redact,
    _u32,
    _update_reports,
    clean_ip,
    log,
    signer_certificates,
    token_message,
)
from .tvapp_builds import BuildsMixin
from .tvapp_config import ConfigMixin
from .tvapp_enrol import EnrolMixin


class TvApp(BuildsMixin, EnrolMixin, ConfigMixin):
    def __init__(self, cfg: Any, vault: Any, db: Any, data: Path, *, adb: Adb | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 notify: Callable[[str, str], Awaitable[None]] | None = None):
        self.cfg = cfg
        self.vault = vault
        self.db = db
        self.dir = data / "tvapp"
        self.adb = adb or Adb(data / "adb")
        self._transport = transport
        self.notify = notify
        # The whole hub config, for IPTV providers. state.py sets it; tests may leave it None.
        self.hub_config: Any = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._waiters: dict[str, list[asyncio.Event]] = {}
        self.last_error: str | None = None
        self.last_check: float | None = None
        # Called with (install_id or None, version) after each config bump. state.py sends `config_changed`.
        self.on_config: Callable[[str | None, int], None] | None = None

    def record_state(self, install_id: str, body: dict[str, Any]) -> dict[str, Any]:
        playing = self._playing(body.get("playing"))
        health = self._health(body.get("health"))
        foreground = bool(body.get("foreground", True))
        now = time.time()
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv is None or tv.get("status") != "approved":
            raise TvAppError(401, "revoked", "This TV was removed on the hub")
        tv.update(playing=playing, foreground=foreground, health=health, state_at=now, last_seen_at=now)
        # Who this is playing for: a person the TV or a phone named on purpose, never a guess. The Now playing
        # alert goes to that person only (hub/nowplaying_live.py).
        watcher = self._watcher(body.get("person_id"), tv) if playing else None
        tv["watcher"] = watcher
        if health.get("app_version"):
            tv["app_version"] = health["app_version"]
        command = _command_result(body)
        if command is not None:
            tv["last_command"] = command
        tvs[install_id] = tv
        self._save_tvs(tvs)
        self.db.log("tvapp.state", "ok", device_id=f"tv:{install_id}", target=install_id,
                    params={"playing": (playing or {}).get("channel_key"), "foreground": foreground,
                            "command": None if command is None else command.get("id")}, source="tv")
        if playing:
            remember(self.db, "kids" if tv.get("profile") == "kids" else "adult", playing, now,
                     person=watcher, tv=install_id)
        event = self.watch_event(install_id)
        if command is not None:
            event["command"] = command
        return event

    def watch_event(self, install_id: str) -> dict[str, Any]:
        tv = self.tvs().get(install_id) or {}
        return {
            "id": install_id,
            "name": tv.get("name"),
            "room": tv.get("room"),
            "profile": tv.get("profile"),
            "media_entity": tv.get("media_entity"),
            "foreground": bool(tv.get("foreground", True)),
            "playing": tv.get("playing"),
            "at": iso(tv.get("state_at")),
        }

    def _watcher(self, raw: object, tv: dict[str, Any] | None = None) -> str | None:
        """A household person the TV says is watching. Anything else is ignored, never an error.

        With no person in the heartbeat, a claim made on purpose (the TV's picker, "I'm watching" on a phone, or
        a phone that sent the show) still counts, so older TV builds follow the person too. A guess from who is
        home does not. A kids' TV only ever records a child."""
        kids = (tv or {}).get("profile") == "kids"
        if not isinstance(raw, str) or not _PERSON_ID.fullmatch(raw):
            claim = (tv or {}).get("watching")
            if not isinstance(claim, dict) or not claim.get("person_id") or claim.get("by") not in ("tv", "phone", "play"):
                return None
            if time.time() - float(claim.get("at") or 0) > WATCHING_CLAIM_S:
                return None
            raw = str(claim["person_id"])
        person = next((p for p in _household(self.db) if p["id"] == raw), None)
        if person is None or (kids and person["role"] != "child"):
            return None
        return raw

    def _playing(self, raw: object) -> dict[str, Any] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise TvAppError(400, "bad_request", "playing must be an object")
        key = raw.get("channel_key")
        if key:
            if not valid_key(str(key)):
                raise TvAppError(400, "bad_key", "channel_key must be an iptv-org, Xtream or tvg id")
            key = str(key)
        else:
            key = None
        number = raw.get("number")
        if number is not None:
            try:
                number = int(number)
            except (TypeError, ValueError):
                raise TvAppError(400, "bad_request", "number must be an integer") from None
            if not 0 <= number <= 99999:
                raise TvAppError(400, "bad_request", "number is out of range")
        title = _clip(raw.get("title"), 120)
        name = _clip(raw.get("name"), 80)
        position = _position(raw.get("position_s")) if "position_s" in raw and raw.get("position_s") is not None else None
        movie = _play_id(raw.get("movie_id"))
        series = _play_id(raw.get("series_id"))
        episode = _play_id(raw.get("episode_id"))
        if key is None and title is None and name is None and number is None and movie is None and episode is None:
            return None
        out: dict[str, Any] = {"channel_key": key, "title": title, "name": name, "number": number}
        if position is not None:
            out["position_s"] = position
        duration = raw.get("duration_s")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 60 <= duration <= 24 * 3600:
            out["duration_s"] = int(duration)
        if movie:
            out["movie_id"] = movie
        if series:
            out["series_id"] = series
        if episode:
            out["episode_id"] = episode
        kind = raw.get("kind")
        if kind in ("live", "catchup", "movie", "episode"):
            out["kind"] = kind
        start = _play_start(raw.get("start"))
        if start is not None:
            out["start"] = start
        shuffle = _play_shuffle(raw.get("shuffle"))
        if shuffle is not None and episode:
            out["shuffle"] = shuffle
        return out

    def _health(self, raw: object) -> dict[str, Any]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise TvAppError(400, "bad_request", "health must be an object")
        out: dict[str, Any] = {}
        version = _clip(raw.get("app_version"), 40)
        if version:
            out["app_version"] = version
        profile = raw.get("profile")
        if profile in PROFILES:
            out["profile"] = profile
        age = raw.get("epg_age_s")
        if age is not None:
            try:
                age_i = int(age)
            except (TypeError, ValueError):
                raise TvAppError(400, "bad_request", "epg_age_s must be a number of seconds") from None
            if age_i >= 0:
                out["epg_age_s"] = min(age_i, 8640000)
        error = _redact(raw.get("last_error"))
        if error:
            out["last_error"] = error
        return out

    def enqueue_command(self, install_id: str, command: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        stored = {"id": new_id("tvc"), "install_id": install_id, "action": command["action"], "at": iso(now), "ts": now}
        for key in _COMMAND_FIELDS:
            if command.get(key) is not None:
                stored[key] = command[key]
        all_commands = self.db.kv_get(KV_COMMANDS) or {}
        queued = [row for row in all_commands.get(install_id, []) if self._command_fresh(row, now)]
        queued.append(stored)
        all_commands[install_id] = queued[-MAX_COMMANDS:]
        self.db.kv_set(KV_COMMANDS, all_commands)
        if stored["action"] == "play":
            self._remember_play(install_id, stored)
        self.kick(install_id)
        return _public_command(stored)

    def _remember_play(self, install_id: str, stored: dict[str, Any]) -> None:
        """What the hub last asked this TV to play, so a title-only heartbeat can still move rooms."""
        tvs = self.tvs()
        tv = tvs.get(install_id)
        if tv is None:
            return
        play = {key: stored[key] for key in _COMMAND_FIELDS if stored.get(key) is not None and key != "resume"}
        tv["last_play"] = play
        tvs[install_id] = tv
        self._save_tvs(tvs)

    def pending_commands(self, install_id: str) -> list[dict[str, Any]]:
        now = time.time()
        all_commands = self.db.kv_get(KV_COMMANDS) or {}
        queued = list(all_commands.get(install_id, []))
        fresh = [row for row in queued if self._command_fresh(row, now)]
        if len(fresh) != len(queued):
            all_commands[install_id] = fresh
            self.db.kv_set(KV_COMMANDS, all_commands)
        return [_public_command(row) for row in fresh]

    def ack_commands(self, install_id: str, ids: list[str]) -> int:
        want = {str(i) for i in ids}
        all_commands = self.db.kv_get(KV_COMMANDS) or {}
        queued = list(all_commands.get(install_id, []))
        kept = [row for row in queued if row.get("id") not in want]
        all_commands[install_id] = kept
        self.db.kv_set(KV_COMMANDS, all_commands)
        return len(queued) - len(kept)

    async def wait_commands(self, install_id: str, timeout: float) -> list[dict[str, Any]]:
        event = asyncio.Event()
        self._waiters.setdefault(install_id, []).append(event)
        try:
            found = self.pending_commands(install_id)
            if found or timeout <= 0:
                return found
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            waiters = self._waiters.get(install_id, [])
            if event in waiters:
                waiters.remove(event)
        return self.pending_commands(install_id)

    def kick(self, install_id: str) -> None:
        for event in self._waiters.get(install_id, []):
            event.set()

    @staticmethod
    def _command_fresh(row: dict[str, Any], now: float) -> bool:
        try:
            return now - float(row.get("ts") or 0) < COMMAND_TTL_S
        except (TypeError, ValueError):
            return False

    # -- background ----------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self.cfg.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                # downloads imports this module, so the inbox is read from inside the loop.
                from .tvbuilds import adopt_inbox

                note = adopt_inbox(self)
                if note:
                    log.info("tvapp inbox: %s", note)
                if self.vault.has(TOKEN_SECRET):
                    await self.check()
                await self.update_all()
                from .tvsetup import reconcile

                await reconcile(self)
            except asyncio.CancelledError:
                raise
            except TvAppError as exc:
                log.info("tvapp check: %s", exc.message)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)[:200]
                log.warning("tvapp loop: %s", exc)
            await asyncio.sleep(max(300, int(self.cfg.check_minutes) * 60))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
