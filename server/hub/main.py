"""ASGI app factory and server entry point."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, data_dir, load_config, master_key_path
from .db import DB
from .routes import admin_web, alerts, appupdate, art, assistant, auth, cameras, climate, family, home, household, kitchen, layout, lights, me, media, media_requests, nas, network, people, printers, recipes, remotes, search, smart, titles, trips, tvapp, tvguide, weather, youtube
from .routes.deps import http_error_handler
from .state import Hub
from .routes import sends as sends_routes
from .vault import Vault, load_master_key

log = logging.getLogger("hub")


def build_app(hub: Hub | None = None, *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        h: Hub = app.state.hub
        zc = None
        if start_background:
            await h.start()
            if h.config.hub.bonjour:
                from .bonjour import advertise

                zc = await advertise(h)
        yield
        if zc is not None:
            await zc.async_close()
        await h.stop()

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
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]

    async def validation_handler(_req, exc: RequestValidationError):
        return JSONResponse({"error": "bad_request", "message": "Invalid request",
                             "fields": [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]}, status_code=422)

    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]

    @app.get("/v1/ping", tags=["meta"])
    async def ping() -> dict[str, Any]:
        """Unauthenticated: lets the app check it found the right hub."""
        return {"ok": True, "id": hub.config.hub.id, "name": hub.config.hub.name, "api": "v1", "version": __version__}

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
    limited = ("/v1/enroll", "/v1/auth/challenge", "/v1/auth/token", "/v1/hello", "/v1/ping", "/admin")
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
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    app.include_router(sends_routes.router)  # films and shows sent to the kids' iPads
    for r in (auth.router, alerts.router, home.router, people.router, media_requests.router, media.router, family.router, trips.router, network.router,
              cameras.router, kitchen.router, nas.router, printers.router, remotes.router, tvapp.router, tvapp.tv_router, tvguide.router, art.router, titles.router, layout.router, recipes.router, search.router, lights.router, appupdate.router, lights.pages, admin_web.api, admin_web.pages,
              assistant.router, youtube.router, smart.router, weather.router, climate.router, me.router, household.router, household.pages):
        app.include_router(r)
    return app


def default_hub(config: Config | None = None) -> Hub:
    cfg = config or load_config()
    vault = Vault(data_dir() / "vault.json", load_master_key(master_key_path()))
    db = DB(data_dir() / "hub.db")
    return Hub(cfg, vault, db)


def run() -> None:
    import uvicorn

    from .tls import ensure_tls

    logging.basicConfig(level=os.environ.get("HUB_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hub = default_hub()
    cert, key = ensure_tls(hub.config)
    app = build_app(hub)
    uvicorn.run(app, host=hub.config.hub.listen_host, port=hub.config.hub.listen_port,
                ssl_certfile=str(cert), ssl_keyfile=str(key), proxy_headers=False, server_header=False,
                log_level=os.environ.get("HUB_LOG_LEVEL", "info").lower())
