"""TrueNAS over its JSON-RPC 2.0 WebSocket API.

TrueNAS 25.04 deprecated the REST API and TrueNAS 26 removed it, so the hub
speaks the WebSocket API at /api/current and logs in with auth.login_ex using
an API key (mechanism API_KEY_PLAIN). Reads only: pools, apps, alerts.
Restarting an app is an admin-tier action and stays dry-run until enabled.
"""

from __future__ import annotations

import itertools
import json
import ssl
from typing import Any

import websockets

from .base import Adapter, AdapterError


class TrueNAS(Adapter):
    name = "truenas"
    title = "NAS"

    def _ws_url(self) -> str:
        url = (self.cfg.url or "").rstrip("/")
        url = url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{url}/api/current"

    async def call_many(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
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
            async with websockets.connect(self._ws_url(), ssl=ctx, open_timeout=self.cfg.timeout_s, max_size=8 * 2**20) as ws:
                async def call(method: str, params: list[Any]) -> Any:
                    rid = next(ids)
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == rid:
                            if "error" in msg:
                                raise AdapterError(f"NAS error on {method}")
                            return msg.get("result")

                # login_ex needs the key owner's user name (often truenas_admin, not root);
                # login_with_api_key does not, so it is the fallback when no name is set.
                users = [self.cfg.username] if self.cfg.username else ["truenas_admin", "root", "admin"]
                logged_in = False
                for user in users:
                    try:
                        ok = await call("auth.login_ex", [{"mechanism": "API_KEY_PLAIN", "username": user, "api_key": key}])
                    except AdapterError:
                        ok = None
                    if ok and ok.get("response_type") == "SUCCESS":
                        logged_in = True
                        break
                if not logged_in:
                    try:
                        logged_in = bool(await call("auth.login_with_api_key", [key]))
                    except AdapterError:
                        logged_in = False
                if not logged_in:
                    raise AdapterError("NAS refused the hub's API key")
                out = [await call(m, p) for m, p in calls]
        except AdapterError as exc:
            self._fail(str(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(f"cannot reach the NAS: {type(exc).__name__}")
            raise AdapterError(self.last_error) from exc
        import time as _t

        self.last_ok, self.last_error = _t.time(), None
        return out

    async def ping(self) -> None:
        await self.call_many([("system.version", [])])

    async def summary(self) -> dict[str, Any]:
        pools, apps, alerts = await self.call_many([
            ("pool.query", []),
            ("app.query", []),
            ("alert.list", []),
        ])
        storage = []
        for p in pools or []:
            size = p.get("size") or 0
            used = p.get("allocated") or 0
            scan = p.get("scan") or {}
            storage.append({
                "name": p.get("name"),
                "healthy": p.get("healthy"),
                "status": p.get("status"),
                "size_tb": round(size / 1e12, 1) if size else None,
                "used_pct": round(100 * used / size) if size else None,
                "scrub_pct": round(scan.get("percentage") or 0) if scan.get("state") == "SCANNING" else None,
            })
        app_rows = [{
            "name": a.get("name"),
            "state": a.get("state"),
            "update": bool(a.get("upgrade_available") or a.get("image_updates_available")),
        } for a in apps or []]
        return {
            "storage": storage,
            "apps": {
                "total": len(app_rows),
                "crashed": sum(1 for a in app_rows if a["state"] in ("CRASHED", "STOPPED_UNEXPECTEDLY")),
                "updates": sum(1 for a in app_rows if a["update"]),
                "items": app_rows,
            },
            "alerts": [{"level": a.get("level"), "text": a.get("formatted")} for a in (alerts or []) if not a.get("dismissed")],
        }

    async def restart_app(self, app_name: str) -> Any:
        (job,) = await self.call_many([("app.redeploy", [app_name])])
        return job
