"""Channel health comes from real plays. The hub never walks the whole lineup to check it."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

def _lineup(hub):
    hub.tv_app.set_lineup([
        {"name": "CNN", "source": "thomas-creek", "stream_id": "10"},
        {"name": "Sports", "source": "thomas-creek", "stream_id": "12"},
    ])


def test_plays_score_sources_and_a_dead_one_is_not_the_default(hub, people):
    _lineup(hub)
    dylan = people["phones"]["Dylan"]
    events = [{"channel_key": "xc:thomas-creek:10", "source": "thomas-creek", "ok": True, "startup_ms": 400}] * 3
    events += [{"channel_key": "xc:thomas-creek:10", "source": "slow", "ok": False, "error": "no signal"}] * 5
    events.append({"channel_key": "xc:thomas-creek:10", "source": "http://user:pass@cdn", "ok": True})
    sent = dylan.client.post("/v1/tv/playback_events", headers=dylan.h, json={"events": events})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"ok": True, "accepted": 8}
    stored = json.dumps(hub.db.kv_get("tv.source_health"))
    assert "http://" not in stored and "pass" not in stored

    lineup = dylan.client.get("/v1/tv/lineup", headers=dylan.h).json()
    cnn = next(row for row in lineup["channels"] if row["name"] == "CNN")
    assert cnn["health"]["score"] > 0 and cnn["health"]["last_ok"]
    assert [row["source"] for row in cnn["sources"]][0] == "thomas-creek"
    assert cnn["sources"][0]["default"] is True
    slow = next(row for row in cnn["sources"] if row["source"] == "slow")
    assert slow["default"] is False and slow["score"] == 0
    sports = next(row for row in lineup["channels"] if row["name"] == "Sports")
    assert "health" not in sports

    worst = dylan.client.get("/v1/tv/health", headers=dylan.h).json()["sources"]
    assert [row["source"] for row in worst] == ["slow", "thomas-creek"]
    assert worst[0]["name"] == "CNN"


def test_candidates_stay_small_and_probing_waits_for_night(hub, people):
    now = datetime.now(ZoneInfo("America/New_York")).timestamp()
    channels = {f"xc:thomas-creek:{n}": {"n": 300 - n, "last": now} for n in range(250)}
    hub.db.kv_set("tv.watch", {"adult": {"channels": channels, "series": {}, "movies": {}}})
    hub.db.execute(
        "INSERT INTO person_settings (person_id, key, value, updated_at, device_id) VALUES (?,?,?,?,?)",
        [people["dylan"]["id"], "fav.media", json.dumps([{"id": "iptv-org:CNN.us"}]), now, None],
    )
    picked = hub.source_health.candidates(now)
    assert len(picked) == 200
    assert picked[0] == "iptv-org:CNN.us"
    assert "xc:thomas-creek:249" not in picked

    noon = datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    night = datetime(2026, 11, 14, 2, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert hub.source_health.overnight(noon) is False
    assert hub.source_health.overnight(night) is True
    assert hub.source_health.concurrency() <= 2

    async def boom(_key):
        raise RuntimeError("http://user:password@cdn.example/live/1.ts")

    hub.source_health.fetch = boom
    assert asyncio.run(hub.source_health.probe_some(noon)) == 0
    done = asyncio.run(hub.source_health.probe_some(night))
    assert done == hub.source_health.concurrency()
    blob = json.dumps(hub.db.kv_get("tv.source_health"))
    assert "password" not in blob and "cdn.example" not in blob
