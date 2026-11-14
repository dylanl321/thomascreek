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
    # The MSP account can hold several boxes; the hub only reads and changes the
    # Thomas Creek one. `box` is that box's gid or its name in the Firewalla app
    # (case and spaces ignored). With one box in the account it is used as is.
    box: str | None = "140Thomas"
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
    # Changing trips goes through TREK's MCP server with a machine client (OAuth client_credentials).
    mcp_client_id_secret: str = "trek.mcp_client_id"
    mcp_client_secret_secret: str = "trek.mcp_client_secret"
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


class PiaConfig(BaseModel):
    """The app's "Private" VPN button: Private Internet Access over WireGuard (hub/vpn_pia.py)."""
    enabled: bool = True          # still needs the two 1Password items below before it shows in the app
    # Typed in on the hub admin page; used first when both are saved.
    admin_username_secret: str = "vpn.pia.admin_username"
    admin_password_secret: str = "vpn.pia.admin_password"
    # Copied from the 1Password item "PIA" (onepassword.refs); the fallback.
    username_secret: str = "vpn.pia.username"
    password_secret: str = "vpn.pia.password"
    roles: list[str] = Field(default_factory=lambda: ["admin", "adult"])
    serverlist_url: str = "https://serverlist.piaservers.net/vpninfo/servers/v6"
    regions_cache_s: int = 6 * 3600
    default_region: str = "us_chicago"  # used only when the app sends no region


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
    pia: PiaConfig = Field(default_factory=PiaConfig)


class YouTubeStarter(BaseModel):
    """A channel suggested on the "Add from YouTube" start page until someone follows it."""
    channel_id: str
    name: str
    note: str | None = None


def _starter() -> list[YouTubeStarter]:
    return [YouTubeStarter(channel_id=c, name=n, note=t) for c, n, t in (
        ("UCPlwvN0w4qFSP1FllALB92w", "Numberblocks", "Counting and maths"),
        ("UC_qs3c0ehDvZkbiEbOj6Drg", "Alphablocks", "Letters and reading"),
        ("UCRFIPG2u1DxKLNuE3y2SjHA", "SciShow Kids", "Science"),
        ("UC5XMF3Inoi8R9nSI8ChOsdQ", "Art for Kids Hub", "Drawing along"),
        ("UCONtPx56PSebXJOxbFv-2jQ", "Crash Course Kids", "Science"),
        ("UCY1kMZp36IQSyNx_9h4mpCg", "Mark Rober", "Engineering"),
    )]


class YouTubeConfig(BaseModel):
    """Kids' YouTube (hub/youtube.py). Files go to `dir` (default <data>/youtube); app only, not a Plex library."""
    enabled: bool = True
    dir: str | None = None
    cap_gb: float = 100
    channel_latest: int = 10      # a channel brings its newest N uploads, then each new one
    channel_max: int = 300        # "the whole channel" brings at most this many
    playlist_max: int = 50
    max_height: int = 1080
    max_duration_s: int = 3 * 3600
    check_every_s: int = 3 * 3600
    auto_update: bool = True      # pip install -U yt-dlp once a day
    starter: list[YouTubeStarter] = Field(default_factory=_starter)  # suggestions on the wizard's start page


class AssistantConfig(BaseModel):
    """Ask Thomas Creek (hub/assistant.py)."""

    enabled: bool = True
    key_secret: str = "anthropic.api_key"
    model: str = "claude-sonnet-5-5"
    effort: str = "low"
    max_steps: int = 8
    fallbacks: bool = True  # server-side fallback model if a request is declined
    house_name: str = "Thomas Creek"


class AppUpdateConfig(BaseModel):
    """Telling phones a newer TestFlight build is out (see hub/appupdate.py)."""

    bundle_id: str = "me.dlewis.thomascreek"
    apple_id: str | None = None  # the app's numeric Apple ID; looked up from App Store Connect when unset
    min_build: int = 0  # a floor in config; the web page can raise it
    latest_build: int | None = None  # a latest build typed in config (the web page and App Store Connect also set it)
    check_minutes: int = 15


class ArtworkConfig(BaseModel):
    """Posters and logos for the TV app. The image databases are public; the two keys are optional."""

    enabled: bool = True
    tmdb_key_secret: str = "tmdb.api_key"
    fanart_key_secret: str = "fanart.api_key"


class TvAppConfig(BaseModel):
    """Thomas Creek TV, the household IPTV app, kept installed on the Android TV boxes (see hub/tvapp.py)."""

    enabled: bool = True
    repo: str = "dylanl321/thomascreek-tv"
    package: str = "me.dlewis.thomascreek.tv"
    adb_port: int = 5555
    check_minutes: int = 360
    # Component set as the home screen when someone asks. Stock launcher stays enabled.
    home_activity: str = "me.dlewis.thomascreek.tv/com.ipvio.stream.MainActivity"


class PrinterEntry(BaseModel):
    """One Bambu printer. Host, serial and access code can live in the vault as
    printer.<id>.host / .serial / .access_code so they're added on the admin page."""
    id: str = "h2s"
    name: str = "H2S"
    model: str = "H2S"
    host: str | None = None
    serial: str | None = None
    # Where project_file finds a file uploaded over FTPS.
    file_url_prefix: str = "file:///sdcard/"


class PrintersConfig(BaseModel):
    enabled: bool = True
    printers: list[PrinterEntry] = Field(default_factory=lambda: [PrinterEntry()])
    # Push "done" / "stopped" / "needs you" to whoever started the print (admins otherwise).
    notify: bool = True


class ChildPolicy(BaseModel):
    climate_min: float = 66
    climate_max: float = 74
    bedtime: str = "20:30"  # shared-space controls go quiet after this
    # Plex content ratings a child may browse and play.
    ratings: list[str] = Field(default_factory=lambda: ["G", "PG", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG"])


class SmartConfig(BaseModel):
    """The "Right now" cards on Home. Times are local, "HH:MM"."""

    enabled: bool = True
    dinner_time: str = "18:15"
    dinner_lead_min: int = 90          # the dinner card shows this long before dinner
    lockup_time: str = "21:30"         # "Lock up for the night" from here until 3 am
    trash_night: str | None = "thursday"  # evening before pickup; None hides the card
    # Bedtime per person (first name or person id) -> "HH:MM". Children without one use
    # 19:30 on the picture-first layout and child_policy.bedtime otherwise.
    bedtimes: dict[str, str] = Field(default_factory=lambda: {"Adeline": "19:30", "Theo": "20:30"})
    wake_time: str = "07:00"           # a bedtime pause lasts until this time
    latitude: float | None = None      # default: Home Assistant's location
    longitude: float | None = None
    weather_alerts: bool = True        # watch NWS/DWD alerts for home (and a trip under way) and push them
    goodnight_mode: str | None = None  # a mode id to run for "Goodnight" instead of the card's own steps
    off_rules: list[str] = Field(default_factory=list)  # rule ids switched off for everyone


class GuestLockCode(BaseModel):
    """Guest passes can put their door code on a lock: Z-Wave JS user codes or Keymaster slots."""

    entity: str | None = None          # e.g. lock.front_door
    door_name: str = "Front door"
    service: str = "zwave_js"          # zwave_js | keymaster
    slots: list[int] = Field(default_factory=lambda: [20, 21, 22, 23])


class GuestInfo(BaseModel):
    wifi_ssid: str | None = None
    address: str | None = None         # shown on a guest pass
    host_phone: str | None = None      # "Text Dylan" on a guest pass
    lock_code: GuestLockCode | None = None
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
    # Sign-in steps that need no token (enroll, challenge, token, hello, the web page),
    # per caller address per minute, for callers away from home. 0 turns it off.
    away_auth_per_minute: int = 30
    access_token_ttl_s: int = 900
    poll_interval_s: float = 15.0
    timezone: str = "America/New_York"
    # The hub as published through the reverse proxy, if Dylan enables that.
    remote_url: str | None = None
    # First admin without a shell on the NAS: on start, if nobody is admin yet and
    # exactly one phone has enrolled (still pending) since `enrolled_after` (Unix
    # time), that phone becomes this person's, as admin. Remove it afterwards.
    bootstrap_admin: dict[str, Any] | None = None
    # Household members to create on start if nobody by that name exists yet,
    # e.g. [{name: Theo, role: child, ui_style: kid}]. Never edits or removes
    # anyone, so later changes in the app stick. Safe to leave in place.
    seed_people: list[dict[str, Any]] = Field(default_factory=list)
    # Secure actions a grown-up may run from a widget, Control Center, CarPlay or the Watch without
    # the Face ID step (the phone must be unlocked, or the Watch on the wrist). Never door unlocks.
    quick_actions: list[str] = Field(default_factory=lambda: ["garage.set"])  # Dylan chose this, 2026-11-12

    @field_validator("quick_actions")
    @classmethod
    def _no_unlock(cls, v: list[str]) -> list[str]:
        return [a for a in v if a in ("garage.set",)]

    @field_validator("home_networks", "trusted_proxies")
    @classmethod
    def _valid_networks(cls, v: list[str]) -> list[str]:
        for net in v:
            ipaddress.ip_network(net, strict=False)
        return v


class AmbientStream(BaseModel):
    """One ambience Dylan assigned a channel or a direct stream to. The id is one of the curated set."""

    id: str
    channel_key: str | None = None
    stream_url: str | None = None
    poster_url: str | None = None


class AmbientConfig(BaseModel):
    """Seasonal scenes for an idle TV. The curated list works with no entries here."""

    pin: str | None = None
    streams: list[AmbientStream] = Field(default_factory=list)


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
    app_update: AppUpdateConfig = Field(default_factory=AppUpdateConfig)
    tv_app: TvAppConfig = Field(default_factory=TvAppConfig)
    artwork: ArtworkConfig = Field(default_factory=ArtworkConfig)
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    printers: PrintersConfig = Field(default_factory=PrintersConfig)
    child_policy: ChildPolicy = Field(default_factory=ChildPolicy)
    smart: SmartConfig = Field(default_factory=SmartConfig)
    guest: GuestInfo = Field(default_factory=GuestInfo)
    ambient: AmbientConfig = Field(default_factory=AmbientConfig)
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
