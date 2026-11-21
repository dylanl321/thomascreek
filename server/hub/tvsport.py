"""Sport for the house: today's games from ESPN's public scoreboards, each with the house channels that show it.

The TVs used to fetch ESPN themselves and match games against sports channels only, so a Sunday-night NFL game
on the local NBC station said "No matching channel". Here the hub fetches each league once for every screen,
keeps it for a short while, and matches a game to channels in three ways, best first:

  1. the broadcaster ESPN names ("NBC", "ESPN", "FS1") against the house's basic lineup, so the local affiliate
     and the standard networks are found even when the guide says only "NFL Football";
  2. guide programmes around the start that name both teams (or the racing series), on any channel;
  3. a channel whose own name names both teams (event channels).

US leagues come first (NFL, NBA, MLB, NHL, college football, racing with F1, NASCAR and IndyCar), then soccer,
tennis, fighting and golf. No key is needed; nothing here is personal.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("hub.sport")

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
LIVE_TTL_S = 60
IDLE_TTL_S = 15 * 60
WINDOW_BEFORE_S = 12 * 3600
WINDOW_AFTER_S = 36 * 3600
EVENT_AHEAD_S = 7 * 86400
MAX_CHANNELS = 4
DAY_RANGE = 14
PAST_TTL_S = 6 * 3600
MAX_BOARDS = 200

TEAM, RACE, FIGHT, GOLF, TENNIS = "team", "race", "fight", "golf", "tennis"

# (name, ESPN path, sport key, kind, date mode). "week": football's scoreboard lists the whole week without a date.
LEAGUES: list[tuple[str, str, str, str, str]] = [
    ("NFL", "football/nfl", "nfl", TEAM, "week"),
    ("NBA", "basketball/nba", "basketball", TEAM, "day"),
    ("MLB", "baseball/mlb", "mlb", TEAM, "day"),
    ("Formula 1", "racing/f1", "f1", RACE, "event"),
    ("NASCAR", "racing/nascar-premier", "racing", RACE, "event"),
    ("IndyCar", "racing/irl", "racing", RACE, "event"),
    ("College Football", "football/college-football", "nfl", TEAM, "week"),
    ("NHL", "hockey/nhl", "nhl", TEAM, "day"),
    ("WNBA", "basketball/wnba", "basketball", TEAM, "day"),
    ("Men's College Basketball", "basketball/mens-college-basketball", "basketball", TEAM, "day"),
    ("MLS", "soccer/usa.1", "soccer", TEAM, "day"),
    ("Premier League", "soccer/eng.1", "soccer", TEAM, "day"),
    ("Champions League", "soccer/uefa.champions", "soccer", TEAM, "day"),
    ("UFC", "mma/ufc", "ufc", FIGHT, "event"),
    ("PGA Tour", "golf/pga", "golf", GOLF, "event"),
]
POPULARITY = {name: i for i, (name, *_rest) in enumerate(LEAGUES)}

# ESPN broadcaster names -> lineup channel names (tvbasic's names; locals are "NBC 11 WXIA"). Streaming-only
# services (Peacock, Prime Video, Netflix, ESPN+) have no channel and are left as text.
BROADCASTERS: dict[str, tuple[str, ...]] = {
    "cbs": ("CBS",), "nbc": ("NBC",), "abc": ("ABC",), "fox": ("FOX",), "cw": ("CW",), "the cw": ("CW",),
    "telemundo": ("Telemundo",), "univision": ("Univision",), "ion": ("ION",),
    "espn": ("ESPN",), "espn2": ("ESPN2",), "espnu": ("ESPNU",), "espnews": ("ESPNews",),
    "fs1": ("FS1",), "fs2": ("FS2",), "fox sports 1": ("FS1",),
    "nfl net": ("NFL Network",), "nfl network": ("NFL Network",), "nfln": ("NFL Network",),
    "nba tv": ("NBA TV",), "nbatv": ("NBA TV",), "mlb net": ("MLB Network",), "mlb network": ("MLB Network",),
    "mlbn": ("MLB Network",), "nhl network": ("NHL Network",), "nhln": ("NHL Network",),
    "tnt": ("TNT",), "tbs": ("TBS",), "trutv": ("TruTV",), "usa net": ("USA Network",), "usa network": ("USA Network",),
    "usa": ("USA Network",), "golf": ("Golf Channel",), "golf channel": ("Golf Channel",),
    "tennis channel": ("Tennis Channel",), "cbssn": ("CBS Sports Network",), "cbs sports network": ("CBS Sports Network",),
    "sec network": ("SEC Network",), "secn": ("SEC Network",), "acc network": ("ACC Network",), "accn": ("ACC Network",),
    "btn": ("Big Ten Network",), "big ten network": ("Big Ten Network",),
}

_RACE = {
    "Formula 1": re.compile(r"formula\s?1|formel\s?1|\bf1\b|grand\s?prix", re.I),
    "NASCAR": re.compile(r"\bnascar\b|cup\s?series|xfinity|daytona\s?500", re.I),
    "IndyCar": re.compile(r"\bindy\s?car\b|indy(anapolis)?\s?500", re.I),
}
_GOLF = re.compile(r"\bgolf\b|\bpga\b", re.I)
_UFC = re.compile(r"\bufc\b", re.I)
_GENERIC = {"fc", "sc", "real", "club", "city", "united", "athletic", "sporting", "inter", "new", "los", "san", "the",
            "state", "university", "college"}
_NONWORD = re.compile(r"[^a-z0-9 ]")
_SPACES = re.compile(r"\s+")
_SESSIONS = {"FP1": "Practice 1", "FP2": "Practice 2", "FP3": "Practice 3", "Qual": "Qualifying", "Race": "Race",
             "SS": "Sprint qualifying", "Sprint": "Sprint"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _SPACES.sub(" ", _NONWORD.sub(" ", text)).strip()


def team_keys(name: str, short: str) -> list[str]:
    """Words that name a team in a guide title: short name, full name, and a distinctive last or first word."""
    full, small = norm(name), norm(short)
    out = [small, full]
    words = full.split()
    if words and len(words[-1]) >= 5 and words[-1] not in _GENERIC:
        out.append(words[-1])
    if words and len(words[0]) >= 5 and words[0] not in _GENERIC:
        out.append(words[0])
    seen: list[str] = []
    for key in out:
        if len(key) >= 3 and key not in seen:
            seen.append(key)
    return seen


def _has(text: str, key: str) -> bool:
    return f" {key} " in f" {text} "


def parse_date(value: str) -> float:
    """ESPN time ("2026-11-16T01:20Z", seconds optional) as unix seconds; 0 when unreadable."""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo("UTC")).timestamp()
        except ValueError:
            continue
    return 0.0


def _state(status: dict[str, Any]) -> str:
    return {"in": "live", "post": "final"}.get(str(((status or {}).get("type") or {}).get("state") or ""), "upcoming")


def _broadcasts(comp: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for row in comp.get("broadcasts") or []:
        for name in (row or {}).get("names") or []:
            if isinstance(name, str) and name.strip() and name.strip() not in names:
                names.append(name.strip())
    for row in comp.get("geoBroadcasts") or []:
        name = (((row or {}).get("media") or {}).get("shortName") or "").strip()
        if name and name not in names:
            names.append(name)
    return names[:8]


def _team(c: dict[str, Any], started: bool = True) -> dict[str, Any]:
    t = c.get("team") or {}
    name = str(t.get("displayName") or t.get("name") or "")
    logo = t.get("logo") or (((t.get("logos") or [{}])[0]) or {}).get("href")
    records = c.get("records") or []
    return {
        "id": str(t.get("id") or ""), "name": name, "short": str(t.get("shortDisplayName") or name),
        "abbr": str(t.get("abbreviation") or ""), "logo": logo if isinstance(logo, str) and logo.startswith("https://") else None,
        "color": t.get("color") or None,
        "score": (str(c["score"]) if started and c.get("score") not in (None, "") else None),
        "winner": bool(c.get("winner")), "record": ((records[0] or {}).get("summary") if records else None),
    }


def parse(body: dict[str, Any], league: tuple[str, str, str, str, str]) -> list[dict[str, Any]]:
    """Games (or race sessions, fights, rounds) of one scoreboard."""
    name, _path, sport, kind, _mode = league
    out: list[dict[str, Any]] = []
    for event in body.get("events") or []:
        if not isinstance(event, dict):
            continue
        comps = [c for c in event.get("competitions") or [] if isinstance(c, dict)]
        if kind == TEAM:
            comps = comps[:1]
        for comp in comps:
            status = comp.get("status") or event.get("status") or {}
            kind_name = str(((status.get("type") or {}).get("name")) or "")
            game: dict[str, Any] = {
                "id": f"{name}|{comp.get('id') or event.get('id')}", "league": name, "sport": sport, "kind": kind,
                "start": parse_date(str(comp.get("date") or event.get("date") or "")),
                "state": _state(status), "status": kind_name,
                "detail": str((status.get("type") or {}).get("shortDetail") or ""),
                "clock": str(status.get("displayClock") or ""), "period": int(status.get("period") or 0),
                "venue": str(((comp.get("venue") or {}).get("fullName")) or ((event.get("circuit") or {}).get("fullName")) or ""),
                "title": str(event.get("name") or event.get("shortName") or ""),
                "broadcasts": _broadcasts(comp) or _broadcasts(event),
                "home": None, "away": None,
            }
            if kind == TEAM:
                teams = [c for c in comp.get("competitors") or [] if isinstance(c, dict)]
                home = next((c for c in teams if c.get("homeAway") == "home"), None)
                away = next((c for c in teams if c.get("homeAway") == "away"), None)
                if home is None or away is None:
                    continue
                started = game["state"] != "upcoming"  # ESPN sends "0" before kickoff
                game["home"], game["away"] = _team(home, started), _team(away, started)
                notes = comp.get("notes") or []
                game["note"] = str((notes[0] or {}).get("headline") or "") if notes else ""
            else:
                abbr = str(((comp.get("type") or {}).get("abbreviation")) or "")
                game["note"] = _SESSIONS.get(abbr, abbr)
            out.append(game)
    return out


class Sport:
    """Scoreboards kept for a minute while something is live, a quarter hour otherwise."""

    def __init__(self, hub: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.hub = hub
        self._transport = transport
        self._boards: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._dated: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # "League|20261118" or "League|now"
        self._failed: set[str] = set()
        self._lock = asyncio.Lock()
        self._refreshing: asyncio.Task[None] | None = None

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.hub.config.hub.timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("America/New_York")

    def _url(self, league: tuple[str, str, str, str, str], now: float) -> str:
        _name, path, _sport, _kind, mode = league
        url = f"{ESPN}/{path}/scoreboard"
        if mode == "day":
            # One local day: ESPN's day boards do not take a range for every league (a range came back empty).
            url += f"?dates={datetime.fromtimestamp(now, self._zone()):%Y%m%d}"
        return url

    async def _fetch(self, client: httpx.AsyncClient, league: tuple[str, str, str, str, str], now: float) -> None:
        name = league[0]
        try:
            resp = await client.get(self._url(league, now))
            resp.raise_for_status()
            games = parse(resp.json(), league)
        except Exception as exc:  # noqa: BLE001
            log.info("sport %s: %s", name, type(exc).__name__)
            # Keep what we had; try again within a minute rather than a quarter hour.
            had = self._boards.get(name)
            self._boards[name] = (now - IDLE_TTL_S + LIVE_TTL_S, had[1] if had else [])
            return
        self._boards[name] = (now, games)

    async def games(self, now: float | None = None) -> list[dict[str, Any]]:
        """The games on now. Once every league has a board, an old board is answered at once and refreshed behind
        it: waiting for ESPN made each minute's first /v1/tv/sport take 2.6 to 8 s (Nov 21 2026 review)."""
        now = time.time() if now is None else now
        if self._stale(now):
            if all(self._boards.get(league[0]) is not None for league in LEAGUES):
                running = self._refreshing
                if running is None or running.done():
                    self._refreshing = asyncio.create_task(self._refresh(now))
                    self._refreshing.add_done_callback(_quiet)
            else:
                await self._refresh(now)
        out = []
        for league in LEAGUES:
            # A race, fight or golf board lists its whole weekend: show it from the week before.
            after = WINDOW_AFTER_S if league[3] == TEAM else EVENT_AHEAD_S
            for game in (self._boards.get(league[0]) or (0, []))[1]:
                if game["state"] == "live" or (game["start"] and now - WINDOW_BEFORE_S <= game["start"] <= now + after):
                    out.append(game)
        return out


    def _stale(self, now: float) -> list[tuple[str, str, str, str, str]]:
        stale = []
        for league in LEAGUES:
            got = self._boards.get(league[0])
            live = got is not None and any(g["state"] == "live" for g in got[1])
            ttl = LIVE_TTL_S if live else IDLE_TTL_S
            if got is None or now - got[0] >= ttl:
                stale.append(league)
        return stale

    async def _refresh(self, now: float) -> None:
        async with self._lock:
            stale = self._stale(now)
            if stale:
                async with httpx.AsyncClient(timeout=8, transport=self._transport,
                                             headers={"User-Agent": "ThomasCreekHub"}) as client:
                    await asyncio.gather(*(self._fetch(client, league, now) for league in stale))

    # -- other days and league pages (#208) ----------------------------------------------------------------------
    def day_of(self, value: str | None, now: float) -> str | None:
        """`2026-11-18`, `20261118` or an offset from today (`-1`, `0`, `+2`) -> `20261118`; None when unreadable
        or further than DAY_RANGE days away (the hub keeps the house to a fortnight either side)."""
        if value is None:
            return None
        text = str(value).strip()
        today = datetime.fromtimestamp(now, self._zone()).date()
        try:
            if re.fullmatch(r"[+-]?\d{1,2}", text):
                day = today + timedelta(days=int(text))
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                day = datetime.strptime(text, "%Y-%m-%d").date()
            elif re.fullmatch(r"\d{8}", text):
                day = datetime.strptime(text, "%Y%m%d").date()
            else:
                return None
        except ValueError:
            return None
        if abs((day - today).days) > DAY_RANGE:
            return None
        return f"{day:%Y%m%d}"

    def _day_bounds(self, ymd: str) -> tuple[float, float]:
        start = datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=self._zone())
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def _ttl(self, ymd: str | None, games: list[dict[str, Any]], now: float) -> float:
        if any(g["state"] == "live" for g in games):
            return LIVE_TTL_S
        if ymd is None:
            return IDLE_TTL_S
        lo, hi = self._day_bounds(ymd)
        if hi <= now:
            return PAST_TTL_S  # a finished day only changes when ESPN corrects a score
        return IDLE_TTL_S

    async def _board(self, client: httpx.AsyncClient, league: tuple[str, str, str, str, str], ymd: str | None,
                     now: float) -> list[dict[str, Any]]:
        key = f"{league[0]}|{ymd or 'now'}"
        got = self._dated.get(key)
        if got is not None and now - got[0] < self._ttl(ymd, got[1], now):
            if key in self._failed:
                raise SportError(f"{league[0]} scores didn't load")
            return got[1]
        url = f"{ESPN}/{league[1]}/scoreboard" + (f"?dates={ymd}" if ymd else "")
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            games = parse(resp.json(), league)
        except Exception as exc:  # noqa: BLE001
            log.info("sport %s %s: %s", league[0], ymd or "now", type(exc).__name__)
            had = got[1] if got else []
            # Keep what we had; try again within a minute.
            self._dated[key] = (now - IDLE_TTL_S + LIVE_TTL_S, had)
            self._failed.add(key)
            raise SportError(f"{league[0]} scores didn't load") from None
        self._dated[key] = (now, games)
        self._failed.discard(key)
        if len(self._dated) > MAX_BOARDS:
            for old in sorted(self._dated, key=lambda k: self._dated[k][0])[: len(self._dated) - MAX_BOARDS]:
                self._dated.pop(old, None)
                self._failed.discard(old)
        return games

    async def day(self, ymd: str, now: float | None = None, league: str | None = None) -> dict[str, Any]:
        """Every game that starts on one local day (all leagues, or one), plus anything live when it is today.

        Returns {games, failed}: `failed` names the leagues whose board didn't load, so a screen can say so."""
        now = time.time() if now is None else now
        leagues = [lg for lg in LEAGUES if league is None or lg[0] == league]
        lo, hi = self._day_bounds(ymd)
        today = lo <= now < hi
        failed: list[str] = []

        async def one(client: httpx.AsyncClient, lg: tuple[str, str, str, str, str]) -> list[dict[str, Any]]:
            try:
                return await self._board(client, lg, ymd, now)
            except SportError:
                failed.append(lg[0])
                return []

        async with self._lock:
            async with httpx.AsyncClient(timeout=8, transport=self._transport,
                                         headers={"User-Agent": "ThomasCreekHub"}) as client:
                boards = await asyncio.gather(*(one(client, lg) for lg in leagues))
        out, seen = [], set()
        for games in boards:
            for game in games:
                if game["id"] in seen:
                    continue
                if (game["start"] and lo <= game["start"] < hi) or (today and game["state"] == "live"):
                    seen.add(game["id"])
                    out.append(game)
        return {"games": out, "failed": [name for name, *_r in LEAGUES if name in failed]}

    async def league_page(self, league: str, now: float | None = None) -> dict[str, Any]:
        """One league's current board as ESPN lists it (football's whole week, a race weekend, today otherwise)."""
        now = time.time() if now is None else now
        lg = next((row for row in LEAGUES if row[0] == league), None)
        if lg is None:
            raise SportError("The hub doesn't follow that league")
        ymd = None if lg[4] in ("week", "event") else f"{datetime.fromtimestamp(now, self._zone()):%Y%m%d}"
        async with self._lock:
            async with httpx.AsyncClient(timeout=8, transport=self._transport,
                                         headers={"User-Agent": "ThomasCreekHub"}) as client:
                games = await self._board(client, lg, ymd, now)
        return {"games": list(games), "failed": []}


class SportError(Exception):
    """A board that didn't load, or a league the hub doesn't follow. The text is safe to show."""


# ── matching ────────────────────────────────────────────────────────────────────────────────────────────────────


def _lineup_index(basic: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Lineup channels by network name ("NBC" -> the local NBC; "ESPN" -> ESPN)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for row in (basic or {}).get("channels") or []:
        name = str(row.get("name") or "")
        if row.get("local"):
            network = name.split(" ")[0]
        else:
            network = name
        index.setdefault(network.casefold(), []).append(row)
    return index


def _by_broadcast(game: dict[str, Any], index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    hits = []
    for name in game.get("broadcasts") or []:
        for network in BROADCASTERS.get(name.strip().casefold(), ()):
            for row in index.get(network.casefold(), []):
                hits.append({"key": row["key"], "why": "broadcast", "show": name})
    return hits


def _guide_hits(games: list[dict[str, Any]], doc: dict[str, Any], now: float) -> dict[str, list[dict[str, Any]]]:
    """Guide programmes that name a game, around its start, on any channel with a guide."""
    wanted = []
    for game in games:
        if not game["start"] or game["state"] == "final":
            continue
        if game["kind"] == TEAM and game["home"] and game["away"]:
            home = team_keys(game["home"]["name"], game["home"]["short"])
            away = team_keys(game["away"]["name"], game["away"]["short"])
            words = {w for key in home + away for w in key.split()}
            wanted.append((game, "team", home, away, words))
        elif game["kind"] == RACE and game["league"] in _RACE:
            wanted.append((game, "race", [], [], set()))
    out: dict[str, list[dict[str, Any]]] = {}
    if not wanted:
        return out
    lo = min(g["start"] for g, *_ in wanted) - 3 * 3600
    hi = max(g["start"] for g, *_ in wanted) + 3 * 3600
    for key, items in (doc.get("guide") or {}).items():
        for item in items or []:
            try:
                begins, finishes = float(item["start_ts"]), float(item["end_ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if finishes < lo or begins > hi or finishes < now - 3600:
                continue
            raw = " ".join(str(item.get(f) or "") for f in ("title", "subtitle"))
            text = norm(raw)
            if not text:
                continue
            tokens = set(text.split())
            for game, mode, home, away, words in wanted:
                live = begins <= now < finishes and game["state"] == "live"
                if not live and abs(begins - game["start"]) > 3 * 3600:
                    continue
                if mode == "team":
                    if not (tokens & words):
                        continue
                    if not (any(_has(text, k) for k in home) and any(_has(text, k) for k in away)):
                        continue
                elif not _RACE[game["league"]].search(raw):
                    continue
                out.setdefault(game["id"], []).append({"key": key, "why": "guide", "show": str(item.get("title") or "")})
    return out


def match(games: list[dict[str, Any]], catalog: Any, basic: dict[str, Any] | None, *, kids_only: bool,
          now: float) -> list[dict[str, Any]]:
    """Each game with `channels` (best first, each once, house lineup before the rest) and `watch` (the first)."""
    doc = catalog.load() if catalog is not None else {}
    overlay = catalog._overlay() if catalog is not None else {}
    shown: dict[str, dict[str, Any]] = {}
    for row in doc.get("channels") or []:
        view = catalog._present_channel(row, overlay)
        if view is not None and (not kids_only or view["kids"]):
            shown[view["key"]] = view
    lineup_keys = {str(row.get("key")) for row in (basic or {}).get("channels") or []}
    names = {str(row.get("key")): row for row in (basic or {}).get("channels") or []}
    index = _lineup_index(basic)
    guide = _guide_hits(games, doc, now)
    named = {key: norm(str(view.get("name") or "")) for key, view in shown.items()}
    by_word: dict[str, set[str]] = {}
    for key, text in named.items():
        for word in set(text.split()):
            by_word.setdefault(word, set()).add(key)
    out = []
    for game in games:
        hits = _by_broadcast(game, index) + sorted(
            guide.get(game["id"], []), key=lambda hit: 0 if hit["key"] in lineup_keys else 1)
        if game["kind"] == TEAM and game["home"] and game["away"] and game["state"] != "final":
            home = team_keys(game["home"]["name"], game["home"]["short"])
            away = team_keys(game["away"]["name"], game["away"]["short"])
            words = {w for key in home + away for w in key.split()}
            for key in {k for w in words for k in by_word.get(w, ())}:
                text = named[key]
                if any(_has(text, k) for k in home) and any(_has(text, k) for k in away):
                    hits.append({"key": key, "why": "name", "show": ""})
        channels, seen = [], set()
        for hit in hits:
            view = shown.get(hit["key"])
            if view is None or hit["key"] in seen:
                continue
            seen.add(hit["key"])
            lineup = names.get(hit["key"])
            channels.append({
                "key": hit["key"], "name": (lineup or {}).get("name") or view["name"],
                "number": (lineup or {}).get("number") or view.get("number"),
                "logo_url": catalog._art("logo", hit["key"]) if catalog is not None else None,
                "why": hit["why"], "show": hit["show"] or None,
            })
            if len(channels) >= MAX_CHANNELS:
                break
        item = dict(game)
        item["channels"] = channels
        item["watch"] = channels[0]["key"] if channels else None
        out.append(item)
    order = {"live": 0, "upcoming": 1, "final": 2}
    out.sort(key=lambda g: (order.get(g["state"], 1), 0 if g["watch"] else 1, POPULARITY.get(g["league"], 99),
                            g["start"] if g["state"] != "final" else -g["start"]))
    return out


def _quiet(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.info("sport: refresh failed (%s)", type(task.exception()).__name__)
