"""Provider list reads run in a child process under `hub serve` (hub rebuild phase 2), so a big refresh never
stalls a request. The child writes the same files and rows; the hub waits without blocking and reads them after."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from hub import catalogjob, tvcatalog
from hub.tvapp import TvAppError


class _FakeProc:
    def __init__(self, out: str, code: int = 0):
        self._out, self.returncode = out.encode(), code

    async def communicate(self):
        await asyncio.sleep(0)
        return self._out, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _spawn(monkeypatch, out: str, code: int = 0, seen: list | None = None):
    async def fake(*args, **kwargs):
        if seen is not None:
            seen.append((args, kwargs))
        return _FakeProc(out, code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


def test_refresh_runs_in_a_child_and_reads_the_new_snapshot(hub, monkeypatch):
    cat = hub.tv_catalog
    seen: list = []
    _spawn(monkeypatch, 'log line\n{"ok": true, "extras": {"strong": "10 channels, 2 films, 1 series"}}\n', seen=seen)
    monkeypatch.setenv(catalogjob.ENV, "1")
    settled = []

    async def settle():
        settled.append(1)

    cat.settle = settle
    # What the child would have written:
    cat._write({"fetched_at": 1.0, "guide_format": tvcatalog.GUIDE_FORMAT, "channels": [], "movies": [],
                "series": [], "guide": {}, "categories": {}})
    cat._loaded = None
    doc = asyncio.run(cat.refresh())
    args, kwargs = seen[0]
    assert args[1:] == ("-m", "hub", "catalog", "refresh")
    assert kwargs["env"][catalogjob.ENV] == "0"  # the child does the work itself, it doesn't spawn again
    assert doc["fetched_at"] == 1.0
    assert settled == [1]  # the AI match step stays in the hub
    assert cat._extra_result == {"strong": "10 channels, 2 films, 1 series"}


def test_a_failed_child_says_why_and_keeps_the_old_lists(hub, monkeypatch):
    _spawn(monkeypatch, json.dumps({"ok": False, "status": 502, "error": "bad_provider",
                                    "message": "The TV provider didn't answer"}) + "\n", code=1)
    monkeypatch.setenv(catalogjob.ENV, "1")
    with pytest.raises(TvAppError) as err:
        asyncio.run(hub.tv_catalog.refresh())
    assert err.value.status == 502 and "didn't answer" in err.value.message


def test_a_child_with_no_result_line_is_a_failure(hub, monkeypatch):
    _spawn(monkeypatch, "Traceback ...\n", code=1)
    monkeypatch.setenv(catalogjob.ENV, "1")
    with pytest.raises(TvAppError):
        asyncio.run(catalogjob.run("extras"))


def test_without_hub_serve_the_read_stays_in_process(hub, monkeypatch):
    monkeypatch.delenv(catalogjob.ENV, raising=False)
    called = []

    async def here(**kw):
        called.append(kw)
        return {}

    monkeypatch.setattr(hub.tv_catalog, "refresh_here", here)
    asyncio.run(hub.tv_catalog.refresh())
    assert called == [{}]


def test_child_main_reads_and_prints_one_result(monkeypatch, capsys, tmp_path):
    data = tmp_path / "child"
    data.mkdir()
    monkeypatch.setenv("HUB_DATA_DIR", str(data))
    monkeypatch.setenv("HUB_MASTER_KEY_FILE", str(data / "key"))
    monkeypatch.delenv("HUB_CONFIG_FILE", raising=False)
    from hub.vault import generate_master_key

    (data / "key").write_text(generate_master_key() + "\n")
    ran = []

    async def here(self, *, settle=True):
        ran.append(settle)
        self._extra_result = {"strong": "not read"}
        return {}

    monkeypatch.setattr(tvcatalog.TvCatalog, "refresh_here", here)
    monkeypatch.setattr(catalogjob.os, "nice", lambda n: 0)
    assert catalogjob.child_main("refresh") == 0
    assert ran == [False]  # the hub settles, not the child
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"ok": True, "extras": {"strong": "not read"}}


def test_list_status_reads_the_childs_result(hub):
    cat = hub.tv_catalog
    hub.db.kv_set(tvcatalog.KV_EXTRA_RESULT, {"x": "not read"})
    cat.__dict__.pop("_extra_result", None)
    assert cat.list_status()[0]["main"] is True  # main row always there; extras read from the saved result


def test_snapshot_writes_are_whole_files(hub, tmp_path):
    """A reader in the other process sees the old file or the new one, never half of one."""
    path = tmp_path / "snap.json"
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                text = path.read_text()
            except OSError:
                continue
            if text:
                try:
                    json.loads(text)
                except json.JSONDecodeError:
                    bad.append(1)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(200):
            tvcatalog._atomic_write(path, json.dumps({"i": i, "pad": "x" * 20000}))
    finally:
        stop.set()
        t.join()
    assert not bad
    assert not list(tmp_path.glob(".*.tmp"))


def test_folds_take_turns_across_processes(tmp_path):
    lock = tmp_path / "fold.lock"
    with tvcatalog._file_lock(lock):
        import fcntl

        with open(lock, "a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(lock, "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again after the block
