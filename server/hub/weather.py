"""Weather for Home: now, today's high and low, rain in the next hours, tonight's low and sunset.

Open-Meteo (free, no key) at the house's latitude/longitude, which come from the
hub config or from Home Assistant's own location. If Open-Meteo can't be reached,
the first `weather.*` entity in Home Assistant gives at least the current reading.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.weather")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes -> (words, SF Symbol day, SF Symbol night)
WMO: dict[int, tuple[str, str, str]] = {
    0: ("Clear", "sun.max.fill", "moon.stars.fill"),
    1: ("Mostly clear", "sun.max.fill", "moon.stars.fill"),
    2: ("Partly cloudy", "cloud.sun.fill", "cloud.moon.fill"),
    3: ("Cloudy", "cloud.fill", "cloud.fill"),
    45: ("Fog", "cloud.fog.fill", "cloud.fog.fill"),
    48: ("Fog", "cloud.fog.fill", "cloud.fog.fill"),
    51: ("Drizzle", "cloud.drizzle.fill", "cloud.drizzle.fill"),
    53: ("Drizzle", "cloud.drizzle.fill", "cloud.drizzle.fill"),
    55: ("Drizzle", "cloud.drizzle.fill", "cloud.drizzle.fill"),
    56: ("Freezing drizzle", "cloud.sleet.fill", "cloud.sleet.fill"),
    57: ("Freezing drizzle", "cloud.sleet.fill", "cloud.sleet.fill"),
    61: ("Light rain", "cloud.rain.fill", "cloud.rain.fill"),
    63: ("Rain", "cloud.rain.fill", "cloud.rain.fill"),
    65: ("Heavy rain", "cloud.heavyrain.fill", "cloud.heavyrain.fill"),
    66: ("Freezing rain", "cloud.sleet.fill", "cloud.sleet.fill"),
    67: ("Freezing rain", "cloud.sleet.fill", "cloud.sleet.fill"),
    71: ("Light snow", "cloud.snow.fill", "cloud.snow.fill"),
    73: ("Snow", "cloud.snow.fill", "cloud.snow.fill"),
    75: ("Heavy snow", "cloud.snow.fill", "cloud.snow.fill"),
    77: ("Snow", "cloud.snow.fill", "cloud.snow.fill"),
    80: ("Showers", "cloud.sun.rain.fill", "cloud.moon.rain.fill"),
    81: ("Showers", "cloud.rain.fill", "cloud.rain.fill"),
    82: ("Heavy showers", "cloud.heavyrain.fill", "cloud.heavyrain.fill"),
    85: ("Snow showers", "cloud.snow.fill", "cloud.snow.fill"),
    86: ("Snow showers", "cloud.snow.fill", "cloud.snow.fill"),
    95: ("Thunderstorms", "cloud.bolt.rain.fill", "cloud.bolt.rain.fill"),
    96: ("Thunderstorms", "cloud.bolt.rain.fill", "cloud.bolt.rain.fill"),
    99: ("Thunderstorms", "cloud.bolt.rain.fill", "cloud.bolt.rain.fill"),
}
RAINY = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

# Home Assistant weather states -> (words, symbol)
HA_WORDS = {
    "clear-night": ("Clear", "moon.stars.fill"), "cloudy": ("Cloudy", "cloud.fill"), "fog": ("Fog", "cloud.fog.fill"),
    "hail": ("Hail", "cloud.hail.fill"), "lightning": ("Storms", "cloud.bolt.fill"),
    "lightning-rainy": ("Thunderstorms", "cloud.bolt.rain.fill"), "partlycloudy": ("Partly cloudy", "cloud.sun.fill"),
    "pouring": ("Heavy rain", "cloud.heavyrain.fill"), "rainy": ("Rain", "cloud.rain.fill"),
    "snowy": ("Snow", "cloud.snow.fill"), "snowy-rainy": ("Sleet", "cloud.sleet.fill"), "sunny": ("Sunny", "sun.max.fill"),
    "windy": ("Windy", "wind"), "windy-variant": ("Windy", "wind"), "exceptional": ("Unusual weather", "exclamationmark.triangle"),
}


class Weather:
    REFRESH_S = 900

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.data: dict[str, Any] | None = None  # shaped, see shape()
        self.fetched_at = 0.0
        self.last_error: str | None = None
        self._latlon: tuple[float, float] | None = None

    async def _location(self) -> tuple[float, float] | None:
        cfg = self.hub.config.smart
        if cfg.latitude is not None and cfg.longitude is not None:
            return (cfg.latitude, cfg.longitude)
        if self._latlon:
            return self._latlon
        if self.hub.ha.enabled:
            try:
                c = await self.hub.ha.get_json("/api/config")
                if c.get("latitude") is not None and c.get("longitude") is not None:
                    self._latlon = (float(c["latitude"]), float(c["longitude"]))
            except Exception as exc:  # noqa: BLE001
                log.info("HA location read failed: %s", exc)
        return self._latlon

    async def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self.fetched_at < self.REFRESH_S:
            return
        self.fetched_at = time.time()
        loc = await self._location()
        if loc:
            try:
                params = {
                    "latitude": loc[0], "longitude": loc[1],
                    "current": "temperature_2m,weather_code,is_day",
                    "hourly": "temperature_2m,precipitation_probability,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max",
                    "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
                    "timezone": self.hub.config.hub.timezone, "forecast_days": 2,
                }
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(OPEN_METEO, params=params)
                    r.raise_for_status()
                self.data = shape(r.json())
                self.last_error = None
                return
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Open-Meteo: {exc}"
                log.info("weather fetch failed: %s", exc)
        self.data = self.from_ha() or self.data

    def from_ha(self) -> dict[str, Any] | None:
        for eid, st in sorted(self.hub.home.states.items()):
            if not eid.startswith("weather.") or st.get("state") in ("unavailable", "unknown"):
                continue
            a = st.get("attributes") or {}
            words, sym = HA_WORDS.get(st.get("state", ""), (str(st.get("state", "")).capitalize(), "cloud.fill"))
            t = a.get("temperature")
            if t is not None and a.get("temperature_unit") == "°C":
                t = t * 9 / 5 + 32
            return {"temp": t, "condition": words, "symbol": sym, "source": "home_assistant"}
        return None

    def now(self) -> dict[str, Any] | None:
        """The part of the weather the app shows in the date line."""
        d = self.data
        if not d:
            return None
        out = {k: d.get(k) for k in ("temp", "condition", "symbol", "high", "low", "sunset", "sunrise")}
        return {k: v for k, v in out.items() if v is not None}


def shape(raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    cur = raw.get("current") or {}
    code = int(cur.get("weather_code") or 0)
    words, day_sym, night_sym = WMO.get(code, ("", "cloud.fill", "cloud.fill"))
    daily = raw.get("daily") or {}
    hourly = raw.get("hourly") or {}

    def at(arr: str, i: int) -> Any:
        v = daily.get(arr) or []
        return v[i] if len(v) > i else None

    # Hourly entries are local wall-clock ISO strings ("2026-11-12T15:00").
    cur_time = cur.get("time") or ""
    hours = []
    for i, t in enumerate(hourly.get("time") or []):
        if t < cur_time[:13]:
            continue
        hours.append({"t": t, "temp": _get(hourly, "temperature_2m", i),
                      "rain": _get(hourly, "precipitation_probability", i), "code": _get(hourly, "weather_code", i)})
    rain_at = None
    for h in hours[:12]:
        if (h["rain"] or 0) >= 60 or (h["code"] in RAINY and (h["rain"] or 0) >= 40):
            rain_at = h["t"]
            break
    # Tonight's low: the coldest hour from now until 9 am tomorrow.
    tonight = [h["temp"] for h in hours if h["temp"] is not None and h["t"] <= _tomorrow_9(cur_time)]
    return {
        "temp": cur.get("temperature_2m"),
        "condition": words,
        "symbol": day_sym if cur.get("is_day", 1) else night_sym,
        "code": code,
        "high": at("temperature_2m_max", 0),
        "low": at("temperature_2m_min", 0),
        "tonight_low": min(tonight) if tonight else at("temperature_2m_min", 1),
        "rain_chance": at("precipitation_probability_max", 0),
        "rain_at": rain_at,  # local "YYYY-MM-DDTHH:MM" of the first likely-rain hour in the next 12, or None
        "sunrise": at("sunrise", 0),
        "sunset": at("sunset", 0),
        "tomorrow": {"high": at("temperature_2m_max", 1), "low": at("temperature_2m_min", 1),
                     "rain_chance": at("precipitation_probability_max", 1)},
        "source": "open-meteo",
        "local_time": cur_time,
    }


def _get(d: dict[str, Any], k: str, i: int) -> Any:
    v = d.get(k) or []
    return v[i] if len(v) > i else None


def _tomorrow_9(cur_time: str) -> str:
    try:
        from datetime import timedelta

        d = datetime.fromisoformat(cur_time).date() + timedelta(days=1)
        return f"{d.isoformat()}T09:00"
    except ValueError:
        return "9999"
