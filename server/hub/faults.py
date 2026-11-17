"""One way to note a failure that a request path survives, so the cause reaches the log without a secret.

A route that falls back (no weather location, a TV without its house, a provider that didn't answer) still says
what went wrong. Network trouble is routine and logged at INFO; anything else is a real fault and is logged as a
WARNING (it shows on the admin page) with the last frames of where it happened. Messages pass through
`describe`, which drops addresses: a provider's address carries its login (AGENTS.md rule 10).
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback

import httpx

_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HOST_PORT = re.compile(r"\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?::\d+)\b", re.I)
ROUTINE = (httpx.HTTPError, OSError, asyncio.TimeoutError, TimeoutError)


def describe(exc: BaseException, limit: int = 160) -> str:
    """`TypeName: message` with addresses removed, short enough for a log line or an admin note."""
    text = str(exc).replace("\n", " ").strip()
    text = _HOST_PORT.sub("[host]", _IP.sub("[address]", _URL.sub("[url]", text)))
    name = type(exc).__name__
    return f"{name}: {text[:limit]}" if text else name


def note(where: str, exc: BaseException, log: logging.Logger | None = None) -> None:
    """Log a failure the caller recovers from. Routine network trouble at INFO, anything else as a WARNING with
    the code location (frames only, no message text) so the cause can be found."""
    log = log or logging.getLogger("hub.routes")
    if isinstance(exc, ROUTINE):
        log.info("%s failed: %s", where, describe(exc))
        return
    frames = traceback.extract_tb(exc.__traceback__)[-3:]
    at = " < ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}" for f in reversed(frames))
    log.warning("%s failed: %s (at %s)", where, describe(exc), at or "?")
