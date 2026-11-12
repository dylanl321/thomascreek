"""Encrypted secrets vault.

Every credential the hub uses (Home Assistant token, Plex token, TrueNAS API key,
APNs signing key, WireGuard server key, ...) lives here and nowhere else.

On disk the vault is one JSON file whose values are each sealed with AES-256-GCM.
The 32-byte master key comes from a file (a Docker/TrueNAS secret mount) and is
never stored next to the vault. Secret names are not secret; values are.

Values are never logged and never returned over the API. The only ways to put a
value in are the `hub secrets set` CLI (reads stdin, no echo) and the hub itself
(for keys it generates, such as the WireGuard server key).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = 1


class VaultError(RuntimeError):
    pass


class Secret(str):
    """A str that hides itself from repr/logging by accident."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Secret(****)"


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def load_master_key(path: Path) -> bytes:
    try:
        raw = path.read_text().strip()
    except FileNotFoundError as exc:
        raise VaultError(f"master key file not found: {path}") from exc
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise VaultError("master key is not valid base64") from exc
    if len(key) != 32:
        raise VaultError("master key must decode to 32 bytes")
    return key


class Vault:
    def __init__(self, path: Path, master_key: bytes):
        self._path = path
        self._aead = AESGCM(master_key)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = {}
        if path.exists():
            doc = json.loads(path.read_text())
            if doc.get("version") != _VERSION:
                raise VaultError("unsupported vault version")
            self._data = doc.get("secrets", {})
            # Fail fast on a wrong master key rather than at first use.
            for name in list(self._data)[:1]:
                self.get(name)

    def _aad(self, name: str) -> bytes:
        # Binding the name stops a value being swapped onto another name.
        return f"thomascreek-vault:{name}".encode()

    def names(self) -> list[str]:
        return sorted(self._data)

    def has(self, name: str) -> bool:
        return name in self._data

    def get(self, name: str) -> Secret | None:
        entry = self._data.get(name)
        if entry is None:
            return None
        nonce = base64.b64decode(entry["n"])
        ct = base64.b64decode(entry["c"])
        try:
            pt = self._aead.decrypt(nonce, ct, self._aad(name))
        except Exception as exc:  # noqa: BLE001
            raise VaultError("cannot decrypt vault (wrong master key?)") from exc
        return Secret(pt.decode())

    def require(self, name: str) -> Secret:
        value = self.get(name)
        if value is None:
            raise VaultError(f"secret not set: {name}")
        return value

    def set(self, name: str, value: str) -> None:
        if not name or any(c.isspace() for c in name):
            raise VaultError("secret names must be non-empty with no spaces")
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, value.encode(), self._aad(name))
        with self._lock:
            self._data[name] = {
                "n": base64.b64encode(nonce).decode(),
                "c": base64.b64encode(ct).decode(),
            }
            self._save()

    def delete(self, name: str) -> bool:
        with self._lock:
            existed = self._data.pop(name, None) is not None
            if existed:
                self._save()
            return existed

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": _VERSION, "secrets": self._data}, fh, indent=1)
        os.replace(tmp, self._path)
