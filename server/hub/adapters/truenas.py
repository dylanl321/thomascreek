"""TrueNAS over its JSON-RPC 2.0 WebSocket API.

TrueNAS 25.04 deprecated the REST API and TrueNAS 26 removed it, so the hub
speaks the WebSocket API at /api/current and logs in with auth.login_ex using
an API key (mechanism API_KEY_PLAIN). The key lives in the hub's vault (copied
from 1Password); the app never sees it.

Reads: system, pools, alerts, apps, container logs, jobs.
Writes (admin tier, through POST /v1/actions): app start/stop/restart/update,
alert dismiss, scrub start/pause, snapshot, sharing services. Nothing here
deletes apps, datasets or pools, edits compose files, or upgrades/reboots the OS.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import ssl
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

import websockets

from .base import Adapter, AdapterError

CRASHED = ("CRASHED", "STOPPED_UNEXPECTEDLY")
# Sharing and admin services the app may switch on or off. Anything else stays in TrueNAS.
SERVICES = ("cifs", "nfs", "ssh", "ftp", "iscsitarget", "ups", "snmp")
SERVICE_TITLES = {"cifs": "SMB", "nfs": "NFS", "ssh": "SSH", "ftp": "FTP", "iscsitarget": "iSCSI", "ups": "UPS",
                  "snmp": "SNMP"}
Call = Callable[[str, list[Any]], Awaitable[Any]]


def _date(v: Any) -> float | None:
    """TrueNAS sends datetimes as {"$date": ms}."""
    if isinstance(v, dict) and "$date" in v:
        return v["$date"] / 1000
    if isinstance(v, (int, float)):
        return float(v)
    return None


class TrueNAS(Adapter):
    name = "truenas"
    title = "NAS"

    def _ws_url(self) -> str:
        url = (self.cfg.url or "").rstrip("/")
        url = url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{url}/api/current"

    @property
    def lan_host(self) -> str:
        return urlparse(self.cfg.url or "").hostname or ""

    # -- connection ---------------------------------------------------------
    @asynccontextmanager
    async def session(self) -> AsyncIterator[tuple[Call, Any]]:
        """A logged-in WebSocket. Yields (call, ws); notifications for subscriptions are queued on ws.events."""
        if not self.enabled:
            raise AdapterError("NAS is not set up")
        key = self.secret(self.cfg.token_secret)
        if not key:
            self._fail("the NAS API key is not saved yet")
            raise AdapterError(self.last_error)
        ctx = None
        if self._ws_url().startswith("wss://"):
            ctx = ssl.create_default_context()
            if not self.cfg.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        ids = itertools.count(1)
        try:
            async with websockets.connect(self._ws_url(), ssl=ctx, open_timeout=self.cfg.timeout_s,
                                          max_size=16 * 2**20) as ws:
                events: list[dict[str, Any]] = []

                async def call(method: str, params: list[Any]) -> Any:
                    rid = next(ids)
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(self.cfg.timeout_s, 20)))
                        if msg.get("id") == rid:
                            if "error" in msg:
                                err = msg["error"] or {}
                                why = ((err.get("data") or {}).get("reason") or err.get("message") or "").strip()
                                raise AdapterError(f"NAS refused {method}" + (f": {why[:160]}" if why else ""))
                            return msg.get("result")
                        if "method" in msg:
                            events.append(msg)

                # login_ex needs the key owner's user name (often truenas_admin, not root);
                # login_with_api_key does not, so it is the fallback when no name is set.
                users = [self.cfg.username] if self.cfg.username else ["truenas_admin", "root", "admin"]
                # Each refused login costs TrueNAS a delay, so start with the name that worked last time.
                good = getattr(self, "_login_user", None)
                if good in users:
                    users = [good] + [u for u in users if u != good]
                logged_in = False
                for user in users:
                    try:
                        ok = await call("auth.login_ex", [{"mechanism": "API_KEY_PLAIN", "username": user, "api_key": key}])
                    except AdapterError:
                        ok = None
                    if ok and ok.get("response_type") == "SUCCESS":
                        logged_in = True
                        self._login_user = user
                        break
                if not logged_in:
                    try:
                        logged_in = bool(await call("auth.login_with_api_key", [key]))
                    except AdapterError:
                        logged_in = False
                if not logged_in:
                    raise AdapterError("NAS refused the hub's API key")
                ws.events = events  # type: ignore[attr-defined]
                yield call, ws
        except AdapterError as exc:
            self._fail(str(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(f"cannot reach the NAS: {type(exc).__name__}")
            raise AdapterError(self.last_error) from exc
        self.last_ok, self.last_error = time.time(), None

    async def call_many(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        async with self.session() as (call, _ws):
            return [await call(m, p) for m, p in calls]

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        (out,) = await self.call_many([(method, params or [])])
        return out

    async def ping(self) -> None:
        await self.call_many([("system.version", [])])

    # -- shaping ------------------------------------------------------------
    @staticmethod
    def _pool(p: dict[str, Any]) -> dict[str, Any]:
        size = p.get("size") or 0
        used = p.get("allocated") or 0
        scan = p.get("scan") or {}
        scanning = scan.get("state") == "SCANNING"
        paused = scanning and bool(scan.get("pause"))
        vdevs = []
        for v in ((p.get("topology") or {}).get("data") or []):
            disks = v.get("children") or [v]
            vdevs.append({
                "type": v.get("type"), "status": v.get("status"),
                "disks": [{"disk": d.get("disk"), "status": d.get("status"),
                           "errors": sum(int(((d.get("stats") or {}).get(k)) or 0)
                                         for k in ("read_errors", "write_errors", "checksum_errors"))}
                          for d in disks],
            })
        return {
            "name": p.get("name"),
            "healthy": p.get("healthy"),
            "status": p.get("status"),
            "size_tb": round(size / 1e12, 1) if size else None,
            "size_bytes": size or None,
            "used_bytes": used or None,
            "used_pct": round(100 * used / size) if size else None,
            "fragmentation": int(p["fragmentation"]) if str(p.get("fragmentation") or "").isdigit() else None,
            "scrub_pct": round(scan.get("percentage") or 0) if scanning else None,
            "scrub": {
                "state": "paused" if paused else ("running" if scanning else (scan.get("state") or "none").lower()),
                "pct": round(scan.get("percentage") or 0, 1) if scan else None,
                "errors": scan.get("errors"),
                "ended": _date(scan.get("end_time")),
                "secs_left": scan.get("total_secs_left") if scanning and not paused else None,
            } if scan else None,
            "vdevs": vdevs,
        }

    def _links(self, a: dict[str, Any]) -> dict[str, str | None]:
        """Where to open the app: a configured link wins, then its TrueNAS portal, then its first published port."""
        name = a.get("name") or ""
        over = (self.cfg.extra.get("links") or {}).get(name) or {}
        lan = over.get("lan")
        if not lan:
            portals = a.get("portals") or {}
            lan = next(iter(portals.values()), None) if portals else None
            if lan:
                lan = lan.replace("0.0.0.0", self.lan_host)
        if not lan:
            ports = ((a.get("active_workloads") or {}).get("used_ports")) or []
            for p in ports:
                if (p.get("protocol") or "tcp").lower() != "tcp":
                    continue
                hp = next((h.get("host_port") for h in p.get("host_ports") or [] if h.get("host_port")), None)
                if hp:
                    scheme = "https" if hp in (443, 8443) or p.get("container_port") in (443, 8443) else "http"
                    lan = f"{scheme}://{self.lan_host}:{hp}/"
                    break
        return {"lan": lan, "remote": over.get("remote")}

    def protection(self, name: str) -> str | None:
        """'hub' for the hub's own app, 'remote' for apps that carry remote access, else None."""
        if name == (self.cfg.extra.get("self_app") or "thomascreek-hub"):
            return "hub"
        if name in (self.cfg.extra.get("remote_apps") or ["nginx-proxy-manager", "tailscale", "cloudflare-ddns"]):
            return "remote"
        return None

    def _app(self, a: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        custom = bool(a.get("custom_app"))
        upgrade = bool(a.get("upgrade_available"))
        images = bool(a.get("image_updates_available"))
        wl = a.get("active_workloads") or {}
        out: dict[str, Any] = {
            "name": a.get("name"),
            "state": a.get("state"),
            "custom": custom,
            "train": a.get("train"),
            "version": a.get("version"),
            "app_version": (a.get("human_version") or "").split("_")[0] or None,
            "latest_version": a.get("latest_version"),
            # Catalog apps upgrade their chart; custom apps (and catalog apps with only new images) pull images.
            "update": "upgrade" if upgrade and not custom else ("images" if images or (upgrade and custom) else None),
            "containers": wl.get("containers", 0),
            "links": self._links(a),
            "protected": self.protection(a.get("name") or ""),
            "notes": a.get("notes") if full else None,
        }
        if full:
            out["container_details"] = [{
                "id": c.get("id"), "service": c.get("service_name"), "image": c.get("image"),
                "state": c.get("state"),
                "ports": [{"container": p.get("container_port"), "protocol": p.get("protocol"),
                           "host": [h.get("host_port") for h in p.get("host_ports") or []]}
                          for p in c.get("port_config") or []],
            } for c in wl.get("container_details") or []]
            out["volumes"] = [{"source": v.get("source"), "destination": v.get("destination")}
                              for v in wl.get("volumes") or [] if v.get("source", "").startswith("/mnt/")]
            out["images"] = wl.get("images") or []
        else:
            out.pop("notes")
        return out

    @staticmethod
    def _alert(a: dict[str, Any]) -> dict[str, Any]:
        return {"id": a.get("uuid") or a.get("id"), "level": a.get("level"), "text": a.get("formatted"),
                "klass": a.get("klass"), "at": _date(a.get("datetime")), "dismissed": bool(a.get("dismissed"))}

    # -- reads --------------------------------------------------------------
    async def summary(self) -> dict[str, Any]:
        """The Home/Systems card: pools, app counts, open alerts (polled every few minutes)."""
        pools, apps, alerts = await self.call_many([
            ("pool.query", []),
            ("app.query", []),
            ("alert.list", []),
        ])
        app_rows = [{
            "name": a.get("name"),
            "state": a.get("state"),
            "update": bool(a.get("upgrade_available") or a.get("image_updates_available")),
        } for a in apps or []]
        storage = []
        for p in pools or []:
            s = self._pool(p)
            storage.append({k: s[k] for k in ("name", "healthy", "status", "size_tb", "used_pct", "scrub_pct")})
        return {
            "storage": storage,
            "apps": {
                "total": len(app_rows),
                "crashed": sum(1 for a in app_rows if a["state"] in CRASHED),
                "crashed_names": [a["name"] for a in app_rows if a["state"] in CRASHED],
                "updates": sum(1 for a in app_rows if a["update"]),
                "items": app_rows,
            },
            "alerts": [{"level": a.get("level"), "text": a.get("formatted")} for a in (alerts or []) if not a.get("dismissed")],
        }

    async def overview(self) -> dict[str, Any]:
        async with self.session() as (call, _ws):
            info = await call("system.info", [])
            pools = await call("pool.query", [])
            alerts = await call("alert.list", [])
            apps = await call("app.query", [])
            disks = await call("disk.query", [[], {"select": ["name", "model", "serial", "size", "type", "pool"]}])
            try:
                services = await call("service.query", [[["service", "in", list(SERVICES)]]])
            except AdapterError:
                services = []
            try:
                os_update = await call("update.status", [])
            except AdapterError:
                os_update = None
        rows = [self._app(a) for a in apps or []]
        status = (os_update or {}).get("status") or {}
        new = status.get("new_version") or {}
        return {
            "system": {
                "hostname": info.get("hostname"),
                "version": info.get("version"),
                "model": info.get("system_product") or info.get("model"),
                "cpu": info.get("model"),
                "cores": info.get("cores"),
                "memory_bytes": info.get("physmem"),
                "ecc": info.get("ecc_memory"),
                "load": info.get("loadavg"),
                "uptime_s": int(info.get("uptime_seconds") or 0),
                "os_update": new.get("version") if isinstance(new, dict) else None,
            },
            "pools": [self._pool(p) for p in pools or []],
            "disks": [{"name": d.get("name"), "model": (d.get("model") or "").replace("_", " "), "serial": d.get("serial"),
                       "size_bytes": d.get("size"), "type": d.get("type"), "pool": d.get("pool")} for d in disks or []],
            "alerts": [self._alert(a) for a in alerts or [] if not a.get("dismissed")],
            "apps": {
                "total": len(rows),
                "running": sum(1 for a in rows if a["state"] == "RUNNING"),
                "crashed": [a["name"] for a in rows if a["state"] in CRASHED],
                "updates": sum(1 for a in rows if a["update"]),
            },
            "services": [{"service": s.get("service"), "title": SERVICE_TITLES.get(s.get("service"), s.get("service")),
                          "running": s.get("state") == "RUNNING", "enabled": s.get("enable")} for s in services or []],
        }

    async def apps(self) -> list[dict[str, Any]]:
        apps = await self.call("app.query", [])
        return [self._app(a) for a in apps or []]

    async def app(self, name: str) -> dict[str, Any] | None:
        async with self.session() as (call, _ws):
            rows = await call("app.query", [[["name", "=", name]]])
            if not rows:
                return None
            out = self._app(rows[0], full=True)
            if out["update"] == "upgrade":
                try:
                    s = await call("app.upgrade_summary", [name])
                    out["upgrade"] = {
                        "to": s.get("latest_version"), "to_app_version": s.get("latest_human_version"),
                        "changelog": (s.get("changelog") or "")[:4000] or None,
                    }
                except AdapterError:
                    out["upgrade"] = None
        return out

    async def logs(self, name: str, container: str | None, tail: int = 300, wait_s: float = 2.0) -> dict[str, Any]:
        """The last `tail` lines of one container, read from the app.container_log_follow event."""
        async with self.session() as (call, ws):
            rows = await call("app.query", [[["name", "=", name]]])
            if not rows:
                raise AdapterError("No such app")
            details = (rows[0].get("active_workloads") or {}).get("container_details") or []
            ids = [c.get("id") for c in details]
            cid = container if container in ids else (ids[0] if ids else None)
            if not cid:
                return {"container": None, "containers": [], "lines": []}
            event = "app.container_log_follow:" + json.dumps({"app_name": name, "container_id": cid, "tail_lines": tail})
            sub = await call("core.subscribe", [event])
            lines: list[dict[str, Any]] = []

            def take(msg: dict[str, Any]) -> None:
                f = ((msg.get("params") or {}).get("fields")) or {}
                if "data" in f:
                    lines.append({"t": f.get("timestamp"), "text": str(f["data"]).rstrip("\n")[:2000]})

            for m in ws.events:
                take(m)
            ws.events.clear()
            deadline = time.monotonic() + wait_s
            quiet = 0.6
            while time.monotonic() < deadline and len(lines) < tail:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=quiet if lines else wait_s))
                except asyncio.TimeoutError:
                    if lines:
                        break
                    continue
                take(msg)
            try:
                await call("core.unsubscribe", [sub])
            except AdapterError:
                pass
        return {
            "container": cid,
            "containers": [{"id": c.get("id"), "service": c.get("service_name"), "state": c.get("state")} for c in details],
            "lines": lines[-tail:],
        }

    async def job(self, job_id: int) -> dict[str, Any] | None:
        rows = await self.call("core.get_jobs", [[["id", "=", job_id]]])
        if not rows:
            return None
        j = rows[0]
        prog = j.get("progress") or {}
        return {"id": j.get("id"), "method": j.get("method"), "state": j.get("state"),
                "pct": prog.get("percent"), "description": prog.get("description"),
                "error": (j.get("error") or "")[:500] or None}

    # -- writes (called only by ActionRunner after policy + Face ID) ---------
    async def app_op(self, name: str, op: str) -> Any:
        if op == "start":
            return await self.call("app.start", [name])
        if op == "stop":
            return await self.call("app.stop", [name])
        if op == "restart":
            return await self.call("app.redeploy", [name])
        if op == "upgrade":
            return await self.call("app.upgrade", [name, {"app_version": "latest", "snapshot_hostpaths": True}])
        if op == "pull":
            return await self.call("app.pull_images", [name, {"redeploy": True}])
        raise AdapterError(f"unknown app operation {op}")

    async def restart_app(self, app_name: str) -> Any:
        return await self.app_op(app_name, "restart")

    async def dismiss_alert(self, uuid: str) -> None:
        await self.call("alert.dismiss", [uuid])

    async def scrub(self, pool: str, action: str) -> Any:
        return await self.call("pool.scrub.scrub", [pool, action])

    async def snapshot(self, dataset: str, name: str) -> Any:
        return await self.call("pool.snapshot.create", [{"dataset": dataset, "name": name}])

    async def service(self, service: str, on: bool) -> Any:
        return await self.call("service.control", ["START" if on else "STOP", service, {"silent": False}])
