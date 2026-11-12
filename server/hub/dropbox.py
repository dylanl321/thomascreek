"""Secret drop folder: `<data>/import/<secret name>` files are moved into the vault.

Lets a trusted helper on the LAN (Dylan's Hermes agent, through the TrueNAS
file API, or Dylan from the Files app over the NAS's SMB share) hand the hub
credentials without anyone printing them. The hub reads
each file, stores it under the file's name, overwrites and deletes the file.
Only names are logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from .config import data_dir
from .onepassword import NAME_RE
from .vault import Vault

log = logging.getLogger("hub.dropbox")


# House assets that may also be dropped in (moved, not wiped): file name -> name under data/assets.
ASSET_FILES = {"house-model-web.json": "house-model-web.json", "header.jpg": "header.jpg"}


def drop_dir() -> Path:
    return data_dir() / "import"


def import_dropped(vault: Vault, folder: Path | None = None) -> list[str]:
    folder = folder or drop_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        # A write-only drop box: anyone with access to the share can put a file in,
        # nobody but the hub can list or read what is there.
        folder.chmod(0o1733)
    except OSError:
        pass
    stored: list[str] = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        name = f.name.removesuffix(".txt").removesuffix(".conf")
        m = re.fullmatch(r"AuthKey_([A-Z0-9]{10})\.p8", f.name)
        if m and vault.has("apns.key_id") and str(vault.get("apns.key_id")).strip() != m.group(1):
            # Push already has its key, so a different AuthKey is the App Store Connect one (new app builds).
            vault.set("appstore.key_id", m.group(1))
            stored.append("appstore.key_id")
            name = "appstore.auth_key_p8"
        elif m:
            # Apple's push key as downloaded: its file name carries the key id.
            vault.set("apns.key_id", m.group(1))
            stored.append("apns.key_id")
            name = "apns.auth_key_p8"
        if f.name in ASSET_FILES:
            # Not a secret: a house asset kept out of the public repo.
            dest = data_dir() / "assets" / ASSET_FILES[f.name]
            dest.parent.mkdir(parents=True, exist_ok=True)
            f.replace(dest)
            log.info("drop folder: stored asset %s", ASSET_FILES[f.name])
            stored.append(f"asset:{ASSET_FILES[f.name]}")
            continue
        try:
            if not NAME_RE.match(name) and not m:
                # Removed anyway: a misnamed file may still hold a credential.
                log.warning("drop folder: discarded a file that is not named like a secret")
                continue
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
