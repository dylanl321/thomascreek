"""Bambu Lab printers (the H2S in the workshop), straight over the home network.

Home Assistant has no printer entities here, so the hub talks to the printer the
way Bambu Studio does in LAN mode:

* MQTT over TLS on port 8883 (user ``bblp``, password = the printer's access
  code). The printer pushes ``device/<serial>/report`` messages that are partial
  updates of one big status object; the hub merges them and asks for a full
  ``pushall`` on connect and every few minutes. Commands go to
  ``device/<serial>/request``.
* Implicit FTPS on port 990 (same login) to put a sliced ``.gcode.3mf`` on the
  printer before ``project_file`` starts it.
* The chamber camera as RTSPS on port 322, turned into JPEG frames by ffmpeg
  only while someone is watching.

Current Bambu firmware only accepts commands from other software when the
printer is in LAN Only mode with Developer Mode switched on. Without that the
printer still reports status (and takes the light), so the hub runs the printer
"watch only" and switches to full control by itself as soon as the printer stops
asking for signed commands (``print.fun`` bit 29, or ``print.flag3`` bit 16 on
firmware that leaves ``fun`` empty).

Two ways in, tried in this order:

1. LAN: host + serial + access code. Works with or without LAN Only mode.
2. Bambu account (``bambu_cloud.py``): when the LAN connection keeps failing or
   no host is known, the same reports come from Bambu's MQTT broker. Always
   watch only. The account also supplies the serial and access code.

Secrets (added on the hub's admin page): ``printer.<id>.access_code``,
``printer.<id>.serial`` and ``printer.<id>.host``, or a Bambu account sign-in.
Host and serial can come from config instead.
"""

from __future__ import annotations

import asyncio
import ftplib
import hashlib
import io
import json
import logging
import re
import shutil
import socket
import ssl
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from xml.etree import ElementTree

from .db import new_id

if TYPE_CHECKING:
    from .config import PrinterEntry, PrintersConfig
    from .state import Hub

log = logging.getLogger("hub.bambu")

MQTT_PORT, FTPS_PORT, RTSP_PORT = 8883, 990, 322
PUSHALL_EVERY_S = 300
STALE_S = 90  # no report for this long = offline
LOCAL_TRIES = 3  # LAN failures in a row before trying the Bambu account
CLOUD_STINT_S = 900  # then try the LAN again after this long
SECURED_FUN = 0x20000000  # print.fun: the printer wants signed commands (no Developer Mode)
SECURED_FLAG3 = 0x10000  # print.flag3: the same, on firmware with an empty fun
WATCH_ONLY = ("{name} is in watch-only mode. For controls, turn on LAN Only and Developer Mode on the printer "
              "(Settings, Network). Until then, use Bambu Handy to pause or stop.")

# gcode_state -> the word the app shows.
STATES = {"IDLE": "idle", "PREPARE": "preparing", "SLICING": "preparing", "RUNNING": "printing",
          "PAUSE": "paused", "FINISH": "done", "FAILED": "failed", "INIT": "idle", "OFFLINE": "offline"}
SPEEDS = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}
SPEED_LEVELS = {v: k for k, v in SPEEDS.items()}
HEATERS = {"nozzle": ("M104", 350), "bed": ("M140", 120), "chamber": ("M141", 65)}
# The printer's "stage" numbers while it gets ready (stg_cur), the common ones only.
STAGES = {1: "Leveling the bed", 2: "Preheating the bed", 3: "Vibration compensation", 4: "Changing filament",
          7: "Heating the nozzle", 8: "Calibrating extrusion", 9: "Scanning the bed", 13: "Homing",
          14: "Cleaning the nozzle", 16: "Paused by you", 17: "Front door open", 18: "Calibrating the micro lidar",
          19: "Calibrating extrusion flow", 20: "Paused: nozzle temperature problem", 21: "Paused: bed temperature problem",
          22: "Unloading filament", 24: "Loading filament", 25: "Calibrating motor noise", 29: "Cooling the chamber"}
MAX_UPLOAD = 300 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()\-]+")


class PrinterError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


# -- reading reports ----------------------------------------------------------
def deep_merge(into: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Reports are partial: merge dicts, replace everything else (lists included)."""
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            deep_merge(into[k], v)
        else:
            into[k] = v
    return into


def _packed(v: Any) -> tuple[float | None, float | None]:
    """H2-series firmware packs a heater as current | target << 16."""
    if not isinstance(v, (int, float)):
        return None, None
    v = int(v)
    return float(v & 0xFFFF), float((v >> 16) & 0xFFFF)


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def color(v: Any) -> str | None:
    """Bambu colours are RRGGBBAA."""
    if not isinstance(v, str) or len(v) < 6 or not re.fullmatch(r"[0-9A-Fa-f]{6,8}", v):
        return None
    return "#" + v[:6].upper()


def temps(p: dict[str, Any], dev: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    out = {
        "nozzle": {"current": _num(p.get("nozzle_temper")), "target": _num(p.get("nozzle_target_temper"))},
        "bed": {"current": _num(p.get("bed_temper")), "target": _num(p.get("bed_target_temper"))},
        "chamber": {"current": _num(p.get("chamber_temper")), "target": None},
    }
    # H2-series: device.extruder.info[i].temp, device.bed.info.temp, device.ctc.info.temp (packed).
    ext = ((dev.get("extruder") or {}).get("info") or [])
    if out["nozzle"]["current"] is None and ext:
        cur, tgt = _packed((ext[0] or {}).get("temp"))
        out["nozzle"] = {"current": cur, "target": tgt}
    bed = (dev.get("bed") or {}).get("info") or {}
    if out["bed"]["current"] is None and "temp" in bed:
        cur, tgt = _packed(bed.get("temp"))
        out["bed"] = {"current": cur, "target": tgt}
    ctc = (dev.get("ctc") or {}).get("info") or {}
    if "temp" in ctc:
        cur, tgt = _packed(ctc.get("temp"))
        out["chamber"] = {"current": cur if out["chamber"]["current"] is None else out["chamber"]["current"],
                          "target": tgt or None}
    return out


def ams_units(p: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    ams = p.get("ams") or {}
    units = []
    for u in ams.get("ams") or []:
        try:
            uid = int(u.get("id", 0))
        except (TypeError, ValueError):
            continue
        trays = []
        for t in u.get("tray") or []:
            try:
                tid = int(t.get("id", 0))
            except (TypeError, ValueError):
                continue
            empty = not t.get("tray_type")
            remain = t.get("remain")
            trays.append({
                "slot": uid * 4 + tid, "index": tid + 1, "empty": empty,
                "color": None if empty else color(t.get("tray_color")),
                "colors": [c for c in (color(x) for x in t.get("cols") or []) if c],
                "material": None if empty else (t.get("tray_type") or None),
                "name": None if empty else (t.get("tray_sub_brands") or t.get("tray_type")),
                "remain": remain if isinstance(remain, int) and 0 <= remain <= 100 else None,
            })
        hum = u.get("humidity_raw") or u.get("humidity")
        units.append({"id": uid, "humidity": _num(hum), "humidity_index": u.get("humidity"),
                      "temp": _num(u.get("temp")), "drying": bool(u.get("dry_time")), "trays": trays})
    now = ams.get("tray_now")
    try:
        active = int(now) if now not in (None, "", "255", 255) else None
    except (TypeError, ValueError):
        active = None
    if active is not None and active >= 254:
        active = None  # 254 = external spool, 255 = none
    return units, active


def light_on(raw: dict[str, Any]) -> bool | None:
    for item in (raw.get("print") or {}).get("lights_report") or []:
        if item.get("node") == "chamber_light":
            return item.get("mode") == "on"
    return None


def status(raw: dict[str, Any], *, connected: bool, last_report: float) -> dict[str, Any]:
    p = raw.get("print") or {}
    dev = p.get("device") or raw.get("device") or {}
    gs = str(p.get("gcode_state") or ("OFFLINE" if not connected else "IDLE")).upper()
    if not connected:
        gs = "OFFLINE"
    state = STATES.get(gs, "idle")
    units, active = ams_units(p)
    pct = p.get("mc_percent")
    left = p.get("mc_remaining_time")
    stage = p.get("stg_cur")
    err = p.get("print_error") or 0
    hms = [h for h in p.get("hms") or [] if isinstance(h, dict)]
    job = None
    if state in ("preparing", "printing", "paused", "done", "failed"):
        job = {
            "name": (p.get("subtask_name") or Path(str(p.get("gcode_file") or "")).stem or "Print").removesuffix(".gcode"),
            "progress": max(0, min(100, int(pct))) if isinstance(pct, (int, float)) else None,
            "minutes_left": int(left) if isinstance(left, (int, float)) and left >= 0 else None,
            "finish_at": time.time() + int(left) * 60 if isinstance(left, (int, float)) and left > 0
            and state == "printing" else None,
            "layer": p.get("layer_num"), "layers": p.get("total_layer_num"),
            "stage": STAGES.get(stage) if isinstance(stage, int) and stage not in (0, 255, -1) else None,
        }
    return {
        "state": state,
        "online": connected and state != "offline",
        "job": job,
        "temps": temps(p, dev),
        "speed": SPEEDS.get(p.get("spd_lvl"), "standard"),
        "light": light_on(raw),
        "fans": {"part": p.get("cooling_fan_speed"), "aux": p.get("big_fan1_speed"), "chamber": p.get("big_fan2_speed")},
        "ams": units,
        "active_slot": active,
        "error": {"code": f"{int(err):08X}", "hms": len(hms)} if err or hms else None,
        "wifi": p.get("wifi_signal"),
        "updated_at": last_report or None,
    }


# -- sliced files ---------------------------------------------------------------
def read_3mf(data: bytes) -> dict[str, Any]:
    """What a sliced .gcode.3mf says about itself: plates, time, grams, colours and a picture."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PrinterError(400, "bad_file", "That isn't a 3MF file") from exc
    names = set(z.namelist())
    plates = sorted(n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
    if not plates:
        raise PrinterError(400, "not_sliced", "That 3MF isn't sliced yet. In Bambu Studio, slice it and use "
                                              "Export plate sliced file, then add that.")
    info: dict[str, Any] = {"plates": [], "printer_model": None}
    if "Metadata/slice_info.config" in names:
        try:
            root = ElementTree.fromstring(z.read("Metadata/slice_info.config"))
            for plate in root.iter("plate"):
                meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
                fil = [{"id": int(f.get("id") or 0), "type": f.get("type"), "color": color((f.get("color") or "").lstrip("#")),
                        "grams": _num(f.get("used_g"))} for f in plate.findall("filament")]
                info["plates"].append({
                    "index": int(meta.get("index") or len(info["plates"]) + 1),
                    "seconds": int(float(meta.get("prediction") or 0)) or None,
                    "grams": _num(meta.get("weight")),
                    "filaments": fil,
                })
                info["printer_model"] = info["printer_model"] or meta.get("printer_model_id")
        except ElementTree.ParseError:
            pass
    if not info["plates"]:
        info["plates"] = [{"index": int(re.search(r"\d+", n).group()), "seconds": None, "grams": None, "filaments": []}  # type: ignore[union-attr]
                          for n in plates]
    pic = next((n for n in ("Metadata/plate_1.png", "Metadata/top_1.png", "Metadata/plate_no_light_1.png") if n in names), None)
    info["thumbnail"] = z.read(pic) if pic else None
    return info


class _ImplicitFTPS(ftplib.FTP_TLS):
    """The printer speaks implicit FTPS and wants the data channel to reuse the TLS session."""

    _sock: ssl.SSLSocket | None = None

    @property
    def sock(self):  # type: ignore[override]
        return self._sock

    @sock.setter
    def sock(self, value):  # type: ignore[override]
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):  # type: ignore[override]
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)  # type: ignore[union-attr]
        return conn, size


def _insecure_ctx() -> ssl.SSLContext:
    # The printer presents a self-signed certificate on the LAN. Only the access code protects it.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def ftps_upload(host: str, code: str, name: str, data: bytes, timeout: float = 60) -> None:
    ftp = _ImplicitFTPS(context=_insecure_ctx(), timeout=timeout)
    try:
        ftp.connect(host, FTPS_PORT)
        ftp.login("bblp", code)
        ftp.prot_p()
        ftp.storbinary(f"STOR {name}", io.BytesIO(data))
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()


# -- one printer ------------------------------------------------------------------
@dataclass
class Printer:
    entry: "PrinterEntry"
    hub: "Hub"
    raw: dict[str, Any] = field(default_factory=dict)
    connected: bool = False
    last_report: float = 0.0
    last_error: str | None = None
    _client: Any = None
    _task: asyncio.Task | None = None
    _seq: int = 0
    _frames: "FrameGrabber | None" = None
    via: str | None = None  # "lan" or "cloud" while connected
    secured: bool | None = None  # True while the printer refuses unsigned commands
    _local_fails: int = 0
    on_change: Callable[["Printer", dict[str, Any], dict[str, Any]], Any] | None = None

    @property
    def id(self) -> str:
        return self.entry.id

    def _secret(self, what: str) -> str | None:
        v = self.hub.vault.get(f"printer.{self.id}.{what}")
        return str(v) if v else None

    @property
    def cloud(self) -> Any:
        c = getattr(self.hub, "bambu_cloud", None)
        return c if c is not None and c.signed_in else None

    def _cloud_device(self) -> dict[str, Any] | None:
        c = self.cloud
        return c.device(self.entry.serial or self._secret("serial"), self.entry.model) if c else None

    @property
    def host(self) -> str | None:
        return self.entry.host or self._secret("host")

    @property
    def serial(self) -> str | None:
        return self.entry.serial or self._secret("serial") or (self._cloud_device() or {}).get("dev_id")

    @property
    def access_code(self) -> str | None:
        return self._secret("access_code") or (self._cloud_device() or {}).get("dev_access_code")

    @property
    def lan_ready(self) -> bool:
        return bool(self.host and self.serial and self.access_code)

    @property
    def cloud_ready(self) -> bool:
        return bool(self.cloud and self.serial)

    def missing(self) -> list[str]:
        if self.cloud_ready:
            return []
        out = []
        if not self.host:
            out.append(f"printer.{self.id}.host")
        if not self.serial:
            out.append(f"printer.{self.id}.serial")
        if not self.access_code:
            out.append(f"printer.{self.id}.access_code")
        return out

    @property
    def control_mode(self) -> str:
        """"full" when the hub may pause, stop, heat and print; "watch" otherwise."""
        return "full" if self.via == "lan" and self.secured is not True else "watch"

    def status(self) -> dict[str, Any]:
        live = self.connected and (time.time() - self.last_report) < STALE_S
        st = status(self.raw, connected=live, last_report=self.last_report)
        ctl = self.control_mode if live else "watch"
        return {"id": self.id, "name": self.entry.name, "model": self.entry.model, "set_up": not self.missing(),
                "missing": self.missing(), "problem": None if live else self.last_error, **st,
                "control": ctl, "via": self.via if live else None,
                "watch_reason": None if ctl == "full" or not live else WATCH_ONLY.format(name=self.entry.name)}

    def summary(self) -> dict[str, Any]:
        s = self.status()
        return {k: s[k] for k in ("id", "name", "model", "state", "online", "set_up")} | {
            "job": s["job"], "color": self.active_color(s)}

    @staticmethod
    def active_color(s: dict[str, Any]) -> str | None:
        slot = s.get("active_slot")
        for u in s.get("ams") or []:
            for t in u["trays"]:
                if t["slot"] == slot:
                    return t["color"]
        return None

    # -- connection ---------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._frames:
            await self._frames.stop()

    async def _run(self) -> None:
        import aiomqtt

        backoff = 5.0
        while True:
            cloud = getattr(self.hub, "bambu_cloud", None)
            if cloud is not None and cloud.signed_in:
                await cloud.refresh_if_needed()
                await cloud.load_devices()
            way = self.pick_way()
            if way is None:
                self.last_error = "Not set up yet"
                await asyncio.sleep(30)
                continue
            try:
                if way == "lan":
                    kw = dict(hostname=self.host, port=MQTT_PORT, username="bblp", password=self.access_code,
                              tls_context=_insecure_ctx(), tls_insecure=True)
                else:
                    from .bambu_cloud import MQTT_HOST

                    kw = dict(hostname=MQTT_HOST, port=MQTT_PORT, username=self.cloud.mqtt_user,
                              password=self.cloud.token, tls_context=ssl.create_default_context())
                async with aiomqtt.Client(**kw, keepalive=30,  # type: ignore[arg-type]
                                          identifier=f"thomascreek-{self.id}-{new_id('m')[-6:]}") as client:
                    self._client = client
                    await client.subscribe(f"device/{self.serial}/report")
                    self.connected, self.via, self.secured = True, way, None
                    self.last_error = None
                    backoff = 5.0
                    if way == "lan":
                        self._local_fails = 0
                    await self.pushall()
                    pusher = asyncio.create_task(self._push_loop())
                    started = time.time()
                    try:
                        async for msg in client.messages:
                            try:
                                self.ingest(json.loads(msg.payload))
                            except (ValueError, TypeError):
                                pass
                            if way == "cloud" and self.lan_ready and time.time() - started > CLOUD_STINT_S:
                                self._local_fails = 0  # go and see whether the LAN works again
                                break
                    finally:
                        pusher.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = _why(exc) if way == "lan" else f"Bambu account: {_why(exc)}"
                if way == "lan":
                    self._local_fails += 1
                log.info("printer %s (%s): %s", self.id, way, self.last_error)
            was = self.connected
            self.connected, self._client, self.via = False, None, None
            if was and self.on_change:
                self.on_change(self, {}, {})
            await asyncio.sleep(1 if way == "cloud" and self._local_fails == 0 and self.lan_ready else backoff)
            backoff = min(backoff * 2, 120)

    def pick_way(self) -> str | None:
        """LAN first; the Bambu account when there's no LAN setup or the LAN keeps failing."""
        if self.lan_ready and (self._local_fails < LOCAL_TRIES or not self.cloud_ready):
            return "lan"
        if self.cloud_ready:
            return "cloud"
        return None

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(PUSHALL_EVERY_S)
            await self.pushall()

    def ingest(self, msg: dict[str, Any]) -> None:
        before = status(self.raw, connected=True, last_report=self.last_report)
        before_ctl = self.control_mode
        p = msg.get("print") or {}
        result = p.get("result")
        if p.get("command") not in (None, "push_status") and result:
            # A command echo (pause, project_file...); keep the reply but don't merge it into status.
            self.raw.setdefault("_replies", {})[p["command"]] = p
            if str(result).lower() == "failed" and "verify" in str(p.get("reason") or "").lower():
                self.secured = True
                self._ctl_changed(before_ctl)
            return
        deep_merge(self.raw, msg)
        self.last_report = time.time()
        self._read_secured(p)
        after = status(self.raw, connected=True, last_report=self.last_report)
        if self.on_change and (before["state"] != after["state"] or before["job"] != after["job"]
                               or before["light"] != after["light"] or before["active_slot"] != after["active_slot"]):
            self.on_change(self, before, after)
        else:
            self._ctl_changed(before_ctl)

    def _read_secured(self, p: dict[str, Any]) -> None:
        fun = p.get("fun")
        try:
            fun_v = int(str(fun), 16) if fun not in (None, "") else 0
        except ValueError:
            fun_v = 0
        if fun_v:
            self.secured = bool(fun_v & SECURED_FUN)
            return
        if "flag3" in p:
            try:
                self.secured = bool(int(p["flag3"]) & SECURED_FLAG3)
            except (TypeError, ValueError):
                pass

    def _ctl_changed(self, before: str) -> None:
        if self.control_mode != before and self.on_change:
            self.on_change(self, {}, {})

    def _need_control(self) -> None:
        if self.control_mode != "full":
            raise PrinterError(409, "watch_only", WATCH_ONLY.format(name=self.entry.name))

    async def publish(self, payload: dict[str, Any]) -> None:
        if not self._client or not self.connected:
            raise PrinterError(409, "printer_offline", f"The {self.entry.name} isn't connected right now")
        self._seq += 1
        for body in payload.values():
            if isinstance(body, dict):
                body.setdefault("sequence_id", str(self._seq))
        await self._client.publish(f"device/{self.serial}/request", json.dumps(payload))

    async def pushall(self) -> None:
        await self.publish({"pushing": {"command": "pushall", "version": 1, "push_target": 1}})

    # -- commands -----------------------------------------------------------
    async def control(self, command: str) -> None:
        if command not in ("pause", "resume", "stop"):
            raise PrinterError(400, "bad_params", "command must be pause, resume or stop")
        self._need_control()
        await self.publish({"print": {"command": command, "param": ""}})

    async def set_light(self, on: bool) -> None:
        await self.publish({"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "on" if on else "off",
                                       "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}})

    async def set_speed(self, level: str) -> None:
        if level not in SPEED_LEVELS:
            raise PrinterError(400, "bad_params", "speed must be silent, standard, sport or ludicrous")
        self._need_control()
        await self.publish({"print": {"command": "print_speed", "param": str(SPEED_LEVELS[level])}})

    async def set_temp(self, heater: str, temp: int) -> None:
        if heater not in HEATERS:
            raise PrinterError(400, "bad_params", "heater must be nozzle, bed or chamber")
        code, top = HEATERS[heater]
        if not 0 <= temp <= top:
            raise PrinterError(400, "bad_params", f"{heater} must be 0 to {top}°C")
        self._need_control()
        await self.publish({"print": {"command": "gcode_line", "param": f"{code} S{int(temp)}\n"}})

    async def start_print(self, name: str, data: bytes, *, plate: int, ams_mapping: list[int] | None,
                          bed_leveling: bool, timelapse: bool, title: str) -> None:
        s = self.status()
        if s["state"] in ("printing", "paused", "preparing"):
            raise PrinterError(409, "printer_busy", f"The {self.entry.name} is busy with {s['job']['name'] if s['job'] else 'a print'}")
        if not s["online"]:
            raise PrinterError(409, "printer_offline", f"The {self.entry.name} isn't connected right now")
        self._need_control()
        host, code = self.host, self.access_code
        try:
            await asyncio.to_thread(ftps_upload, host, code, name, data)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            raise PrinterError(502, "upload_failed", f"Couldn't copy the file to the printer: {_why(exc)}") from exc
        use_ams = bool(ams_mapping)
        await self.publish({"print": {
            "command": "project_file", "param": f"Metadata/plate_{plate}.gcode",
            "url": f"{self.entry.file_url_prefix}{name}", "md5": hashlib.md5(data).hexdigest().upper(),  # noqa: S324
            "subtask_name": title, "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
            "use_ams": use_ams, "ams_mapping": ams_mapping or [], "timelapse": timelapse,
            "bed_leveling": bed_leveling, "bed_type": "auto", "flow_cali": False, "vibration_cali": False,
            "layer_inspect": True,
        }})

    def frames(self) -> "FrameGrabber":
        if self._frames is None:
            self._frames = FrameGrabber(self)
        return self._frames


def _why(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    if "Not authorized" in text or "bad user name or password" in text.lower() or "530" in text:
        return "The access code was refused. Check it on the printer (Settings, Network)."
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "The printer didn't answer. Is it on and on the network?"
    if "refused" in text.lower():
        return "The printer refused the connection. Check the IP address, and that the printer is on."
    return text[:200]


class FrameGrabber:
    """The chamber camera as JPEGs: ffmpeg runs only while someone looked in the last 20 s."""

    IDLE_S = 20

    def __init__(self, printer: Printer):
        self.printer = printer
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.wanted_at = 0.0
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self.cond = asyncio.Condition()
        self.error: str | None = None

    def url(self) -> str:
        p = self.printer
        return f"rtsps://bblp:{p.access_code}@{p.host}:{RTSP_PORT}/streaming/live/1"

    def touch(self) -> None:
        self.wanted_at = time.time()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def latest(self, wait: float = 6.0) -> bytes | None:
        self.touch()
        if self.frame and time.time() - self.frame_at < 3:
            return self.frame
        try:
            async with self.cond:
                await asyncio.wait_for(self.cond.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        return self.frame

    async def _run(self) -> None:
        if not shutil.which("ffmpeg") or not self.printer.lan_ready:
            self.error = ("Camera isn't available" if self.printer.lan_ready
                          else "Add the printer's IP address on the hub's admin page to see the camera")
            return
        args = ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", self.url(), "-vf", "fps=2,scale=1280:-2",
                "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        try:
            self._proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                              stderr=asyncio.subprocess.DEVNULL)
            buf = b""
            assert self._proc.stdout
            while time.time() - self.wanted_at < self.IDLE_S:
                chunk = await asyncio.wait_for(self._proc.stdout.read(65536), timeout=15)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2)
                    if start < 0 or end < 0:
                        break
                    self.frame, self.frame_at = buf[start:end + 2], time.time()
                    self.error = None
                    buf = buf[end + 2:]
                    async with self.cond:
                        self.cond.notify_all()
                if len(buf) > 8_000_000:
                    buf = b""
        except (asyncio.TimeoutError, OSError) as exc:
            self.error = "The camera didn't send a picture. Turn on LAN Only Liveview on the printer (Settings, Network)."
            log.info("printer camera: %s", exc)
        finally:
            await self.stop_proc()

    async def stop_proc(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            try:
                await self._proc.wait()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.stop_proc()


# -- the library and the whole workshop ---------------------------------------------
class Workshop:
    """Every configured printer plus the shared library of sliced models."""

    def __init__(self, hub: "Hub", cfg: "PrintersConfig", data_dir: Path):
        self.hub = hub
        self.cfg = cfg
        self.dir = data_dir
        self.printers: dict[str, Printer] = {}
        if cfg.enabled:
            for e in cfg.printers:
                p = Printer(e, hub)
                p.on_change = self._changed
                self.printers[e.id] = p
        self._started: dict[str, str | None] = {}  # printer id -> person who started the current job

    @property
    def enabled(self) -> bool:
        return bool(self.printers)

    def get(self, pid: str) -> Printer:
        p = self.printers.get(pid)
        if p is None:
            raise PrinterError(404, "not_found", "That printer isn't in the house")
        return p

    def start(self) -> None:
        for p in self.printers.values():
            p.start()

    async def stop(self) -> None:
        for p in self.printers.values():
            await p.stop()

    def summaries(self) -> list[dict[str, Any]]:
        return [p.summary() for p in self.printers.values()]

    def health(self) -> list[dict[str, Any]]:
        out = []
        for p in self.printers.values():
            s = p.status()
            # A printer with no access code yet is "not set up", not broken: keep it off the house status.
            out.append({"id": f"printer_{p.id}", "title": f"{p.entry.name} printer", "enabled": not p.missing(),
                        "ok": s["online"], "last_ok": p.last_report or None,
                        "error": None if s["online"] else (p.last_error or "Not connected")})
        return out

    # -- events ----------------------------------------------------------------
    def _changed(self, p: Printer, before: dict[str, Any], after: dict[str, Any]) -> None:
        self.hub.bus.publish({"type": "printer", "data": p.status()})
        if not before or not after:
            return  # connection or control mode changed, nothing to tell anyone
        a, b = after["state"], before["state"]
        if a == b:
            return
        name = (after["job"] or before["job"] or {}).get("name") or "The print"
        msg = None
        if a == "done" and b in ("printing", "preparing"):
            msg = (f"{name} is done", "Take it off the plate when it has cooled.", "fyi")
        elif a == "failed":
            msg = (f"{name} stopped", "The printer stopped the print. Check it in the app.", "attention")
        elif a == "paused" and after.get("error"):
            msg = (f"{p.entry.name} needs you", f"{name} paused. It may be out of filament.", "attention")
        if msg and self.cfg.notify:
            starter = self._started.get(p.id)
            coro = self.hub.notify.send(title=msg[0], body=msg[1], cat="printer", level=msg[2],
                                        person_ids=[starter] if starter else None,
                                        roles=None if starter else ["admin"], ref=f"printer:{p.id}",
                                        group_key=f"printer:{p.id}")
            asyncio.ensure_future(coro)
        if a in ("idle", "done", "failed"):
            self._started.pop(p.id, None)

    # -- library ----------------------------------------------------------------
    @property
    def lib(self) -> Path:
        d = self.dir / "library"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index(self) -> list[dict[str, Any]]:
        f = self.lib / "index.json"
        try:
            return json.loads(f.read_text())
        except (FileNotFoundError, ValueError):
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        tmp = self.lib / "index.json.tmp"
        tmp.write_text(json.dumps(items))
        tmp.replace(self.lib / "index.json")

    def library(self) -> list[dict[str, Any]]:
        return [self.public(i) for i in sorted(self._index(), key=lambda i: -i.get("last_used", i["added_at"]))]

    @staticmethod
    def public(i: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in i.items() if k not in ("file",)}
        out["thumbnail_url"] = f"/v1/printers/library/{i['id']}/thumb.png" if i.get("thumb") else None
        return out

    def item(self, mid: str) -> dict[str, Any]:
        for i in self._index():
            if i["id"] == mid:
                return i
        raise PrinterError(404, "not_found", "That model isn't in the library")

    def add(self, filename: str, data: bytes, person_id: str | None, title: str | None = None) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD:
            raise PrinterError(413, "too_big", "That file is over 300 MB")
        info = read_3mf(data)
        mid = new_id("mdl")
        base = SAFE_NAME.sub("", Path(filename).name.removesuffix(".3mf").removesuffix(".gcode")).strip() or "Model"
        (self.lib / f"{mid}.gcode.3mf").write_bytes(data)
        if info["thumbnail"]:
            (self.lib / f"{mid}.png").write_bytes(info["thumbnail"])
        item = {
            "id": mid, "title": (title or base)[:80], "file": f"{mid}.gcode.3mf", "bytes": len(data),
            "added_at": time.time(), "added_by": person_id, "plates": info["plates"],
            "printer_model": info["printer_model"], "thumb": bool(info["thumbnail"]), "prints": 0,
        }
        items = self._index()
        items.append(item)
        self._save(items)
        return self.public(item)

    def rename(self, mid: str, title: str) -> dict[str, Any]:
        items = self._index()
        for i in items:
            if i["id"] == mid:
                i["title"] = title.strip()[:80] or i["title"]
                self._save(items)
                return self.public(i)
        raise PrinterError(404, "not_found", "That model isn't in the library")

    def remove(self, mid: str) -> None:
        items = self._index()
        keep = [i for i in items if i["id"] != mid]
        if len(keep) == len(items):
            raise PrinterError(404, "not_found", "That model isn't in the library")
        for suffix in (".gcode.3mf", ".png"):
            (self.lib / f"{mid}{suffix}").unlink(missing_ok=True)
        self._save(keep)

    def thumb(self, mid: str) -> bytes:
        self.item(mid)
        f = self.lib / f"{mid}.png"
        if not f.exists():
            raise PrinterError(404, "not_found", "No picture for that model")
        return f.read_bytes()

    async def print_model(self, pid: str, mid: str, params: dict[str, Any], person_id: str | None) -> str:
        p = self.get(pid)
        item = self.item(mid)
        plate = int(params.get("plate") or 1)
        if plate not in [pl["index"] for pl in item["plates"]]:
            raise PrinterError(400, "bad_params", "That plate isn't in the file")
        mapping = params.get("ams_mapping")
        if mapping is not None:
            if not isinstance(mapping, list) or not all(isinstance(x, int) and -1 <= x < 16 for x in mapping):
                raise PrinterError(400, "bad_params", "ams_mapping must be a list of AMS slots")
        data = (self.lib / item["file"]).read_bytes()
        name = f"tc-{mid}.gcode.3mf"
        await p.start_print(name, data, plate=plate, ams_mapping=mapping,
                            bed_leveling=bool(params.get("bed_leveling", True)),
                            timelapse=bool(params.get("timelapse", False)), title=item["title"])
        self._started[p.id] = person_id
        items = self._index()
        for i in items:
            if i["id"] == mid:
                i["prints"] = int(i.get("prints") or 0) + 1
                i["last_used"] = time.time()
        self._save(items)
        return item["title"]
