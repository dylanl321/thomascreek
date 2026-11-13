"""What a TV's remote can do through Home Assistant: buttons, app shortcuts, inputs and typing.

Every key goes through the TV's own Home Assistant integration (Android TV Remote protocol v2 for Google /
Android TVs, pyatv for Apple TV, ECP for Roku, and so on), so a TV paired once on the hub works from every
phone. The app draws a full remote for adults and a simple one for kids from what is described here.
"""

from __future__ import annotations

import re
from typing import Any

# Hub key name -> the integration's own key, per integration. A key is offered only when the TV's
# integration has it; the app hides the rest.
REMOTE_KEYS: dict[str, dict[str, str]] = {
    "androidtv_remote": {
        "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT", "select": "DPAD_CENTER",
        "back": "BACK", "home": "HOME", "menu": "MENU", "play_pause": "MEDIA_PLAY_PAUSE",
        "rewind": "MEDIA_REWIND", "fast_forward": "MEDIA_FAST_FORWARD", "ch_up": "CHANNEL_UP",
        "ch_down": "CHANNEL_DOWN", "input": "TV_INPUT", "guide": "GUIDE", "info": "INFO", "settings": "SETTINGS",
        "search": "SEARCH", "assistant": "ASSIST", "captions": "CAPTIONS",
    },
    "apple_tv": {
        "up": "up", "down": "down", "left": "left", "right": "right", "select": "select", "back": "menu",
        "home": "home", "menu": "top_menu", "play_pause": "play_pause", "rewind": "skip_backward",
        "fast_forward": "skip_forward",
    },
    "roku": {
        "up": "up", "down": "down", "left": "left", "right": "right", "select": "select", "back": "back",
        "home": "home", "play_pause": "play", "rewind": "reverse", "fast_forward": "forward", "info": "info",
        "ch_up": "channel_up", "ch_down": "channel_down", "search": "search", "replay": "replay",
    },
    "samsungtv": {
        "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT", "select": "KEY_ENTER",
        "back": "KEY_RETURN", "home": "KEY_HOME", "menu": "KEY_MENU", "play_pause": "KEY_PLAY",
        "rewind": "KEY_REWIND", "fast_forward": "KEY_FF", "ch_up": "KEY_CHUP", "ch_down": "KEY_CHDOWN",
        "input": "KEY_SOURCE", "guide": "KEY_GUIDE", "info": "KEY_INFO",
    },
    "webostv": {
        "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "select": "ENTER", "back": "BACK",
        "home": "HOME", "menu": "MENU", "play_pause": "PLAY", "rewind": "REWIND", "fast_forward": "FASTFORWARD",
        "ch_up": "CHANNELUP", "ch_down": "CHANNELDOWN", "info": "INFO", "exit": "EXIT",
    },
    "braviatv": {
        "up": "Up", "down": "Down", "left": "Left", "right": "Right", "select": "Confirm", "back": "Return",
        "home": "Home", "menu": "Options", "play_pause": "Play", "rewind": "Rewind", "fast_forward": "Forward",
        "ch_up": "ChannelUp", "ch_down": "ChannelDown", "input": "Input", "guide": "GGuide", "info": "Display",
    },
    "androidtv": {
        "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "select": "CENTER", "back": "BACK",
        "home": "HOME", "menu": "MENU",
    },
}
# Integrations whose buttons go to a service on the media player itself, not a remote entity.
PLAYER_KEY_SERVICES = {"webostv": ("webostv", "button", "button"), "androidtv": ("androidtv", "adb_command", "command")}
DPAD = ("up", "down", "left", "right", "select", "back", "home")
ALL_KEYS = tuple(dict.fromkeys(k for keys in REMOTE_KEYS.values() for k in keys))

# One catalog of apps for every kind of TV. The app shows the same "YouTube" or "Netflix" button on a
# Google TV, an Apple TV, a Roku or an LG; the hub works out how to open it on each. Google / Android TV opens
# an app by package name (a TV without it installed opens its store page); the others open one of the apps
# the TV itself lists, matched by name. "kid" apps are the ones a child's remote shows and may open.
APPS: list[dict[str, Any]] = [
    {"id": "youtube", "name": "YouTube", "kid": False, "android": "com.google.android.youtube.tv",
     "aliases": ["youtube"]},
    {"id": "youtube_kids", "name": "YouTube Kids", "kid": True, "android": "com.google.android.youtube.tvkids",
     "aliases": ["youtube kids", "youtubekids"]},
    {"id": "netflix", "name": "Netflix", "kid": True, "android": "com.netflix.ninja", "aliases": ["netflix"]},
    {"id": "plex", "name": "Plex", "kid": True, "android": "com.plexapp.android", "aliases": ["plex"]},
    {"id": "disney_plus", "name": "Disney+", "kid": True, "android": "com.disney.disneyplus",
     "aliases": ["disney+", "disney plus", "disneyplus", "disney"]},
    {"id": "pbs_kids", "name": "PBS Kids", "kid": True, "android": "com.pbs.kids.pbskidsvideo",
     "aliases": ["pbs kids", "pbs kids video"]},
    {"id": "prime_video", "name": "Prime Video", "kid": False, "android": "com.amazon.amazonvideo.livingroom",
     "aliases": ["prime video", "amazon prime video", "amazon video", "prime"]},
    {"id": "max", "name": "Max", "kid": False, "android": "com.wbd.stream", "aliases": ["max", "hbo max"]},
    {"id": "hulu", "name": "Hulu", "kid": False, "android": "com.hulu.livingroomplus", "aliases": ["hulu"]},
    {"id": "spotify", "name": "Spotify", "kid": False, "android": "com.spotify.tv.android", "aliases": ["spotify"]},
    {"id": "apple_tv_app", "name": "Apple TV", "kid": False, "android": "com.apple.atve.androidtv.appletv",
     "aliases": ["tv", "apple tv", "apple tv+"]},
    # The family's live TV: TREX on the Android boxes, UHF on the Apple TVs. TREX is sideloaded, so its package
    # name is learned the first time it's seen open on any box (see learn()), or set in extra.app_packages.
    {"id": "live_tv", "name": "TREX / UHF", "kid": False, "android": None, "android_name": "TREX",
     "learn": r"trex", "aliases": ["trex", "uhf", "uhf love your iptv", "trex uhf"]},
]
# Packages for catalog apps that aren't in an app store: {app id: package}, learned or configured.
LEARNED: dict[str, str] = {}


def android_package(a: dict[str, Any]) -> str | None:
    return a.get("android") or LEARNED.get(a["id"])


def allowed_packages(*, kids: bool) -> list[str]:
    """Android packages a TV may show. A kids profile gets the kid apps; an adult profile gets the catalog."""
    out: list[str] = []
    for app in APPS:
        package = android_package(app)
        if not package:
            continue
        if kids and not app.get("kid"):
            continue
        out.append(package)
    return out


def learn(app_ids: Any) -> bool:
    """Remember the package of a sideloaded catalog app (TREX) from what the boxes report as open."""
    new = False
    for pkg in app_ids:
        if not isinstance(pkg, str) or not re.fullmatch(r"[a-zA-Z][\w]*(\.[\w]+)+", pkg):
            continue
        for a in APPS:
            if a.get("learn") and LEARNED.get(a["id"]) != pkg and re.search(a["learn"], pkg, re.I):
                LEARNED[a["id"]] = pkg
                new = True
    return new
ANDROID_APPS = [{"id": a["android"], "name": a["name"], "kid": a["kid"]} for a in APPS if a["android"]]
_BY_ID = {a["id"]: a for a in APPS}
_KNOWN_APPS = {a["id"]: a["name"] for a in ANDROID_APPS} | {
    "com.google.android.apps.tv.launcherx": "Home",
    "com.google.android.tvlauncher": "Home",
    "com.google.android.leanbacklauncher": "Home",
    "com.nvidia.shield.ask": "Home",
    "com.android.tv.settings": "Settings",
    "com.google.android.tv.remote.service": "Home",
    "com.google.android.youtube.tvunplugged": "YouTube TV",
    "com.google.android.videos": "Google TV",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "com.peacocktv.peacockandroid": "Peacock",
    "com.cbs.ott": "Paramount+",
    "com.pbs.kids.pbskidsvideo": "PBS Kids",
    "com.google.android.katniss": "Google Assistant",
    "com.google.android.tungsten.setupwraith": "Setup",
    "com.android.vending": "Play Store",
}
# Names that mean a TV input rather than an app in a source list.
_INPUT = re.compile(r"^(hdmi|input|av|component|composite|tv|live tv|antenna|cable|usb|dvi|vga|scart|pc|"
                    r"tuner|game|aux|optical)\b|hdmi", re.I)
def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9+]", " ", (name or "").lower()).strip()


_BY_ALIAS = {re.sub(r"\s+", " ", _key(al)): a for a in APPS for al in a["aliases"] + [a["name"]]}


def catalog_app(name: str | None) -> dict[str, Any] | None:
    """'YouTube', 'youtube', 'com.google.android.youtube.tv' or 'Disney Plus' -> the catalog entry."""
    if not name:
        return None
    if name in _BY_ID:
        return _BY_ID[name]
    for a in APPS:
        if name in (a["android"], LEARNED.get(a["id"])):
            return a
    return _BY_ALIAS.get(re.sub(r"\s+", " ", _key(name)))


def app_label(app: str | None) -> str | None:
    """'com.google.android.apps.tv.launcherx' -> 'Home'; an unknown package becomes a readable name."""
    if not app:
        return None
    if app in _KNOWN_APPS:
        return _KNOWN_APPS[app]
    for a in APPS:
        if a.get("learn") and app == LEARNED.get(a["id"]):
            return a.get("android_name") or a["name"]
    if re.fullmatch(r"[a-z][\w]*(\.[\w]+){2,}", app):
        parts = [p for p in app.split(".") if p not in ("com", "tv", "android", "app", "apps", "google", "ott",
                                                          "livingroom", "androidtv", "leanback", "atv")]
        name = parts[0] if parts else app.rsplit(".", 1)[-1]
        return name[:1].upper() + name[1:]
    return app


def is_input(name: str) -> bool:
    return bool(_INPUT.search(name or ""))


def kid_app(app: str | None) -> bool:
    """Only catalog apps marked for kids; anything else on a child's remote asks a grown-up."""
    if app and app.startswith("src:"):
        app = app[4:]
    c = catalog_app(app)
    return bool(c and c["kid"])


def device_apps(plat: str | None, sources: list[str]) -> list[dict[str, Any]]:
    """Apps this TV can open, as {id, name, kid, target}. The id is the same on every TV ("youtube");
    target is what this TV needs (a package name, or the name in its own source list)."""
    if plat == "androidtv_remote":
        return [{"id": a["id"], "name": a.get("android_name") or a["name"], "kid": a["kid"], "target": pkg}
                for a in APPS if (pkg := android_package(a))]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in sources:
        if is_input(src):
            continue
        c = catalog_app(src)
        aid = c["id"] if c else f"src:{src}"
        if aid in seen:
            continue
        seen.add(aid)
        name = c["name"] if c and not c.get("learn") else src  # live TV keeps the TV's own name (UHF)
        out.append({"id": aid, "name": name, "kid": bool(c and c["kid"]), "target": src})
    return out


def find_app(apps: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """The device's entry for an app asked for by id, name, package or source name."""
    c = catalog_app(wanted)
    for a in apps:
        if wanted in (a["id"], a["target"], a["name"]) or (c and a["id"] == c["id"]):
            return a
    return None


def remote_info(st: dict[str, Any], link: dict[str, Any] | None) -> dict[str, Any] | None:
    """The remote for one TV: its keys, app shortcuts, inputs, and whether it takes typed text."""
    link = link or {}
    plat = link.get("platform")
    a = st.get("attributes", {})
    sources = [str(x) for x in a.get("source_list") or []]
    has_keys = plat in REMOTE_KEYS and bool(link.get("remote") or plat in PLAYER_KEY_SERVICES)
    keys = list(REMOTE_KEYS[plat]) if has_keys else []
    apps = [{k: v for k, v in x.items() if k != "target"} for x in device_apps(plat, sources)]
    inputs = [s for s in sources if is_input(s)]
    if not (keys or apps or inputs):
        return None
    return {
        "platform": plat,
        "keys": keys,
        "apps": apps,
        "inputs": inputs,
        "text": plat == "androidtv_remote" and has_keys,
        "app": app_label(a.get("app_name") or a.get("app_id") or a.get("source")),
    }


# Typing on Android TV: one key per character (the TV's search boxes take lowercase letters, digits and
# a little punctuation). Anything else is left out rather than sent wrong.
_TEXT_KEYS = {" ": "SPACE", ".": "PERIOD", ",": "COMMA", "-": "MINUS", "'": "APOSTROPHE", "@": "AT",
              "/": "SLASH", "+": "PLUS", "=": "EQUALS", ";": "SEMICOLON"}


def text_keys(text: str) -> list[str]:
    out: list[str] = []
    for ch in text.lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch.upper())
        elif ch in _TEXT_KEYS:
            out.append(_TEXT_KEYS[ch])
    return out
