"""Weather page: shaping each source, places, who sees what, radar tiles, alert pushes and the warning card."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from hub import forecast as F

HOME = (38.9, -77.0)

OM = {
    "timezone": "America/New_York", "utc_offset_seconds": -18000,
    "current": {"time": "2026-11-12T15:00", "temperature_2m": 64.2, "apparent_temperature": 63.0,
                "relative_humidity_2m": 86, "dew_point_2m": 60.1, "wind_speed_10m": 16.0, "wind_direction_10m": 190,
                "wind_gusts_10m": 38.0, "pressure_msl": 1005.0, "visibility": 6437.0, "uv_index": 1.0,
                "cloud_cover": 95, "precipitation": 0.0, "weather_code": 95, "is_day": 1},
    "minutely_15": {"time": ["2026-11-12T15:00", "2026-11-12T15:15", "2026-11-12T15:30", "2026-11-12T15:45"],
                    "precipitation": [0.0, 0.0, 0.12, 0.2]},
    "hourly": {"time": [f"2026-11-12T{h:02d}:00" for h in range(12, 24)],
               "temperature_2m": [60, 62, 63, 64, 66, 65, 63, 60, 58, 55, 52, 50],
               "precipitation_probability": [10, 20, 40, 80, 90, 70, 40, 20, 10, 5, 5, 0],
               "precipitation": [0] * 12, "weather_code": [3, 3, 61, 95, 95, 63, 61, 3, 3, 2, 1, 0],
               "is_day": [1] * 6 + [0] * 6, "wind_speed_10m": [10] * 12, "wind_gusts_10m": [20] * 12,
               "pressure_msl": [1010, 1009, 1008, 1005, 1004, 1004, 1005, 1006, 1007, 1008, 1009, 1010]},
    "daily": {"time": ["2026-11-12", "2026-11-13"], "weather_code": [95, 3], "temperature_2m_max": [68.4, 55.0],
              "temperature_2m_min": [44.0, 38.0], "precipitation_probability_max": [90, 20],
              "precipitation_sum": [0.81, 0.0], "snowfall_sum": [0, 0], "wind_speed_10m_max": [20, 12],
              "wind_gusts_10m_max": [40, 22], "wind_direction_10m_dominant": [200, 315], "uv_index_max": [2.1, 3.4],
              "sunrise": ["2026-11-12T06:51", "2026-11-13T06:52"], "sunset": ["2026-11-12T16:58", "2026-11-13T16:57"]},
}

NWS_ALERTS = {"features": [
    {"id": "a2", "geometry": None, "properties": {
        "id": "urn:wind", "status": "Actual", "messageType": "Alert", "event": "Wind Advisory", "severity": "Moderate",
        "headline": "Wind Advisory issued", "areaDesc": "Fairfax", "senderName": "NWS Sterling VA",
        "onset": "2026-11-12T13:00:00-05:00", "ends": "2026-11-12T19:00:00-05:00", "description": "Gusts to 50.",
        "instruction": "Secure objects.", "parameters": {}, "references": []}},
    {"id": "a1", "geometry": {"type": "Polygon", "coordinates": [[[-77.1, 38.8], [-76.9, 38.8], [-77.0, 39.0], [-77.1, 38.8]]]},
     "properties": {
        "id": "urn:svr", "status": "Actual", "messageType": "Alert", "event": "Severe Thunderstorm Warning",
        "severity": "Severe", "certainty": "Observed", "urgency": "Immediate", "headline": "SVR issued",
        "areaDesc": "Fairfax", "senderName": "NWS Sterling VA", "onset": "2026-11-12T13:52:00-05:00",
        "ends": "2026-11-12T14:45:00-05:00", "description": "60 mph wind gusts.", "instruction": "Move inside.",
        "parameters": {"NWSheadline": ["A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT"]}, "references": []}},
    {"id": "a3", "geometry": None, "properties": {
        "id": "urn:cancel", "status": "Actual", "messageType": "Cancel", "event": "Flood Watch", "references": []}},
]}

NWS_POINT = {"properties": {"forecast": "https://api.weather.gov/gridpoints/LWX/98,72/forecast", "gridId": "LWX",
                            "radarStation": "KLWX", "timeZone": "America/New_York",
                            "relativeLocation": {"properties": {"city": "Washington", "state": "DC"}}}}
NWS_TEXT = {"properties": {"periods": [
    {"name": "This Afternoon", "startTime": "2026-11-12T13:00:00-05:00", "isDaytime": True, "temperature": 68,
     "shortForecast": "Thunderstorms", "detailedForecast": "Showers and thunderstorms, some severe."},
    {"name": "Tonight", "startTime": "2026-11-12T18:00:00-05:00", "isDaytime": False, "temperature": 44,
     "shortForecast": "Clearing", "detailedForecast": "Clearing, low around 44."}]}}

CPC_610 = {"results": [
    {"layerId": 0, "attributes": {"Forecast Date": "11/12/2026", "Start Date": "11/18/2026", "End Date": "11/22/2026",
                                  "Probability": "70", "Category": "Above"}},
    {"layerId": 1, "attributes": {"Forecast Date": "11/12/2026", "Start Date": "11/18/2026", "End Date": "11/22/2026",
                                  "Probability": "33", "Category": "Below"}}]}
CPC_MONTH = {"results": [{"layerId": 0, "attributes": {"fcst_date": "10/31/2026", "valid_seas": "Nov 2026",
                                                       "prob": "50", "cat": "Above"}}]}
CPC_SEA = {"results": [
    {"layerId": 0, "attributes": {"Forecast Date": "10/15/2026", "Valid Season": "DJF 2026-2027", "Probability": "40",
                                  "Category": "Above"}},
    {"layerId": 1, "attributes": {"Valid Season": "JFM 2027", "Probability": "50", "Category": "Above"}}]}

BRIGHT_SKY = {"location": {"name": "Berl. - Mitte"}, "alerts": [{
    "alert_id": 3053026, "status": "actual", "severity": "moderate", "certainty": "likely", "urgency": "immediate",
    "event_en": "gale-force gusts", "event_de": "STURMBÖEN", "headline_en": "Official WARNING of GALE-FORCE GUSTS",
    "headline_de": "Amtliche WARNUNG vor STURMBÖEN", "description_en": "Gusts up to 80 km/h.",
    "instruction_en": "Secure objects.", "onset": "2026-11-13T02:00:00+00:00", "expires": "2026-11-13T23:00:00+00:00"}]}


# -- shaping ---------------------------------------------------------------------------

def test_forecast_shape_now_hours_days_and_next_hour():
    f = F.shape_forecast(OM)
    assert f["now"]["temp"] == 64 and f["now"]["condition"] == "Thunderstorms"
    assert f["now"]["wind_dir"] == "S" and f["now"]["pressure"] == 29.68 and f["now"]["pressure_trend"] == "falling"
    assert f["now"]["visibility"] == 4.0 and f["now"]["uv_words"] == "Low"
    assert f["hourly"][0]["t"] == "2026-11-12T15:00" and len(f["hourly"]) == 9
    assert f["daily"][0]["high"] == 68 and f["daily"][1]["wind_dir"] == "NW"
    assert f["next_hour"] == "Heavy rain starting in about 30 min."


def test_next_hour_wording():
    assert F.next_hour([{"precip": 0}] * 8, [], {}) == "No rain in the next two hours."
    assert F.next_hour([{"precip": 0.05}] * 8, [], {"code": 73}) == "Snow continuing for the next two hours."
    assert F.next_hour([{"precip": 0.05}, {"precip": 0.01}, {"precip": 0}], [], {}) == "Rain ending in about 30 min."


def test_nws_alerts_sorted_warning_first_cancel_dropped():
    a = F.shape_nws_alerts(NWS_ALERTS)
    assert [x["event"] for x in a] == ["Severe Thunderstorm Warning", "Wind Advisory"]
    svr = a[0]
    assert svr["level"] == "warning" and not svr["urgent"]
    assert svr["headline"] == "A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT"
    assert svr["polygon"][0] == [-77.1, 38.8]
    assert a[1]["level"] == "advisory"
    tor = F.shape_nws_alerts({"features": [{"properties": {"id": "t", "event": "Tornado Warning", "severity": "Extreme"}}]})
    assert tor[0]["urgent"]


def test_dwd_alerts_in_english_with_german_original():
    a = F.shape_dwd_alerts(BRIGHT_SKY)[0]
    assert a["event"] == "Gale-force gusts (level 2 of 4)" and a["level"] == "watch"
    assert a["headline_local"].startswith("Amtliche") and a["area"] == "Berl. - Mitte" and a["source"] == "DWD"


def test_cpc_outlooks_read_both_attribute_styles():
    rows = F.shape_cpc({"6_10": CPC_610["results"], "8_14": None, "month_temp": CPC_MONTH["results"],
                        "month_precip": [], "season_temp": CPC_SEA["results"], "season_precip": []})
    ids = [r["id"] for r in rows]
    assert ids == ["6_10", "month", "season"]
    six = rows[0]
    assert six["dates"] == "Nov 18–22" and F.span("11/28/2026", "12/02/2026") == "Nov 28–Dec 2"
    assert six["temp"]["words"] == "70% chance warmer than normal" and six["temp"]["tone"] == "warm"
    assert six["precip"]["words"] == "33% chance drier than normal"
    month = rows[1]
    assert month["title"] == "Nov 2026" and month["precip"]["words"] == "equal chances"
    assert rows[2]["title"] == "Dec–Feb" and rows[2]["temp"]["prob"] == 40  # lead 1 only


def test_seasonal_words():
    rows = F.shape_seasonal({"monthly": {"time": ["2026-12-01", "2027-01-01"], "temperature_2m_anomaly": [1.4, 0.1],
                                         "precipitation_anomaly": [12.0, -10.0]}})
    assert rows[0]["title"] == "December 2026" and rows[0]["temp"]["tone"] == "warm" and rows[0]["precip"]["tone"] == "wet"
    assert rows[1]["temp"]["tone"] == "even" and rows[1]["precip"]["tone"] == "dry"


def test_radar_frames_and_tile_urls(hub):
    fr = F.iem_frames("2026-11-13T01:50:00Z")
    assert len(fr) == 11 and fr[-1] == {"id": "now", "t": "2026-11-13T01:50:00Z", "future": False}
    assert fr[0]["id"] == "m50m" and fr[0]["t"] == "2026-11-13T01:00:00Z"
    dwd = F.dwd_frames(datetime(2026, 11, 13, 2, 7, tzinfo=timezone.utc))
    assert dwd[10]["t"] == "2026-11-13T02:00:00Z" and not dwd[10]["future"] and dwd[-1]["future"]
    fc = hub.forecasts
    assert fc.tile_url("nexrad", "m10m", 6, 18, 24).endswith("nexrad-n0q-900913-m10m/6/18/24.png")
    assert fc.tile_url("nexrad", "m07m", 6, 18, 24) is None
    assert "time=2026-11-13T02:00:00.000Z" in fc.tile_url("dwd", "20261113t0200", 6, 34, 21)
    assert fc.tile_url("rainviewer", "abc", 8, 1, 1) is None


def test_wear_for_kids():
    cold = F.wear({"now": {"temp": 30}, "daily": [{"high": 34, "rain_chance": 0, "snow": 2}]})
    assert [w["words"] for w in cold] == ["Winter coat", "Gloves and hat", "Snow boots"]
    wet = F.wear({"now": {"temp": 60}, "daily": [{"high": 64, "rain_chance": 80}]})
    assert "Umbrella" in [w["words"] for w in wet]


# -- the page --------------------------------------------------------------------------------

def mock_us(mock):
    mock.get(url__startswith=F.OPEN_METEO).mock(return_value=httpx.Response(200, json=OM))
    mock.get(url__startswith=F.AIR).mock(return_value=httpx.Response(200, json={"current": {"us_aqi": 34}}))
    mock.get(url__startswith=f"{F.NWS}/points/").mock(return_value=httpx.Response(200, json=NWS_POINT))
    mock.get(url__startswith=f"{F.NWS}/alerts/active").mock(return_value=httpx.Response(200, json=NWS_ALERTS))
    mock.get(url__startswith="https://api.weather.gov/gridpoints/").mock(return_value=httpx.Response(200, json=NWS_TEXT))
    mock.get(url__regex=r".*cpc_6_10_day_outlk.*").mock(return_value=httpx.Response(200, json=CPC_610))
    mock.get(url__regex=r".*cpc_8_14_day_outlk.*").mock(return_value=httpx.Response(200, json=CPC_610))
    mock.get(url__regex=r".*cpc_mthly_.*").mock(return_value=httpx.Response(200, json=CPC_MONTH))
    mock.get(url__regex=r".*cpc_sea_.*").mock(return_value=httpx.Response(200, json=CPC_SEA))
    mock.get(F.IEM_TMS).mock(return_value=httpx.Response(200, json={"services": [
        {"id": "ridge_uscomp_n0q", "utc_valid": "2026-11-13T01:50:00Z"}]}))


@pytest.fixture
def at_home(hub):
    hub.config.smart.latitude, hub.config.smart.longitude = HOME
    return hub


def test_page_for_grown_up(at_home, people):
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        r = people["phones"]["Dylan"].client.get("/v1/weather", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["place"]["kind"] == "home" and d["place"]["country"] == "US"
    assert d["now"]["temp"] == 64 and d["air"]["words"] == "Good"
    assert d["summary"].startswith("Showers and thunderstorms")
    assert d["daily"][0]["text_day"].startswith("Showers") and d["daily"][0]["text_night"].startswith("Clearing")
    assert d["alerts"][0]["event"] == "Severe Thunderstorm Warning"
    assert d["radar"]["provider"] == "nexrad" and len(d["radar"]["frames"]) == 11
    assert [o["id"] for o in d["outlook"]] == ["6_10", "8_14", "month", "season"]
    assert d["places"][0]["id"] == "home"


def test_child_gets_pictures_not_alert_text_or_radar(at_home, people):
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        ph = people["phones"]["Theo"]
        d = ph.client.get("/v1/weather", headers=ph.h).json()
        tile = ph.client.get("/v1/weather/radar/nexrad/now/6/18/24.png", headers=ph.h)
    assert "radar" not in d and "outlook" not in d and d["wear"]
    assert d["alerts"] == [{"id": "urn:svr", "event": "Severe Thunderstorm Warning", "level": "warning"}]
    assert tile.status_code == 403


def test_here_near_home_is_home_and_far_away_is_named(at_home, people):
    ph = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        near = ph.client.get("/v1/weather", params={"place": "here", "lat": 38.95, "lon": -77.05}, headers=ph.h).json()
        far = ph.client.get("/v1/weather", params={"place": "here", "lat": 35.6, "lon": -82.5}, headers=ph.h).json()
    assert near["place"]["kind"] == "home"
    assert far["place"]["kind"] == "here" and far["place"]["name"] == "Washington, DC"
    assert far["place"]["lat"] == 35.6


def test_germany_uses_dwd_alerts_radar_and_seasonal(hub, people):
    hub.config.smart.latitude, hub.config.smart.longitude = (48.14, 11.58)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=F.OPEN_METEO).mock(return_value=httpx.Response(200, json=OM))
        mock.get(url__startswith=F.AIR).mock(return_value=httpx.Response(200, json={"current": {}}))
        mock.get(url__startswith=f"{F.NWS}/points/").mock(return_value=httpx.Response(404, json={}))
        mock.get(url__startswith=F.BRIGHT_SKY).mock(return_value=httpx.Response(200, json=BRIGHT_SKY))
        mock.get(url__startswith=F.SEASONAL).mock(return_value=httpx.Response(200, json={"monthly": {
            "time": ["2026-12-01"], "temperature_2m_anomaly": [1.4], "precipitation_anomaly": [5.1]}}))
        ph = people["phones"]["Avery"]
        d = ph.client.get("/v1/weather", headers=ph.h).json()
    assert d["place"]["country"] == "DE" and d["radar"]["provider"] == "dwd"
    assert d["alerts"][0]["source"] == "DWD" and d["outlook"][0]["title"] == "December 2026"
    assert d["air"] is None and d["summary"] is None


def test_trip_place_from_trek(at_home, people):
    hub = at_home
    hub.config.trek.enabled = True
    hub.config.trek.url = "http://trek.test"
    hub.smart.trips = [{"id": 7, "title": "Germany", "status": "upcoming", "start_date": "2026-11-16",
                        "end_date": "2026-11-26"},
                       {"id": 8, "title": "Beach", "status": "upcoming", "start_date": "2027-06-01"}]

    async def trip(tid):
        return {"accommodations": [{"name": "Hotel", "lat": None, "lng": None}],
                "days": [{"places": [{"name": "Marienplatz", "lat": 48.137, "lng": 11.575}]}]}

    hub.trek.trip = trip  # type: ignore[method-assign]
    hub.policy.now = lambda: datetime(2026, 11, 12, 12, tzinfo=hub.policy.tz)  # type: ignore[method-assign]
    assert [p["id"] for p in hub.forecasts.places()] == ["home", "trip:7"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=F.OPEN_METEO).mock(return_value=httpx.Response(200, json=OM))
        mock.get(url__startswith=F.AIR).mock(return_value=httpx.Response(200, json={"current": {}}))
        mock.get(url__startswith=f"{F.NWS}/points/").mock(return_value=httpx.Response(404, json={}))
        mock.get(url__startswith=F.BRIGHT_SKY).mock(return_value=httpx.Response(200, json={"alerts": []}))
        mock.get(url__startswith=F.SEASONAL).mock(return_value=httpx.Response(200, json={"monthly": {}}))
        ph = people["phones"]["Dylan"]
        d = ph.client.get("/v1/weather", params={"place": "trip:7"}, headers=ph.h).json()
        gone = ph.client.get("/v1/weather", params={"place": "trip:8"}, headers=ph.h)
    assert d["place"]["name"] == "Germany" and d["place"]["spot"] == "Marienplatz" and d["place"]["country"] == "DE"
    assert gone.status_code == 404


def test_radar_tile_is_proxied_and_cached(at_home, people):
    ph = people["phones"]["Dylan"]
    png = b"\x89PNG\r\n\x1a\nfake"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*nexrad-n0q-900913-m10m/6/18/24\.png").mock(
            return_value=httpx.Response(200, content=png, headers={"content-type": "image/png"}))
        a = ph.client.get("/v1/weather/radar/nexrad/m10m/6/18/24.png", headers=ph.h)
        b = ph.client.get("/v1/weather/radar/nexrad/m10m/6/18/24.png", headers=ph.h)
        bad = ph.client.get("/v1/weather/radar/nexrad/m10m/6/99/24.png", headers=ph.h)
        evil = ph.client.get("/v1/weather/radar/elsewhere/x/1/0/0.png", headers=ph.h)
    assert a.status_code == 200 and a.content == png and b.content == png
    assert route.call_count == 1
    assert bad.status_code == 204 and evil.status_code == 404


# -- pushes and the card -----------------------------------------------------------------

@pytest.fixture
def pushed(hub, people):
    out = []
    orig = hub.notify.send

    async def send(**kw):
        out.append(kw)
        return await orig(**kw)

    hub.notify.send = send  # type: ignore[method-assign]
    hub.config.smart.latitude, hub.config.smart.longitude = HOME
    return out


async def test_warnings_push_once_to_grown_ups_and_advisories_wait_for_opt_in(hub, people, pushed):
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        n = await hub.wx_watch.check()
        again = await hub.wx_watch.check()
    names = sorted(hub.db.person(p["person_ids"][0])["name"] for p in pushed)
    assert names == ["Avery", "Dylan"]  # the warning only: advisories are off by default, Theo is a child
    assert n == 2 and again == 0
    w = pushed[0]
    assert w["cat"] == "weather" and not w["silent"] and w["level"] == "act_now"
    assert w["title"] == "Severe Thunderstorm Warning" and w["ref"] == "thomascreek://weather/alert/urn:svr"
    hist = hub.alerts.history(people["dylan"]["id"], kind="weather")
    assert {h["title"] for h in hist} == {"Severe Thunderstorm Warning", "Wind Advisory"}


async def test_watch_off_and_quiet_hours(hub, people, pushed):
    hub.alerts.set_prefs(people["avery"]["id"], {"weather": {"warnings": False}})
    hub.alerts.set_prefs(people["dylan"]["id"], {"weather": {"advisories": True},
                                                 "quiet": {"on": True, "from": "00:00", "to": "23:59"}})
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        await hub.wx_watch.check()
    assert all(hub.db.person(p["person_ids"][0])["name"] == "Dylan" for p in pushed)
    assert len(pushed) == 2 and all(p["silent"] for p in pushed)  # quiet hours: not an urgent warning


async def test_tornado_rings_through_quiet_hours(hub, people, pushed):
    hub.alerts.set_prefs(people["dylan"]["id"], {"quiet": {"on": True, "from": "00:00", "to": "23:59"}})
    tor = {"features": [{"properties": {"id": "urn:tor", "status": "Actual", "event": "Tornado Warning",
                                        "severity": "Extreme", "references": []}}]}
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        mock.get(url__startswith=f"{F.NWS}/alerts/active").mock(return_value=httpx.Response(200, json=tor))
        await hub.wx_watch.check()
    dylan = [p for p in pushed if p["person_ids"] == [people["dylan"]["id"]]]
    assert dylan and not dylan[0]["silent"]


async def test_warning_card_and_home_line(hub, people, pushed):
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        await hub.forecasts.home_alerts()
    dylan = hub.subject_for(hub.db.device(people["phones"]["Dylan"].device_id), True)
    theo = hub.subject_for(hub.db.device(people["phones"]["Theo"].device_id), True)
    cards = hub.smart.cards(dylan)
    assert cards[0]["rule"] == "wx_alert" and cards[0]["title"] == "Severe Thunderstorm Warning"
    assert cards[0]["ref"] == "thomascreek://weather/alert/urn:svr"
    assert "wx_alert" not in [c["rule"] for c in hub.smart.cards(theo)]
    w = hub.home._weather_now(theo)
    assert w["alert"]["event"] == "Severe Thunderstorm Warning" and "alert_count" not in w


def test_alert_detail_route(at_home, people):
    ph = people["phones"]["Dylan"]
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        ok = ph.client.get("/v1/weather/alerts/urn:svr", headers=ph.h)
        gone = ph.client.get("/v1/weather/alerts/urn:nope", headers=ph.h)
        kid = people["phones"]["Theo"].client.get("/v1/weather/alerts/urn:svr", headers=people["phones"]["Theo"].h)
    assert ok.status_code == 200 and ok.json()["instruction"] == "Move inside."
    assert gone.status_code == 404 and kid.status_code == 403


# -- build 24 QA ------------------------------------------------------------------------

def test_tonight_low_is_tonights_not_this_mornings():
    f = F.shape_forecast(OM)
    assert f["daily"][0]["low"] == 44 and f["tonight_low"] == 50 and f["evening"]  # coldest hour left in the fixture
    assert F.nws_tonight(F.shape_nws_text(NWS_TEXT)) == 44
    assert F.nws_tonight([{"day": True, "temp": 60}, {"day": True, "temp": 61}, {"day": False, "temp": 40}]) is None


def test_page_tonight_low_matches_the_forecast_text(at_home, people):
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        ph = people["phones"]["Dylan"]
        d = ph.client.get("/v1/weather", headers=ph.h).json()
    assert d["tonight_low"] == 44 and "low around 44" in d["daily"][0]["text_night"]
    # the cached forecast underneath wasn't changed by the text
    hit = next(v for k, v in at_home.forecasts._c.items() if k.startswith("forecast:"))
    assert hit[1]["tonight_low"] == 50 and "text_night" not in hit[1]["daily"][0]


async def test_home_line_uses_the_weather_pages_forecast(hub, people):
    hub.config.smart.latitude, hub.config.smart.longitude = HOME
    hub.smart.weather.data = {"temp": 49.4, "condition": "Cloudy", "symbol": "cloud.fill", "high": 60, "low": 40}
    with respx.mock(assert_all_called=False) as mock:
        mock_us(mock)
        await hub.forecasts.warm_home()
    dylan = hub.subject_for(hub.db.device(people["phones"]["Dylan"].device_id), True)
    w = hub.home._weather_now(dylan)
    assert w["temp"] == 64 and w["condition"] == "Thunderstorms" and w["symbol"] == "cloud.bolt.rain.fill"
    assert w["high"] == 68 and w["low"] == 50  # evening: tonight's low
