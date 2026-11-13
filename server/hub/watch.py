"""Watch this on that TV: turn the TV on, open Plex on it, start the title, with tracks the family can follow.

The hub does all of it, so every phone does the same thing with one tap:
1. the TV's own remote turns it on and opens the Plex app (works for Google TV, Apple TV, Roku, LG, Samsung);
2. the hub waits for that Plex app to show up as a player on the network and matches it to the TV
   (by hardware address, else by name);
3. the hub asks the Plex server to start the title on that player, from where it was left off;
4. if the title isn't in English, it picks English audio when there is one, else the original audio with
   English subtitles.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .policy import Subject
    from .state import Hub

log = logging.getLogger("hub.watch")

ENGLISH = ("en", "eng", "english")
FIND_PLAYER_S = 30.0


def _is(lang: str | None, want: tuple[str, ...] = ENGLISH) -> bool:
    return (lang or "").lower().split("-")[0] in want


def choose_tracks(audio: list[dict[str, Any]], subtitles: list[dict[str, Any]],
                  want: tuple[str, ...] = ENGLISH) -> dict[str, Any]:
    """Pick audio and subtitles a family can follow.

    - English audio exists: use it; subtitles off, unless there's a forced English track (signs, foreign lines).
    - No English audio: keep the title's own audio (the default track) and turn on full English subtitles,
      preferring plain ones over forced-only or hearing-impaired.
    Returns {audio_id, subtitle_id (0 = off), why}.
    """
    eng_audio = [a for a in audio if _is(a.get("language"), want)]
    eng_subs = [s for s in subtitles if _is(s.get("language"), want)]
    if eng_audio:
        pick = next((a for a in eng_audio if a.get("default")), eng_audio[0])
        forced = next((s for s in eng_subs if s.get("forced")), None)
        return {"audio_id": pick.get("id"), "subtitle_id": forced.get("id") if forced else 0,
                "why": "English audio" + (", with forced English subtitles" if forced else "")}
    keep = next((a for a in audio if a.get("default")), audio[0] if audio else None)
    full = [s for s in eng_subs if not s.get("forced")]
    sub = (next((s for s in full if not s.get("hearing_impaired")), None) or (full[0] if full else None)
           or (eng_subs[0] if eng_subs else None))
    if sub is None:
        return {"audio_id": keep.get("id") if keep else None, "subtitle_id": None,
                "why": "No English audio or subtitles in this file"}
    return {"audio_id": keep.get("id") if keep else None, "subtitle_id": sub.get("id"),
            "why": "Original audio with English subtitles"}


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Watch:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.sleep = asyncio.sleep  # tests swap this out
        self.tasks: set[asyncio.Task[Any]] = set()

    # -- finding the TV's Plex app ---------------------------------------------------------------------
    async def _tv_macs(self, eid: str) -> set[str]:
        tpl = ("{%- set d = device_id('" + eid + "') -%}{%- set ns = namespace(m=[]) -%}"
               "{%- for c in (device_attr(d, 'connections') or []) if d -%}{%- if c[0] == 'mac' -%}"
               "{%- set ns.m = ns.m + [c[1] | lower] -%}{%- endif -%}{%- endfor -%}{{ ns.m | join(',') }}")
        try:
            raw = await self.hub.ha.render(tpl)
        except Exception:  # noqa: BLE001
            return set()
        from .remotes import _mac

        return {m for m in (_mac(x) for x in str(raw).split(",")) if m}

    def _names(self, eid: str) -> set[str]:
        home = self.hub.home
        st = home.states.get(eid, {})
        names = {_norm(st.get("attributes", {}).get("friendly_name"))}
        dev = home.ha_devices.get(home.entity_device.get(eid, "")) or {}
        names.add(_norm(dev.get("name")))
        return {n for n in names if n}

    async def find_player(self, eid: str, wait_s: float = FIND_PLAYER_S) -> dict[str, Any] | None:
        """The Plex player running on this TV, once its Plex app is open."""
        macs = await self._tv_macs(eid)
        names = self._names(eid)
        from .remotes import arp_mac

        end = time.monotonic() + wait_s
        while True:
            try:
                clients = await self.hub.plex.clients()
            except Exception as exc:  # noqa: BLE001
                log.info("Plex players unavailable: %s", exc)
                clients = []
            for c in clients:
                addr = c.get("address") or c.get("host")
                if macs and addr and await asyncio.to_thread(arp_mac, addr) in macs:
                    return c
            for c in clients:
                if _norm(c.get("name")) in names:
                    return c
            if time.monotonic() >= end:
                return None
            await self.sleep(2)

    # -- the whole flow ---------------------------------------------------------------------------------
    async def start(self, s: "Subject", eid: str, item_id: str, *, fix_language: bool = True) -> None:
        """Runs after the TV has been told to open Plex; reports how it went on the event stream."""
        name = self.hub.home.states.get(eid, {}).get("attributes", {}).get("friendly_name", eid)
        result: dict[str, Any] = {"entity_id": eid, "item_id": item_id}
        try:
            player = await self.find_player(eid)
            if player is None:
                # Plex may have opened behind something else, or the TV was slow to wake: open it once more.
                await self._reopen_plex(eid)
                player = await self.find_player(eid, wait_s=15.0)
            if player is None:
                result.update(ok=False, message=f"Plex opened on {name}, but it didn't show up as a player. "
                                                "Start the title from the TV, or turn on Advertise as player in "
                                                "Plex's settings on that TV.")
                return
            streams = await self.hub.plex.streams(item_id)
            if fix_language and streams:
                pick = choose_tracks(streams["audio"], streams["subtitles"])
                if pick["audio_id"] is not None or pick["subtitle_id"] is not None:
                    await self.hub.plex.set_streams(streams["part_id"], pick["audio_id"], pick["subtitle_id"])
                result["tracks"] = pick["why"]
            offset = int((streams or {}).get("view_offset") or 0)
            try:
                await self.hub.plex.play_on_client(str(player.get("machineIdentifier")), item_id, offset)
            except Exception as exc:  # noqa: BLE001 - a just-opened Plex app sometimes misses the first request
                log.info("first play request failed (%s); retrying", exc)
                await self.sleep(3)
                await self.hub.plex.play_on_client(str(player.get("machineIdentifier")), item_id, offset)
            result.update(ok=True, message=f"Playing on {name}")
        except Exception as exc:  # noqa: BLE001
            log.warning("watch on %s failed: %s", eid, exc)
            result.update(ok=False, message=f"Couldn't start it on {name}. Plex is open there, so pick it from the "
                                            "TV, or try again in a minute.")
            result["detail"] = str(exc)[:200]
        finally:
            self.hub.db.log("media.watch", "ok" if result.get("ok") else "error", person_id=s.person_id,
                            target=eid, params={"item_id": item_id}, detail=result.get("message"))
            self.hub.bus.publish({"type": "watch", "data": result}, person_id=s.person_id)

    async def _reopen_plex(self, eid: str) -> None:
        plat = (self.hub.home.media_links.get(eid) or {}).get("platform")
        try:
            for dom, svc, data in self.hub.actions._app_call(eid, plat, "plex"):
                if svc != "turn_on":
                    await self.hub.ha.call_service(dom, svc, data)
        except Exception as exc:  # noqa: BLE001
            log.info("reopening Plex on %s failed: %s", eid, exc)

    def launch(self, s: "Subject", eid: str, item_id: str) -> None:
        t = asyncio.get_running_loop().create_task(self.start(s, eid, item_id))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def fix_language(self, item_id: str, eid: str | None = None) -> dict[str, Any]:
        """Pick followable tracks for a title, and switch them now if it's playing on that TV's Plex app."""
        streams = await self.hub.plex.streams(item_id)
        if not streams:
            return {"ok": False, "message": "That title has no file to check"}
        pick = choose_tracks(streams["audio"], streams["subtitles"])
        await self.hub.plex.set_streams(streams["part_id"], pick["audio_id"], pick["subtitle_id"])
        live = False
        if eid:
            player = None
            try:
                clients = await self.hub.plex.clients()
                names = self._names(eid)
                player = next((c for c in clients if _norm(c.get("name")) in names), None)
            except Exception:  # noqa: BLE001
                player = None
            if player:
                await self.hub.plex.client_streams(str(player.get("machineIdentifier")), pick["audio_id"],
                                                   pick["subtitle_id"])
                live = True
        audio = next((a["title"] for a in streams["audio"] if a.get("id") == pick["audio_id"]), None)
        sub = next((x["title"] for x in streams["subtitles"] if x.get("id") == pick["subtitle_id"]), None)
        return {"ok": True, "message": pick["why"], "audio": audio, "subtitles": sub if pick["subtitle_id"] else None,
                "switched_now": live}
