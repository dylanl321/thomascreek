"""Thomas Creek TV config: the IPTV source, kids' categories, the config version, theme and the channel lineup."""

from __future__ import annotations

import time
from typing import Any


from .tvlauncher import config_block as launcher_block
from .tvlineup import LineupError, normalize_channel, public_channel, valid_key
from .tvapp_common import (
    TvAppParts,
    IPTV_SECRETS,
    KV_CONFIG_VERSION,
    KV_KIDS,
    KV_LINEUP,
    MAX_CHANNELS,
    METADATA_KEYS,
    TvAppError,
    _favorite_channels,
    _household,
    _person_layers,
    log,
)


class ConfigMixin(TvAppParts):
    """Part of TvApp (hub/tvapp.py); uses its attributes (cfg, vault, db, dir, adb, ...)."""

    def kids_categories(self) -> list[str]:
        return list(self.db.kv_get(KV_KIDS) or [])

    def set_kids_categories(self, cats: list[str]) -> list[str]:
        clean = list(dict.fromkeys(str(c).strip()[:80] for c in cats if str(c).strip()))[:200]
        self.db.kv_set(KV_KIDS, clean)
        self.bump_config()
        return clean

    def source_summary(self) -> dict[str, Any]:
        """What kind of source is set, without any secret in it."""
        have = {n: self.vault.has(n) for n in IPTV_SECRETS}
        kind = "xtream" if all(have[n] for n in IPTV_SECRETS[:3]) else "m3u" if have["iptv.m3u_url"] else None
        return {"kind": kind, "epg": have["iptv.epg_url"]}

    def _source(self) -> dict[str, Any]:
        def v(n: str) -> str:
            return str(self.vault.get(n) or "").strip()

        playlists: list[dict[str, Any]] = []
        # One playlist per provider, so the TV lists every provider's catalog. The TV loads it with
        # that provider's first login; playback picks a free login per stream through /v1/tv/tune.
        seen: set[str] = set()
        for login in self._iptv_logins():
            provider = str(login.get("provider") or "")
            secret = str(login.get("password_secret") or "")
            password = str(self.vault.get(secret) or "").strip() if secret and self.vault.has(secret) else ""
            if not provider or provider in seen or not login.get("server") or not login.get("username") or not password:
                continue
            seen.add(provider)
            # The original login keeps its address exactly as saved, so a TV that has it doesn't add it twice.
            server = v("iptv.xtream_server") if secret == "iptv.xtream_password" else login["server"]
            playlists.append({"type": "xtream", "name": str(login.get("name") or provider), "server": server,
                              "username": login["username"], "password": password})
        if v("iptv.m3u_url"):
            playlists.append({"type": "m3u", "name": "Thomas Creek" if not playlists else "Thomas Creek M3U",
                              "url": v("iptv.m3u_url")})
        return {"playlists": playlists, "epg_url": v("iptv.epg_url") or None}

    def _iptv_logins(self) -> list[dict[str, Any]]:
        """Saved logins in provider order. The legacy single login comes back as provider "thomas-creek"."""
        from types import SimpleNamespace

        from . import iptvplay

        shim = SimpleNamespace(config=self.hub_config or SimpleNamespace(iptv=None), vault=self.vault, db=self.db)
        rows = iptvplay.logins(shim)
        # logins() drops the original vault login once any other is saved; the TV still wants its catalog.
        v = lambda n: str(self.vault.get(n) or "").strip() if self.vault.has(n) else ""  # noqa: E731
        if v("iptv.xtream_server") and v("iptv.xtream_username") and v("iptv.xtream_password") \
                and not any(r.get("password_secret") == "iptv.xtream_password" for r in rows):
            rows.append({"provider": "thomas-creek", "login": "main", "server": v("iptv.xtream_server"),
                         "username": v("iptv.xtream_username"), "password_secret": "iptv.xtream_password",
                         "name": "Thomas Creek"})
        # The original login leads its provider, so the TV keeps loading the list it already has.
        return sorted(rows, key=lambda r: (str(r.get("provider")), r.get("password_secret") != "iptv.xtream_password",
                                           str(r.get("login"))))


    def config_for(self, install_id: str, ts: int, signature: str, ip: str | None, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        tv = self._verified(install_id, ts, signature, f"tvconfig:{install_id}:{int(ts)}".encode(), now,
                            log_action="tvapp.config")
        if tv["status"] != "approved":
            return {"status": "pending"}
        tvs = self.tvs()
        tvs[install_id].update(last_config_at=now, ip=ip or tv.get("ip"))
        self._save_tvs(tvs)
        src = self._source()
        out: dict[str, Any] = {
            "status": "approved",
            "version": self.config_version(),
            "name": tv.get("name"),
            "profile": tv.get("profile", "adult"),
            "room": tv.get("room"),
            "media_entity": tv.get("media_entity"),
            "playlists": src["playlists"],
            # Channel-id label. Fixed, so favorites keep their ids when providers are added or reordered.
            "source": "thomas-creek",
            "epg_url": src["epg_url"],
            "lineup_version": self.lineup_version(),
            "theme": self.theme(),
            "keys": self.metadata_keys(),
            # What follows the hub on every screen (see hub/tvs.py). Picture and audio stay on the TV.
            "settings": dict(tv.get("settings") or {}),
            "settings_rev": int(tv.get("settings_rev") or 0),
            # Who made the last change: "tv" (this TV pushed it), "phone" or "admin". A TV only takes the
            # device set back when someone else changed it.
            "settings_by": tv.get("settings_by"),
            # Each person's own TV settings; the TV lays the one who is watching over its own.
            "person_settings": _person_layers(self.db),
        }
        # Every TV gets the kids categories and the household, so it can keep a kids profile per child.
        out["kids_categories"] = self.kids_categories()
        out["people"] = _household(self.db)
        out["launcher"] = launcher_block(tv)
        return out

    def approved_config(self, install_id: str) -> dict[str, Any]:
        """The config body for a TV that is already approved. Pairing returns this with the token."""
        tv = self.tvs().get(install_id) or {}
        if tv.get("status") != "approved":
            return {"status": "pending"}
        src = self._source()
        out: dict[str, Any] = {
            "status": "approved",
            "version": self.config_version(),
            "name": tv.get("name"),
            "profile": tv.get("profile", "adult"),
            "room": tv.get("room"),
            "media_entity": tv.get("media_entity"),
            "playlists": src["playlists"],
            # Channel-id label. Fixed, so favorites keep their ids when providers are added or reordered.
            "source": "thomas-creek",
            "epg_url": src["epg_url"],
            "lineup_version": self.lineup_version(),
            "theme": self.theme(),
            "keys": self.metadata_keys(),
            "favorites": _favorite_channels(self.db),
            "settings": dict(tv.get("settings") or {}),
            "settings_rev": int(tv.get("settings_rev") or 0),
            # Who made the last change: "tv" (this TV pushed it), "phone" or "admin". A TV only takes the
            # device set back when someone else changed it.
            "settings_by": tv.get("settings_by"),
            # Each person's own TV settings; the TV lays the one who is watching over its own.
            "person_settings": _person_layers(self.db),
        }
        # Every TV gets the kids categories and the household, so it can keep a kids profile per child.
        out["kids_categories"] = self.kids_categories()
        out["people"] = _household(self.db)
        out["launcher"] = launcher_block(tv)
        return out

    def metadata_keys(self) -> dict[str, str]:
        """Read-only lookup keys for posters, ratings and subtitle search, sent only to approved TVs.

        An empty value means the hub has no key. A key typed on the TV itself always wins there.
        """
        out: dict[str, str] = {}
        for field, secret in METADATA_KEYS.items():
            try:
                value = str(self.vault.get(secret) or "").strip() if self.vault.has(secret) else ""
            except Exception:  # noqa: BLE001 - a broken vault entry must not break the TV config
                value = ""
            out[field] = value
        return out

    def config_version(self) -> int:
        return int(self.db.kv_get(KV_CONFIG_VERSION) or 1)

    def bump_config(self, install_id: str | None = None) -> int:
        """A new config version. TVs hear `config_changed` (one TV, or all when install_id is None) and pull at once."""
        version = self.config_version() + 1
        self.db.kv_set(KV_CONFIG_VERSION, version)
        if self.on_config is not None:
            try:
                self.on_config(install_id, version)
            except Exception as exc:  # noqa: BLE001 - a push that fails must not undo the change
                log.warning("tvapp: config_changed not sent: %s", exc)
        return version

    def theme(self) -> dict[str, str] | None:
        raw = self.db.kv_get("tvapp.theme")
        if not isinstance(raw, dict):
            return None
        out: dict[str, str] = {}
        for key in ("accent", "background", "motion"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()[:40]
        return out or None


    def lineup_version(self) -> int:
        return int(self._lineup().get("version") or 0)

    def _lineup(self) -> dict[str, Any]:
        raw = self.db.kv_get(KV_LINEUP) or {}
        if not isinstance(raw, dict):
            return {"version": 0, "channels": []}
        channels = raw.get("channels")
        return {"version": int(raw.get("version") or 0),
                "channels": channels if isinstance(channels, list) else []}

    def set_lineup(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if len(rows) > MAX_CHANNELS:
            raise TvAppError(400, "too_many", "Too many channels")
        try:
            channels = [normalize_channel(row) for row in rows]
        except LineupError as exc:
            raise TvAppError(400, exc.code, exc.message) from None
        seen: set[str] = set()
        for row in channels:
            if row["key"] in seen:
                raise TvAppError(400, "bad_channel", f"Channel {row['key']} is listed twice")
            seen.add(row["key"])
        version = self.lineup_version() + 1
        self.db.kv_set(KV_LINEUP, {"version": version, "channels": channels})
        self.bump_config()
        shown = self.public_lineup(kids_only=False)
        shown["hidden"] = sum(1 for row in channels if row.get("hidden"))
        return shown

    def public_lineup(self, *, kids_only: bool) -> dict[str, Any]:
        doc = self._lineup()
        rows = []
        for row in doc["channels"]:
            if row.get("hidden"):
                continue
            if kids_only and not row.get("kids"):
                continue
            shown = public_channel(row)
            art = getattr(self, "artwork", None)
            shown["logo_url"] = art.public_url("logo", shown["key"]) if art is not None else None
            rows.append(shown)
        rows.sort(key=lambda row: (row["number"] is None, row["number"] or 0, row["name"].casefold()))
        return {"version": doc["version"], "channels": rows}

    def require_channel(self, key: str) -> dict[str, Any]:
        if not valid_key(key or ""):
            raise TvAppError(400, "bad_key", "channel_key must be an iptv-org, Xtream or tvg id")
        for row in self._lineup()["channels"]:
            if row.get("key") == key and not row.get("hidden"):
                return row
        raise TvAppError(404, "not_found", "That channel isn't in the lineup")

    def resolve(self, target: str) -> tuple[str, dict[str, Any]]:
        tvs = self.tvs()
        if target in tvs:
            return target, tvs[target]
        matches = [(iid, tv) for iid, tv in tvs.items()
                   if tv.get("media_entity") == target and tv.get("status") == "approved"]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TvAppError(409, "ambiguous", "That player is linked to more than one TV")
        raise TvAppError(404, "not_found", "That TV isn't registered")
