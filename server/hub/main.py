"""ASGI app factory and server entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import __version__
from . import kidcerts  # noqa: F401 - imported at start so its quiet window counts from the hub's start
from .config import Config, data_dir, load_config, master_key_path
from .db import DB
from .routes import mcp as mcp_routes
from .routes import admin_web, alerts, ambient, people_web, appupdate, art, assistant, auth, brain, cameras, catalog, climate, downloads, dvr, family, feasts, fleet_web, home, home_alerts, household, iptvplay, kitchen, layout, lights, me, media, media_requests, nas, network, people, printers, recipes, remotes, search, smart, titles, trips, tvapp, tvfleet, tvguide, tvcameras, tvpair, tvphone, tvs, weather, youtube
from .routes.deps import http_error_handler
from .state import Hub
from .timings import HOST_EVERY_S, HostLoad, TimingMiddleware, Timings
from .routes import sends as sends_routes
from .routes import phonelogs, phoneproviders
from .vault import Vault, load_master_key

SWITCH_INTERVAL_S = 0.001  # see `run`

log = logging.getLogger("hub")


def build_app(hub: Hub | None = None, *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        h: Hub = app.state.hub
        zc = None
        dog = None
        host_task = None
        certs_task = None
        thumbs_task = None
        if start_background:
            # A hub whose loop is stuck restarts itself and leaves a note on where (see watchdog.py).
            from .watchdog import Watchdog

            dog = Watchdog(h.tv_catalog.path.parents[1])
            dog.start()
            app.state.watchdog = dog
            await h.start()

            async def host_loop() -> None:
                while True:
                    await asyncio.to_thread(app.state.host_load.sample)
                    await asyncio.sleep(HOST_EVERY_S)

            host_task = asyncio.create_task(host_loop())
            certs_task = asyncio.create_task(kidcerts.warm_later(h))  # a kids' page never waits for the scan
            if getattr(h, "tv_app", None) is not None:
                from . import playerthumbs

                thumbs_task = asyncio.create_task(playerthumbs.loop(h, h.tv_app.dir / "thumbs"))
            if h.config.hub.bonjour:
                from .bonjour import advertise

                zc = await advertise(h)
        yield
        if host_task is not None:
            host_task.cancel()
        if certs_task is not None:
            certs_task.cancel()
        if thumbs_task is not None:
            thumbs_task.cancel()
        if zc is not None:
            await zc.async_close()
        await h.stop()
        art = getattr(h, "artwork", None)
        if art is not None:
            art.flush()  # notes and matches kept since the last timed write
        if dog is not None:
            await dog.stop()

    app = FastAPI(
        title="Thomas Creek Hub",
        version=__version__,
        description="Home-side service for the Thomas Creek app. Holds every credential, enforces roles.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    if hub is None:
        hub = default_hub()
    app.state.hub = hub
    app.state.timings = Timings()
    app.state.host_load = HostLoad()
    hub.timings, hub.host_load = app.state.timings, app.state.host_load  # for the admin page
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]

    async def validation_handler(_req, exc: RequestValidationError):
        return JSONResponse({"error": "bad_request", "message": "Invalid request",
                             "fields": [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]}, status_code=422)

    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]

    @app.get("/v1/ping", tags=["meta"])
    async def ping() -> dict[str, Any]:
        """Unauthenticated: lets the app check it found the right hub."""
        return {"ok": True, "id": hub.config.hub.id, "name": hub.config.hub.name, "api": "v1", "version": __version__}

    @app.get("/v1/ping/stalls", tags=["meta"])
    async def ping_stalls(request: Request) -> dict[str, Any]:
        """Home network only, no sign-in: where the hub's loop was when it stopped answering for a few seconds
        (code locations only), so a hub that hangs can be diagnosed without a log or a key."""
        from .routes.deps import caller_ip, error

        if not hub.is_home(caller_ip(request, hub)):
            raise error(403, "not_home", "Only on the home network")
        dog = getattr(app.state, "watchdog", None)
        rows = list(dog.samples) if dog is not None else []
        return {"running": dog is not None, "stalls": rows[::-1]}

    @app.get("/v1/ping/timings", tags=["meta"])
    async def ping_timings(request: Request, window: int | None = None) -> dict[str, Any]:
        """Home network only, no sign-in: p50, p95 and the slowest answer per route (route templates only, never an
        address or an id), and how busy the NAS and the hub are. `window` = only the last that many seconds. `slow`:
        the last answers over a second with their own steps (Server-Timing names and milliseconds)."""
        from .routes.deps import caller_ip, error

        if not hub.is_home(caller_ip(request, hub)):
            raise error(403, "not_home", "Only on the home network")
        t: Timings = app.state.timings
        host: HostLoad = app.state.host_load
        return {"since": round(t.since), "version": __version__, "build": os.environ.get("HUB_BUILD"),
                "host": host.last(), "host_hour": host.hour(),
                "routes": t.summary(window_s=float(window) if window else None), "slow": t.slow()}

    @app.get("/v1/hello", tags=["meta"])
    async def hello() -> dict[str, Any]:
        """Unauthenticated: who this hub is, plus the SHA-256 of its TLS public key
        (the same `pin` as the Bonjour TXT record) so a phone that typed the address
        can check it reached the right hub before trusting the certificate."""
        from .tls import ensure_tls, spki_fingerprint

        if not hasattr(app.state, "pin"):
            cert, _ = ensure_tls(hub.config)
            app.state.pin = spki_fingerprint(cert)
        return {"name": hub.config.hub.name, "id": hub.config.hub.id, "api": "v1",
                "version": __version__, "pin": app.state.pin}

    # Unauthenticated sign-in steps, rate-limited for callers away from home (the hub is
    # published through a reverse proxy). Home callers are never limited.
    limited = ("/v1/enroll", "/v1/auth/challenge", "/v1/auth/token", "/v1/hello", "/v1/ping", "/admin", "/oauth/",
               "/phone")
    hits: dict[str, list[float]] = {}

    @app.middleware("http")
    async def away_rate_limit(request, call_next):
        per_min = hub.config.hub.away_auth_per_minute
        path = request.url.path
        if per_min > 0 and path.startswith(limited):
            peer = request.client.host if request.client else None
            ip = hub.client_ip(peer, request.headers.get("x-forwarded-for")) or "unknown"
            if not hub.is_home(ip if ip != "unknown" else None):
                now = time.monotonic()
                recent = [t for t in hits.get(ip, []) if now - t < 60]
                if len(recent) >= per_min:
                    hits[ip] = recent
                    return JSONResponse({"error": "rate_limited", "message": "Too many tries. Wait a minute."},
                                        status_code=429, headers={"Retry-After": "60"})
                recent.append(now)
                hits[ip] = recent
                if len(hits) > 5000:  # forget idle callers
                    for k in [k for k, v in hits.items() if not v or now - v[-1] > 60]:
                        hits.pop(k, None)
        return await call_next(request)

    @app.middleware("http")
    async def admin_session_renew(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/admin"):
            admin_web.renew_session(request, resp, hub)
        return resp

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # Playback passes (the pass in the address is the whole permission, no cookie or bearer) may be opened by a
    # cast receiver: a Chromecast or a Cast-enabled hotel TV is a web page on another origin, so it needs CORS to
    # read the HLS lists and ranges. Nothing else on the hub answers another origin.
    cast_paths = ("/v1/tv/phone/file/", "/v1/tv/phone/live/", "/v1/tv/phone/cast/", "/v1/tv/plex/file/",
                  "/v1/media/hls/")

    @app.middleware("http")
    async def cast_cors(request, call_next):
        if not request.url.path.startswith(cast_paths):
            return await call_next(request)
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type",
                "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Type",
                "Access-Control-Max-Age": "3600"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        resp = await call_next(request)
        for k, v in cors.items():
            resp.headers[k] = v
        return resp

    app.include_router(mcp_routes.router)  # MCP for outside AI apps, and its OAuth sign-in
    app.include_router(sends_routes.router)  # films and shows sent to the kids' iPads
    for r in (auth.router, alerts.router, home.router, people.router, media_requests.router, media.router, family.router, feasts.router, trips.router, network.router,
              cameras.router, kitchen.router, nas.router, printers.router, remotes.router, tvapp.router, tvapp.tv_router, tvguide.router, tvphone.router, phonelogs.router, phoneproviders.router, catalog.router, iptvplay.router, tvfleet.router, tvpair.router, tvs.router, tvs.tv_router, home_alerts.router, tvcameras.router, ambient.router, art.router, dvr.router, titles.router, layout.router, recipes.router, search.router, lights.router, appupdate.router, lights.pages, admin_web.api, admin_web.pages, people_web.pages, fleet_web.pages, downloads.router,
              assistant.router, brain.router, youtube.router, smart.router, weather.router, climate.router, me.router, household.router, household.pages):
        app.include_router(r)
    # Outermost, so its time covers every other middleware too.
    app.add_middleware(TimingMiddleware, timings=app.state.timings)
    return app


def default_hub(config: Config | None = None) -> Hub:
    cfg = config or load_config()
    vault = Vault(data_dir() / "vault.json", load_master_key(master_key_path()))
    db = DB(data_dir() / "hub.db")
    return Hub(cfg, vault, db)


def _preload() -> None:
    """Import the big SDKs before the hub listens. Imported lazily on first use, `anthropic` took 40 s on the live
    hub (Nov 19 2026, busy CPU), holding the loop and every request with it. Missing ones are fine (tests, dev)."""
    import importlib

    for name in ("anthropic", "websockets", "aiomqtt", "zeroconf.asyncio"):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - the feature that needs it says so when used
            pass


def run() -> None:
    import uvicorn

    from .tls import ensure_tls

    logging.basicConfig(level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request's full address at INFO, query string included: an IPTV password or an API key
    # passed as ?key= would land in the container log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Provider list reads run in a child process, so they never hold up a request (catalogjob.py).
    os.environ.setdefault("HUB_CATALOG_CHILD", "1")
    # The event loop gives up the GIL on every SQLite call and waits for it back while a worker thread parses the
    # catalog; at Python's 5 ms switch interval a loop tick with a few hundred small reads took 10 to 20 s after a
    # start (Nov 19 2026, /v1/ping/stalls). A shorter interval hands it back sooner, at a small cost to the parse.
    sys.setswitchinterval(SWITCH_INTERVAL_S)
    _preload()
    hub = default_hub()
    cert, key = ensure_tls(hub.config)
    app = build_app(hub)
    uvicorn.run(app, host=hub.config.hub.listen_host, port=hub.config.hub.listen_port,
                ssl_certfile=str(cert), ssl_keyfile=str(key), proxy_headers=False, server_header=False,
                log_level=os.environ.get("HUB_LOG_LEVEL", "info").lower())
