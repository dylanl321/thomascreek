"""Advertise the hub on home Wi-Fi as _thomascreek._tcp so the app finds it
with nothing to type. TXT: api=v1, id=<hub id>, pin=<sha256 of TLS public key>."""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from .state import Hub
from .tls import ensure_tls, spki_fingerprint

log = logging.getLogger("hub.bonjour")


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def advertise(h: Hub) -> AsyncZeroconf | None:
    try:
        cert, _ = ensure_tls(h.config)
        props = {"api": "v1", "id": h.config.hub.id, "pin": spki_fingerprint(cert)}
        info = AsyncServiceInfo(
            "_thomascreek._tcp.local.",
            f"{h.config.hub.name} Hub._thomascreek._tcp.local.",
            addresses=[socket.inet_aton(_lan_ip())],
            port=h.config.hub.listen_port,
            properties=props,
            server="thomascreek-hub.local.",
        )
        zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await zc.async_register_service(info)
        log.info("advertising _thomascreek._tcp on port %s", h.config.hub.listen_port)
        return zc
    except Exception as exc:  # noqa: BLE001
        log.warning("Bonjour advertising failed: %s", exc)
        return None
