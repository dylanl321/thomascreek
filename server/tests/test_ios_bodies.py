"""The bodies the iPhone decodes, captured from the hub's own routes into ios/Bodies (#206).

The fixtures in ios/Bodies/Tests/HubBodiesTests/Fixtures are what these routes answer for the test household
(no real names, addresses or keys). The Swift tests there decode each one with the app's own models
(`swift test` in ios/Bodies, run by scripts/premerge.sh). This test fails when a route's shape no longer matches
its fixture, so a hub change that could break the phone is seen here first:

    IOS_FIXTURES=update .venv/bin/python -m pytest -q tests/test_ios_bodies.py   # rewrite them
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.test_upnext import _catalog

FIXTURES = Path(__file__).resolve().parents[2] / "ios" / "Bodies" / "Tests" / "HubBodiesTests" / "Fixtures"

# fixture name -> (path, query). Each is decoded on the phone by the model named in BodyTests.swift.
ROUTES: dict[str, tuple[str, dict[str, str]]] = {
    "home": ("/v1/home", {}),
    "me": ("/v1/me", {}),
    "smart": ("/v1/smart", {"plan": "open"}),
    "inbox": ("/v1/inbox", {}),
    "activity": ("/v1/activity", {"limit": "8"}),
    "search": ("/v1/search", {"q": "kitchen"}),
    "tv_explore": ("/v1/tv/explore", {}),
    "tv_upnext": ("/v1/tv/upnext", {}),
    "tv_guide": ("/v1/tv/guide", {}),
    "tv_players": ("/v1/tv/players", {}),
    "tv_series": ("/v1/tv/series", {}),
    "tv_movies": ("/v1/tv/movies", {}),
}


def shape(value: Any) -> Any:
    """Keys and nesting, not values: what a decoder cares about."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return None


# The test household's first names never reach the phone fixtures (AGENTS.md rule 10).
SCRUB = {"Dylan": "Parent", "Avery": "Grown-up", "Theo": "Child"}


def scrub(body: Any) -> Any:
    text = json.dumps(body)
    for name, role in SCRUB.items():
        text = text.replace(name, role)
    return json.loads(text)


# Home and Right now depend on the time of day (the "Lock up for the night" card is a pinned safety card after
# 21:30), so the bodies are captured at one fixed evening moment, whenever the suite runs.
FIXED_NOW = (2026, 11, 17, 22, 0)


def _bodies(people, hub, home_client, monkeypatch) -> dict[str, Any]:
    from datetime import datetime

    from hub.upnext import remember

    from tests.test_tvs import _tv

    at = datetime(*FIXED_NOW, tzinfo=hub.policy.tz)
    monkeypatch.setattr(hub.policy, "now", lambda: at)
    _catalog(hub)
    _, tv = _tv(hub, home_client)
    home_client.post("/v1/tv/state", headers=tv, json={"foreground": True, "playing": {
        "channel_key": "xc:thomas-creek:10", "title": "Newsroom"}})
    ph = people["phones"]["Dylan"]
    remember(hub.db, "adult", {"series_id": "series:thomas-creek:50", "episode_id": "ep:thomas-creek:900",
                               "title": "Pilot", "position_s": 80}, person=people["dylan"]["id"])
    out = {}
    for name, (path, query) in ROUTES.items():
        r = ph.client.get(path, params=query, headers=ph.h)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
        out[name] = scrub(r.json())
    return out


def test_the_phone_fixtures_match_what_the_hub_sends(hub, people, home_client, monkeypatch):
    bodies = _bodies(people, hub, home_client, monkeypatch)
    if os.environ.get("IOS_FIXTURES") == "update":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, body in bodies.items():
            (FIXTURES / f"{name}.json").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
        return
    if not FIXTURES.exists():
        pytest.skip("no ios/ checkout next to server/")
    stale = [name for name, body in bodies.items()
             if not (FIXTURES / f"{name}.json").exists()
             or shape(json.loads((FIXTURES / f"{name}.json").read_text())) != shape(body)]
    assert not stale, (f"These hub bodies changed shape: {', '.join(stale)}. Rewrite the fixtures with "
                       "IOS_FIXTURES=update and check the iPhone models still decode them (swift test in ios/Bodies).")


def test_fixtures_carry_no_addresses_or_first_names():
    names = tuple(SCRUB)
    for f in FIXTURES.glob("*.json"):
        text = f.read_text()
        assert "://" not in text.replace("thomascreek://", ""), f.name
        for n in names:
            assert n not in text, f"{f.name} carries a first name ({n})"
