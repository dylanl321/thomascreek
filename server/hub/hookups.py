"""Which TV a streaming box is plugged into, and on which input.

An Android TV box, Apple TV or Roku stick has no idea which TV it's on, and Home Assistant can't tell either,
so an adult says it once in the app: "the Living Room Apple TV is plugged into the Samsung TV, on HDMI 2".
After that, using the box does the whole job:
- anything sent to the box first turns the TV on and switches it to the box's input, when it isn't already;
- volume and mute go to the TV (the box usually can't change the TV's volume);
- power turns the box and the TV on or off together.
Stored in the hub's own settings table, so every phone sees the same hookups.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import Hub

KEY = "tv.hookups"
ASLEEP = ("off", "standby", "unavailable", "unknown")
VOLUME = ("vol_up", "vol_down", "mute", "volume")


class Hookups:
    def __init__(self, hub: "Hub"):
        self.hub = hub

    def all(self) -> dict[str, dict[str, Any]]:
        raw = self.hub.db.kv_get(KEY) or {}
        return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("display")}

    def get(self, box: str) -> dict[str, Any] | None:
        return self.all().get(box)

    def boxes_on(self, display: str) -> list[str]:
        return [b for b, v in self.all().items() if v.get("display") == display]

    def set(self, box: str, display: str | None, input_name: str | None = None) -> dict[str, Any] | None:
        cur = self.hub.db.kv_get(KEY) or {}
        if display:
            cur[box] = {"display": display, "input": input_name or None}
        else:
            cur.pop(box, None)
        self.hub.db.kv_set(KEY, cur)
        return cur.get(box)

    # -- what to send ---------------------------------------------------------------------------------
    def _st(self, eid: str) -> dict[str, Any]:
        return self.hub.home.states.get(eid, {})

    def prepare(self, box: str) -> list[tuple[str, str, dict[str, Any]]]:
        """Calls that get the box's TV ready: on, and on the box's input. Empty when it already is."""
        link = self.get(box)
        if not link:
            return []
        tv = link["display"]
        st = self._st(tv)
        if not st:
            return []
        calls: list[tuple[str, str, dict[str, Any]]] = []
        asleep = st.get("state") in ASLEEP
        if asleep:
            calls.append(("media_player", "turn_on", {"entity_id": tv}))
        want = link.get("input")
        sources = st.get("attributes", {}).get("source_list") or []
        if want and want in sources and (asleep or st.get("attributes", {}).get("source") != want):
            calls.append(("media_player", "select_source", {"entity_id": tv, "source": want}))
        return calls

    def summary(self, box: str) -> dict[str, Any] | None:
        link = self.get(box)
        if not link:
            return None
        tv = link["display"]
        name = self._st(tv).get("attributes", {}).get("friendly_name", tv)
        return {"entity_id": tv, "name": name, "input": link.get("input")}
