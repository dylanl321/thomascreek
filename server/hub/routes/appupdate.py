"""Is there a newer build of the app? The app asks on launch and when it comes back to the front."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import testkeys
from ..crypto import CryptoError
from ..state import Hub
from .deps import _token_from, hub

router = APIRouter(prefix="/v1/app", tags=["meta"])


@router.get("/update")
async def app_update(request: Request, build: int | None = Query(default=None, ge=0, le=10_000_000),
                     version: str | None = Query(default=None, max_length=20),
                     channel: str | None = Query(default=None, max_length=20),
                     h: Hub = Depends(hub)) -> dict[str, Any]:
    """The newest build TestFlight has, and the oldest build still allowed.

    No sign-in needed (a phone that is still pending, or was just reset, must still hear
    about a required update). When a signed-in phone sends its own `build`, the hub
    remembers it so the web page can show who hasn't updated. A real phone on a TestFlight
    install that is newer than anything known also tells the hub that build is out, so the
    others hear about it even without the App Store Connect key. `channel` is how the app was
    installed (testflight, appstore, xcode, simulator); builds older than 25 don't send it.
    """
    if build is not None:
        token = _token_from(request, False)
        if token:
            try:
                claims = h.tokens.verify(token)
                h.db.update("devices", "id", claims["sub"],
                            {"app_version": f"{version} ({build})" if version else str(build), "last_seen_at": time.time()})
                dev = h.db.device(claims["sub"]) or {}
                if _real_install(dev, channel):
                    h.app_update.learn(build, version)
            except (CryptoError, KeyError):
                pass
    info = h.app_update.info()
    if build is not None:
        latest = info["latest_build"]
        info["update_available"] = bool(latest and latest > build)
        info["update_required"] = bool(info["min_build"] and build < info["min_build"])
    return info


class Release(BaseModel):
    build: int = Field(ge=1, le=10_000_000)
    version: str | None = Field(default=None, max_length=20)


@router.post("/release")
async def app_release(body: Release, x_test_key: str | None = Header(default=None),
                      h: Hub = Depends(hub)) -> dict[str, Any]:
    """The Mac's upload script: TestFlight has finished processing `build`, tell the phones.

    Signed with the Mac's test key (~/.thomascreek/test-key, made on the hub web page), which
    must belong to an admin. Only ever raises the latest build, never lowers it.
    """
    row = testkeys.check(h, x_test_key or "")
    person = h.db.person(row["person_id"]) if row else None
    if not person or person.get("role") != "admin":
        raise HTTPException(status_code=401, detail="Needs the Mac's test key")
    changed = h.app_update.learn(body.build, body.version, source="upload")
    h.db.log("app.release", "ok", person_id=person["id"], target=str(body.build),
             params={"version": body.version, "changed": changed}, source="mac")
    return {"changed": changed, **h.app_update.info()}


# uname() machine names a Simulator reports instead of "iPhone17,2".
_SIMULATOR_MODELS = {"arm64", "x86_64", "i386"}


def _real_install(dev: dict[str, Any], channel: str | None) -> bool:
    """A family phone running a TestFlight (or App Store) install, not a Simulator or an Xcode build."""
    if dev.get("status") != "active" or dev.get("kind") == "test" or dev.get("test_key_id"):
        return False
    if (dev.get("model") or "") in _SIMULATOR_MODELS:
        return False
    if channel is not None:
        return channel in ("testflight", "appstore")
    # Builds before 25 don't say how they were installed. Xcode builds register for sandbox push.
    return dev.get("apns_env") != "sandbox"
