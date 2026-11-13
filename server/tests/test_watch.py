"""Watch this on that TV: TV on, Plex open, title started, English tracks picked (hub/watch.py)."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from hub.watch import choose_tracks

PLEX = "http://plex.test"
HA = "http://ha.test"


def _a(i, lang, default=False, title=""):
    return {"id": i, "language": lang, "default": default, "title": title or lang}


def _s(i, lang, forced=False, hi=False):
    return {"id": i, "language": lang, "forced": forced, "hearing_impaired": hi, "title": lang}


def test_english_audio_wins_and_subtitles_go_off():
    pick = choose_tracks([_a(1, "jpn", default=True), _a(2, "eng")], [_s(5, "eng")])
    assert pick == {"audio_id": 2, "subtitle_id": 0, "why": "English audio"}


def test_english_audio_keeps_forced_english_subtitles():
    pick = choose_tracks([_a(2, "eng")], [_s(5, "eng"), _s(6, "eng", forced=True)])
    assert pick["subtitle_id"] == 6


def test_foreign_film_gets_full_english_subtitles_not_forced_or_sdh():
    pick = choose_tracks([_a(1, "kor", default=True)],
                         [_s(4, "eng", forced=True), _s(5, "eng", hi=True), _s(6, "eng"), _s(7, "kor")])
    assert pick == {"audio_id": 1, "subtitle_id": 6, "why": "Original audio with English subtitles"}
    assert choose_tracks([_a(1, "kor")], [_s(5, "en-US", hi=True)])["subtitle_id"] == 5
    assert choose_tracks([_a(1, "kor")], [])["subtitle_id"] is None


META = {"MediaContainer": {"Metadata": [{
    "ratingKey": "42", "type": "movie", "title": "Parasite", "contentRating": "R", "viewOffset": 60000,
    "Media": [{"Part": [{"id": 7, "key": "/library/parts/7/file.mkv", "Stream": [
        {"id": 11, "streamType": 2, "languageCode": "kor", "default": True, "displayTitle": "Korean (AAC 5.1)"},
        {"id": 21, "streamType": 3, "languageCode": "eng", "displayTitle": "English (SRT)"},
        {"id": 22, "streamType": 3, "languageCode": "eng", "forced": True, "displayTitle": "English Forced"},
    ]}]}]}]}}


@pytest.fixture
def plex(hub):
    hub.config.plex.enabled = True
    hub.config.plex.url = PLEX
    hub.vault.set("plex.token", "tok")
    hub.config.hub.dry_run = False
    st = hub.home.states["media_player.theo_tv"]
    st["state"] = "off"
    st["attributes"].update(supported_features=128 | 256 | 1 | 16384 | 1024 | 4 | 8, friendly_name="Theo's TV")
    hub.home.media_links = {"media_player.theo_tv": {"remote": "remote.theo_tv", "platform": "androidtv_remote"}}
    return hub


def _mock_plex(mock, clients):
    mock.get(f"{PLEX}/library/metadata/42").mock(return_value=httpx.Response(200, json=META))
    mock.get(f"{PLEX}/clients").mock(return_value=httpx.Response(200, json={"MediaContainer": {"Server": clients}}))
    mock.get(f"{PLEX}/identity").mock(return_value=httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "srv1"}}))
    mock.post(f"{HA}/api/template").mock(return_value=httpx.Response(200, text=""))
    parts = mock.put(f"{PLEX}/library/parts/7").mock(return_value=httpx.Response(200))
    mock.post(f"{PLEX}/playQueues").mock(return_value=httpx.Response(200, json={"MediaContainer": {"playQueueID": 99}}))
    play = mock.get(f"{PLEX}/player/playback/playMedia").mock(return_value=httpx.Response(200))
    return parts, play


def test_watch_this_on_a_tv_turns_it_on_opens_plex_and_starts_the_movie(plex, people, home_client):
    events = []
    plex.bus.publish = lambda e, person_id=None: e["type"] == "watch" and events.append(e["data"])  # type: ignore[method-assign]  # noqa: E501
    clients = [{"name": "Theo's TV", "machineIdentifier": "tv-abc", "address": "10.9.9.9", "product": "Plex for Android (TV)"}]
    with respx.mock(assert_all_called=False) as mock:
        parts, play = _mock_plex(mock, clients)
        r = home_client.post("/v1/media/play", json={"item_id": "42", "target_device_id": "media_player.theo_tv"},
                             headers=people["phones"]["Dylan"].h)
        assert r.status_code == 200, r.text
        assert r.json()["pending"] is True
        for _ in range(100):  # the rest runs in the background once Plex is open
            if events:
                break
            time.sleep(0.02)
    assert ("media_player", "turn_on", {"entity_id": "media_player.theo_tv"}) in plex.calls
    assert ("media_player", "play_media", {"entity_id": "media_player.theo_tv", "media_content_type": "app",
                                           "media_content_id": "com.plexapp.android"}) in plex.calls
    assert events and events[-1]["ok"] is True and events[-1]["tracks"] == "Original audio with English subtitles"
    q = dict(parts.calls[0].request.url.params)
    assert q["audioStreamID"] == "11" and q["subtitleStreamID"] == "21"
    req = play.calls[0].request
    assert req.headers["X-Plex-Target-Client-Identifier"] == "tv-abc"
    assert req.url.params["key"] == "/library/metadata/42" and req.url.params["offset"] == "60000"
    assert req.url.params["containerKey"].startswith("/playQueues/99")


def test_fix_language_button(plex, people, home_client):
    with respx.mock(assert_all_called=False) as mock:
        parts, _ = _mock_plex(mock, [{"name": "Theo's TV", "machineIdentifier": "tv-abc"}])
        live = mock.get(f"{PLEX}/player/playback/setStreams").mock(return_value=httpx.Response(200))
        r = home_client.post("/v1/media/42/language", json={"target_device_id": "media_player.theo_tv"},
                             headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subtitles"] == "English (SRT)" and body["audio"] == "Korean (AAC 5.1)" and body["switched_now"]
    assert live.calls[0].request.url.params["subtitleStreamID"] == "21"
    assert json.loads(json.dumps(body))["message"] == "Original audio with English subtitles"
