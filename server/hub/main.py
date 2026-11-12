"""ASGI app factory and server entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, data_dir, load_config, master_key_path
from .db import DB
from .routes import admin_web, alerts, appupdate, auth, cameras, family, home, lights, media, network, people, trips
from .routes.deps import http_error_handler
from .state import Hub
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

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    for r in (auth.router, alerts.router, home.router, people.router, media.router, family.router, trips.router, network.router,
              cameras.router, lights.router, appupdate.router, lights.pages, admin_web.api, admin_web.pages):
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
