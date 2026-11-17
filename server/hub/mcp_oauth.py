"""OAuth sign-in for the hub's MCP server, so claude.ai and the Claude apps can connect.

The flow (OAuth 2.1, authorization code with PKCE S256, dynamic client registration):

1. The AI app registers (`POST /oauth/register`). Only redirect addresses on `ALLOWED_REDIRECTS`
   (Claude's own sites, and loopback for desktop apps) are accepted, so a sign-in can only ever
   hand a key to a Claude app.
2. It opens `/oauth/authorize`. The page shows an 8-character code and waits.
3. The person types that code in the Thomas Creek app (Systems, Hub web page) and confirms with
   Face ID (`POST /v1/admin/web-approve`, which hands OAuth codes to `approve`). Only adults and
   admins can connect an AI app.
4. The page sends the browser back to the AI app with a one-time authorization code, and the app
   swaps it (`POST /oauth/token`) for an AI key (hub/aikeys.py) that acts as that person. The key
   shows on the admin page under AI connections, where it can be turned off.

Pending sign-ins and authorization codes live in memory for a few minutes; registered clients
are kept in hub_kv.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from . import aikeys

if TYPE_CHECKING:
    from .state import Hub

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PENDING_TTL_S = 300
AUTH_CODE_TTL_S = 120
MAX_CLIENTS = 50
KV_CLIENTS = "mcp.oauth.clients"
# Where a sign-in may send the browser back to: Claude on the web and its apps (loopback).
ALLOWED_REDIRECTS = ("https://claude.ai/", "https://claude.com/", "https://www.claude.ai/")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


class OAuthError(Exception):
    def __init__(self, status: int, error: str, description: str):
        super().__init__(description)
        self.status, self.error, self.description = status, error, description

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def redirect_ok(uri: str) -> bool:
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.scheme:
        return False
    if any(uri.startswith(p) for p in ALLOWED_REDIRECTS):
        return True
    return u.scheme == "http" and (u.hostname or "") in LOOPBACK_HOSTS


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class OAuth:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.pending: dict[str, dict[str, Any]] = {}   # shown code -> sign-in waiting for the app
        self.codes: dict[str, dict[str, Any]] = {}     # authorization code -> approved sign-in

    def base(self, request_base: str) -> str:
        return (self.hub.config.hub.remote_url or request_base).rstrip("/")

    # -- discovery ----------------------------------------------------------
    def metadata(self, base: str) -> dict[str, Any]:
        return {
            "issuer": base, "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token", "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["home"],
        }

    def resource(self, base: str) -> dict[str, Any]:
        return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": ["home"],
                "bearer_methods_supported": ["header"], "resource_name": "Thomas Creek"}

    # -- clients -------------------------------------------------------------
    def _clients(self) -> dict[str, Any]:
        return self.hub.db.kv_get(KV_CLIENTS) or {}

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(isinstance(u, str) for u in uris):
            raise OAuthError(400, "invalid_redirect_uri", "redirect_uris is required")
        bad = [u for u in uris if not redirect_ok(u)]
        if bad:
            raise OAuthError(400, "invalid_redirect_uri", "Only Claude apps can connect to this hub")
        name = str(body.get("client_name") or "AI app").strip()[:40] or "AI app"
        clients = self._clients()
        if len(clients) >= MAX_CLIENTS:  # drop the oldest
            for cid in sorted(clients, key=lambda c: clients[c]["created_at"])[: len(clients) - MAX_CLIENTS + 1]:
                clients.pop(cid)
        cid = "tcmcp_" + secrets.token_urlsafe(16)
        clients[cid] = {"name": name, "redirect_uris": uris, "created_at": time.time()}
        self.hub.db.kv_set(KV_CLIENTS, clients)
        return {"client_id": cid, "client_name": name, "redirect_uris": uris, "grant_types": ["authorization_code"],
                "response_types": ["code"], "token_endpoint_auth_method": "none"}

    def client(self, cid: str) -> dict[str, Any]:
        c = self._clients().get(cid or "")
        if c is None:
            raise OAuthError(400, "invalid_client", "Unknown app; connect it again")
        return c

    # -- authorize -----------------------------------------------------------
    def _sweep(self) -> None:
        now = time.time()
        for d in (self.pending, self.codes):
            for k in [k for k, v in d.items() if v["exp"] < now]:
                d.pop(k, None)

    def start(self, q: dict[str, str]) -> dict[str, Any]:
        """Check an authorize request and make a pending sign-in with a code to show."""
        self._sweep()
        client = self.client(q.get("client_id", ""))
        redirect = q.get("redirect_uri", "")
        if redirect not in client["redirect_uris"]:
            raise OAuthError(400, "invalid_request", "That return address isn't registered for this app")
        if q.get("response_type") != "code":
            raise OAuthError(400, "unsupported_response_type", "Only response_type=code")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            raise OAuthError(400, "invalid_request", "PKCE S256 is required")
        if len(self.pending) > 20:
            raise OAuthError(429, "slow_down", "Too many sign-ins waiting; try again in a few minutes")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        p = {"id": secrets.token_urlsafe(16), "exp": time.time() + PENDING_TTL_S, "client_id": q["client_id"],
             "client_name": client["name"], "redirect_uri": redirect, "state": q.get("state"),
             "challenge": q["code_challenge"], "resource": q.get("resource"), "approved": None}
        self.pending[code] = p
        return {**p, "code": code}

    def is_pending(self, code: str) -> bool:
        p = self.pending.get(code)
        return bool(p and p["exp"] >= time.time() and not p["approved"])

    def approve(self, code: str, person: dict[str, Any], device_id: str) -> str:
        """The person typed the code in the app and gave Face ID. Returns the app's name."""
        p = self.pending.get(code)
        if not p or p["exp"] < time.time() or p["approved"]:
            raise OAuthError(404, "not_found", "That code is not valid any more")
        if person["role"] not in ("admin", "adult"):
            raise OAuthError(403, "forbidden", "Only grown-ups can connect an AI app")
        auth_code = secrets.token_urlsafe(32)
        self.codes[auth_code] = {"exp": time.time() + AUTH_CODE_TTL_S, "person_id": person["id"], "device_id": device_id,
                                 "client_id": p["client_id"], "client_name": p["client_name"],
                                 "redirect_uri": p["redirect_uri"], "challenge": p["challenge"]}
        p["approved"] = auth_code
        return p["client_name"]

    def poll(self, pid: str) -> str | None:
        """Where to send the browser once the code was approved (the app's return address with the code)."""
        for shown, p in list(self.pending.items()):
            if secrets.compare_digest(p["id"], pid or "") and p["approved"]:
                self.pending.pop(shown, None)
                q = {"code": p["approved"]}
                if p.get("state"):
                    q["state"] = p["state"]
                sep = "&" if "?" in p["redirect_uri"] else "?"
                return p["redirect_uri"] + sep + urlencode(q)
        return None

    # -- token ---------------------------------------------------------------
    def token(self, form: dict[str, str]) -> dict[str, Any]:
        self._sweep()
        if form.get("grant_type") != "authorization_code":
            raise OAuthError(400, "unsupported_grant_type", "Only authorization_code")
        c = self.codes.pop(form.get("code", ""), None)
        if c is None:
            raise OAuthError(400, "invalid_grant", "That sign-in code expired or was used")
        if form.get("client_id") != c["client_id"] or form.get("redirect_uri", c["redirect_uri"]) != c["redirect_uri"]:
            raise OAuthError(400, "invalid_grant", "That code was for another app")
        if not secrets.compare_digest(_s256(form.get("code_verifier", "")), c["challenge"]):
            raise OAuthError(400, "invalid_grant", "PKCE check failed")
        _, key = aikeys.create(self.hub, c["person_id"], c["client_name"], created_by=c["person_id"])
        self.hub.db.log("mcp.connect", "ok", person_id=c["person_id"], device_id=c["device_id"],
                        params={"app": c["client_name"]}, source="mcp")
        return {"access_token": key, "token_type": "Bearer", "scope": "home"}

    # -- the page ------------------------------------------------------------
    @staticmethod
    def page_body(p: dict[str, Any]) -> str:
        e = html.escape
        shown = f"{p['code'][:4]}-{p['code'][4:]}"
        return f"""<h1>Connect {e(p['client_name'])} to Thomas Creek</h1>
<div class=card style="padding:20px 16px"><p>{e(p['client_name'])} wants to control the house as you, with your rights.
In the Thomas Creek app, open <b>Systems → Hub web page</b>, type this code and confirm with Face ID.</p>
<p class=code>{shown}</p><p class=dim id=s>Waiting for the app… the code works for 5 minutes.</p>
<p class=dim>Locks, the garage and deletes will still ask for Face ID on your iPhone each time.
You can turn this connection off on the hub's admin page under AI connections.</p></div>
<script>
const id={p['id']!r};
async function poll(){{try{{const r=await fetch('/oauth/poll?id='+encodeURIComponent(id),{{cache:'no-store'}});
if(r.ok){{const j=await r.json();if(j.redirect){{document.getElementById('s').textContent='Connected. Returning to the app…';location.href=j.redirect;return}}}}
else if(r.status===404){{document.getElementById('s').textContent='This code expired. Start connecting again.';return}}}}catch(e){{}}
setTimeout(poll,2000)}}
poll();
</script>"""
