"""The NAS section: TrueNAS adapter shaping (against a fake JSON-RPC server), routes and nas.* actions."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
import websockets

from hub.adapters.truenas import TrueNAS
from hub.config import ServiceConfig
from hub.vault import Vault

APPS = [
    {"name": "plex", "state": "RUNNING", "custom_app": False, "train": "stable", "version": "1.3.14",
     "human_version": "1.43.4_1.3.14", "latest_version": "1.3.25", "upgrade_available": True,
     "image_updates_available": False, "portals": {"Web UI": "http://0.0.0.0:32400/web"},
     "active_workloads": {"containers": 1, "used_ports": [], "container_details": [
         {"id": "c-plex", "service_name": "plex", "image": "plexinc/pms", "state": "running", "port_config": []}]}},
    {"name": "dispatcharr", "state": "CRASHED", "custom_app": True, "train": "stable", "version": "1.0.0",
     "upgrade_available": True, "image_updates_available": True, "portals": {},
     "active_workloads": {"containers": 1, "used_ports": [
         {"container_port": 9191, "protocol": "tcp", "host_ports": [{"host_port": 9191, "host_ip": "0.0.0.0"}]}],
         "container_details": [{"id": "c-disp", "service_name": "web", "image": "dispatcharr", "state": "exited"},
                               {"id": "c-redis", "service_name": "redis", "image": "redis", "state": "running"}]}},
    {"name": "thomascreek-hub", "state": "RUNNING", "custom_app": True, "upgrade_available": False,
     "image_updates_available": False, "portals": {}, "active_workloads": {"containers": 1}},
]
POOLS = [{"name": "HDDs", "healthy": True, "status": "ONLINE", "size": 112e12, "allocated": 74e12, "fragmentation": "40",
          "scan": {"state": "SCANNING", "percentage": 24.16, "pause": {"$date": 1}, "errors": 0},
          "topology": {"data": [{"type": "RAIDZ1", "status": "ONLINE", "children": [
              {"disk": "sda", "status": "ONLINE", "stats": {"read_errors": 0, "write_errors": 0, "checksum_errors": 2}}]}]}}]
ALERTS = [{"uuid": "a-1", "level": "WARNING", "formatted": "Scrub paused", "klass": "ScrubPaused",
           "datetime": {"$date": 1794531623000}, "dismissed": False},
          {"uuid": "a-2", "level": "CRITICAL", "formatted": "old", "dismissed": True}]


class FakeTrueNAS:
    """A tiny JSON-RPC 2.0 WebSocket server shaped like /api/current."""

    def __init__(self):
        self.calls: list[tuple[str, list]] = []
        self.port = 0
        self.ready = threading.Event()

    async def handler(self, ws):
        authed = False
        async for raw in ws:
            msg = json.loads(raw)
            m, p, rid = msg["method"], msg.get("params") or [], msg["id"]
            self.calls.append((m, p))
            if m == "auth.login_ex":
                authed = p[0]["api_key"] == "k" and p[0]["username"] == "truenas_admin"
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                          "result": {"response_type": "SUCCESS" if authed else "AUTH_ERR"}}))
                continue
            if not authed:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"message": "not authenticated"}}))
                continue
            result = {
                "system.info": {"hostname": "nas", "version": "25.10.1", "system_product": "PowerEdge T320",
                                "model": "Xeon", "cores": 20, "physmem": 64e9, "ecc_memory": True,
                                "loadavg": [1, 2, 3], "uptime_seconds": 3600},
                "pool.query": POOLS, "alert.list": ALERTS, "disk.query": [{"name": "sda", "model": "WDC_X"}],
                "service.query": [{"service": "cifs", "state": "RUNNING", "enable": True}],
                "update.status": {"status": {"new_version": None}},
                "app.upgrade_summary": {"latest_version": "1.3.25", "latest_human_version": "1.43.5_1.3.25",
                                        "changelog": "Fixes"},
                "core.subscribe": "sub-1", "core.unsubscribe": None, "app.redeploy": 41, "app.stop": 42,
                "app.upgrade": 43, "app.pull_images": 44, "alert.dismiss": None,
                "core.get_jobs": [{"id": 41, "method": "app.redeploy", "state": "RUNNING",
                                   "progress": {"percent": 50, "description": "Restarting"}}],
            }.get(m)
            if m == "core.get_jobs":
                jid = p[0][0][2]
                result = [{"id": jid, "method": "app", "state": "RUNNING" if jid == 41 else "SUCCESS",
                           "progress": {"percent": 50 if jid == 41 else 100, "description": "Working"}}]
            if m == "app.query":
                result = [a for a in APPS if not p or not p[0] or a["name"] == p[0][0][2]]
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))
            if m == "core.subscribe":
                args = json.loads(p[0].split(":", 1)[1])
                for i in range(3):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "collection_update", "params": {
                        "msg": "added", "collection": p[0],
                        "fields": {"data": f"{args['container_id']} line {i}\n", "timestamp": f"t{i}"}}}))

    def run(self):
        async def main():
            async with websockets.serve(self.handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.ready.set()
                await asyncio.Future()
        loop = asyncio.new_event_loop()
        threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True).start()
        self.ready.wait(5)
        return self


@pytest.fixture(scope="module")
def fake_nas():
    return FakeTrueNAS().run()


@pytest.fixture
def nas(tmp_path, fake_nas):
    import secrets
    v = Vault(tmp_path / "v.json", secrets.token_bytes(32))
    v.set("truenas.api_key", "k")
    cfg = ServiceConfig(url=f"http://127.0.0.1:{fake_nas.port}", token_secret="truenas.api_key",
                        extra={"links": {"plex": {"remote": "https://plex.example"}}})
    return TrueNAS(cfg, v)


def test_adapter_shapes_apps_links_and_protection(nas):
    apps = {a["name"]: a for a in asyncio.run(nas.apps())}
    assert apps["plex"]["update"] == "upgrade" and apps["plex"]["app_version"] == "1.43.4"
    assert apps["plex"]["links"] == {"lan": "http://127.0.0.1:32400/web", "remote": "https://plex.example"}
    assert apps["dispatcharr"]["update"] == "images"
    assert apps["dispatcharr"]["links"]["lan"] == "http://127.0.0.1:9191/"
    assert apps["thomascreek-hub"]["protected"] == "hub" and apps["plex"]["protected"] is None


def test_adapter_overview_and_detail(nas):
    o = asyncio.run(nas.overview())
    assert o["system"]["hostname"] == "nas" and o["apps"]["crashed"] == ["dispatcharr"]
    pool = o["pools"][0]
    assert pool["scrub"]["state"] == "paused" and pool["used_pct"] == 66
    assert pool["vdevs"][0]["disks"][0]["errors"] == 2
    assert [a["id"] for a in o["alerts"]] == ["a-1"]  # dismissed ones hidden
    assert o["services"][0] == {"service": "cifs", "title": "SMB", "running": True, "enabled": True}
    d = asyncio.run(nas.app("plex"))
    assert d["upgrade"]["changelog"] == "Fixes" and d["container_details"][0]["id"] == "c-plex"


def test_adapter_reads_logs_from_the_follow_event(nas, fake_nas):
    out = asyncio.run(nas.logs("dispatcharr", "c-redis", tail=50, wait_s=1.0))
    assert out["container"] == "c-redis" and [c["service"] for c in out["containers"]] == ["web", "redis"]
    assert [ln["text"] for ln in out["lines"]] == ["c-redis line 0", "c-redis line 1", "c-redis line 2"]
    sub = [p for m, p in fake_nas.calls if m == "core.subscribe"][-1][0]
    assert json.loads(sub.split(":", 1)[1]) == {"app_name": "dispatcharr", "container_id": "c-redis", "tail_lines": 50}
    # An unknown container falls back to the first one
    assert asyncio.run(nas.logs("dispatcharr", "nope", wait_s=1.0))["container"] == "c-disp"


def test_adapter_without_a_key_says_so(tmp_path, fake_nas):
    import secrets
    from hub.adapters.base import AdapterError
    t = TrueNAS(ServiceConfig(url=f"http://127.0.0.1:{fake_nas.port}", token_secret="truenas.api_key"),
                Vault(tmp_path / "v.json", secrets.token_bytes(32)))
    with pytest.raises(AdapterError, match="API key"):
        asyncio.run(t.apps())


# -- routes and actions (adapter replaced by a stub) -------------------------
@pytest.fixture
def stub(hub, nas):
    hub.config.hub.dry_run = False
    hub.config.truenas.enabled = True
    hub.config.truenas.url = nas.cfg.url
    hub.truenas = nas
    return nas


def test_nas_routes_are_admin_only(stub, home_client, people):
    assert people["phones"]["Avery"].client.get("/v1/nas", headers=people["phones"]["Avery"].h).status_code == 403
    dylan = people["phones"]["Dylan"]
    r = dylan.client.get("/v1/nas", headers=dylan.h)
    assert r.status_code == 200, r.text
    assert r.json()["apps"]["total"] == 3
    r = dylan.client.get("/v1/nas/apps/dispatcharr/logs?tail=20", headers=dylan.h)
    assert r.status_code == 200 and len(r.json()["lines"]) == 3
    assert dylan.client.get("/v1/nas/apps/nope/logs", headers=dylan.h).status_code == 404
    assert dylan.client.get("/v1/nas/apps/Bad_Name", headers=dylan.h).status_code == 400
    assert dylan.client.get("/v1/nas/jobs/41", headers=dylan.h).json()["pct"] == 50


def test_nas_app_actions(stub, hub, fake_nas, home_client, people):
    dylan = people["phones"]["Dylan"]
    r = dylan.secure_post("/v1/actions", {"action": "nas.app", "target": "plex", "params": {"op": "restart"}})
    assert r.status_code == 200, r.text
    assert r.json()["job"] == 41 and ("app.redeploy", ["plex"]) in fake_nas.calls
    r = dylan.secure_post("/v1/actions", {"action": "nas.app", "target": "plex", "params": {"op": "update"}})
    assert r.json()["job"] == 43
    assert ("app.upgrade", ["plex", {"app_version": "latest", "snapshot_hostpaths": True}]) in fake_nas.calls
    r = dylan.secure_post("/v1/actions", {"action": "nas.app", "target": "dispatcharr", "params": {"op": "update"}})
    assert ("app.pull_images", ["dispatcharr", {"redeploy": True}]) in fake_nas.calls
    # The hub never stops itself
    r = dylan.secure_post("/v1/actions", {"action": "nas.app", "target": "thomascreek-hub", "params": {"op": "stop"}})
    assert r.status_code == 400
    # Not for adults or children
    for who in ("Avery", "Theo"):
        r = people["phones"][who].secure_post("/v1/actions", {"action": "nas.app", "target": "plex", "params": {"op": "stop"}})
        assert r.status_code == 403
    r = dylan.secure_post("/v1/actions", {"action": "nas.app", "target": "plex", "params": {"op": "delete"}})
    assert r.status_code == 400
    r = dylan.secure_post("/v1/actions", {"action": "nas.alert", "target": "a-1"})
    assert r.status_code == 200 and ("alert.dismiss", ["a-1"]) in fake_nas.calls
    logged = [(r["action"], r["target"], r["outcome"]) for r in hub.db.all("SELECT * FROM activity ORDER BY id")]
    assert ("nas.app", "plex", "ok") in logged and ("nas.app", "plex", "denied") in logged


def test_hub_restarts_itself_after_answering(stub, fake_nas, home_client, people):
    dylan = people["phones"]["Dylan"]
    r = dylan.secure_post("/v1/actions", {"action": "nas.app", "target": "thomascreek-hub", "params": {"op": "restart"}})
    assert r.status_code == 200 and "reconnects" in r.json()["message"]


def test_remote_apps_warn_when_away(stub, app, away_client, people):
    dylan = people["phones"]["Dylan"]
    dylan.client = away_client
    dylan.sign_in()
    body = {"action": "nas.app", "target": "nginx-proxy-manager", "params": {"op": "stop"}}
    r = dylan.secure_post("/v1/actions", body)
    assert r.status_code == 409 and r.json()["error"] == "away_warning"


def test_update_all_skips_protected_apps_and_reports(stub, hub, fake_nas, home_client, people, monkeypatch):
    import hub.actions as actions
    monkeypatch.setattr(actions, "NAS_JOB_POLL_S", 0.01)
    sent = []

    async def send(**kw):
        sent.append(kw)
    hub.notify.send = send
    fake_nas.calls.clear()
    dylan = people["phones"]["Dylan"]
    r = dylan.secure_post("/v1/actions", {"action": "nas.update_all"})
    assert r.status_code == 200, r.text
    assert "2 apps" in r.json()["message"]

    async def wait():
        await hub.actions._nas_batch
    # The batch runs on the app's loop; wait for it through the test client's portal.
    home_client.portal.call(wait)
    ops = [(m, p[0]) for m, p in fake_nas.calls if m in ("app.upgrade", "app.pull_images", "app.redeploy")]
    assert ops == [("app.upgrade", "plex"), ("app.pull_images", "dispatcharr")]
    assert sent and sent[0]["title"].startswith("Updated")


def test_log_lines_lose_terminal_codes_but_keep_their_colour():
    from hub.adapters.truenas import clean_log_line
    assert clean_log_line("\x1b[1;31mboom\x1b[0m\n") == ("boom", "error")
    assert clean_log_line("\x1b[33mslow disk\x1b[0m") == ("slow disk", "warn")
    assert clean_log_line("\x1b[32mINFO\x1b[0m started\r") == ("INFO started", None)
    assert clean_log_line("2026 WARNING: low space") == ("2026 WARNING: low space", "warn")
    assert clean_log_line("ok, 0 errors") == ("ok, 0 errors", None)
