"""Private VPN through Private Internet Access (PIA), WireGuard.

The PIA login lives only in the vault (copied from 1Password as `vpn.pia.username`
and `vpn.pia.password`). Phones never see it. A phone makes its own WireGuard key
and sends the public half; the hub signs in to PIA (a token good for 24 hours),
registers that key on a server in the chosen region with `addKey`, and hands the
phone back the server's settings. PIA forgets keys after a while, so the app asks
for fresh settings each time someone taps Connect.

API as used by PIA's own scripts (github.com/pia-foss/manual-connections):
  POST https://www.privateinternetaccess.com/api/client/v2/token  (form username, password) -> {"token"}
  GET  https://serverlist.piaservers.net/vpninfo/servers/v6  (JSON on the first line)
  GET  https://<server cn>:1337/addKey?pt=<token>&pubkey=<key>   (TLS signed by PIA's own CA, dialled by IP)
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import random
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import PiaConfig
from .vault import Vault

log = logging.getLogger("hub.vpn.pia")

# PIA's root for the addKey API on port 1337 (ca.rsa.4096.crt from pia-foss/manual-connections, valid to 2034).
PIA_CA = """-----BEGIN CERTIFICATE-----
MIIHqzCCBZOgAwIBAgIJAJ0u+vODZJntMA0GCSqGSIb3DQEBDQUAMIHoMQswCQYD
VQQGEwJVUzELMAkGA1UECBMCQ0ExEzARBgNVBAcTCkxvc0FuZ2VsZXMxIDAeBgNV
BAoTF1ByaXZhdGUgSW50ZXJuZXQgQWNjZXNzMSAwHgYDVQQLExdQcml2YXRlIElu
dGVybmV0IEFjY2VzczEgMB4GA1UEAxMXUHJpdmF0ZSBJbnRlcm5ldCBBY2Nlc3Mx
IDAeBgNVBCkTF1ByaXZhdGUgSW50ZXJuZXQgQWNjZXNzMS8wLQYJKoZIhvcNAQkB
FiBzZWN1cmVAcHJpdmF0ZWludGVybmV0YWNjZXNzLmNvbTAeFw0xNDA0MTcxNzQw
MzNaFw0zNDA0MTIxNzQwMzNaMIHoMQswCQYDVQQGEwJVUzELMAkGA1UECBMCQ0Ex
EzARBgNVBAcTCkxvc0FuZ2VsZXMxIDAeBgNVBAoTF1ByaXZhdGUgSW50ZXJuZXQg
QWNjZXNzMSAwHgYDVQQLExdQcml2YXRlIEludGVybmV0IEFjY2VzczEgMB4GA1UE
AxMXUHJpdmF0ZSBJbnRlcm5ldCBBY2Nlc3MxIDAeBgNVBCkTF1ByaXZhdGUgSW50
ZXJuZXQgQWNjZXNzMS8wLQYJKoZIhvcNAQkBFiBzZWN1cmVAcHJpdmF0ZWludGVy
bmV0YWNjZXNzLmNvbTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBALVk
hjumaqBbL8aSgj6xbX1QPTfTd1qHsAZd2B97m8Vw31c/2yQgZNf5qZY0+jOIHULN
De4R9TIvyBEbvnAg/OkPw8n/+ScgYOeH876VUXzjLDBnDb8DLr/+w9oVsuDeFJ9K
V2UFM1OYX0SnkHnrYAN2QLF98ESK4NCSU01h5zkcgmQ+qKSfA9Ny0/UpsKPBFqsQ
25NvjDWFhCpeqCHKUJ4Be27CDbSl7lAkBuHMPHJs8f8xPgAbHRXZOxVCpayZ2SND
fCwsnGWpWFoMGvdMbygngCn6jA/W1VSFOlRlfLuuGe7QFfDwA0jaLCxuWt/BgZyl
p7tAzYKR8lnWmtUCPm4+BtjyVDYtDCiGBD9Z4P13RFWvJHw5aapx/5W/CuvVyI7p
Kwvc2IT+KPxCUhH1XI8ca5RN3C9NoPJJf6qpg4g0rJH3aaWkoMRrYvQ+5PXXYUzj
tRHImghRGd/ydERYoAZXuGSbPkm9Y/p2X8unLcW+F0xpJD98+ZI+tzSsI99Zs5wi
jSUGYr9/j18KHFTMQ8n+1jauc5bCCegN27dPeKXNSZ5riXFL2XX6BkY68y58UaNz
meGMiUL9BOV1iV+PMb7B7PYs7oFLjAhh0EdyvfHkrh/ZV9BEhtFa7yXp8XR0J6vz
1YV9R6DYJmLjOEbhU8N0gc3tZm4Qz39lIIG6w3FDAgMBAAGjggFUMIIBUDAdBgNV
HQ4EFgQUrsRtyWJftjpdRM0+925Y6Cl08SUwggEfBgNVHSMEggEWMIIBEoAUrsRt
yWJftjpdRM0+925Y6Cl08SWhge6kgeswgegxCzAJBgNVBAYTAlVTMQswCQYDVQQI
EwJDQTETMBEGA1UEBxMKTG9zQW5nZWxlczEgMB4GA1UEChMXUHJpdmF0ZSBJbnRl
cm5ldCBBY2Nlc3MxIDAeBgNVBAsTF1ByaXZhdGUgSW50ZXJuZXQgQWNjZXNzMSAw
HgYDVQQDExdQcml2YXRlIEludGVybmV0IEFjY2VzczEgMB4GA1UEKRMXUHJpdmF0
ZSBJbnRlcm5ldCBBY2Nlc3MxLzAtBgkqhkiG9w0BCQEWIHNlY3VyZUBwcml2YXRl
aW50ZXJuZXRhY2Nlc3MuY29tggkAnS7684Nkme0wDAYDVR0TBAUwAwEB/zANBgkq
hkiG9w0BAQ0FAAOCAgEAJsfhsPk3r8kLXLxY+v+vHzbr4ufNtqnL9/1Uuf8NrsCt
pXAoyZ0YqfbkWx3NHTZ7OE9ZRhdMP/RqHQE1p4N4Sa1nZKhTKasV6KhHDqSCt/dv
Em89xWm2MVA7nyzQxVlHa9AkcBaemcXEiyT19XdpiXOP4Vhs+J1R5m8zQOxZlV1G
tF9vsXmJqWZpOVPmZ8f35BCsYPvv4yMewnrtAC8PFEK/bOPeYcKN50bol22QYaZu
LfpkHfNiFTnfMh8sl/ablPyNY7DUNiP5DRcMdIwmfGQxR5WEQoHL3yPJ42LkB5zs
6jIm26DGNXfwura/mi105+ENH1CaROtRYwkiHb08U6qLXXJz80mWJkT90nr8Asj3
5xN2cUppg74nG3YVav/38P48T56hG1NHbYF5uOCske19F6wi9maUoto/3vEr0rnX
JUp2KODmKdvBI7co245lHBABWikk8VfejQSlCtDBXn644ZMtAdoxKNfR2WTFVEwJ
iyd1Fzx0yujuiXDROLhISLQDRjVVAvawrAtLZWYK31bY7KlezPlQnl/D9Asxe85l
8jO5+0LdJ6VyOs/Hd4w52alDW/MFySDZSfQHMTIc30hLBJ8OnCEIvluVQQ2UQvoW
+no177N9L2Y+M9TcTA62ZyMXShHQGeh20rb4kK8f+iFX8NxtdHVSkxMEFSfDDyQ=
-----END CERTIFICATE-----
"""

TOKEN_URL = "https://www.privateinternetaccess.com/api/client/v2/token"
TOKEN_LIFE_S = 24 * 3600
TOKEN_RENEW_S = 20 * 3600


class PiaError(RuntimeError):
    pass


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Dial a PIA server by IP while checking its certificate against its name (curl --connect-to)."""

    def __init__(self, ip: str, port: int, cn: str, context: ssl.SSLContext, timeout: float):
        super().__init__(ip, port, context=context, timeout=timeout)
        self._cn = cn

    def connect(self) -> None:  # noqa: D401
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._cn)


def _ca_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=PIA_CA)
    ctx.check_hostname = True
    # PIA's 2014 root doesn't mark Basic Constraints critical, which Python 3.13+ rejects in strict mode.
    # Checked live against montreal430:1337 on 2026-11-13: verifies once strict mode is off.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


class Pia:
    def __init__(self, cfg: PiaConfig, vault: Vault, http: httpx.AsyncClient | None = None, add_key=None):
        self.cfg = cfg
        self.vault = vault
        self._http = http
        self._token: tuple[str, float, int] | None = None
        self._regions: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()
        self._add_key = add_key or self._add_key_blocking
        self.last_error: str | None = None
        self.last_ok: float | None = None

    # -- state ---------------------------------------------------------------
    def _login(self) -> tuple[str, str, str] | None:
        """(username, password, where): the admin page's pair first, else the one from 1Password."""
        for u, p, where in ((self.cfg.admin_username_secret, self.cfg.admin_password_secret, "admin page"),
                            (self.cfg.username_secret, self.cfg.password_secret, "1Password")):
            user, pw = str(self.vault.get(u) or "").strip(), str(self.vault.get(p) or "").strip()
            if user and pw:
                return user, pw, where
        return None

    @property
    def configured(self) -> bool:
        return self.cfg.enabled and self._login() is not None

    @property
    def login_source(self) -> str | None:
        login = self._login()
        return login[2] if login else None

    def allowed(self, role: str | None) -> bool:
        return self.configured and (role in self.cfg.roles)

    def status(self) -> str:
        if not self.cfg.enabled:
            return "off"
        if not self.configured:
            return "waiting for the PIA login (Private VPN row below, or the 1Password item PIA)"
        if self.last_error:
            return f"error: {self.last_error}"
        return f"ready, login from {self.login_source}" + ("" if self.last_ok else " (not used yet)")

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        return self._http

    # -- token ---------------------------------------------------------------
    async def token(self, force: bool = False) -> str:
        login = self._login() if self.cfg.enabled else None
        if login is None:
            raise PiaError("the PIA login isn't on the hub yet")
        user, pw, _ = login
        # A changed login (saved on the admin page) drops the old token.
        stamp = hash((user, pw))
        if not force and self._token and self._token[2] == stamp and time.time() - self._token[1] < TOKEN_RENEW_S:
            return self._token[0]
        res = await self._client().post(TOKEN_URL, data={"username": user, "password": pw})
        tok = None
        try:
            tok = res.json().get("token")
        except ValueError:
            pass
        if res.status_code != 200 or not tok:
            self.last_error = f"PIA refused the login (HTTP {res.status_code})"
            raise PiaError(self.last_error)
        self._token = (tok, time.time(), stamp)
        self.last_error = None
        return tok

    # -- regions -------------------------------------------------------------
    async def regions(self) -> list[dict[str, Any]]:
        """Online regions that offer WireGuard: id, name, country, servers [{ip, cn}]. Cached."""
        if self._regions and time.time() - self._regions[0] < self.cfg.regions_cache_s:
            return self._regions[1]
        res = await self._client().get(self.cfg.serverlist_url)
        if res.status_code != 200:
            raise PiaError(f"PIA server list answered HTTP {res.status_code}")
        try:
            data = json.loads(res.text.split("\n", 1)[0])
        except ValueError as exc:
            raise PiaError("PIA server list was not readable") from exc
        out = []
        for r in data.get("regions", []):
            wg = [s for s in (r.get("servers") or {}).get("wg", []) if s.get("ip") and s.get("cn")]
            if r.get("offline") or not wg:
                continue
            out.append({"id": r["id"], "name": r.get("name") or r["id"], "country": r.get("country") or "",
                        "geo": bool(r.get("geo")), "auto": bool(r.get("auto_region")) and not r.get("geo"),
                        "servers": wg})
        if not out:
            raise PiaError("PIA server list had no WireGuard regions")
        self._regions = (time.time(), out)
        return out

    async def public_regions(self) -> list[dict[str, Any]]:
        """What the app needs to pick the nearest region: one server per region to time a connection to."""
        # PIA's own apps time only "auto_region" ones (real locations) to find the nearest.
        return [{"id": r["id"], "name": r["name"], "country": r["country"], "auto": r["auto"],
                 "probe": f"{r['servers'][0]['ip']}:1337"} for r in await self.regions()]

    # -- addKey --------------------------------------------------------------
    def _add_key_blocking(self, ip: str, cn: str, token: str, pubkey: str) -> dict[str, Any]:
        conn = _PinnedHTTPS(ip, 1337, cn, _ca_context(), timeout=10)
        try:
            conn.request("GET", "/addKey?" + urlencode({"pt": token, "pubkey": pubkey}), headers={"Host": cn})
            res = conn.getresponse()
            body = res.read(65536)
        finally:
            conn.close()
        try:
            return json.loads(body)
        except ValueError:
            return {"status": "ERROR", "message": f"HTTP {res.status}"}

    async def config_for(self, pubkey: str, region_id: str | None) -> dict[str, Any]:
        """WireGuard settings for this phone's key on a server in region_id (or the configured default)."""
        async with self._lock:
            regions = await self.regions()
            want = region_id or self.cfg.default_region
            region = next((r for r in regions if r["id"] == want), None)
            if region is None:
                region = next((r for r in regions if r["id"] == self.cfg.default_region), None) or regions[0]
            token = await self.token()
            servers = list(region["servers"])
            random.shuffle(servers)
            last = "no server answered"
            for retry_token in (False, True):
                if retry_token:
                    token = await self.token(force=True)
                for s in servers[:3]:
                    try:
                        ans = await asyncio.to_thread(self._add_key, s["ip"], s["cn"], token, pubkey)
                    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                        last = f"{s['cn']}: {type(exc).__name__}"
                        continue
                    if ans.get("status") == "OK" and ans.get("server_key") and ans.get("peer_ip"):
                        self.last_error, self.last_ok = None, time.time()
                        return self._peer(region, s, ans)
                    last = str(ans.get("message") or ans.get("status") or "refused")[:120]
                    # A refused token (expired early) is the usual reason; sign in again once.
                    break
            self.last_error = f"addKey failed: {last}"
            raise PiaError(f"PIA would not take this phone's key ({last})")

    def _peer(self, region: dict[str, Any], server: dict[str, Any], ans: dict[str, Any]) -> dict[str, Any]:
        ip = ans.get("server_ip") or server["ip"]
        dns = [d for d in ans.get("dns_servers") or [] if d][:2]
        return {
            "interface": {"addresses": [f"{ans['peer_ip']}/32"], "dns": dns},
            "peer": {
                "public_key": ans["server_key"],
                "endpoint": f"{ip}:{ans.get('server_port') or 1337}",
                # Everything goes through PIA. ::/0 keeps IPv6 from leaking around the tunnel (PIA has no IPv6).
                "allowed_ips": ["0.0.0.0/0", "::/0"],
                "persistent_keepalive": 25,
            },
            "exit": "private",
            "region": {"id": region["id"], "name": region["name"], "country": region["country"]},
            "home_ssids": [],
        }
