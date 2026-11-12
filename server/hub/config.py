"""Hub configuration.

Non-secret settings (addresses, feature switches, room layout) live in
`<data_dir>/config.yaml` on the NAS dataset, never in this repository, because
the repository is public. Secret values never appear here: each service names
the vault entry that holds its credential (for example `token_secret:
home_assistant.token`).
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ServiceConfig(BaseModel):
    enabled: bool = True
    url: str | None = None
    verify_tls: bool = True
    timeout_s: float = 8.0
    # Names of vault entries; never values.
    token_secret: str | None = None
    username: str | None = None
    password_secret: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class FrigateConfig(ServiceConfig):
    go2rtc_url: str | None = None
    # Public URL the app uses for WebRTC signalling through the hub.
    cameras: list[str] = Field(default_factory=list)


class FirewallaConfig(BaseModel):
    enabled: bool = False
    msp_domain: str | None = None
    token_secret: str = "firewalla.msp_token"
    timeout_s: float = 8.0
    # person_id -> list of Firewalla rule ids that block that person's devices.
    # Dylan creates one "block internet" rule per child device group in the
    # Firewalla app; the hub only pauses (= internet allowed) and resumes them.
    person_rules: dict[str, list[str]] = Field(default_factory=dict)


# Files the show player's /house page loads by absolute path.
PAGE_ASSETS = ["layout.json", "frame", "house.obj", "house.mtl", "js"]


class ShowPlayerConfig(ServiceConfig):
    # show_player.py expects "Authorization: Bearer <APP_TOKEN>".
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "
    # Paths a role may reach. Keys are role names; values are path prefixes.
    allowed_paths: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "admin": [""],
            "adult": [
                "status", "start", "stop", "next", "play", "queue", "unqueue",
                "look", "brightness", "schedule", "api/live", "api/notifications",
                "house", "static", *PAGE_ASSETS,
            ],
            "child": ["status", "look", "play", "api/live", "house", "static", *PAGE_ASSETS],
            "guest": ["status"],
            "tablet": ["status", "look", "brightness", "next", "play", "api/live", "house", "static", *PAGE_ASSETS],
        }
    )


class TrekConfig(ServiceConfig):
    """TREK trip planner. `url` is how the hub reaches it on the LAN; `web_url` is
    the address a phone opens for "Plan in TREK" (the proxied site, if there is one)."""
    web_url: str | None = None
    token_secret: str | None = "trek.api_key"
    # Trip ids children and the kitchen tablet never see (surprises, adults-only trips).
    hidden_from_children: list[int] = Field(default_factory=list)


class OnePasswordConfig(BaseModel):
    """Copy credentials from 1Password into the vault (see hub/onepassword.py)."""
    enabled: bool = False
    token_secret: str = "onepassword.service_account_token"
    vault: str | None = "Thomas Creek Hub"
    refs: dict[str, str] = Field(default_factory=dict)   # hub secret name -> op://vault/item/field
    refresh_s: int = 3600


class ApnsConfig(BaseModel):
    enabled: bool = False
    team_id: str | None = None
    key_id: str | None = None
    key_secret: str = "apns.auth_key_p8"
    topic: str = "me.dlewis.thomascreek"


class VpnConfig(BaseModel):
    provider: str = "none"  # "none" | "wg-cli" | "pool"
    # provider "pool": WireGuard client configs made on another server (Firewalla),
    # stored as vault secrets named <pool_prefix><n>, one handed to each phone.
    pool_prefix: str = "vpn.pool."
    interface: str = "wg0"
    endpoint: str | None = None  # host:port the phones dial from outside
    subnet: str = "10.77.0.0/24"
    dns: list[str] = Field(default_factory=list)
    allowed_ips: list[str] = Field(default_factory=list)  # home LAN ranges
    home_ssids: list[str] = Field(default_factory=list)
    persistent_keepalive: int = 25
    server_key_secret: str = "wireguard.server_private_key"


class YouTubeConfig(BaseModel):
    """Kids' YouTube (hub/youtube.py). Files go to `dir` (default <data>/youtube); app only, not a Plex library."""
    enabled: bool = True
    dir: str | None = None
    cap_gb: float = 100
    channel_latest: int = 10      # a channel brings its newest N uploads, then each new one
    playlist_max: int = 50
    max_height: int = 1080
    max_duration_s: int = 3 * 3600
    check_every_s: int = 3 * 3600
    auto_update: bool = True      # pip install -U yt-dlp once a day


class ChildPolicy(BaseModel):
    climate_min: float = 66
    climate_max: float = 74
    bedtime: str = "20:30"  # shared-space controls go quiet after this
    # Plex content ratings a child may browse and play.
    ratings: list[str] = Field(default_factory=lambda: ["G", "PG", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG"])


class GuestInfo(BaseModel):
    wifi_ssid: str | None = None
    wifi_password_secret: str = "guest_wifi.password"
    host_name: str | None = None
    guidance: list[dict[str, str]] = Field(default_factory=list)  # {icon,title,detail}


class AreaOverride(BaseModel):
    name: str | None = None
    floor: str | None = None
    symbol: str | None = None
    entities: list[str] = Field(default_factory=list)
    shared: bool = False


class ModeConfig(BaseModel):
    id: str
    name: str
    symbol: str = "sparkles"
    # Ordered steps. Each step is {action, target, params}; steps run through
    # the same policy and dry-run checks as a tap.
    steps: list[dict[str, Any]] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=lambda: ["admin", "adult"])


class HubSettings(BaseModel):
    id: str = "thomascreek-hub"
    name: str = "Thomas Creek"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8443
    tls_cert: str | None = None  # default: <data>/tls/cert.pem (self-signed)
    tls_key: str | None = None
    bonjour: bool = True
    # Writes are simulated unless the action is listed in live_actions.
    dry_run: bool = True
    live_actions: list[str] = Field(default_factory=list)
    # Requests from these networks count as "at home" (enrollment, camera tier).
    home_networks: list[str] = Field(default_factory=lambda: ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"])
    # Reverse proxies whose X-Forwarded-For is believed. Empty: trust nobody.
    trusted_proxies: list[str] = Field(default_factory=list)
    access_token_ttl_s: int = 900
    poll_interval_s: float = 15.0
    timezone: str = "America/New_York"
    # The hub as published through the reverse proxy, if Dylan enables that.
    remote_url: str | None = None
    # First admin without a shell on the NAS: on start, if nobody is admin yet and
    # exactly one phone has enrolled (still pending) since `enrolled_after` (Unix
    # time), that phone becomes this person's, as admin. Remove it afterwards.
    bootstrap_admin: dict[str, Any] | None = None

    @field_validator("home_networks", "trusted_proxies")
    @classmethod
    def _valid_networks(cls, v: list[str]) -> list[str]:
        for net in v:
            ipaddress.ip_network(net, strict=False)
        return v


class Config(BaseModel):
    hub: HubSettings = Field(default_factory=HubSettings)
    home_assistant: ServiceConfig = Field(default_factory=lambda: ServiceConfig(token_secret="home_assistant.token"))
    frigate: FrigateConfig = Field(default_factory=lambda: FrigateConfig(enabled=False))
    plex: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="plex.token"))
    jellyseerr: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="jellyseerr.api_key"))
    sonarr: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="sonarr.api_key"))
    radarr: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="radarr.api_key"))
    truenas: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="truenas.api_key"))
    mealie: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="mealie.token"))
    homebox: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, password_secret="homebox.password"))
    family_board: ServiceConfig = Field(default_factory=lambda: ServiceConfig(enabled=False, token_secret="family_board.admin_token"))
    trek: TrekConfig = Field(default_factory=lambda: TrekConfig(enabled=False, token_secret="trek.api_key"))
    show_player: ShowPlayerConfig = Field(default_factory=lambda: ShowPlayerConfig(enabled=False, token_secret="show_player.app_token"))
    firewalla: FirewallaConfig = Field(default_factory=FirewallaConfig)
    apns: ApnsConfig = Field(default_factory=ApnsConfig)
    onepassword: OnePasswordConfig = Field(default_factory=OnePasswordConfig)
    vpn: VpnConfig = Field(default_factory=VpnConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    child_policy: ChildPolicy = Field(default_factory=ChildPolicy)
    guest: GuestInfo = Field(default_factory=GuestInfo)
    areas: dict[str, AreaOverride] = Field(default_factory=dict)
    floors: list[dict[str, Any]] = Field(default_factory=list)
    modes: list[ModeConfig] = Field(default_factory=list)
    favorites: list[str] = Field(default_factory=list)
    assets: dict[str, str] = Field(default_factory=dict)  # name -> path under data dir


def data_dir() -> Path:
    return Path(os.environ.get("HUB_DATA_DIR", "/data"))


def master_key_path() -> Path:
    return Path(os.environ.get("HUB_MASTER_KEY_FILE", "/run/secrets/hub_master_key"))


def load_config(path: Path | None = None) -> Config:
    if path is None:
        override = os.environ.get("HUB_CONFIG_FILE")
        path = Path(override) if override else data_dir() / "config.yaml"
    if not path.exists():
        return Config()
    raw = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(raw)
