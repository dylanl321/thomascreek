"""The hub's last few warnings and errors, for the admin page. In memory only; a restart starts empty."""

from __future__ import annotations

import collections
import logging
import time
from typing import Any

MAX = 60


class RecentLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.rows: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        self.rows.append({"ts": record.created or time.time(), "level": record.levelname, "name": record.name,
                          "msg": msg[:300]})

    def latest(self, n: int = 25) -> list[dict[str, Any]]:
        return list(reversed(list(self.rows)[-n:]))


RECENT = RecentLog()


def install() -> None:
    root = logging.getLogger()
    if RECENT not in root.handlers:
        root.addHandler(RECENT)


def redact(text: str, values: list[str]) -> str:
    """Hide any saved key that slipped into a log line."""
    for v in values:
        if v and len(v) >= 8 and v in text:
            text = text.replace(v, "•••")
    return text
