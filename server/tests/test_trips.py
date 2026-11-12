from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from hub.adapters import trips as shape

BASE = "http://trek.test"

LIST = {"trips": [
    {"id": 12, "title": "Thanksgiving in Asheville", "description": None, "start_date": "2026-11-26",
     "end_date": "2026-11-30", "currency": "USD", "archived": False, "updated_at": None},
    {"id": 15, "title": "Outer Banks", "description": None, "start_date": "2027-03-27",
     "end_date": "2027-04-03", "currency": "USD", "archived": False, "updated_at": None},
    {"id": 9, "title": "Lake week", "description": None, "start_date": "2026-07-11",
     "end_date": "2026-07-18", "currency": "USD", "archived": False, "updated_at": None},
    {"id": 20, "title": "Anniversary weekend", "description": None, "start_date": "2026-12-04",
     "end_date": "2026-12-06", "currency": "USD", "archived": False, "updated_at": None},
]}

DETAIL = {
    **LIST["trips"][0],
    "days": [{"date": "2026-11-27", "day_number": 2, "title": "Biltmore", "notes": None, "places": [
        {"name": "Biltmore Estate", "address": None, "lat": 35.54, "lng": -82.55, "time": "17:30", "end_time": None,
         "duration_minutes": None, "category": None, "notes": "Candlelight tour", "transport_mode": "driving"}],
        "day_notes": [], "reservations": [
        {"type": "event", "title": "Candlelight tickets", "location": None, "time": "17:30", "end_time": None,
         "status": "confirmed", "notes": "Confirmation BLT-48213"}]}],
    "accommodations": [{"name": "Laurel Ridge cabin", "address": None, "lat": None, "lng": None,
                        "start_date": "2026-11-26", "end_date": "2026-11-29", "check_in": "16:00",
                        "check_out": "10:00", "notes": "Door code 4512"}],
    "travellers": [{"name": "Dylan", "owner": True}, {"name": "Avery", "owner": False}],
    "unplanned_places": [], "unscheduled_reservations": [],
}


def test_timing_and_order():
    today = date(2026, 11, 12)
    rows = shape.sort_trips([shape.summary(t, today) for t in LIST["trips"]])
    assert [t["id"] for t in rows] == [12, 20, 15, 9]
    assert rows[0]["status"] == "upcoming" and rows[0]["days_until"] == 14 and rows[0]["day_count"] == 5
    assert rows[-1]["status"] == "past"
    during = shape.summary(LIST["trips"][0], date(2026, 11, 28))
    assert during["status"] == "now" and during["day_number"] == 3


def test_children_lose_booking_and_stay_notes():
    kid = shape.detail(DETAIL, date(2026, 11, 12), child=True)
    assert "notes" not in kid["days"][0]["reservations"][0]
    assert "notes" not in kid["accommodations"][0]
    assert kid["days"][0]["places"][0]["notes"] == "Candlelight tour"  # what happens stays
    adult = shape.detail(DETAIL, date(2026, 11, 12), child=False)
    assert adult["accommodations"][0]["notes"] == "Door code 4512"


def test_calendar_entries():
    ev = shape.calendar_events(LIST["trips"][:1], date(2026, 11, 25), 4)
    assert [e["start"] for e in ev] == ["2026-11-26", "2026-11-27", "2026-11-28"]
    assert ev[0]["title"] == "Thanksgiving in Asheville (day 1 of 5)" and ev[0]["all_day"] is True


def _trek(hub):
    hub.config.trek.enabled = True
    hub.config.trek.url = BASE
    hub.config.trek.web_url = "https://trek.example.invalid"
    hub.config.trek.hidden_from_children = [20]
    hub.vault.set("trek.api_key", "trek_testkey")
    hub.policy.now = lambda: datetime(2026, 11, 12, 9, tzinfo=ZoneInfo("America/New_York"))


def test_trips_routes_by_role(hub, people, home_client):
    _trek(hub)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        lst = mock.get(f"{BASE}/api/v1/trips").mock(return_value=httpx.Response(200, json=LIST))
        mock.get(url__regex=rf"{BASE}/api/v1/trips/\d+.*").mock(return_value=httpx.Response(200, json=DETAIL))

        r = home_client.get("/v1/trips", headers=dylan.h).json()
        assert [t["id"] for t in r["trips"]] == [12, 20, 15, 9]
        assert r["next"]["id"] == 12 and r["trips"][0]["plan_url"] == "https://trek.example.invalid/trips/12"
        assert r["trips"][0]["travellers"][0]["name"] == "Dylan"
        sent = lst.calls[0].request.headers
        assert sent["authorization"] == "Bearer trek_testkey" and sent["x-forwarded-proto"] == "https"

        k = home_client.get("/v1/trips", headers=theo.h).json()
        assert [t["id"] for t in k["trips"]] == [12, 15, 9] and k["plan_url"] is None
        assert "plan_url" not in k["trips"][0]
        assert home_client.get("/v1/trips/20", headers=theo.h).status_code == 404
        kid_trip = home_client.get("/v1/trips/12", headers=theo.h).json()
        assert "notes" not in kid_trip["accommodations"][0] and kid_trip["plan_url"] is None

        cal = home_client.get("/v1/calendar?days=31", headers=theo.h).json()["events"]
        assert any(e["id"] == "trek-12-2026-11-26" for e in cal)
        assert not any(e["id"].startswith("trek-20-") for e in cal)


def test_trips_off_until_configured(hub, people, home_client):
    r = home_client.get("/v1/trips", headers=people["phones"]["Dylan"].h)
    assert r.status_code == 503 and r.json()["message"] == "Trips aren't set up yet"
