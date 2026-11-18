"""The household's "basic cable" lineup: local stations for the home market plus the usual US networks.

An IPTV provider lists thousands of channels: every city's locals, East/West feeds, SD/HD/4K copies,
backups. A family wants what a cable box would show: about 100 channels on familiar numbers. This picks,
from the provider's own channels:

  - the local affiliates (ABC, NBC, CBS, FOX, PBS, CW, MyNetwork, ION, Telemundo, Univision) of the
    market nearest the house, numbered by the station's own channel number when the name carries it;
  - one copy of each standard network (East, HD, with a guide where there is a choice), numbered in a
    fixed cable-like order from 20 up.

Nothing is hidden on the provider side; the TV shows this as "Your TV" and keeps the rest under "More".
The pick is pure: `build(channels, home, has_guide)` takes the catalog rows and returns the lineup.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable

# 2: channels with no guide are left out (Dylan, Nov 15); 3: "NY" alone no longer means New York;
# 4: stations that name the market come first
VERSION = 4

# Local broadcast networks, in the order a cable guide lists them when a station has no number in its name.
LOCAL_NETWORKS: list[tuple[str, str]] = [
    ("CBS", r"cbs"), ("NBC", r"nbc"), ("ABC", r"abc"), ("FOX", r"fox"), ("PBS", r"pbs"),
    ("CW", r"cw"), ("MyNetwork", r"my\s?network|mytv|my\s?\d{1,2}"), ("ION", r"ion"),
    ("Telemundo", r"telemundo"), ("Univision", r"univision"),
]

# Standard networks (number, display name, pattern on the cleaned name). The order is a typical cable tier.
NETWORKS: list[tuple[int, str, str]] = [
    (20, "CNN", r"cnn"), (21, "HLN", r"hln|headline news"), (22, "Fox News", r"fox news(?! business)|fnc"),
    (23, "MSNBC", r"msnbc|ms now"), (24, "CNBC", r"cnbc"), (25, "Fox Business", r"fox business"),
    (26, "Bloomberg TV", r"bloomberg"), (27, "The Weather Channel", r"weather channel|twc"),
    (28, "C-SPAN", r"c-?span(?! ?2)"), (29, "NewsNation", r"newsnation|news nation"),
    (30, "ESPN", r"espn(?! ?2| ?u| ?news| deportes| ?\+|plus)"), (31, "ESPN2", r"espn ?2"),
    (32, "ESPNU", r"espn ?u"), (33, "ESPNews", r"espn ?news"), (34, "FS1", r"fs1|fox sports 1"),
    (35, "FS2", r"fs2|fox sports 2"), (36, "NFL Network", r"nfl network"), (37, "NBA TV", r"nba ?tv"),
    (38, "MLB Network", r"mlb network"), (39, "NHL Network", r"nhl network"), (40, "Golf Channel", r"golf"),
    (41, "Tennis Channel", r"tennis channel"), (42, "CBS Sports Network", r"cbs sports network|cbssn"),
    (43, "SEC Network", r"sec network"), (44, "ACC Network", r"acc network"), (45, "Big Ten Network", r"big ten|btn"),
    (50, "TNT", r"tnt(?! sports)"), (51, "TBS", r"tbs"), (52, "USA Network", r"usa network|usa"),
    (53, "FX", r"fx(?!x|m)"), (54, "FXX", r"fxx"), (55, "FXM", r"fxm"), (56, "AMC", r"amc(?!\+)"),
    (57, "A&E", r"a ?& ?e|a and e"), (58, "Bravo", r"bravo"), (59, "E!", r"e!|e entertainment"),
    (60, "Freeform", r"freeform"), (61, "Paramount Network", r"paramount network"), (62, "Comedy Central", r"comedy central"),
    (63, "Syfy", r"syfy"), (64, "TruTV", r"tru ?tv"), (65, "Lifetime", r"lifetime(?! movie)"),
    (66, "Lifetime Movies", r"lifetime movie|lmn"), (67, "Hallmark Channel", r"hallmark(?! movies| mystery| drama)"),
    (68, "Hallmark Movies & Mysteries", r"hallmark (movies|mystery)"), (69, "OWN", r"own|oprah"),
    (70, "BET", r"bet(?! her)"), (71, "VH1", r"vh1"), (72, "MTV", r"mtv(?! ?2|live)"), (73, "CMT", r"cmt"),
    (74, "TV Land", r"tv ?land"), (75, "WE tv", r"we ?tv"), (76, "Oxygen", r"oxygen"), (77, "Reelz", r"reelz"),
    (78, "IFC", r"ifc"), (79, "Sundance TV", r"sundance"), (80, "Ovation", r"ovation"), (81, "INSP", r"insp"),
    (82, "GSN", r"gsn|game show"), (83, "Pop TV", r"pop ?tv"),
    (90, "Discovery", r"discovery(?! (family|life|science|id|investigation|turbo))"),
    (91, "TLC", r"tlc"), (92, "Animal Planet", r"animal planet"), (93, "History", r"history"),
    (94, "Science", r"science channel|discovery science"), (95, "National Geographic", r"national geographic(?! wild)|nat ?geo(?! wild)"),
    (96, "Nat Geo Wild", r"nat(ional)? ?geo(graphic)? wild"), (97, "Smithsonian", r"smithsonian"),
    (98, "Investigation Discovery", r"investigation discovery|\bid\b"), (99, "HGTV", r"hgtv"),
    (100, "Food Network", r"food network"), (101, "Cooking Channel", r"cooking channel"), (102, "Travel Channel", r"travel channel"),
    (103, "Magnolia", r"magnolia"), (104, "Destination America", r"destination america"), (105, "Motor Trend", r"motor ?trend"),
    (110, "Disney Channel", r"disney channel|disney(?! ?jr| ?junior| ?xd| ?\+)"), (111, "Disney Junior", r"disney ?(jr|junior)"),
    (112, "Disney XD", r"disney ?xd"), (113, "Nickelodeon", r"nickelodeon|nick(?! ?jr| ?junior|toons|elodeon)"),
    (114, "Nick Jr.", r"nick ?(jr|junior)"), (115, "Nicktoons", r"nicktoons"), (116, "Cartoon Network", r"cartoon network"),
    (117, "Boomerang", r"boomerang"), (118, "Discovery Family", r"discovery family"), (119, "Universal Kids", r"universal kids"),
    (120, "PBS Kids", r"pbs kids"),
    (200, "HBO", r"hbo(?! ?2| family| comedy| signature| zone| latino)"), (201, "Cinemax", r"cinemax|max ?prime"),
    (202, "Showtime", r"showtime(?! ?2| extreme| beyond| next| women)"), (203, "Starz", r"starz(?! encore| edge| kids| cinema| comedy)"),
    (204, "MGM+", r"mgm ?\+|epix"), (205, "TCM", r"tcm|turner classic"),
]

# Words that make a copy a worse pick, and ones that make it a different channel.
_WORSE = re.compile(r"\b(sd|west|pacific|mountain|\+\s?1|backup|alt|raw|low|lq)\b", re.I)
_BETTER = re.compile(r"\b(hd|fhd|east)\b", re.I)
_OTHER = re.compile(r"\b(4k|uhd|ppv|event|replay|24/7|vip|test|espanol|español|latino|deportes|canada|uk|mexico)\b", re.I)
_US = re.compile(r"^\s*[\[(|]?\s*(us|usa|u\.s\.?|united states)\b", re.I)
_PREFIX = re.compile(r"^\s*[\[(|]?\s*[A-Za-z]{2,4}\s*[\])|:]\s*[-–|:]?\s*|^\s*[A-Za-z]{2,4}\s+[-–|:]\s*")
_CALL = re.compile(r"\b([KW][A-Z]{2,3})(?:-?(?:DT|TV|HD|LD|CD))?\b")
_NUMBER = re.compile(r"\b(\d{1,2})(?:\.\d)?\b")

# Television markets: the main city names a provider puts in local channel names, with a point near the centre.
MARKETS: list[tuple[str, float, float, str]] = [
    ("New York", 40.71, -74.01, r"new york|nyc|\bny\b"), ("Los Angeles", 34.05, -118.24, r"los angeles|\bla\b"),
    ("Chicago", 41.88, -87.63, r"chicago"), ("Philadelphia", 39.95, -75.17, r"philadelphia|philly"),
    ("Dallas", 32.78, -96.80, r"dallas|fort worth|dfw"), ("San Francisco", 37.77, -122.42, r"san francisco|bay area|oakland|san jose"),
    ("Atlanta", 33.75, -84.39, r"atlanta"), ("Houston", 29.76, -95.37, r"houston"), ("Washington", 38.91, -77.04, r"washington|\bdc\b"),
    ("Boston", 42.36, -71.06, r"boston"), ("Phoenix", 33.45, -112.07, r"phoenix"), ("Seattle", 47.61, -122.33, r"seattle|tacoma"),
    ("Tampa", 27.95, -82.46, r"tampa|st\.? ?petersburg"), ("Minneapolis", 44.98, -93.27, r"minneapolis|st\.? ?paul|twin cities"),
    ("Detroit", 42.33, -83.05, r"detroit"), ("Denver", 39.74, -104.99, r"denver"), ("Orlando", 28.54, -81.38, r"orlando"),
    ("Miami", 25.76, -80.19, r"miami|fort lauderdale"), ("Cleveland", 41.50, -81.69, r"cleveland|akron"),
    ("Sacramento", 38.58, -121.49, r"sacramento"), ("Charlotte", 35.23, -80.84, r"charlotte"), ("Portland", 45.52, -122.68, r"portland"),
    ("Raleigh", 35.78, -78.64, r"raleigh|durham"), ("St. Louis", 38.63, -90.20, r"st\.? ?louis"), ("Indianapolis", 39.77, -86.16, r"indianapolis"),
    ("Pittsburgh", 40.44, -80.00, r"pittsburgh"), ("Nashville", 36.16, -86.78, r"nashville"), ("Baltimore", 39.29, -76.61, r"baltimore"),
    ("Salt Lake City", 40.76, -111.89, r"salt lake"), ("San Diego", 32.72, -117.16, r"san diego"), ("San Antonio", 29.42, -98.49, r"san antonio"),
    ("Hartford", 41.76, -72.68, r"hartford|new haven"), ("Kansas City", 39.10, -94.58, r"kansas city"), ("Columbus", 39.96, -83.00, r"columbus"),
    ("Austin", 30.27, -97.74, r"austin"), ("Cincinnati", 39.10, -84.51, r"cincinnati"), ("Milwaukee", 43.04, -87.91, r"milwaukee"),
    ("Greenville", 34.85, -82.40, r"greenville|spartanburg|asheville"), ("West Palm Beach", 26.72, -80.05, r"west palm|palm beach"),
    ("Las Vegas", 36.17, -115.14, r"las vegas"), ("Jacksonville", 30.33, -81.66, r"jacksonville"), ("Grand Rapids", 42.96, -85.67, r"grand rapids"),
    ("Oklahoma City", 35.47, -97.52, r"oklahoma city|okc"), ("Birmingham", 33.52, -86.80, r"birmingham"), ("Harrisburg", 40.27, -76.88, r"harrisburg|lancaster|york"),
    ("Norfolk", 36.85, -76.29, r"norfolk|virginia beach|hampton roads"), ("Greensboro", 36.07, -79.79, r"greensboro|winston"),
    ("Albuquerque", 35.08, -106.65, r"albuquerque"), ("Louisville", 38.25, -85.76, r"louisville"), ("New Orleans", 29.95, -90.07, r"new orleans"),
    ("Memphis", 35.15, -90.05, r"memphis"), ("Buffalo", 42.89, -78.88, r"buffalo"), ("Providence", 41.82, -71.41, r"providence"),
    ("Richmond", 37.54, -77.44, r"richmond"), ("Fort Myers", 26.64, -81.87, r"fort myers|naples"), ("Wilkes-Barre", 41.25, -75.88, r"wilkes|scranton"),
    ("Knoxville", 35.96, -83.92, r"knoxville"), ("Albany", 42.65, -73.76, r"albany|schenectady"), ("Little Rock", 34.75, -92.29, r"little rock"),
    ("Tulsa", 36.15, -95.99, r"tulsa"), ("Fresno", 36.74, -119.79, r"fresno"), ("Dayton", 39.76, -84.19, r"dayton"),
    ("Spokane", 47.66, -117.43, r"spokane"), ("Honolulu", 21.31, -157.86, r"honolulu|hawaii"), ("Omaha", 41.26, -95.93, r"omaha"),
    ("Rochester", 43.16, -77.61, r"rochester"), ("Syracuse", 43.05, -76.15, r"syracuse"), ("Roanoke", 37.27, -79.94, r"roanoke|lynchburg"),
    ("Charleston SC", 32.78, -79.93, r"charleston"), ("Savannah", 32.08, -81.09, r"savannah"), ("Chattanooga", 35.05, -85.31, r"chattanooga"),
    ("Columbia SC", 34.00, -81.03, r"columbia"), ("Madison", 43.07, -89.40, r"madison"), ("Des Moines", 41.59, -93.62, r"des moines"),
    ("Tucson", 32.22, -110.97, r"tucson"), ("Boise", 43.62, -116.20, r"boise"), ("Anchorage", 61.22, -149.90, r"anchorage"),
    # Smaller markets whose channels carry a state abbreviation another market uses alone ("Binghamton NY",
    # "Shreveport LA"): listed so those stations are theirs, not New York's or Los Angeles's.
    ("Binghamton", 42.10, -75.91, r"binghamton"), ("Elmira", 42.09, -76.81, r"elmira|corning"),
    ("Utica", 43.10, -75.23, r"utica"), ("Watertown", 43.97, -75.91, r"watertown"),
    ("Burlington", 44.48, -73.21, r"burlington|plattsburgh"), ("Baton Rouge", 30.45, -91.15, r"baton rouge"),
    ("Shreveport", 32.53, -93.75, r"shreveport"), ("Lafayette", 30.22, -92.02, r"lafayette"),
    ("Lake Charles", 30.23, -93.22, r"lake charles"), ("Monroe", 32.51, -92.12, r"monroe"),
]


# A short alternative that other markets' channels also carry ("WTEN Albany NY", "KTLA LA"): it counts for its
# market only when no other market is named in the same channel.
WEAK: dict[str, str] = {"New York": r"\bny\b", "Los Angeles": r"\bla\b", "Washington": r"\bdc\b"}
_STRONG = [
    (name, re.compile(r"\b(" + "|".join(alt for alt in pattern.split("|") if alt != WEAK.get(name)) + r")\b", re.I))
    for name, _lat, _lon, pattern in MARKETS
]


def _named(text: str, name: str) -> bool:
    return any(rx.search(text) for market, rx in _STRONG if market == name)


def in_market(text: str, name: str, pattern: str) -> bool:
    """Whether a cleaned channel text belongs to market [name] (see WEAK)."""
    if not re.search(rf"\b({pattern})\b", text, re.I):
        return False
    weak = WEAK.get(name)
    if weak is None:
        return True
    strong = next(rx for market, rx in _STRONG if market == name)
    if strong.search(text):
        return True
    return not any(rx.search(text) for market, rx in _STRONG if market != name)


def clean(name: str) -> str:
    """The name without a country/provider prefix, lower case, single spaces."""
    text = _PREFIX.sub("", name or "", count=1)
    text = re.sub(r"[\[\]()|_]", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def is_us(row: dict[str, Any]) -> bool:
    return bool(_US.match(str(row.get("name") or "")) or _US.match(str(row.get("category") or row.get("group") or "")))


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _network(text: str) -> str | None:
    for name, pattern in LOCAL_NETWORKS:
        if re.search(rf"\b({pattern})\b", text):
            return name
    return None


def _quality(row: dict[str, Any], text: str, has_guide: Callable[[str], bool]) -> tuple[int, int, int, str]:
    worse = 1 if _WORSE.search(text) else 0
    better = 0 if _BETTER.search(text) else 1
    guide = 0 if has_guide(str(row.get("key"))) else 1
    return (guide, worse, better, str(row.get("key")))


def market_for(home: tuple[float, float] | None, channels: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The nearest market (within 250 km) that has at least two local affiliates among [channels]."""
    if home is None:
        return None
    texts = [clean(str(row.get("name") or "")) + " " + str(row.get("category") or "").casefold() for row in channels]
    texts = [t for t in texts if _network(t)]
    best = None
    for name, lat, lon, pattern in MARKETS:
        km = distance_km(home, (lat, lon))
        if km > 250:
            continue
        hits = sum(1 for t in texts if in_market(t, name, pattern))
        if hits >= 2 and (best is None or km < best["km"]):
            best = {"name": name, "km": round(km), "pattern": pattern}
    return best


def locals_per_market(texts: list[str] | list[tuple[str, str]]) -> dict[str, int]:
    """How many local stations each market's lineup would show: one per network (ABC, NBC, ...), as `build` picks.
    Each text is a channel's cleaned name with its category, or a (name, name and category) pair as `build`
    reads them."""
    pairs = [(t, t) if isinstance(t, str) else t for t in texts]
    found = [(where, network) for name, where in pairs if not _OTHER.search(name) and (network := _network(name))]
    out: dict[str, int] = {}
    for market, _lat, _lon, pattern in MARKETS:
        networks = {network for where, network in found if in_market(where, market, pattern)}
        if networks:
            out[market] = len(networks)
    return out


def build(channels: list[dict[str, Any]], home: tuple[float, float] | None,
          has_guide: Callable[[str], bool] = lambda key: False) -> dict[str, Any]:
    """The basic lineup from catalog rows (`key`, `name`, `category`)."""
    us = [row for row in channels if row.get("key") and is_us(row)]
    market = market_for(home, us)
    picked: list[dict[str, Any]] = []
    used: set[str] = set()
    taken_numbers: set[int] = set()

    cleaned = {row["key"]: clean(str(row.get("name") or "")) for row in us}
    if market:
        by_network: dict[str, list[tuple[tuple, dict[str, Any], str]]] = {}
        for row in us:
            text = cleaned[row["key"]]
            where = text + " " + str(row.get("category") or "").casefold()
            if _OTHER.search(text) or not _network(text) or not in_market(where, market["name"], market["pattern"]):
                continue
            network = _network(text)
            if network is None:
                continue
            # A station that names the market ("New York") beats one that only says "NY".
            named = 0 if _named(where, market["name"]) else 1
            by_network.setdefault(network, []).append(((named, *_quality(row, text, has_guide)), row, text))
        for order, (network, _) in enumerate(LOCAL_NETWORKS):
            options = sorted(by_network.get(network) or [], key=lambda option: option[0])
            if not options:
                continue
            _, row, text = options[0]
            number = None
            for match in _NUMBER.finditer(text):
                value = int(match.group(1))
                if 2 <= value <= 69 and value not in taken_numbers:
                    number = value
                    break
            call = _CALL.search(str(row.get("name") or ""))
            picked.append({"key": row["key"], "name": network + (f" {number}" if number else "") + (f" {call.group(1)}" if call else ""),
                           "number": number, "local": True, "_order": order})
            used.add(row["key"])
            if number:
                taken_numbers.add(number)
        slot = 2
        for entry in sorted((e for e in picked if e["number"] is None), key=lambda e: e["_order"]):
            while slot in taken_numbers:
                slot += 1
            entry["number"] = slot
            taken_numbers.add(slot)

    for number, name, pattern in NETWORKS:
        rx = re.compile(rf"^(?:{pattern})\b", re.I)
        options = []
        for row in us:
            if row["key"] in used:
                continue
            text = cleaned[row["key"]]
            if not rx.search(text) or _OTHER.search(text):
                continue
            options.append((_quality(row, text, has_guide), row))
        if not options:
            continue
        options.sort(key=lambda option: option[0])
        row = options[0][1]
        picked.append({"key": row["key"], "name": name, "number": number, "local": False})
        used.add(row["key"])

    for entry in picked:
        entry.pop("_order", None)
    # "Your TV" only lists channels that say what's on. If the guide is missing for nearly everything
    # (no guide loaded yet), keep the lineup rather than show an empty one.
    guided = [entry for entry in picked if has_guide(str(entry["key"]))]
    if len(guided) >= 5:
        picked = guided
    picked.sort(key=lambda entry: entry["number"])
    return {"version": VERSION, "market": market["name"] if market else None, "channels": picked}
