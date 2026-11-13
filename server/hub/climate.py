"""Thermostat history for the chart: indoor temperature, set points, and when heating or cooling ran.

Home Assistant's recorder keeps every climate state change with its attributes (climate is one of
the history "significant" domains, so attribute-only changes such as a new temperature reading are
kept too). One thermostat's rows for the chosen range are read from /api/history/period and shaped
into series the app draws directly. The recorder keeps 10 days by default, so 7 days is the longest
range. The outdoor line is Open-Meteo's past hours at the house, the same source as Home's weather.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.climate")

RANGES = {"6h": 6 * 3600, "24h": 24 * 3600, "3d": 3 * 86400, "7d": 7 * 86400}
MAX_POINTS = 600
RUNNING = ("heating", "cooling", "fan", "drying", "preheating", "defrosting")
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ts(v: Any) -> float | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _setpoints(mode: str, a: dict[str, Any]) -> dict[str, float | None]:
    """The heat and cool set points the thermostat holds in this mode (both in Auto)."""
    target, low, high = _num(a.get("temperature")), _num(a.get("target_temp_low")), _num(a.get("target_temp_high"))
    if mode == "heat":
        return {"heat": target if target is not None else low, "cool": None, "target": None}
    if mode == "cool":
        return {"heat": None, "cool": target if target is not None else high, "target": None}
    if mode in ("heat_cool", "auto"):
        if low is not None or high is not None:
            return {"heat": low, "cool": high, "target": None}
        return {"heat": None, "cool": None, "target": target}
    return {"heat": None, "cool": None, "target": None}  # off, fan_only, dry, unavailable


def _downsample(points: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    """Time buckets with the mean reading, so a week stays light; gaps (None) are kept as gaps."""
    if len(points) <= MAX_POINTS:
        return points
    width = (end - start) / MAX_POINTS
    out: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    key = None

    def flush() -> None:
        if not bucket:
            return
        temps = [p["temp"] for p in bucket if p["temp"] is not None]
        hums = [p["humidity"] for p in bucket if p["humidity"] is not None]
        out.append({"t": bucket[0]["t"], "temp": round(sum(temps) / len(temps), 2) if temps else None,
                    "humidity": round(sum(hums) / len(hums), 1) if hums else None})

    for p in points:
        k = int((p["t"] - start) // width)
        if k != key:
            flush()
            bucket, key = [], k
        bucket.append(p)
    flush()
    return out


def shape(rows: list[dict[str, Any]], start: float, end: float) -> dict[str, Any]:
    """Turn one entity's history rows into the chart's series and a summary."""
    rows = sorted(rows, key=lambda r: _ts(r.get("last_updated") or r.get("last_changed")) or 0)
    points: list[dict[str, Any]] = []
    setpoints: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    modes: list[dict[str, Any]] = []
    reports_action = False
    last_sp: dict[str, Any] | None = None
    last_pt: tuple[Any, Any] | None = None

    for r in rows:
        t = _ts(r.get("last_updated") or r.get("last_changed"))
        if t is None:
            continue
        t = min(max(t, start), end)
        mode = str(r.get("state") or "unknown")
        a = r.get("attributes") or {}
        down = mode in ("unavailable", "unknown")
        temp = None if down else _num(a.get("current_temperature"))
        hum = None if down else _num(a.get("current_humidity"))
        if (temp, hum) != last_pt:
            points.append({"t": t, "temp": temp, "humidity": hum})
            last_pt = (temp, hum)
        sp = {"t": t, **_setpoints(mode, a)}
        if last_sp is None or any(sp[k] != last_sp[k] for k in ("heat", "cool", "target")):
            setpoints.append(sp)
            last_sp = sp
        if not modes or modes[-1]["mode"] != mode:
            if modes:
                modes[-1]["end"] = t
            modes.append({"start": t, "end": end, "mode": mode})
        action = a.get("hvac_action")
        if action:
            reports_action = True
        action = action if action in RUNNING and not down else None
        if runs and runs[-1]["end"] is None:
            if runs[-1]["action"] != action:
                runs[-1]["end"] = t
                if action:
                    runs.append({"start": t, "end": None, "action": action})
        elif action:
            runs.append({"start": t, "end": None, "action": action})
    for run in runs:
        if run["end"] is None:
            run["end"] = end
    runs = [r for r in runs if r["end"] > r["start"]]
    modes = [m for m in modes if m["end"] > m["start"]]
    # The line runs on to "now" at the last reading, so the right edge isn't empty.
    if points and points[-1]["t"] < end and points[-1]["temp"] is not None:
        points.append({"t": end, "temp": points[-1]["temp"], "humidity": points[-1]["humidity"]})
    if setpoints and setpoints[-1]["t"] < end:
        setpoints.append({**setpoints[-1], "t": end})

    # Time-weighted average, so a burst of readings doesn't count more than a quiet stretch.
    span = 0.0
    total = 0.0
    for p, q in zip(points, points[1:]):
        if p["temp"] is not None:
            total += p["temp"] * (q["t"] - p["t"])
            span += q["t"] - p["t"]
    temps = [p["temp"] for p in points if p["temp"] is not None]
    hums = [p["humidity"] for p in points if p["humidity"] is not None]

    def secs(action: str) -> int:
        return int(sum(r["end"] - r["start"] for r in runs if r["action"] == action))

    summary = {
        "heating_s": secs("heating"), "cooling_s": secs("cooling"), "fan_s": secs("fan"),
        "heating_cycles": sum(1 for r in runs if r["action"] == "heating"),
        "cooling_cycles": sum(1 for r in runs if r["action"] == "cooling"),
        "temp_min": min(temps) if temps else None, "temp_max": max(temps) if temps else None,
        "temp_avg": round(total / span, 1) if span else (temps[0] if temps else None),
        "humidity_now": hums[-1] if hums else None,
        "humidity_min": min(hums) if hums else None, "humidity_max": max(hums) if hums else None,
    }
    return {"points": _downsample(points, start, end), "setpoints": setpoints, "runs": runs, "modes": modes,
            "reports_action": reports_action, "summary": summary}


class ClimateHistory:
    CACHE_S = 60
    OUTDOOR_CACHE_S = 1800

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._outdoor: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}

    async def history(self, eid: str, rng: str) -> dict[str, Any]:
        """Raises AdapterError when Home Assistant can't answer."""
        hit = self._cache.get((eid, rng))
        if hit and time.time() - hit[0] < self.CACHE_S:
            return hit[1]
        end = time.time()
        start = end - RANGES[rng]
        iso = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()  # noqa: E731
        raw = await self.hub.ha.get_json(f"/api/history/period/{iso(start)}", params={
            "filter_entity_id": eid, "end_time": iso(end), "significant_changes_only": "0"})
        rows = next((lst for lst in raw or [] if lst and lst[0].get("entity_id") == eid), [])
        st = self.hub.home.states.get(eid) or {}
        attrs = st.get("attributes") or {}
        unit = attrs.get("temperature_unit") or next(
            ((r.get("attributes") or {}).get("temperature_unit") for r in reversed(rows)
             if (r.get("attributes") or {}).get("temperature_unit")), None) or "°F"
        out = {"entity_id": eid, "name": attrs.get("friendly_name", eid), "unit": unit, "range": rng,
               "start": start, "end": end, **shape(rows, start, end)}
        out["outdoor"] = await self.outdoor(start, end, unit)
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[(eid, rng)] = (time.time(), out)
        return out

    async def outdoor(self, start: float, end: float, unit: str) -> list[dict[str, Any]]:
        """Hourly outdoor temperature at the house; empty when the location or Open-Meteo is missing."""
        try:
            loc = await self.hub.smart.weather._location()
        except Exception:  # noqa: BLE001
            loc = None
        if not loc:
            return []
        days = min(92, math.ceil((end - start) / 86400) + 1)
        celsius = unit == "°C"
        key = (round(loc[0], 3), round(loc[1], 3), days, celsius)
        hit = self._outdoor.get(key)
        if hit and time.time() - hit[0] < self.OUTDOOR_CACHE_S:
            rows = hit[1]
        else:
            try:
                async with httpx.AsyncClient(timeout=8) as c:
                    r = await c.get(OPEN_METEO, params={
                        "latitude": loc[0], "longitude": loc[1], "hourly": "temperature_2m",
                        "temperature_unit": "celsius" if celsius else "fahrenheit",
                        "past_days": days, "forecast_days": 1, "timeformat": "unixtime"})
                    r.raise_for_status()
                    h = r.json().get("hourly") or {}
                rows = [{"t": float(t), "temp": v} for t, v in zip(h.get("time") or [], h.get("temperature_2m") or [])
                        if v is not None]
            except Exception as exc:  # noqa: BLE001
                log.info("outdoor history failed: %s", exc)
                return []
            if len(self._outdoor) > 16:
                self._outdoor.clear()
            self._outdoor[key] = (time.time(), rows)
        return [r for r in rows if start - 3600 <= r["t"] <= end]
