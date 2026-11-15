"""Does each saved key or login still work? The admin page's "Test" buttons and the six-hourly check.

A check signs in to the real service with what's in the vault and reduces the answer to a state:

  ok    the service accepted it
  warn  it works, but something needs a look soon (expires within two weeks, quota used up)
  bad   the service refused it, or it has expired
  down  the service couldn't be reached, so the key wasn't really tested
  skip  nothing to test with (not saved, or the service is off)

Values never leave the vault through here: results carry a state, a short reason, an expiry date and how
long the check took. Reasons come from status codes and the services' own error codes, never from echoing
a request. When a key that worked stops working, admins get one notice (Systems).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from .adapters.base import AdapterError, NoAnswer, NotConfigured, ServiceError, Unreachable

if TYPE_CHECKING:
    from .state import Hub

log = logging.getLogger("hub.keycheck")

KV = "admin.key_checks"
INTERVAL_S = 6 * 3600
TIMEOUT_S = 20.0
SOON_S = 14 * 86400
STATES = ("ok", "warn", "bad", "down", "skip")


class Result(dict):
    """{"state", "detail", "expires", "at", "ms"}."""


def result(state: str, detail: str = "", expires: float | None = None) -> Result:
    assert state in STATES
    return Result(state=state, detail=detail[:200], expires=expires)


def _expiring(expires: float | None, detail: str, now: float | None = None) -> Result:
    now = time.time() if now is None else now
    if expires is None:
        return result("ok", detail)
    if expires < now:
        return result("bad", f"expired {_date(expires)}", expires)
    if expires - now < SOON_S:
        return result("warn", f"expires {_date(expires)}" + (f" · {detail}" if detail else ""), expires)
    return result("ok", detail or f"good until {_date(expires)}", expires)


def _date(ts: float) -> str:
    return time.strftime("%b %-d, %Y", time.localtime(ts))


@dataclass
class Check:
    id: str
    title: str
    secrets: tuple[str, ...]
    run: Callable[["Hub"], Awaitable[Result]]
    ready: Callable[["Hub"], bool]


def _from_error(exc: BaseException, title: str) -> Result:
    """An adapter's exception, as a state."""
    if isinstance(exc, ServiceError):
        if exc.status in (401, 403):
            return result("bad", f"{title} refused it" + (f" ({exc.reason[:60]})" if exc.reason else ""))
        return result("down", f"{title} answered {exc.status}")
    if isinstance(exc, (Unreachable, NoAnswer, asyncio.TimeoutError, httpx.HTTPError)):
        return result("down", f"couldn't reach {title}")
    if isinstance(exc, NotConfigured):
        return result("skip", f"{title} is off")
    msg = str(exc) or type(exc).__name__
    if re.search(r"refused|rejected|turned the key down|invalid|expired|\b40[13]\b", msg, re.I):
        return result("bad", msg)
    return result("down", msg)


# -- the checks -------------------------------------------------------------------------------------------------------
def _adapter_check(attr: str) -> Callable[["Hub"], Awaitable[Result]]:
    async def run(h: "Hub") -> Result:
        a = getattr(h, attr)
        # Start from nothing cached, so a replaced key is what gets tested.
        if attr == "homebox":
            a._token = None
        try:
            await a.ping()
        except Exception as exc:  # noqa: BLE001
            return _from_error(exc, a.title)
        return result("ok", f"{a.title} accepted it")
    return run


def _adapter_ready(attr: str, need_secret: bool = True) -> Callable[["Hub"], bool]:
    def ready(h: "Hub") -> bool:
        a = getattr(h, attr)
        names = [n for n in (getattr(a.cfg, 'token_secret', None), getattr(a.cfg, 'password_secret', None)) if n]
        return bool(a.enabled and (not need_secret or any(h.vault.has(n) for n in names)))
    return ready


async def _family_board(h: "Hub") -> Result:
    try:
        # An admin-only read: the board answers 401 to a wrong parent PIN.
        await h.family_board.request("GET", "/api/integrations")
    except Exception as exc:  # noqa: BLE001
        return _from_error(exc, "The family board")
    return result("ok", "the board accepted the parent PIN")


async def _anthropic(h: "Hub") -> Result:
    key = str(h.vault.get(h.config.brain.key_secret) or "").strip()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.anthropic.com/v1/models", params={"limit": 1},
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if r.status_code == 200:
        return result("ok", "Anthropic accepted it")
    if r.status_code in (401, 403):
        return result("bad", "Anthropic refused it (revoked, or a typo)")
    if r.status_code == 429:
        return result("warn", "accepted, but rate limited right now")
    return result("down", f"Anthropic answered {r.status_code}")


async def _youtube(h: "Hub") -> Result:
    key = str(h.vault.get("youtube.api_key") or "").strip()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://www.googleapis.com/youtube/v3/videos",
                        params={"part": "id", "id": "jNQXAC9IVRw", "key": key})
    if r.status_code == 200:
        return result("ok", "YouTube accepted it")
    try:
        reason = r.json()["error"]["errors"][0].get("reason") or ""
    except Exception:  # noqa: BLE001
        reason = ""
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return result("warn", "works, but today's quota is used up")
    if reason == "accessNotConfigured":
        return result("bad", "YouTube Data API v3 isn't enabled for this key's project")
    if r.status_code in (400, 401, 403):
        return result("bad", "YouTube refused it" + (f" ({reason})" if reason else ""))
    return result("down", f"YouTube answered {r.status_code}")


def _github_expiry(value: str | None) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S %z"):
        try:
            dt = datetime.strptime(value.strip(), fmt)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            continue
    return None


async def _github(h: "Hub") -> Result:
    tok = str(h.vault.get("github.tv_app_token") or "").strip()
    repo = h.config.tv_app.repo
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://api.github.com/repos/{repo}",
                        headers={"Authorization": f"Bearer {tok}", "X-GitHub-Api-Version": "2022-11-28",
                                 "User-Agent": "thomascreek-hub"})
    if r.status_code == 401:
        return result("bad", "GitHub refused it (expired or revoked)")
    if r.status_code in (403, 404):
        return result("bad", f"the token can't see {repo}")
    if r.status_code != 200:
        return result("down", f"GitHub answered {r.status_code}")
    return _expiring(_github_expiry(r.headers.get("github-authentication-token-expiration")), f"can read {repo}")


async def _apns(h: "Hub") -> Result:
    from .push import HOSTS

    try:
        token = h.apns._provider_token()
    except Exception:  # noqa: BLE001
        return result("bad", "the key file can't be read as an Apple .p8 key")
    # A made-up device: Apple checks the key first (403 if it's wrong), then says the device is unknown.
    r = await h.apns._c().post(f"{HOSTS['production']}/3/device/{'0' * 64}", json={"aps": {"content-available": 1}},
                               headers={"authorization": f"bearer {token}", "apns-topic": h.apns.cfg.topic,
                                        "apns-push-type": "background", "apns-priority": "5"})
    try:
        reason = r.json().get("reason") or ""
    except Exception:  # noqa: BLE001
        reason = ""
    if r.status_code == 400 and reason in ("BadDeviceToken", "DeviceTokenNotForTopic"):
        return result("ok", "Apple accepted the key")
    if reason == "TooManyProviderTokenUpdates":
        return result("warn", "Apple asked the hub to slow down; try again in 20 minutes")
    if r.status_code == 403:
        h.apns._jwt = None
        why = {"InvalidProviderToken": "Apple refused it (check the Key ID and team)",
               "ExpiredProviderToken": "Apple says the sign-in token expired; it renews by itself",
               "TopicDisallowed": f"this key may not push to {h.apns.cfg.topic}"}.get(reason, f"Apple refused it ({reason})")
        return result("bad", why)
    return result("down", f"Apple answered {r.status_code} {reason}".strip())


async def _appstore(h: "Hub") -> Result:
    h.app_update._jwt = None
    try:
        await h.app_update._get("/v1/apps", {"limit": "1", "fields[apps]": "bundleId"})
    except httpx.HTTPError:
        return result("down", "couldn't reach App Store Connect")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or type(exc).__name__
        if "said 5" in msg:
            return result("down", msg)
        return result("bad", msg if "App Store" in msg else "the key file can't be read as an Apple .p8 key")
    return result("ok", "App Store Connect accepted it")


async def _pia(h: "Hub") -> Result:
    try:
        await h.pia.token(force=True)
    except httpx.HTTPError:
        return result("down", "couldn't reach PIA")
    except Exception as exc:  # noqa: BLE001
        return _from_error(exc, "PIA")
    return result("ok", f"PIA accepted the login from the {h.pia.login_source}")


async def _onepassword(h: "Hub") -> Result:
    try:
        client = await h.onepassword._client()
        vaults = await client.vaults.list()
    except ImportError:
        return result("skip", "1Password support isn't installed")
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if re.search(r"connect|timeout|network", f"{name} {exc}", re.I):
            return result("down", "couldn't reach 1Password")
        return result("bad", f"1Password refused it ({name})")
    want = h.config.onepassword.vault
    if want and not any(getattr(v, "title", None) == want for v in vaults):
        return result("warn", f"signed in, but the vault {want!r} isn't shared with it")
    return result("ok", "1Password accepted it")


async def _trek_mcp(h: "Hub") -> Result:
    h.trek.__dict__.pop("_mcp_tok", None)
    try:
        await h.trek._mcp_token()
    except Exception as exc:  # noqa: BLE001
        return _from_error(exc, "TREK")
    return result("ok", "TREK accepted the machine client")


async def _bambu(h: "Hub") -> Result:
    from .bambu_cloud import token_expiry

    c = h.bambu_cloud
    if not c.signed_in:
        return result("bad", "signed out")
    exp = token_expiry(c.token)
    r = _expiring(exp, "signed in")
    if c.last_error and r["state"] == "ok":
        return result("warn", c.last_error, exp)
    return r


def _xtream_check(login: dict[str, Any]) -> Callable[["Hub"], Awaitable[Result]]:
    async def run(h: "Hub") -> Result:
        password = str(h.vault.get(login["password_secret"]) or "")
        server = str(login["server"]).rstrip("/")
        if not server.startswith(("http://", "https://")):
            return result("bad", "the server address should start with http:// or https://")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as c:
            r = await c.get(f"{server}/player_api.php", params={"username": login["username"], "password": password})
        if r.status_code in (401, 403):
            return result("bad", "the provider refused the login")
        if r.status_code >= 300:
            return result("down", f"the provider answered {r.status_code}")
        try:
            info = r.json().get("user_info") or {}
        except Exception:  # noqa: BLE001
            return result("down", "the provider's answer wasn't readable")
        if str(info.get("auth")) != "1":
            return result("bad", "the provider refused the login")
        status = str(info.get("status") or "Active")
        if status.lower() != "active":
            return result("bad", f"the account is {status.lower()}")
        try:
            exp = float(info.get("exp_date")) if info.get("exp_date") not in (None, "", "null") else None
        except (TypeError, ValueError):
            exp = None
        cons = f"{info.get('active_cons', 0)} of {info.get('max_connections', '?')} streams in use"
        return _expiring(exp, cons)
    return run


def _url_check(name: str, marker: bytes | None) -> Callable[["Hub"], Awaitable[Result]]:
    async def run(h: "Hub") -> Result:
        url = str(h.vault.get(name) or "").strip()
        if not url.startswith(("http://", "https://")):
            return result("bad", "it should start with http:// or https://")
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                if r.status_code in (401, 403):
                    return result("bad", "the provider refused it")
                if r.status_code == 404:
                    return result("bad", "nothing at that address")
                if r.status_code >= 400:
                    return result("down", f"the provider answered {r.status_code}")
                head = b""
                async for chunk in r.aiter_bytes():
                    head += chunk
                    if len(head) >= 512:
                        break
        if marker and marker not in head[:512].lstrip(b"\xef\xbb\xbf").lstrip()[:len(marker) + 8]:
            return result("warn", "it answered, but not with a playlist")
        return result("ok", "the address works")
    return run


def _printer_check(pid: str) -> Callable[["Hub"], Awaitable[Result]]:
    async def run(h: "Hub") -> Result:
        row = next((r for r in h.workshop.health() if r["id"] == f"printer_{pid}"), None)
        if row is None:
            return result("skip", "printer not set up")
        if row["ok"]:
            return result("ok", "the printer is connected")
        err = row.get("error") or "not connected"
        return result("bad" if re.search(r"auth|refused|access code|denied", err, re.I) else "down", err)
    return run


def _has(*names: str) -> Callable[["Hub"], bool]:
    return lambda h: all(h.vault.has(n) for n in names)


def checks(h: "Hub") -> list[Check]:
    """Every check that applies to this hub's config, built fresh (logins and printers can change)."""
    out: list[Check] = []
    for attr in ("ha", "plex", "jellyseerr", "sonarr", "radarr", "truenas", "mealie", "homebox", "trek",
                 "show_player", "firewalla", "frigate"):
        a = getattr(h, attr, None)
        if a is None:
            continue
        names = tuple(n for n in (getattr(a.cfg, 'token_secret', None), getattr(a.cfg, 'password_secret', None)) if n)
        if attr == "frigate":
            names = (a.cfg.password_secret,) if getattr(a.cfg, 'password_secret', None) else ()
        if not names:
            continue
        out.append(Check(attr, a.title, names, _adapter_check(attr), _adapter_ready(attr)))
    fb = h.family_board
    if fb.cfg.token_secret:
        out.append(Check("family_board", "Family board", (fb.cfg.token_secret,), _family_board,
                         lambda h: h.family_board.enabled and h.vault.has(h.family_board.cfg.token_secret)))
    out.append(Check("anthropic", "Claude API", (h.config.brain.key_secret,), _anthropic, _has(h.config.brain.key_secret)))
    out.append(Check("youtube", "YouTube", ("youtube.api_key",), _youtube, _has("youtube.api_key")))
    out.append(Check("github", "GitHub (TV app builds)", ("github.tv_app_token",), _github, _has("github.tv_app_token")))
    out.append(Check("apns", "Apple push", (h.apns.cfg.key_secret, "apns.key_id"), _apns, lambda h: h.apns.enabled))
    out.append(Check("appstore", "App Store Connect", ("appstore.auth_key_p8", "appstore.key_id", "appstore.issuer_id"),
                     _appstore, lambda h: h.app_update.configured))
    pia = h.config.vpn.pia
    out.append(Check("pia", "Private VPN (PIA)", (pia.admin_username_secret, pia.admin_password_secret,
                                                  pia.username_secret, pia.password_secret),
                     _pia, lambda h: h.pia.configured))
    out.append(Check("onepassword", "1Password", (h.config.onepassword.token_secret,), _onepassword,
                     _has(h.config.onepassword.token_secret)))
    tc = h.trek.cfg
    out.append(Check("trek_mcp", "TREK machine client", (tc.mcp_client_id_secret, tc.mcp_client_secret_secret), _trek_mcp,
                     lambda h: h.trek.enabled and h.trek.mcp_ready()))
    from . import bambu_cloud

    out.append(Check("bambu", "Bambu account", (bambu_cloud.TOKEN, bambu_cloud.REFRESH, bambu_cloud.UID), _bambu,
                     lambda h: h.vault.has(bambu_cloud.TOKEN)))
    for pid in getattr(h.workshop, "printers", {}):
        names = tuple(f"printer.{pid}.{w}" for w in ("access_code", "serial", "host"))
        out.append(Check(f"printer_{pid}", "3D printer", names, _printer_check(pid),
                         lambda h, n=names[0]: h.vault.has(n)))
    try:
        from .iptvplay import logins

        for lg in logins(h):
            names = (lg["password_secret"],)
            if lg["provider"] == "thomas-creek" and lg["login"] == "main":
                names = ("iptv.xtream_server", "iptv.xtream_username", lg["password_secret"])
            out.append(Check(f"iptv:{lg['provider']}:{lg['login']}", f"IPTV login ({lg['name']})", names,
                             _xtream_check(lg), lambda h, n=lg["password_secret"]: h.vault.has(n)))
    except Exception as exc:  # noqa: BLE001
        log.debug("iptv logins: %s", exc)
    out.append(Check("iptv_m3u", "M3U playlist", ("iptv.m3u_url",), _url_check("iptv.m3u_url", b"#EXTM3U"),
                     _has("iptv.m3u_url")))
    out.append(Check("iptv_epg", "TV guide", ("iptv.epg_url",), _url_check("iptv.epg_url", None), _has("iptv.epg_url")))
    return out


def forget_cached(h: "Hub", name: str) -> None:
    """A key was saved or removed: drop anything that still holds the old one."""
    fw = getattr(h, "firewalla", None)
    if fw is not None and name == fw.cfg.token_secret and fw._client is not None:
        old, fw._client = fw._client, None
        try:
            asyncio.get_running_loop().create_task(old.aclose())
        except RuntimeError:
            pass
    if name == getattr(h.homebox.cfg, 'password_secret', None):
        h.homebox._token = None
    if name in (h.trek.cfg.mcp_client_id_secret, h.trek.cfg.mcp_client_secret_secret):
        h.trek.__dict__.pop("_mcp_tok", None)
    if name in (h.apns.cfg.key_secret, "apns.key_id"):
        h.apns._jwt = None


class KeyChecks:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        saved = hub.db.kv_get(KV)
        self.results: dict[str, dict[str, Any]] = saved if isinstance(saved, dict) else {}
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()

    # -- reading ------------------------------------------------------------------------------------------------------
    def for_secret(self, name: str) -> list[tuple[Check, dict[str, Any] | None]]:
        return [(c, self.results.get(c.id)) for c in checks(self.hub) if name in c.secrets]

    def summary(self) -> list[dict[str, Any]]:
        """One row per check that applies: id, title, secrets, state, detail, expires, at, ms. No values."""
        rows = []
        for c in checks(self.hub):
            r = self.results.get(c.id) or {}
            ready = _safe_ready(c, self.hub)
            rows.append({"id": c.id, "title": c.title, "secrets": list(c.secrets), "ready": ready,
                         "state": r.get("state") if ready else "skip", "detail": r.get("detail") if ready else "",
                         "expires": r.get("expires"), "at": r.get("at"), "ms": r.get("ms")})
        return rows

    # -- running ------------------------------------------------------------------------------------------------------
    async def run_one(self, c: Check) -> dict[str, Any]:
        if not _safe_ready(c, self.hub):
            r: dict[str, Any] = result("skip", "nothing saved to test")
        else:
            # A check of this key is already under way (say the one started right after a save):
            # wait for it, then test again, so the answer is about the key as it is now.
            deadline = time.monotonic() + TIMEOUT_S + 1
            while c.id in self._running:
                if time.monotonic() > deadline:
                    return self.results.get(c.id) or result("skip", "already being checked")
                await asyncio.sleep(0.05)
            self._running.add(c.id)
            t0 = time.monotonic()
            try:
                r = await asyncio.wait_for(c.run(self.hub), timeout=TIMEOUT_S)
            except asyncio.TimeoutError:
                r = result("down", f"{c.title} took too long to answer")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                r = _from_error(exc, c.title)
            finally:
                self._running.discard(c.id)
            r["ms"] = int((time.monotonic() - t0) * 1000)
        r["at"] = time.time()
        before = self.results.get(c.id) or {}
        self.results[c.id] = dict(r)
        self._save()
        await self._tell(c, before, r)
        return r

    async def run(self, ids: list[str] | None = None, secret: str | None = None) -> list[tuple[Check, dict[str, Any]]]:
        todo = [c for c in checks(self.hub) if (ids is None or c.id in ids) and (secret is None or secret in c.secrets)]
        if secret is None and ids is None:
            todo = [c for c in todo if _safe_ready(c, self.hub)]
        done = await asyncio.gather(*(self.run_one(c) for c in todo))
        return list(zip(todo, done))

    def after_save_many(self, names: list[str]) -> None:
        """Several keys changed at once: forget the old ones, then run each affected check once."""
        for n in names:
            forget_cached(self.hub, n)
        ids = [c.id for c in checks(self.hub) if any(n in c.secrets for n in names)]
        if ids:
            try:
                asyncio.get_running_loop().create_task(self.run(ids=ids))
            except RuntimeError:
                pass

    def after_save(self, name: str) -> None:
        """A key changed: forget the old one, then test the new one in the background."""
        forget_cached(self.hub, name)
        if any(name in c.secrets for c in checks(self.hub)):
            try:
                asyncio.get_running_loop().create_task(self.run(secret=name))
            except RuntimeError:
                pass

    def _save(self) -> None:
        try:
            self.hub.db.kv_set(KV, self.results)
        except Exception as exc:  # noqa: BLE001
            log.debug("save key checks: %s", exc)

    async def _tell(self, c: Check, before: dict[str, Any], now: dict[str, Any]) -> None:
        """One notice when a key that worked stops working, or starts to expire soon."""
        was, state = before.get("state"), now.get("state")
        if state not in ("bad", "warn") or was == state or (state == "warn" and not now.get("expires")):
            return
        if was not in ("ok", "warn", None):
            return
        if was is None and state == "warn":
            return
        title = f"{c.title}: key {'stopped working' if state == 'bad' else 'expires soon'}"
        try:
            await self.hub.notify.send(title=title, body=f"{now.get('detail') or ''}. Replace it on the hub web page.",
                                       cat="systems", level="maintenance", roles=["admin"], group_key=f"keycheck:{c.id}")
        except Exception as exc:  # noqa: BLE001
            log.info("key check notice: %s", exc)

    # -- background ---------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(90)
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("key checks: %s", exc)
            await asyncio.sleep(INTERVAL_S)


def _safe_ready(c: Check, h: "Hub") -> bool:
    try:
        return bool(c.ready(h))
    except Exception:  # noqa: BLE001
        return False
