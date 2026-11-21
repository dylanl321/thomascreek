"""Per-route timing and the NAS load readout (phase 1 of the hub rebuild: measure first)."""

from __future__ import annotations

from hub.timings import SLOW_MS, HostLoad, Timings


def test_every_answer_says_its_total_time(home_client):
    res = home_client.get("/v1/ping")
    assert res.status_code == 200
    assert "total;dur=" in res.headers["server-timing"]


def test_timings_keep_route_templates_never_addresses(home_client):
    home_client.get("/v1/ping")
    home_client.get("/v1/tv/library/secret-code-123")  # an unknown address must not be kept as typed
    body = home_client.get("/v1/ping/timings").json()
    routes = {r["route"] for r in body["routes"]}
    assert "/v1/ping" in routes
    assert not any("secret-code-123" in r for r in routes)
    row = next(r for r in body["routes"] if r["route"] == "/v1/ping")
    assert row["n"] >= 1 and row["p95_ms"] >= row["p50_ms"] >= 0
    assert "host" in body and "host_hour" in body


def test_timings_are_home_only(away_client):
    assert away_client.get("/v1/ping/timings").status_code == 403


def test_summary_counts_slow_answers_and_windows():
    clock = [1000.0]
    t = Timings(clock=lambda: clock[0])
    for ms in (10, 20, 30, 40, SLOW_MS + 5):
        t.record("GET", "/v1/x", ms, 200)
    clock[0] += 7200
    t.record("GET", "/v1/x", 50, 500)
    all_rows = t.summary()[0]
    assert all_rows["n"] == 6 and all_rows["over_1s"] == 1 and all_rows["max_ms"] >= SLOW_MS
    recent = t.summary(window_s=3600)[0]
    assert recent["n"] == 1 and recent["errors"] == 1 and recent["over_1s_total"] == 1


def test_host_load_splits_busy_from_disk_wait(tmp_path):
    (tmp_path / "self").mkdir()
    stat, me = tmp_path / "stat", tmp_path / "self" / "stat"
    (tmp_path / "loadavg").write_text("13.4 13.6 14.0 3/900 1\n")
    clock = [0.0]

    def write(cpu, mine):
        stat.write_text("cpu " + " ".join(str(v) for v in cpu) + "\n")
        me.write_text("1 (hub serve) S " + " ".join(["0"] * 10) + f" {mine} 0 0\n")

    host = HostLoad(proc=str(tmp_path), clock=lambda: clock[0])
    write([100, 0, 100, 700, 100, 0, 0, 0], 0)
    assert "busy_pct" not in host.sample()
    clock[0] = 10.0
    # +200 user, +0 system, +600 idle, +200 iowait over 1000 ticks: 20% busy, 20% waiting on disk
    write([300, 0, 100, 1300, 300, 0, 0, 0], host._tick * 2)
    out = host.sample()
    assert out["busy_pct"] == 20.0 and out["iowait_pct"] == 20.0
    assert out["load"] == [13.4, 13.6, 14.0]
    assert out["hub_cpu_pct"] == 20.0  # 2 s of CPU in 10 s
    assert host.hour()["busy_avg_pct"] == 20.0


def test_slow_answers_keep_their_step_names_and_times_only():
    t = Timings(clock=lambda: 1000.0)
    t.record("POST", "/v1/tv/phone/play", 20, 200, "ready;dur=1, lease;dur=15")
    t.record("POST", "/v1/tv/phone/play", SLOW_MS + 400, 200,
             'ready;dur=1, lease;dur=1300, bad;desc="http://x"')
    slow = t.slow()
    assert len(slow) == 1
    assert slow[0]["route"] == "/v1/tv/phone/play" and slow[0]["steps"] == {"ready": 1.0, "lease": 1300.0}
    assert "http" not in str(slow)


def test_long_polls_are_waits_not_slow_answers():
    """A TV's /v1/tv/commands and /v1/tv/manage hold the call open until a command or the timeout: listed last with
    `long_poll`, never over a second, never in the slow steps (Nov 21 2026)."""
    t = Timings()
    t.record("GET", "/v1/tv/commands", 25000, 200, "wait;dur=25000")
    t.record("GET", "/v1/tv/manage", 20000, 200)
    t.record("GET", "/v1/x", 1200, 200, "a;dur=1200")
    rows = t.summary()
    assert [r["route"] for r in rows] == ["/v1/x", "/v1/tv/commands", "/v1/tv/manage"]
    assert rows[1]["long_poll"] is True and rows[1]["over_1s"] == 0 and rows[1]["over_1s_total"] == 0
    assert "long_poll" not in rows[0] and rows[0]["over_1s"] == 1
    assert [s["route"] for s in t.slow()] == ["/v1/x"]
