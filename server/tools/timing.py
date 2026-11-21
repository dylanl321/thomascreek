"""Time the family's real calls against a hub, from wherever this runs (the Mac at home, a laptop abroad).

    python3 tools/timing.py https://<hub address>            # no sign-in: network and TLS, plus /v1/ping
    python3 tools/timing.py https://<address> --enroll       # once, at home: this machine joins as a device
    python3 tools/timing.py https://<address>                # then: signs in as that device, times every route
    HUB_TOKEN=... python3 tools/timing.py https://<address>  # or with a token you already have
    python3 tools/timing.py https://<address> --server       # from home: the hub's own per-route p50/p95 too

Signed-in timing (`--enroll`): the script makes its own P-256 device key, enrolls like a phone does (on the home
network), and waits as "pending" until an admin picks whose it is in the app (People). Pick a test person, adult
or child, to time what that role sees. A hub test key (`--test-key FILE`, made on the hub web page) makes it
active at once for the key's person instead. After that every run signs a challenge with the key, exactly as the
iPhone does, and times the routes through whatever address you pass, the public one included. The key lives in
`--device` (default ~/.thomascreek/timing-device.json, readable by you only); nothing else is written.

For each call it prints the time a person waits (first byte), the hub's own share of it (the `total` part of
`Server-Timing`), and so the network's share. A cold call opens a new connection; warm calls reuse one, as the
apps do. Anything over 1 s is a bug (the Nov 19 performance rule) and makes the run exit 1. Only the address you
pass is contacted; no address, token or answer is printed or written anywhere.

The TLS check is off because the hub's certificate is its own (the apps pin it); this tool reads only timings.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import statistics
import sys
import time

import httpx

DEVICE_FILE = os.path.expanduser("~/.thomascreek/timing-device.json")

ROUTES = [  # (label, path) of the calls a TV or phone makes when someone sits down
    ("ping", "/v1/ping"),
    ("me", "/v1/me"),
    ("home", "/v1/home"),
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


def _client(base: str, ctx: ssl.SSLContext) -> httpx.Client:
    return httpx.Client(base_url=base, verify=ctx, timeout=30)


def enroll(base: str, ctx: ssl.SSLContext, path: str, test_key: str | None) -> int:
    """Make a device key and join the hub with it, as a phone does on first open at home."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if os.path.exists(path):
        print(f"{path} already holds a device; remove it to enroll again")
        return 1
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    body: dict[str, object] = {"device_name": "Timing test", "model": "timing script", "platform": "script",
                               "public_key": base64.b64encode(public).decode()}
    if test_key:
        body.update(simulator=True, test_key=test_key)
    with _client(base, ctx) as c:
        r = c.post("/v1/enroll", json=body)
    if r.status_code != 200:
        print(f"enroll: {r.status_code} {r.json().get('message') if r.headers.get('content-type', '').startswith('application/json') else ''}")
        return 1
    answer = r.json()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"device_id": answer["device_id"], "key": pem}, f)
    if answer.get("status") == "active":
        print("enrolled and active")
    else:
        print("enrolled; waiting for an admin to choose whose it is in the app (People, \"Timing test\")")
    return 0


def sign_in(base: str, ctx: ssl.SSLContext, path: str) -> tuple[str | None, str]:
    """A fresh access token for the saved device: challenge, sign the nonce, token. (token, who or why not)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    with open(path) as f:
        saved = json.load(f)
    key = serialization.load_pem_private_key(saved["key"].encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    with _client(base, ctx) as c:
        r = c.post("/v1/auth/challenge", json={"device_id": saved["device_id"]})
        if r.status_code != 200:
            return None, f"challenge answered {r.status_code}"
        nonce = r.json()["nonce"]
        signature = base64.b64encode(key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))).decode()
        r = c.post("/v1/auth/token", json={"device_id": saved["device_id"], "nonce": nonce, "signature": signature})
    if r.status_code != 200:
        return None, f"token answered {r.status_code}"
    body = r.json()
    if body.get("role") == "pending":
        return None, "the device is still waiting for an admin to choose whose it is"
    person = body.get("person") or {}
    return body["access_token"], f"signed in, role {body.get('role')}, {person.get('ui_style') or 'standard'} style"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", help="the hub's address, https://…")
    ap.add_argument("-n", type=int, default=5, help="warm calls per route (default 5)")
    ap.add_argument("--server", action="store_true", help="also print the hub's own numbers (home network only)")
    ap.add_argument("--device", default=DEVICE_FILE, help=f"this machine's device key (default {DEVICE_FILE})")
    ap.add_argument("--enroll", action="store_true", help="join the hub as a device (at home), then exit")
    ap.add_argument("--test-key", metavar="FILE", help="with --enroll: a hub test key file, active at once")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if args.enroll:
        test_key = open(os.path.expanduser(args.test_key)).read().strip() if args.test_key else None
        return enroll(base, ctx, args.device, test_key)
    token = os.environ.get("HUB_TOKEN", "").strip()
    if not token and os.path.exists(args.device):
        started = time.perf_counter()
        got, who = sign_in(base, ctx, args.device)
        print(f"{who} ({(time.perf_counter() - started) * 1000:.0f} ms)")
        if got is None:
            return 1
        token = got
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"} if token else {"Accept-Encoding": "gzip"}
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
