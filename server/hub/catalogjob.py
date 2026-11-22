"""Provider list reads run in their own process (hub rebuild phase 2).

Reading a provider means downloading lists of up to 150,000 rows and a guide of hundreds of MB, parsing them,
writing the snapshot and folding every provider's titles together. In the hub's own process that work holds
Python's interpreter lock for seconds at a time even inside a worker thread, so every TV, phone and heartbeat
waited behind it (Nov 19 2026). Here the same code runs in a child process (`hub catalog refresh|extras|report`), at a
lower CPU priority, and the hub only waits for it without blocking: requests keep being answered from the last
snapshot, and the hub reads the new files when the child is done.

The child writes the same files and database rows as before (the snapshot JSON files under `tvapp/`, the folded
catalog and address health in `hub.db`), so nothing that reads them changes. Its last line on stdout is one JSON
object: `{"ok": true, "extras": {...}}` or `{"ok": false, "status": 502, "error": "...", "message": "..."}`. Its
log goes to the container log like the hub's. It never prints a login, password or provider address.

`hub serve` turns this on (HUB_CATALOG_CHILD=1). Tests and other callers keep the in-process path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

log = logging.getLogger("hub.catalogjob")

ENV = "HUB_CATALOG_CHILD"
TIMEOUT_S = 45 * 60  # a cold guide download plus the parse; a stuck child is ended after this
NICE = 10


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


async def run(job: str) -> dict[str, Any]:
    """Run `hub catalog <job>` and return its result. Raises TvAppError with the child's reason on failure."""
    from .tvapp import TvAppError

    started = time.monotonic()
    env = {**os.environ, ENV: "0"}  # the child does the work itself
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "hub", "catalog", job,
        stdout=asyncio.subprocess.PIPE, stderr=None, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("catalog %s: child stopped after %d min", job, TIMEOUT_S // 60)
        raise TvAppError(504, "provider_slow", "Reading the TV provider took too long; the last lists stay") from None
    except asyncio.CancelledError:
        proc.kill()
        raise
    result: dict[str, Any] = {}
    for line in reversed(out.decode(errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
    log.info("catalog %s: child finished in %.0f s (exit %s)", job, time.monotonic() - started, proc.returncode)
    if not result.get("ok"):
        raise TvAppError(int(result.get("status") or 502), str(result.get("error") or "bad_provider"),
                         str(result.get("message") or "The TV provider didn't answer"))
    return result


def child_main(job: str) -> int:
    """`hub catalog refresh|extras|report`: read the provider lists in this process and print one JSON result line."""
    from .config import data_dir, load_config
    from .db import DB
    from .tvapp import TvAppError
    from .tvapp_config import KV_KIDS
    from .tvcatalog import TvCatalog
    from .vault import Vault, load_master_key
    from .config import master_key_path

    logging.basicConfig(level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs full addresses, query strings included
    try:
        os.nice(NICE)
    except OSError:
        pass
    load_config()
    data = data_dir()
    db = DB(data / "hub.db")
    vault = Vault(data / "vault.json", load_master_key(master_key_path()))
    catalog = TvCatalog(vault, db, data)
    catalog.kids_categories = lambda: list(db.kv_get(KV_KIDS) or [])
    try:
        if job == "refresh":
            asyncio.run(catalog.refresh_here(settle=False))
            extras = catalog.__dict__.get("_extra_result") or {}
        elif job in ("extras", "extras-due"):
            extras = asyncio.run(catalog.refresh_extras_here(only_stale=job == "extras-due"))
        elif job == "report":  # count the stored lists again; nothing is downloaded
            catalog.write_playlist_report()
            extras = {}
        else:
            print(json.dumps({"ok": False, "status": 400, "error": "bad_request", "message": f"unknown job {job}"}))
            return 2
    except TvAppError as exc:
        print(json.dumps({"ok": False, "status": exc.status, "error": exc.code, "message": exc.message}))
        return 1
    except Exception as exc:  # noqa: BLE001 - the parent shows the reason; the old lists keep serving
        log.warning("catalog %s failed: %s", job, type(exc).__name__)
        print(json.dumps({"ok": False, "status": 502, "error": "bad_provider",
                          "message": f"Reading the provider failed ({type(exc).__name__})"}))
        return 1
    print(json.dumps({"ok": True, "extras": extras}))
    return 0
