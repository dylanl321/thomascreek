"""Who's home, from signals that never track anyone away from the house.

Three signals feed one home/away answer per person:

- **Wi-Fi**: the Firewalla sees the person's phone or tablet on the home network.
  The hub learns which device is whose by itself: when someone's app calls the hub
  from home Wi-Fi, the caller's address is matched to the Firewalla device with that
  address. A phone that changes its private Wi-Fi address is learned again the next
  time the app opens at home. A child's linked devices (network rules, or a Firewalla
  group named after them) count too. Wi-Fi marks someone away only after `WIFI_GRACE_S`
  off the network, because a sleeping phone drops off now and then.
- **Location at the house** (opt-in per phone): a circle round the house. iOS wakes
  the app only when the phone crosses it and the app sends "arrive" or "leave", never
  where it is. Leaving the circle is the authority for leaving: Wi-Fi going quiet does
  not undo an "arrive", and Wi-Fi still showing the phone just after a "leave" (the
  Firewalla lags) does not undo it either.
- **Home Assistant** `person.<first name>` (or the person's chosen `presence_entity`,
  which then decides alone, as before).

The most recent change among them wins. Device addresses are kept on the hub only and
never logged.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

log = logging.getLogger("hub.presence")

WIFI_GRACE_S = 15 * 60       # off Wi-Fi this long before Wi-Fi alone says "away"
LEAVE_LAG_S = 10 * 60        # Wi-Fi seen this long after a geofence "leave" means they came back
LOCATION_TTL_S = 18 * 3600   # a phone's arrive/leave counts this long without a newer report
WIFI_POLL_S = 60             # how often the hub asks the Firewalla for its devices
LEARNED_KEEP_S = 45 * 86400  # a learned device address is forgotten after this long unseen at home
SKIP_ROLES = ("tablet", "pending", "guest")


def _ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def wifi_signal(devs: list[dict[str, Any]], now: float) -> tuple[bool, float] | None:
    """(home, changed_at) from the person's Wi-Fi devices, or None with none known."""
    if not devs:
        return None
    online = [d for d in devs if d.get("online")]
    if online:
        return True, min(float(d.get("online_since") or now) for d in online)
    seen = max((float(d.get("last_seen") or 0) for d in devs), default=0.0)
    if not seen:
        return None
    if now - seen < WIFI_GRACE_S:
        return True, seen
    return False, seen + WIFI_GRACE_S


def combine(*, ha: dict[str, Any] | None, location: dict[str, Any] | None,
            wifi: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """One answer: {home, changed_at, source, where}. home None means the hub can't tell.

    `ha`: {home, changed_at, where}; `location`: {home, at} (the phone's last arrive/leave);
    `wifi`: the person's devices as the Firewalla last saw them ({online, last_seen, online_since}).
    """
    now = now or time.time()
    cands: list[tuple[float, bool, str, str | None]] = []
    if ha is not None:
        cands.append((ha.get("changed_at") or 0.0, bool(ha["home"]), "home_assistant", ha.get("where")))
    loc = location if location and now - float(location.get("at") or 0) < LOCATION_TTL_S else None
    if loc is not None:
        cands.append((float(loc["at"]), bool(loc["home"]), "phone", None))
    w = wifi_signal(wifi, now)
    if w is not None:
        home, at = w
        last_seen = max((float(d.get("last_seen") or 0) for d in wifi), default=0.0)
        if loc is not None and loc["home"] and not home:
            w = None  # the circle owns leaving: a quiet phone at home is still home
        elif loc is not None and not loc["home"] and home and last_seen < float(loc["at"]) + LEAVE_LAG_S:
            w = None  # the Firewalla still lists a phone that just left
        if w is not None:
            cands.append((at, home, "wifi", None))
    if not cands:
        return {"home": None, "changed_at": None, "source": None, "where": None}
    # Latest news wins; on a tie, being home wins.
    at, home, source, where = max(cands, key=lambda c: (c[0], c[1]))
    return {"home": home, "changed_at": at or None, "source": source, "where": where if not home else None}


class WifiPresence:
    """What the Firewalla last saw, and which device addresses belong to whom."""

    def __init__(self, hub: Any):
        self.hub = hub
        self.devices: dict[str, dict[str, Any]] = {}  # address -> {online, last_seen, online_since, group, name}
        self.by_ip: dict[str, str] = {}
        self.polled_at = 0.0
        self.last_error: str | None = None
        self._learned: dict[str, dict[str, dict[str, Any]]] | None = None
        self.kid_macs: dict[str, set[str]] = {}

    # -- the Firewalla ----------------------------------------------------
    @property
    def enabled(self) -> bool:
        fw = getattr(self.hub, "firewalla", None)
        return bool(fw and fw.enabled)

    async def poll(self, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or time.time()
        try:
            rows = await self.hub.firewalla.devices()
        except Exception as exc:  # noqa: BLE001  presence keeps the last picture
            self.last_error = type(exc).__name__
            log.info("wifi presence: Firewalla devices failed (%s); keeping the last picture", self.last_error)
            return False
        self.update(rows, now)
        return True

    def update(self, rows: list[dict[str, Any]], now: float) -> None:
        devices: dict[str, dict[str, Any]] = {}
        by_ip: dict[str, str] = {}
        for d in rows:
            mac = str(d.get("mac") or "").upper()
            if not mac:
                continue
            online = bool(d.get("online"))
            last_seen = _ts(d.get("last_seen")) or (now if online else 0.0)
            if online:
                last_seen = max(last_seen, now)
            old = self.devices.get(mac) or {}
            since = old.get("online_since") if old.get("online") else None
            devices[mac] = {"online": online, "last_seen": last_seen,
                            "online_since": (since or now) if online else None,
                            "group": d.get("group"), "name": d.get("name")}
            if d.get("ip") and online:
                by_ip[str(d["ip"])] = mac
        self.devices, self.by_ip, self.polled_at, self.last_error = devices, by_ip, now, None
        # A child's devices, by the same rule as their network alerts: linked by the hub, a Firewalla
        # group named after them, or a device named "Theo's iPad".
        kids: dict[str, set[str]] = {}
        alerts = getattr(self.hub, "alerts", None)
        if alerts is not None:
            for d in rows:
                mac = str(d.get("mac") or "").upper()
                try:
                    kid = alerts.kid_for_device(d) if mac else None
                except Exception:  # noqa: BLE001  presence still works for the grown-ups
                    kid = None
                if kid:
                    kids.setdefault(kid["id"], set()).add(mac)
        self.kid_macs = kids

    # -- learning whose device is whose -----------------------------------
    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self._learned is None:
            self._learned = self.hub.db.kv_get("presence.wifi_devices") or {}
        return self._learned

    def saw_at_home(self, person_id: str | None, role: str | None, device_id: str | None, ip: str | None,
                    now: float | None = None) -> bool:
        """The person's app called from home Wi-Fi: remember which Firewalla device it is."""
        if not person_id or not device_id or not ip or role in SKIP_ROLES:
            return False
        mac = self.by_ip.get(ip)
        if not mac:
            return False
        now = now or time.time()
        learned = self._load()
        mine = learned.setdefault(person_id, {})
        row = mine.get(device_id)
        if row and row.get("mac") == mac and now - float(row.get("at") or 0) < 3600:
            return False
        # The address is this phone's now: nobody else keeps it.
        for pid, devs in learned.items():
            for did in [k for k, v in devs.items() if v.get("mac") == mac and (pid, k) != (person_id, device_id)]:
                del devs[did]
        mine[device_id] = {"mac": mac, "at": now}
        for pid in list(learned):
            learned[pid] = {k: v for k, v in learned[pid].items() if now - float(v.get("at") or 0) < LEARNED_KEEP_S}
            if not learned[pid]:
                del learned[pid]
        self.hub.db.kv_set("presence.wifi_devices", learned)
        return True

    def macs_for(self, p: dict[str, Any]) -> set[str]:
        out = {str(v.get("mac")).upper() for v in (self._load().get(p["id"]) or {}).values() if v.get("mac")}
        if p.get("role") == "child":
            out |= self.kid_macs.get(p["id"], set())
            for r in self.hub.db.all("SELECT mac FROM network_rules WHERE person_id=?", [p["id"]]):
                if r.get("mac"):
                    out.add(str(r["mac"]).upper())
        return out

    def for_person(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.polled_at:
            return []
        return [self.devices[m] for m in self.macs_for(p) if m in self.devices]

    def learned_count(self, person_id: str) -> int:
        return len(self._load().get(person_id) or {})
