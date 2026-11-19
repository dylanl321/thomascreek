"""Time the family's real calls against a hub, from wherever this runs (the Mac at home, a laptop abroad).

    python3 tools/timing.py https://<hub address>            # no sign-in: network and TLS, plus /v1/ping
    HUB_TOKEN=... python3 tools/timing.py https://<address>  # also the TV and phone routes, with that device's token
    python3 tools/timing.py https://<address> --server       # from home: the hub's own per-route p50/p95 too

For each call it prints the time a person waits (first byte), the hub's own share of it (the `total` part of
`Server-Timing`), and so the network's share. A cold call opens a new connection; warm calls reuse one, as the
apps do. Anything over 1 s is a bug (the Nov 19 performance rule). Only the address you pass is contacted; no
address, token or answer is written anywhere.

The TLS check is off because the hub's certificate is its own (the apps pin it); this tool reads only timings.
"""

from __future__ import annotations

import argparse
import os
import ssl
import statistics
import sys
import time

import httpx

ROUTES = [  # (label, path) of the calls a TV or phone makes when someone sits down
    ("ping", "/v1/ping"),
    ("explore", "/v1/tv/explore"),
    ("lineup", "/v1/tv/lineup"),
    ("groups", "/v1/tv/groups"),
    ("guide", "/v1/tv/guide"),
    ("films", "/v1/tv/movies?limit=40"),
    ("series", "/v1/tv/series?limit=40"),
    ("search", "/v1/tv/library/search?q=star"),
    ("upnext", "/v1/tv/upnext"),
    ("sport", "/v1/tv/sport"),
]


def server_ms(res: httpx.Response) -> float | None:
    for part in res.headers.get("server-timing", "").split(","):
        name, _, rest = part.strip().partition(";")
        if name == "total" and rest.startswith("dur="):
            try:
                return float(rest[4:])
            except ValueError:
                return None
    return None


def timed(client: httpx.Client, path: str, headers: dict[str, str]) -> tuple[float, float | None, int, int]:
    started = time.perf_counter()
    with client.stream("GET", path, headers=headers) as res:
        first = (time.perf_counter() - started) * 1000
        size = sum(len(chunk) for chunk in res.iter_raw())
        return first, server_ms(res), res.status_code, size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", help="the hub's address, https://…")
    ap.add_argument("-n", type=int, default=5, help="warm calls per route (default 5)")
    ap.add_argument("--server", action="store_true", help="also print the hub's own numbers (home network only)")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    token = os.environ.get("HUB_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"} if token else {"Accept-Encoding": "gzip"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    routes = ROUTES if token else ROUTES[:1]

    print(f"{'call':<9} {'cold ms':>8} {'warm p50':>9} {'warm p95':>9} {'hub p50':>8} {'net p50':>8} {'KB':>6}  status")
    worst = 0.0
    for label, path in routes:
        with httpx.Client(base_url=base, verify=ctx, timeout=30, http2=False) as cold:
            try:
                c_first, _, _, _ = timed(cold, path, headers)
            except httpx.HTTPError as exc:
                print(f"{label:<9} failed: {type(exc).__name__}")
                continue
        firsts, hubs, codes, size = [], [], set(), 0
        with httpx.Client(base_url=base, verify=ctx, timeout=30) as warm:
            timed(warm, "/v1/ping", {})  # open the connection, as the apps do at launch
            for _ in range(max(1, args.n)):
                first, hub_ms, code, size = timed(warm, path, headers)
                firsts.append(first)
                codes.add(code)
                if hub_ms is not None:
                    hubs.append(hub_ms)
        firsts.sort()
        p50 = statistics.median(firsts)
        p95 = firsts[min(len(firsts) - 1, round(0.95 * (len(firsts) - 1)))]
        h50 = statistics.median(hubs) if hubs else None
        worst = max(worst, c_first, p95)
        hub_s = f"{h50:8.0f}" if h50 is not None else f"{'-':>8}"
        net_s = f"{p50 - h50:8.0f}" if h50 is not None else f"{'-':>8}"
        flag = "  SLOW" if max(c_first, p95) > 1000 else ""
        print(f"{label:<9} {c_first:8.0f} {p50:9.0f} {p95:9.0f} {hub_s} {net_s} {size / 1024:6.1f}  "
              f"{','.join(str(c) for c in sorted(codes))}{flag}")

    if args.server:
        with httpx.Client(base_url=base, verify=ctx, timeout=30) as c:
            r = c.get("/v1/ping/timings", params={"window": 3600})
        if r.status_code != 200:
            print(f"\nhub numbers: {r.status_code} (they are read from the home network only)")
        else:
            body = r.json()
            hh = body.get("host_hour") or {}
            if hh:
                print(f"\nNAS last {hh['minutes']} min: CPU {hh['busy_avg_pct']}% busy (peak {hh['busy_max_pct']}%), "
                      f"{hh['iowait_avg_pct']}% disk wait; hub {hh['hub_cpu_avg_pct']}% of a thread "
                      f"(peak {hh['hub_cpu_max_pct']}%)")
            print(f"\n{'route':<52} {'n':>5} {'p50':>7} {'p95':>7} {'max':>8} {'>1s':>4}")
            for row in body.get("routes", [])[:25]:
                print(f"{row['method'] + ' ' + row['route']:<52} {row['n']:5d} {row['p50_ms']:7.0f} "
                      f"{row['p95_ms']:7.0f} {row['max_ms']:8.0f} {row['over_1s']:4d}")
    return 1 if worst > 1000 else 0


if __name__ == "__main__":
    sys.exit(main())
