"""The Weather page: now, the next hours and days, official alerts, radar and the weeks and months ahead.

Free sources only, no keys. The hub fetches and caches everything, so the app only ever talks to the
pinned hub (at home and away) and the family's phones never call outside weather services.

- Forecast (now, 15-minute rain, 48 hours, 10 days): Open-Meteo. It blends NOAA models in the US and
  DWD's ICON model in Germany on its own.
- Air quality: Open-Meteo air quality (US AQI).
- US: National Weather Service (api.weather.gov) for the written forecast and alerts, NOAA's Climate
  Prediction Center outlooks (6-10 day, 8-14 day, month, 3 months) read at the exact place, and NEXRAD
  radar tiles from Iowa State's mesonet (every 5 minutes, last 50 minutes).
- Germany: Deutscher Wetterdienst warnings through Bright Sky (with English text) and DWD's own radar
  (every 5 minutes, including its 2-hour nowcast).
- Elsewhere: Open-Meteo seasonal (ECMWF) for the months ahead, RainViewer radar (zoom 7 at most).

Places: "home" (the house), "here" (a phone's location, sent by the app, when it is away from home) and
"trip:<id>" (a TREK trip's first stop with coordinates, from a week before it starts until it ends).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .weather import WMO

if TYPE_CHECKING:
    from .policy import Subject
    from .state import Hub

log = logging.getLogger("hub.forecast")

UA = {"User-Agent": "ThomasCreekHub/1.0 (family home weather page)", "Accept": "application/geo+json, application/json"}
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
SEASONAL = "https://seasonal-api.open-meteo.com/v1/seasonal"
NWS = "https://api.weather.gov"
BRIGHT_SKY = "https://api.brightsky.dev"
CPC = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks"
IEM_TMS = "https://mesonet.agron.iastate.edu/json/tms.json"
IEM_TILE = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/{layer}/{z}/{x}/{y}.png"
DWD_WMS = "https://maps.dwd.de/geoserver/dwd/wms"
RAINVIEWER = "https://api.rainviewer.com/public/weather-maps.json"

TTL = {"forecast": 600, "air": 1800, "alerts": 120, "text": 1800, "outlook": 6 * 3600, "points": 7 * 86400,
       "radar": 120}
NEAR_HOME_MILES = 25
# Germany's radar composite (DWD) covers roughly this box.
DE_BOX = (47.0, 55.2, 5.5, 15.5)
RADAR_KEEP_S = 3 * 3600
TILE_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def compass(deg: float | None) -> str | None:
    if deg is None:
        return None
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]


def symbol(code: int | None, day: bool = True) -> str:
    words, d, n = WMO.get(int(code or 0), ("", "cloud.fill", "cloud.fill"))
    return d if day else n


def words(code: int | None) -> str:
    return WMO.get(int(code or 0), ("Unknown", "", ""))[0]


def aqi_words(v: float | None) -> str | None:
    if v is None:
        return None
    for top, w in ((50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"), (200, "Unhealthy"),
                   (300, "Very unhealthy")):
        if v <= top:
            return w
    return "Hazardous"


def uv_words(v: float | None) -> str | None:
    if v is None:
        return None
    return "Low" if v < 3 else "Moderate" if v < 6 else "High" if v < 8 else "Very high" if v < 11 else "Extreme"


def _at(d: dict[str, Any], k: str, i: int) -> Any:
    v = d.get(k) or []
    return v[i] if len(v) > i else None


def _r(v: Any, n: int = 0) -> Any:
    if v is None:
        return None
    return round(float(v), n) if n else round(float(v))


# ---------------------------------------------------------------------------
# Shaping Open-Meteo

def shape_forecast(raw: dict[str, Any]) -> dict[str, Any]:
    cur = raw.get("current") or {}
    day = bool(cur.get("is_day", 1))
    code = cur.get("weather_code")
    now = {
        "temp": _r(cur.get("temperature_2m")), "feels": _r(cur.get("apparent_temperature")),
        "condition": words(code), "symbol": symbol(code, day), "code": code, "is_day": day,
        "humidity": cur.get("relative_humidity_2m"), "dew_point": _r(cur.get("dew_point_2m")),
        "wind": _r(cur.get("wind_speed_10m")), "wind_dir": compass(cur.get("wind_direction_10m")),
        "gust": _r(cur.get("wind_gusts_10m")), "pressure": _r((cur.get("pressure_msl") or 0) * 0.02953, 2) or None,
        "visibility": _r((cur.get("visibility") or 0) / 1609.34, 1) if cur.get("visibility") is not None else None,
        "uv": _r(cur.get("uv_index"), 1), "cloud": cur.get("cloud_cover"), "precip": cur.get("precipitation"),
    }
    now["uv_words"] = uv_words(now["uv"])
    if now["visibility"] is not None and now["visibility"] > 10:
        now["visibility"] = 10
    cur_t = cur.get("time") or ""

    hourly = raw.get("hourly") or {}
    hours = []
    pressures = []
    for i, t in enumerate(hourly.get("time") or []):
        if t[:13] < cur_t[:13]:
            p = _at(hourly, "pressure_msl", i)
            if p is not None:
                pressures.append(p)
            continue
        if len(hours) >= 48:
            break
        c = _at(hourly, "weather_code", i)
        hours.append({"t": t, "temp": _r(_at(hourly, "temperature_2m", i)), "code": c,
                      "symbol": symbol(c, bool(_at(hourly, "is_day", i) if _at(hourly, "is_day", i) is not None else 1)),
                      "rain_chance": _at(hourly, "precipitation_probability", i), "precip": _at(hourly, "precipitation", i),
                      "wind": _r(_at(hourly, "wind_speed_10m", i)), "gust": _r(_at(hourly, "wind_gusts_10m", i))})
    if pressures and cur.get("pressure_msl") is not None:
        diff = cur["pressure_msl"] - pressures[max(0, len(pressures) - 3)]
        now["pressure_trend"] = "rising" if diff > 0.7 else "falling" if diff < -0.7 else "steady"

    daily = raw.get("daily") or {}
    days = []
    for i, d in enumerate(daily.get("time") or []):
        c = _at(daily, "weather_code", i)
        days.append({"date": d, "high": _r(_at(daily, "temperature_2m_max", i)), "low": _r(_at(daily, "temperature_2m_min", i)),
                     "code": c, "symbol": symbol(c), "condition": words(c),
                     "rain_chance": _at(daily, "precipitation_probability_max", i),
                     "precip": _r(_at(daily, "precipitation_sum", i), 2), "snow": _r(_at(daily, "snowfall_sum", i), 1),
                     "wind": _r(_at(daily, "wind_speed_10m_max", i)), "gust": _r(_at(daily, "wind_gusts_10m_max", i)),
                     "wind_dir": compass(_at(daily, "wind_direction_10m_dominant", i)),
                     "uv": _r(_at(daily, "uv_index_max", i), 1),
                     "sunrise": _at(daily, "sunrise", i), "sunset": _at(daily, "sunset", i)})
    today = days[0] if days else {}
    m15 = raw.get("minutely_15") or {}
    soon = [{"t": t, "precip": _at(m15, "precipitation", i)} for i, t in enumerate(m15.get("time") or []) if t >= cur_t[:16]][:8]
    return {"now": now, "hourly": hours, "daily": days, "minutely": soon, "next_hour": next_hour(soon, hours, now),
            "local_time": cur_t, "tz": raw.get("timezone"), "utc_offset_s": raw.get("utc_offset_seconds"),
            "sunrise": today.get("sunrise"), "sunset": today.get("sunset")}


def next_hour(soon: list[dict[str, Any]], hours: list[dict[str, Any]], now: dict[str, Any]) -> str:
    """One plain line about rain or snow in the next two hours."""
    wet = [(i, s["precip"] or 0) for i, s in enumerate(soon)]
    snowy = now.get("code") in (71, 73, 75, 77, 85, 86)
    what = "Snow" if snowy else "Rain"
    if not wet:
        return ""
    raining = wet[0][1] >= 0.005
    if raining:
        dry = next((i for i, v in wet if v < 0.005), None)
        if dry is None:
            return f"{what} continuing for the next two hours."
        return f"{what} ending in about {max(15, dry * 15)} min."
    start = next((i for i, v in wet if v >= 0.005), None)
    if start is None:
        return "No rain in the next two hours."
    heavy = max(v for _, v in wet) >= 0.1
    return f"{'Heavy ' + what.lower() if heavy else what} starting in about {start * 15} min."


# ---------------------------------------------------------------------------
# Alerts

def nws_level(event: str, severity: str | None) -> str:
    e = event.lower()
    if "warning" in e or severity in ("Extreme",):
        return "warning"
    if "watch" in e:
        return "watch"
    if "advisory" in e:
        return "advisory"
    return "statement"


URGENT_EVENTS = ("tornado warning", "flash flood warning", "extreme wind warning", "tsunami warning",
                 "hurricane warning", "storm surge warning", "flash flood emergency")


def shape_nws_alerts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in raw.get("features") or []:
        p = f.get("properties") or {}
        if p.get("status") not in (None, "Actual") or p.get("messageType") == "Cancel":
            continue
        ev = p.get("event") or "Weather alert"
        geom = f.get("geometry") or None
        poly = None
        if geom and geom.get("type") == "Polygon":
            poly = [[round(x, 4), round(y, 4)] for x, y in (geom.get("coordinates") or [[]])[0]][:400]
        params = p.get("parameters") or {}
        out.append({
            "id": p.get("id") or f.get("id"), "source": "NWS", "event": ev, "level": nws_level(ev, p.get("severity")),
            "severity": p.get("severity"), "certainty": p.get("certainty"), "urgency": p.get("urgency"),
            "urgent": ev.lower() in URGENT_EVENTS or p.get("severity") == "Extreme",
            "headline": ((params.get("NWSheadline") or [None])[0]) or p.get("headline"),
            "area": p.get("areaDesc"), "sender": p.get("senderName") or "National Weather Service",
            "sent": p.get("sent"), "onset": p.get("onset") or p.get("effective"), "ends": p.get("ends") or p.get("expires"),
            "description": (p.get("description") or "").strip() or None,
            "instruction": (p.get("instruction") or "").strip() or None,
            "update": p.get("messageType") == "Update",
            "replaces": [r.get("identifier") for r in p.get("references") or [] if r.get("identifier")],
            "polygon": poly,
        })
    order = {"warning": 0, "watch": 1, "advisory": 2, "statement": 3}
    out.sort(key=lambda a: (not a["urgent"], order[a["level"]], a.get("onset") or ""))
    return out


DWD_LEVEL = {"minor": "advisory", "moderate": "watch", "severe": "warning", "extreme": "warning"}


def shape_dwd_alerts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    loc = raw.get("location") or {}
    for a in raw.get("alerts") or []:
        if a.get("status") not in (None, "actual"):
            continue
        sev = (a.get("severity") or "").lower()
        ev = (a.get("event_en") or a.get("event_de") or "Weather warning").strip()
        level_no = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}.get(sev)
        out.append({
            "id": f"dwd-{a.get('alert_id') or a.get('id')}", "source": "DWD",
            "event": ev[:1].upper() + ev[1:] + (f" (level {level_no} of 4)" if level_no else ""),
            "level": DWD_LEVEL.get(sev, "advisory"), "severity": sev.capitalize() or None,
            "certainty": (a.get("certainty") or "").capitalize() or None, "urgency": a.get("urgency"),
            "urgent": sev == "extreme",
            "headline": a.get("headline_en") or a.get("headline_de"), "headline_local": a.get("headline_de"),
            "area": loc.get("name") or loc.get("district"), "sender": "Deutscher Wetterdienst",
            "sent": a.get("effective"), "onset": a.get("onset") or a.get("effective"), "ends": a.get("expires"),
            "description": a.get("description_en") or a.get("description_de"),
            "instruction": a.get("instruction_en") or a.get("instruction_de"),
            "update": False, "replaces": [], "polygon": None,
        })
    out.sort(key=lambda a: ({"warning": 0, "watch": 1, "advisory": 2}.get(a["level"], 3), a.get("onset") or ""))
    return out


# ---------------------------------------------------------------------------
# NWS written forecast

def shape_nws_text(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for p in (raw.get("properties") or {}).get("periods") or []:
        out.append({"name": p.get("name"), "start": p.get("startTime"), "day": p.get("isDaytime"),
                    "temp": p.get("temperature"), "short": p.get("shortForecast"), "text": p.get("detailedForecast")})
    return out[:14]


def attach_text(days: list[dict[str, Any]], periods: list[dict[str, Any]]) -> None:
    by_date: dict[str, dict[str, str]] = {}
    for p in periods:
        d = (p.get("start") or "")[:10]
        if d and p.get("text"):
            by_date.setdefault(d, {})["text_day" if p.get("day") else "text_night"] = p["text"]
    for d in days:
        d.update(by_date.get(d["date"], {}))


# ---------------------------------------------------------------------------
# Outlooks

def _norm(attrs: dict[str, Any]) -> dict[str, Any]:
    return {re.sub(r"[^a-z]+", "_", k.lower()).strip("_"): v for k, v in attrs.items()}


CAT_WORDS = {
    "temp": {"Above": ("warm", "warmer than normal"), "Below": ("cold", "cooler than normal"), "Normal": ("even", "near normal")},
    "precip": {"Above": ("wet", "wetter than normal"), "Below": ("dry", "drier than normal"), "Normal": ("even", "near normal")},
}


def cpc_value(results: list[dict[str, Any]], kind: str, layer: int | None = None) -> dict[str, Any] | None:
    """The odds at the point from one CPC layer: the highest-probability polygon that contains it."""
    best = None
    for r in results:
        if layer is not None and r.get("layerId") != layer:
            continue
        a = _norm(r.get("attributes") or {})
        cat = a.get("cat") or a.get("category")
        try:
            prob = int(float(a.get("prob") or a.get("probability") or 0))
        except ValueError:
            prob = 0
        if best is None or prob > best["prob"]:
            best = {"cat": cat, "prob": prob, "valid": a.get("valid_seas") or a.get("valid_season"),
                    "start": a.get("start_date"), "end": a.get("end_date"), "issued": a.get("fcst_date") or a.get("forecast_date")}
    if best is None:
        # No polygon here: CPC leaves "equal chances" areas blank.
        return {"tone": "even", "words": "equal chances", "prob": None, "cat": "EC"}
    tone, w = CAT_WORDS[kind].get(best["cat"] or "", ("even", "equal chances"))
    best.update({"tone": tone, "words": f"{best['prob']}% chance {w}" if best["cat"] in CAT_WORDS[kind] else w})
    return best


def _md(s: str | None) -> str | None:
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%b %-d") if s else None
    except ValueError:
        return s


def span(a: str | None, b: str | None) -> str | None:
    """'11/18/2026', '11/22/2026' -> 'Nov 18–22' (or 'Nov 28–Dec 2')."""
    x, y = _md(a), _md(b)
    if not x or not y:
        return x or y
    if x.split()[0] == y.split()[0]:
        return f"{x}–{y.split()[1]}"
    return f"{x}–{y}"


SEASONS = {"DJF": "Dec–Feb", "JFM": "Jan–Mar", "FMA": "Feb–Apr", "MAM": "Mar–May", "AMJ": "Apr–Jun", "MJJ": "May–Jul",
           "JJA": "Jun–Aug", "JAS": "Jul–Sep", "ASO": "Aug–Oct", "SON": "Sep–Nov", "OND": "Oct–Dec", "NDJ": "Nov–Jan"}


def season_words(v: str | None) -> str | None:
    if not v:
        return v
    code = v.split()[0]
    return SEASONS.get(code, v)


def shape_cpc(parts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for key, title in (("6_10", "6–10 days"), ("8_14", "8–14 days")):
        res = parts.get(key)
        if res is None:
            continue
        t, p = cpc_value(res, "temp", 0), cpc_value(res, "precip", 1)
        dates = span(t.get("start"), t.get("end")) if t and t.get("start") else None
        out.append({"id": key, "title": title, "dates": dates, "temp": t, "precip": p})
    if parts.get("month_temp") is not None or parts.get("month_precip") is not None:
        t = cpc_value(parts.get("month_temp") or [], "temp")
        p = cpc_value(parts.get("month_precip") or [], "precip")
        out.append({"id": "month", "title": t.get("valid") or p.get("valid") or "Next month", "dates": "monthly outlook",
                    "temp": t, "precip": p})
    if parts.get("season_temp") is not None or parts.get("season_precip") is not None:
        t = cpc_value(parts.get("season_temp") or [], "temp", 0)
        p = cpc_value(parts.get("season_precip") or [], "precip", 0)
        valid = t.get("valid") or p.get("valid")
        out.append({"id": "season", "title": season_words(valid) or "Next 3 months", "dates": "3-month outlook",
                    "temp": t, "precip": p})
    return out


def shape_seasonal(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Open-Meteo (ECMWF) monthly anomalies -> the same rows as CPC, in plain words."""
    m = raw.get("monthly") or {}
    out = []
    for i, t in enumerate((m.get("time") or [])[:4]):
        ta = _at(m, "temperature_2m_anomaly", i)
        pa = _at(m, "precipitation_anomaly", i)
        temp = ({"tone": "warm", "words": "milder than usual"} if ta is not None and ta >= 0.5 else
                {"tone": "cold", "words": "cooler than usual"} if ta is not None and ta <= -0.5 else
                {"tone": "even", "words": "near normal"})
        precip = ({"tone": "wet", "words": "wetter than usual"} if pa is not None and pa >= 8 else
                  {"tone": "dry", "words": "drier than usual"} if pa is not None and pa <= -8 else
                  {"tone": "even", "words": "near normal"})
        if ta is not None:
            temp["anomaly_f"] = round(ta * 1.8, 1)
        try:
            title = datetime.fromisoformat(t).strftime("%B %Y")
        except ValueError:
            title = t
        out.append({"id": f"m{i}", "title": title, "dates": "ECMWF seasonal", "temp": temp, "precip": precip})
    return out


# ---------------------------------------------------------------------------
# Radar

def iem_frames(valid_utc: str | None, now: datetime | None = None) -> list[dict[str, Any]]:
    """NEXRAD composite: the newest scan plus 5..50 minutes back."""
    try:
        t0 = datetime.fromisoformat((valid_utc or "").replace("Z", "+00:00"))
    except ValueError:
        t0 = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
        t0 -= timedelta(minutes=t0.minute % 5)
    out = []
    for back in range(50, -1, -5):
        fid = f"m{back:02d}m" if back else "now"
        out.append({"id": fid, "t": (t0 - timedelta(minutes=back)).isoformat().replace("+00:00", "Z"), "future": False})
    return out


def dwd_frames(now: datetime | None = None) -> list[dict[str, Any]]:
    """DWD radar: 50 minutes back and 60 minutes of nowcast, 5-minute steps (data lags ~5 min)."""
    n = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    n -= timedelta(minutes=n.minute % 5 + 5)
    out = []
    for step in range(-10, 13):
        t = n + timedelta(minutes=5 * step)
        out.append({"id": t.strftime("%Y%m%dt%H%M"), "t": t.isoformat().replace("+00:00", "Z"), "future": step > 0})
    return out


def tile_bbox_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * 20037508.342789244 / (2 ** z)
    minx = -20037508.342789244 + x * size
    maxy = 20037508.342789244 - y * size
    return minx, maxy - size, minx + size, maxy


class Forecasts:
    def __init__(self, hub: "Hub", cache_dir: Path):
        self.hub = hub
        self.cache_dir = cache_dir
        self._c: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None
        self._pruned = 0.0

    # -- plumbing ------------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _cached(self, key: str, ttl: float, fn) -> Any:
        hit = self._c.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._c.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            try:
                val = await fn()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{key.split(':')[0]}: {exc}"
                log.info("weather %s failed: %s", key, exc)
                if hit:
                    return hit[1]  # stale beats nothing
                raise
            self._c[key] = (time.time(), val)
            if len(self._c) > 400:
                for k in sorted(self._c, key=lambda k: self._c[k][0])[:100]:
                    self._c.pop(k, None)
            return val

    async def _json(self, url: str, **params: Any) -> Any:
        r = await self.client().get(url, params=params or None)
        r.raise_for_status()
        return r.json()

    # -- places ----------------------------------------------------------------
    async def home(self) -> tuple[float, float] | None:
        return await self.hub.smart.weather._location()

    def trip_places(self, today=None) -> list[dict[str, Any]]:
        """Trips from a week before they start until they end."""
        today = today or self.hub.policy.now().date()
        out = []
        for t in self.hub.smart.trips:
            if t.get("status") not in ("now", "upcoming") or not t.get("start_date"):
                continue
            try:
                start = datetime.fromisoformat(t["start_date"]).date()
            except ValueError:
                continue
            if t["status"] == "upcoming" and (start - today).days > 7:
                continue
            out.append({"id": f"trip:{t['id']}", "kind": "trip", "name": t.get("title") or "Trip",
                        "trip_id": t["id"], "start_date": t.get("start_date"), "end_date": t.get("end_date"),
                        "now": t["status"] == "now"})
        return out

    async def trip_location(self, trip_id: int) -> tuple[float, float, str | None] | None:
        async def load():
            raw = await self.hub.trek.trip(trip_id)
            spots: list[dict[str, Any]] = []
            for a in raw.get("accommodations") or []:
                spots.append(a)
            for d in raw.get("days") or []:
                spots.extend(d.get("places") or d.get("assignments") or [])
            spots.extend(raw.get("unplanned_places") or [])
            for s in spots:
                place = s.get("place") if isinstance(s.get("place"), dict) else s
                lat, lng = place.get("lat"), place.get("lng") or place.get("lon")
                if lat is not None and lng is not None:
                    return (float(lat), float(lng), place.get("name"))
            return None

        return await self._cached(f"trip:{trip_id}", 3600, load)

    async def resolve(self, place: str, lat: float | None, lon: float | None) -> dict[str, Any] | None:
        home = await self.home()
        if place == "here" and lat is not None and lon is not None:
            here = (round(lat, 2), round(lon, 2))  # ~1 km: enough for weather, and keeps the cache small
            if home and miles(here, home) <= NEAR_HOME_MILES:
                return {"id": "home", "kind": "home", "name": "Home", "lat": home[0], "lon": home[1], "near_home": True}
            return {"id": "here", "kind": "here", "name": "Here", "lat": here[0], "lon": here[1]}
        if place.startswith("trip:") and self.hub.trek.enabled:
            try:
                tid = int(place.split(":", 1)[1])
            except ValueError:
                return None
            trip = next((t for t in self.trip_places() if t["trip_id"] == tid), None)
            if not trip:
                return None
            loc = await self.trip_location(tid)
            if not loc:
                return None
            return {**trip, "lat": round(loc[0], 2), "lon": round(loc[1], 2), "spot": loc[2]}
        if home:
            return {"id": "home", "kind": "home", "name": "Home", "lat": home[0], "lon": home[1]}
        return None

    async def nws_point(self, lat: float, lon: float) -> dict[str, Any] | None:
        """The NWS office and grid for a US point; None outside the US (NWS answers 404)."""
        async def load():
            r = await self.client().get(f"{NWS}/points/{lat:.4f},{lon:.4f}")
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            p = r.json().get("properties") or {}
            rel = ((p.get("relativeLocation") or {}).get("properties") or {})
            return {"forecast": p.get("forecast"), "city": rel.get("city"), "state": rel.get("state"),
                    "office": p.get("gridId"), "radar": p.get("radarStation"), "tz": p.get("timeZone")}

        try:
            got = await self._cached(f"points:{lat:.2f},{lon:.2f}", TTL["points"], load)
        except Exception:  # noqa: BLE001
            return None
        return got or None

    @staticmethod
    def in_germany(lat: float, lon: float) -> bool:
        return DE_BOX[0] <= lat <= DE_BOX[1] and DE_BOX[2] <= lon <= DE_BOX[3]

    # -- sources -----------------------------------------------------------------
    async def forecast(self, lat: float, lon: float) -> dict[str, Any]:
        async def load():
            raw = await self._json(OPEN_METEO, latitude=lat, longitude=lon, timezone="auto", forecast_days=10,
                                   temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch",
                                   current="temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,"
                                           "wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,visibility,"
                                           "uv_index,cloud_cover,precipitation,weather_code,is_day",
                                   minutely_15="precipitation", forecast_minutely_15=12, past_hours=6,
                                   hourly="temperature_2m,precipitation_probability,precipitation,weather_code,is_day,"
                                          "wind_speed_10m,wind_gusts_10m,pressure_msl",
                                   daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                                         "precipitation_sum,snowfall_sum,wind_speed_10m_max,wind_gusts_10m_max,"
                                         "wind_direction_10m_dominant,uv_index_max,sunrise,sunset")
            return shape_forecast(raw)

        return await self._cached(f"forecast:{lat:.2f},{lon:.2f}", TTL["forecast"], load)

    async def air(self, lat: float, lon: float) -> dict[str, Any] | None:
        async def load():
            raw = await self._json(AIR, latitude=lat, longitude=lon, current="us_aqi,pm2_5,ozone")
            cur = raw.get("current") or {}
            v = cur.get("us_aqi")
            return {"aqi": v, "words": aqi_words(v), "pm2_5": cur.get("pm2_5")} if v is not None else {}

        try:
            return (await self._cached(f"air:{lat:.2f},{lon:.2f}", TTL["air"], load)) or None
        except Exception:  # noqa: BLE001
            return None

    async def alerts(self, lat: float, lon: float, country: str) -> list[dict[str, Any]]:
        if country == "US":
            return await self._cached(f"alerts:{lat:.3f},{lon:.3f}", TTL["alerts"], lambda: self._nws_alerts(lat, lon))
        if country == "DE":
            return await self._cached(f"alerts:{lat:.3f},{lon:.3f}", TTL["alerts"], lambda: self._dwd_alerts(lat, lon))
        return []

    async def _nws_alerts(self, lat: float, lon: float) -> list[dict[str, Any]]:
        return shape_nws_alerts(await self._json(f"{NWS}/alerts/active", point=f"{lat:.4f},{lon:.4f}"))

    async def _dwd_alerts(self, lat: float, lon: float) -> list[dict[str, Any]]:
        return shape_dwd_alerts(await self._json(f"{BRIGHT_SKY}/alerts", lat=lat, lon=lon))

    async def nws_text(self, url: str) -> list[dict[str, Any]]:
        return await self._cached(f"text:{url}", TTL["text"], lambda: self._nws_text(url))

    async def _nws_text(self, url: str) -> list[dict[str, Any]]:
        return shape_nws_text(await self._json(url))

    async def outlook(self, lat: float, lon: float, country: str) -> list[dict[str, Any]]:
        key = f"outlook:{lat:.1f},{lon:.1f}"
        if country == "US":
            return await self._cached(key, TTL["outlook"], lambda: self._cpc(lat, lon))
        return await self._cached(key, TTL["outlook"], lambda: self._seasonal(lat, lon))

    async def _cpc(self, lat: float, lon: float) -> list[dict[str, Any]]:
        services = {"6_10": "cpc_6_10_day_outlk", "8_14": "cpc_8_14_day_outlk", "month_temp": "cpc_mthly_temp_outlk",
                    "month_precip": "cpc_mthly_precip_outlk", "season_temp": "cpc_sea_temp_outlk",
                    "season_precip": "cpc_sea_precip_outlk"}

        async def one(svc: str) -> list[dict[str, Any]] | None:
            try:
                d = await self._json(f"{CPC}/{svc}/MapServer/identify", geometry=f"{lon},{lat}",
                                     geometryType="esriGeometryPoint", sr=4326, layers="all", tolerance=1,
                                     mapExtent="-130,20,-60,55", imageDisplay="800,600,96", returnGeometry="false",
                                     f="json")
                return d.get("results") or []
            except Exception as exc:  # noqa: BLE001
                log.info("CPC %s: %s", svc, exc)
                return None

        got = await asyncio.gather(*(one(s) for s in services.values()))
        parts = {k: v for k, v in zip(services, got)}
        if all(v is None for v in parts.values()):
            raise RuntimeError("CPC outlooks unavailable")
        return shape_cpc(parts)

    async def _seasonal(self, lat: float, lon: float) -> list[dict[str, Any]]:
        raw = await self._json(SEASONAL, latitude=lat, longitude=lon,
                               monthly="temperature_2m_anomaly,precipitation_anomaly")
        return shape_seasonal(raw)

    # -- radar -------------------------------------------------------------------
    async def radar(self, country: str) -> dict[str, Any]:
        if country == "US":
            async def load():
                d = await self._json(IEM_TMS)
                svc = next((s for s in d.get("services") or [] if s.get("id") == "ridge_uscomp_n0q"), {})
                return iem_frames(svc.get("utc_valid"))
            frames = await self._cached("radar:us", TTL["radar"], load)
            return {"provider": "nexrad", "frames": frames, "max_zoom": 12, "step_min": 5,
                    "attribution": "NEXRAD via Iowa Environmental Mesonet"}
        if country == "DE":
            return {"provider": "dwd", "frames": dwd_frames(), "max_zoom": 11, "step_min": 5,
                    "attribution": "Deutscher Wetterdienst"}

        async def load_rv():
            d = await self._json(RAINVIEWER)
            past = (d.get("radar") or {}).get("past") or []
            return [{"id": p["path"].rsplit("/", 1)[-1], "t": datetime.fromtimestamp(p["time"], timezone.utc)
                     .isoformat().replace("+00:00", "Z"), "future": False} for p in past if p.get("path")]
        frames = await self._cached("radar:rv", TTL["radar"], load_rv)
        return {"provider": "rainviewer", "frames": frames, "max_zoom": 7, "step_min": 10, "attribution": "RainViewer"}

    def tile_url(self, provider: str, frame: str, z: int, x: int, y: int) -> str | None:
        if provider == "nexrad":
            layer = "nexrad-n0q-900913" + ("" if frame == "now" else f"-{frame}")
            if frame != "now" and not re.fullmatch(r"m(05|10|15|20|25|30|35|40|45|50)m", frame):
                return None
            return IEM_TILE.format(layer=layer, z=z, x=x, y=y)
        if provider == "dwd":
            m = re.fullmatch(r"(\d{8})t(\d{4})", frame)
            if not m:
                return None
            d, hm = m.groups()
            t = f"{d[:4]}-{d[4:6]}-{d[6:]}T{hm[:2]}:{hm[2:]}:00.000Z"
            minx, miny, maxx, maxy = tile_bbox_3857(z, x, y)
            return (f"{DWD_WMS}?service=WMS&version=1.3.0&request=GetMap&layers=dwd:Niederschlagsradar&styles="
                    f"&crs=EPSG:3857&bbox={minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}&width=256&height=256"
                    f"&format=image/png&transparent=true&time={t}")
        if provider == "rainviewer":
            if z > 7:
                return None
            return f"https://tilecache.rainviewer.com/v2/radar/{frame}/256/{z}/{x}/{y}/2/1_1.png"
        return None

    async def tile(self, provider: str, frame: str, z: int, x: int, y: int) -> bytes | None:
        if not TILE_RE.match(frame) or not (0 <= z <= 14) or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
            return None
        url = self.tile_url(provider, frame, z, x, y)
        if not url:
            return None
        # "now" moves every 5 minutes; key it by the newest scan so a cached tile never goes stale.
        stamp = frame
        if provider == "nexrad" and frame == "now":
            frames = (self._c.get("radar:us") or (0, []))[1]
            stamp = "now-" + (frames[-1]["t"].replace(":", "") if frames else str(int(time.time() // 300)))
        path = self.cache_dir / provider / stamp / str(z) / str(x) / f"{y}.png"
        if path.exists():
            return path.read_bytes()
        r = await self.client().get(url)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
        except OSError as exc:
            log.info("radar tile cache: %s", exc)
        self.prune()
        return r.content

    def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        if now - self._pruned < 600 or not self.cache_dir.exists():
            return
        self._pruned = now
        for prov in self.cache_dir.iterdir():
            if not prov.is_dir():
                continue
            for frame in prov.iterdir():
                try:
                    if now - frame.stat().st_mtime > RADAR_KEEP_S:
                        import shutil

                        shutil.rmtree(frame, ignore_errors=True)
                except OSError:
                    pass

    # -- the page ---------------------------------------------------------------
    async def page(self, s: "Subject", place: str = "home", lat: float | None = None,
                   lon: float | None = None) -> dict[str, Any] | None:
        p = await self.resolve(place, lat, lon)
        if not p:
            return None
        la, lo = p["lat"], p["lon"]
        pt = await self.nws_point(la, lo)
        country = "US" if pt else "DE" if self.in_germany(la, lo) else "other"
        if p["kind"] == "here":
            if pt and pt.get("city"):
                p["name"] = f"{pt['city']}, {pt['state']}" if pt.get("state") else pt["city"]
        p["country"] = country

        async def safe(coro, default):
            try:
                return await coro
            except Exception:  # noqa: BLE001
                return default

        child = s.role == "child"
        fc_task = self.forecast(la, lo)
        jobs = [safe(self.alerts(la, lo, country), None), safe(self.air(la, lo), None)]
        if not child:
            jobs += [safe(self.outlook(la, lo, country), []), safe(self.radar(country), None)]
        if pt and pt.get("forecast"):
            jobs.append(safe(self.nws_text(pt["forecast"]), []))
        try:
            fc = await fc_task
        except Exception as exc:  # noqa: BLE001
            return {"place": p, "error": f"The forecast service didn't answer ({type(exc).__name__}).",
                    "places": self.places()}
        got = await asyncio.gather(*jobs)
        alerts, air = got[0], got[1]
        outlook, radar = (got[2], got[3]) if not child else ([], None)
        periods = got[-1] if pt and pt.get("forecast") else []
        if periods:
            attach_text(fc["daily"], periods)
        summary = periods[0]["text"] if periods else None
        p["tz"] = fc.get("tz")
        out: dict[str, Any] = {
            "place": p, "places": self.places(), "units": {"temp": "F", "speed": "mph", "precip": "in"},
            **fc, "summary": summary, "air": air,
            "alerts": alerts or [], "alerts_ok": alerts is not None,
            "sources": self.sources(country, bool(periods)), "updated": datetime.now(timezone.utc).isoformat(),
        }
        if child:
            out["alerts"] = [{"id": a["id"], "event": a["event"], "level": a["level"]} for a in out["alerts"]
                             if a["level"] == "warning"]
            out["wear"] = wear(fc)
        else:
            out["outlook"] = outlook or []
            out["outlook_source"] = ("NOAA Climate Prediction Center, odds at this place" if country == "US"
                                     else "Open-Meteo seasonal (ECMWF), compared with normal")
            out["radar"] = radar
        return out

    def places(self) -> list[dict[str, Any]]:
        out = [{"id": "home", "kind": "home", "name": "Home"}]
        out += [{k: t[k] for k in ("id", "kind", "name", "start_date", "end_date", "now")} for t in self.trip_places()]
        return out

    @staticmethod
    def sources(country: str, text: bool) -> list[str]:
        if country == "US":
            out = ["Forecast: Open-Meteo (NOAA models)", "Alerts: National Weather Service",
                   "Radar: NEXRAD via Iowa State", "Outlooks: NOAA Climate Prediction Center"]
            if text:
                out.insert(1, "Forecast text: National Weather Service")
            return out
        if country == "DE":
            return ["Forecast: Open-Meteo (DWD ICON)", "Warnings: Deutscher Wetterdienst via Bright Sky",
                    "Radar: Deutscher Wetterdienst", "Outlook: ECMWF seasonal via Open-Meteo"]
        return ["Forecast: Open-Meteo", "Radar: RainViewer", "Outlook: ECMWF seasonal via Open-Meteo"]

    # -- for the Home date line, smart cards and push --------------------------
    async def home_alerts(self) -> list[dict[str, Any]]:
        home = await self.home()
        if not home:
            return []
        pt = await self.nws_point(*home)
        country = "US" if pt else "DE" if self.in_germany(*home) else "other"
        return await self.alerts(home[0], home[1], country)

    def cached_home_alerts(self) -> list[dict[str, Any]]:
        h = getattr(self.hub.smart.weather, "_latlon", None)
        cfg = self.hub.config.smart
        loc = (cfg.latitude, cfg.longitude) if cfg.latitude is not None and cfg.longitude is not None else h
        if not loc:
            return []
        hit = self._c.get(f"alerts:{loc[0]:.3f},{loc[1]:.3f}")
        return hit[1] if hit and time.time() - hit[0] < 1800 else []


def wear(fc: dict[str, Any]) -> list[dict[str, str]]:
    """What a child should put on, from today's weather. Emoji first: the 5-year-old reads pictures."""
    days = fc.get("daily") or []
    today = days[0] if days else {}
    hi = today.get("high")
    lo = (fc.get("now") or {}).get("temp")
    feel = min(x for x in (hi, lo) if x is not None) if (hi is not None or lo is not None) else 60
    rain = (today.get("rain_chance") or 0) >= 50
    snow = (today.get("snow") or 0) > 0.2
    out: list[dict[str, str]] = []
    if feel <= 35:
        out += [{"emoji": "🧥", "words": "Winter coat"}, {"emoji": "🧤", "words": "Gloves and hat"}]
    elif feel <= 55:
        out.append({"emoji": "🧥", "words": "Jacket"})
    elif feel <= 68:
        out.append({"emoji": "👕", "words": "Long sleeves"})
    else:
        out.append({"emoji": "👕", "words": "T-shirt"})
    if snow:
        out.append({"emoji": "🥾", "words": "Snow boots"})
    elif rain:
        out += [{"emoji": "☔️", "words": "Umbrella"}, {"emoji": "🥾", "words": "Rain boots"}]
    elif (hi or 0) >= 80:
        out += [{"emoji": "🧢", "words": "Hat"}, {"emoji": "🧴", "words": "Sunscreen"}]
    else:
        out.append({"emoji": "👟", "words": "Sneakers"})
    return out[:3]


# ---------------------------------------------------------------------------
# Push: official alerts for home (and a trip that's under way)

PUSH_ROLES = ("admin", "adult")
SENT_KEY = "weather.alerts_sent"


def push_text(a: dict[str, Any], place: str | None) -> tuple[str, str]:
    title = a["event"] + (f" · {place}" if place and place != "Home" else "")
    ends = a.get("ends")
    when = ""
    try:
        if ends:
            when = "until " + datetime.fromisoformat(ends).strftime("%-I:%M %p").lower().replace(":00", "")
    except ValueError:
        pass
    head = (a.get("headline") or a.get("description") or "").replace("\n", " ").strip()
    body = (when.capitalize() + ". " if when else "") + head
    return title, body[:300]


class AlertWatch:
    """Checks for new official alerts every couple of minutes and pushes them to grown-ups.

    Warnings ring (tornado, flash flood and extreme-wind warnings ring even in quiet hours); watches and
    advisories arrive quietly, if the person wants them. Each alert goes out once; a later update to a
    warning arrives quietly. Children never get these."""

    def __init__(self, fc: Forecasts):
        self.fc = fc
        self.hub = fc.hub

    def _sent(self) -> dict[str, float]:
        raw = self.hub.db.kv_get(SENT_KEY) or {}
        now = time.time()
        return {k: v for k, v in raw.items() if v > now}

    async def check(self, now: float | None = None) -> int:
        now = now or time.time()
        batches: list[tuple[str, str | None, list[dict[str, Any]]]] = []
        try:
            batches.append(("home", "Home", await self.fc.home_alerts()))
        except Exception as exc:  # noqa: BLE001
            log.info("home alerts: %s", exc)
        if self.hub.trek.enabled:
            for t in self.fc.trip_places():
                if not t.get("now"):
                    continue
                try:
                    loc = await self.fc.trip_location(t["trip_id"])
                    if loc:
                        la, lo = round(loc[0], 2), round(loc[1], 2)
                        pt = await self.fc.nws_point(la, lo)
                        country = "US" if pt else "DE" if self.fc.in_germany(la, lo) else "other"
                        batches.append(("trip", t["name"], await self.fc.alerts(la, lo, country)))
                except Exception as exc:  # noqa: BLE001
                    log.info("trip alerts: %s", exc)
        sent = self._sent()
        n = 0
        for kind, place, alerts in batches:
            for a in alerts:
                if a["id"] in sent:
                    continue
                ends = a.get("ends")
                try:
                    exp = datetime.fromisoformat(ends).timestamp() if ends else now + 86400
                except ValueError:
                    exp = now + 86400
                sent[a["id"]] = max(exp, now + 3600) + 86400
                is_update = a.get("update") or any(r in sent for r in a.get("replaces") or [])
                n += await self.deliver(a, kind, place, is_update)
        self.hub.db.kv_set(SENT_KEY, sent)
        return n

    async def deliver(self, a: dict[str, Any], kind: str, place: str | None, is_update: bool) -> int:
        lvl = a["level"]
        if lvl == "statement":
            return 0
        if is_update and lvl != "warning":
            return 0
        pref_key = {"warning": "warnings", "watch": "watches", "advisory": "advisories"}[lvl]
        title, body = push_text(a, place)
        if is_update:
            title = "Updated: " + title
        people = []
        for p in self.hub.db.people_with_role(*PUSH_ROLES):
            prefs = self.hub.alerts.prefs(p["id"])
            w = prefs.get("weather") or {}
            if not w.get(pref_key, False) or (kind == "trip" and not w.get("trips", True)):
                continue
            people.append((p, prefs))
        everyone = list(self.hub.db.people_with_role(*PUSH_ROLES))
        self.hub.alerts.record("weather", title, body, everyone, ref=f"thomascreek://weather/alert/{a['id']}",
                               payload={"alert_id": a["id"], "level": lvl})
        n = 0
        for person, prefs in people:
            ring = lvl == "warning" and not is_update
            quiet = self.hub.alerts._quiet_now(prefs["quiet"])
            silent = not ring or (quiet and not a.get("urgent"))
            await self.hub.notify.send(
                title=title, body=body, cat="weather", level="act_now" if ring and not silent else "fyi",
                person_ids=[person["id"]], ref=f"thomascreek://weather/alert/{a['id']}", group_key=f"wx:{a['id']}",
                silent=silent, interruption="time-sensitive" if ring and not silent else None,
            )
            n += 1
        return n
