"""Secret drop folder: `<data>/import/<secret name>` files are moved into the vault.

Lets a trusted helper on the LAN (Dylan's Hermes agent, through the TrueNAS
file API) hand the hub credentials without anyone printing them. The hub reads
each file, stores it under the file's name, overwrites and deletes the file.
Only names are logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .config import data_dir
from .onepassword import NAME_RE
from .vault import Vault

log = logging.getLogger("hub.dropbox")


def drop_dir() -> Path:
    return data_dir() / "import"


def import_dropped(vault: Vault, folder: Path | None = None) -> list[str]:
    folder = folder or drop_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stored: list[str] = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        name = f.name.removesuffix(".txt")
        if not NAME_RE.match(name):
            log.warning("drop folder: ignoring %s (not a secret name)", f.name)
            continue
        try:
            raw = f.read_text()
            value = raw.strip()
            if "\n" in value:
                value += "\n"
            if value:
                vault.set(name, value)
                stored.append(name)
        finally:
            try:
                size = f.stat().st_size
                with open(f, "r+b") as fh:
                    fh.write(b"\0" * size)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass
            f.unlink(missing_ok=True)
    if stored:
        log.info("drop folder: stored %s", ", ".join(stored))
    return stored


async def watch(vault: Vault, interval: float = 15.0) -> None:
    while True:
        try:
            import_dropped(vault)
        except Exception as exc:  # noqa: BLE001
            log.warning("drop folder failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)
