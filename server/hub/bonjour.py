"""Advertise the hub on home Wi-Fi as _thomascreek._tcp so the app finds it
with nothing to type. TXT: api=v1, id=<hub id>, pin=<sha256 of TLS public key>.

On TrueNAS the host's own mDNS responder (Avahi) owns UDP 5353, so an mDNS
library inside the container never hears the phones' queries. When the host's
D-Bus socket is mounted into the container, the hub therefore asks the host's
Avahi to publish the service; otherwise it runs its own responder (zeroconf).
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Any

from .state import Hub
from .tls import ensure_tls, spki_fingerprint

log = logging.getLogger("hub.bonjour")

SERVICE_TYPE = "_thomascreek._tcp"
DBUS_SOCKET = Path(os.environ.get("HUB_DBUS_SOCKET", "/run/dbus/system_bus_socket"))


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _txt(h: Hub) -> dict[str, str]:
    cert, _ = ensure_tls(h.config)
    return {"api": "v1", "id": h.config.hub.id, "pin": spki_fingerprint(cert)}


class _Avahi:
    """Publish through the host's Avahi daemon over the system D-Bus."""

    def __init__(self, bus: Any, group: str):
        self.bus, self.group = bus, group

    @classmethod
    async def publish(cls, name: str, port: int, txt: dict[str, str]) -> "_Avahi":
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_address=f"unix:path={DBUS_SOCKET}", bus_type=BusType.SYSTEM).connect()

        async def call(path: str, iface: str, member: str, sig: str = "", body: list | None = None) -> Any:
            reply = await bus.call(Message(destination="org.freedesktop.Avahi", path=path, interface=iface,
                                           member=member, signature=sig, body=body or []))
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(f"avahi {member}: {reply.error_name} {reply.body}")
            return reply.body

        (group,) = await call("/", "org.freedesktop.Avahi.Server", "EntryGroupNew")
        records = [f"{k}={v}".encode() for k, v in txt.items()]
        # interface -1 = all, protocol 0 = IPv4, flags 0; empty domain/host = Avahi's defaults.
        await call(group, "org.freedesktop.Avahi.EntryGroup", "AddService", "iiussssqaay",
                   [-1, 0, 0, name, SERVICE_TYPE, "", "", port, records])
        await call(group, "org.freedesktop.Avahi.EntryGroup", "Commit")
        return cls(bus, group)

    async def async_close(self) -> None:
        from dbus_fast import Message

        try:
            await self.bus.call(Message(destination="org.freedesktop.Avahi", path=self.group,
                                        interface="org.freedesktop.Avahi.EntryGroup", member="Free"))
        finally:
            self.bus.disconnect()


async def advertise(h: Hub) -> Any | None:
    name = f"{h.config.hub.name} Hub"
    port = h.config.hub.listen_port
    try:
        props = _txt(h)
    except Exception as exc:  # noqa: BLE001
        log.warning("Bonjour advertising failed: %s", exc)
        return None
    if DBUS_SOCKET.exists():
        try:
            pub = await _Avahi.publish(name, port, props)
            log.info("advertising %s on port %s through the host's Avahi", SERVICE_TYPE, port)
            return pub
        except Exception as exc:  # noqa: BLE001
            log.warning("host Avahi not usable (%s); using the built-in responder", exc)
    try:
        from zeroconf import IPVersion
        from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

        info = AsyncServiceInfo(
            f"{SERVICE_TYPE}.local.",
            f"{name}.{SERVICE_TYPE}.local.",
            addresses=[socket.inet_aton(_lan_ip())],
            port=port,
            properties=props,
            server="thomascreek-hub.local.",
        )
        zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await zc.async_register_service(info)
        log.info("advertising %s on port %s", SERVICE_TYPE, port)
        return zc
    except Exception as exc:  # noqa: BLE001
        log.warning("Bonjour advertising failed: %s", exc)
        return None
