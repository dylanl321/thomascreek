"""Alert settings (per person), what Dylan offers from Home Assistant, and the
endpoint Home Assistant automations call to send an alert through the app."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..alerts import ALERT_ROLES, GAPS, LABEL_TITLES
from ..notify import CATEGORIES, DEFAULT_PREFS
from ..state import Hub
from .cameras import camera_name
from .deps import Caller, caller, caller_ip, error, hub, require_confirm

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

# Person-level keys a child may change (cameras, doors and garage are grown-up alerts).
CHILD_KEYS = {"on", "quiet", "entities", "automations", "hazards"}
# Device categories now covered by the per-person alert choices.
REPLACED = {"cameras", "doors", "house"}


def _person(c: Caller) -> str:
    if not c.subject.person_id:
        raise error(403, "forbidden", "This phone isn't assigned to anyone yet")
    return c.subject.person_id


def _view(h: Hub, c: Caller) -> dict[str, Any]:
    pid = _person(c)
    role = c.subject.role
    grown = role in ALERT_ROLES
    prefs = h.alerts.prefs(pid)
    if not grown:
        prefs = {k: v for k, v in prefs.items() if k in CHILD_KEYS}
    admin_only = {"systems", "devices"}
    cats = [cat for cat in CATEGORIES if cat["id"] not in REPLACED and (cat["id"] not in admin_only or role == "admin")]
    out: dict[str, Any] = {
        "role": role,
        "prefs": prefs,
        "device_prefs": {**DEFAULT_PREFS, **(c.device.get("prefs") or {})},
        "categories": cats,
        "push_ready": h.apns.enabled and bool(c.device.get("apns_token")),
        "gaps": list(GAPS),
        "found": h.alerts.summary(),
    }
    if role != "admin":
        prefs.pop("network", None)
    if grown:
        out["found"]["kids"] = [p["name"] for p in h.db.people_with_role("child")]
        out["found"]["network"] = h.firewalla.enabled
        out["found"]["ignored"] = len(h.alerts.suppressions())
    if not grown:
        found = out["found"]
        out["found"] = {"extra": found["extra"], "hazards": found["hazards"], "listening": found["listening"]}
    else:
        out["cameras"] = [{"id": cam, "name": camera_name(h, cam)} for cam, info in sorted(
            h.frigate._info.items(), key=lambda kv: (kv[1].get("order") or 0, kv[0])) if info.get("enabled", True)]
        out["labels"] = [{"id": k, "title": v} for k, v in LABEL_TITLES.items()]
    if role == "admin":
        out["sources"] = h.alerts.sources()
    return out


@router.get("")
async def get_alerts(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    return _view(h, c)


@router.put("")
async def put_alerts(body: dict[str, Any], c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    pid = _person(c)
    changes = body.get("prefs") if isinstance(body.get("prefs"), dict) else {}
    if c.subject.role not in ALERT_ROLES:
        changes = {k: v for k, v in changes.items() if k in CHILD_KEYS}
    h.alerts.set_prefs(pid, changes)
    dev = body.get("device_prefs")
    if isinstance(dev, dict):
        known = {cat["id"] for cat in CATEGORIES}
        merged = {**DEFAULT_PREFS, **(c.device.get("prefs") or {}),
                  **{k: bool(v) for k, v in dev.items() if k in known and isinstance(v, bool)}}
        h.db.update("devices", "id", c.device["id"], {"prefs": merged})
        c.device["prefs"] = merged
    h.db.log("alerts.prefs", "ok", person_id=pid, device_id=c.device["id"], source="app")
    return _view(h, c)


@router.post("/test")
async def test_alert(c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """A sample camera alert to this phone only, shaped like a real one, with Apple's answer."""
    _person(c)
    if not h.apns.enabled:
        raise error(503, "push_unavailable", "Push isn't set up on the hub yet")
    token = c.device.get("apns_token")
    if not token:
        raise error(409, "no_token", "This device hasn't registered for alerts yet")
    from ..push import alert_payload

    cam = None
    if c.subject.role in ALERT_ROLES:
        cam = next(iter(h.alerts.default_cameras()), None) or next(iter(h.frigate._info), None)
    payload = alert_payload("Test alert", f"{camera_name(h, cam)} camera, right now" if cam else "Alerts are working",
                            cat="cameras", ref=f"thomascreek://camera/{cam}" if cam else None,
                            image=f"/v1/cameras/{cam}/snapshot.jpg" if cam else None, interruption="active")
    res = await h.apns.send(token, payload, env=c.device.get("apns_env") or "production")
    if res.token_dead:
        h.db.update("devices", "id", c.device["id"], {"apns_token": None})
    return {"ok": res.ok, "picture": bool(cam), "status": res.status, "reason": res.reason,
            "env": c.device.get("apns_env") or "production"}


# -- history: every alert, pushed or not --------------------------------------
HISTORY_TITLES = {"camera": "Cameras", "package": "Packages", "doorbell": "Doorbell", "doors": "Doors and garage",
                  "house": "House", "kids": "Kids", "network": "Network", "media": "Media",
                  "weather": "Weather"}


def _history_kinds(role: str) -> list[str]:
    if role == "admin":
        return list(HISTORY_TITLES)
    if role in ALERT_ROLES:
        return [k for k in HISTORY_TITLES if k != "network"]
    return ["house", "media"]


@router.get("/history")
async def history(kind: str | None = None, before: float | None = None, limit: int = 50,
                  c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """Everything that could have alerted this person, whether or not they turned its push on."""
    pid = _person(c)
    allowed = _history_kinds(c.subject.role)
    kinds = [k for k in (kind.split(",") if kind else allowed) if k in allowed]
    if not kinds:
        return {"items": [], "kinds": [{"id": k, "title": HISTORY_TITLES[k]} for k in allowed]}
    items = h.alerts.history(pid, ",".join(kinds), before, limit)
    return {"items": items, "kinds": [{"id": k, "title": HISTORY_TITLES[k]} for k in allowed],
            "keep_days": h.alerts.KEEP_DAYS}


# -- Dylan: what's on offer -----------------------------------------------
def _admin(c: Caller) -> None:
    if c.subject.role != "admin":
        raise error(403, "forbidden", "Only Dylan can change this")


@router.get("/entities")
async def entities(q: str = "", c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    return {"entities": h.alerts.candidates(q)}


class Extra(BaseModel):
    entity_id: str = Field(max_length=200, pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    name: str | None = Field(default=None, max_length=80)


class SourcesBody(BaseModel):
    doorbell_entity: str | None = Field(default=None, max_length=200)
    doorbell_camera: str | None = Field(default=None, max_length=64)
    outside_doors: list[str] | None = None
    extra: list[Extra] | None = None
    garage_after_s: int | None = Field(default=None, ge=60, le=6 * 3600)


@router.put("/sources")
async def put_sources(body: SourcesBody, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    _admin(c)
    vals = body.model_dump(exclude_unset=True)
    if "extra" in vals and vals["extra"] is not None:
        vals["extra"] = [e for e in vals["extra"]][:50]
    if "outside_doors" in vals and vals["outside_doors"] is not None:
        vals["outside_doors"] = [d for d in vals["outside_doors"] if isinstance(d, str)][:30]
    h.alerts.set_sources(vals)
    h.db.log("alerts.sources", "ok", person_id=c.subject.person_id, device_id=c.device["id"], params=vals)
    return _view(h, c)


AUTOMATION_YAML = """# configuration.yaml (restart Home Assistant after adding)
rest_command:
  thomas_creek_alert:
    url: "{url}/v1/alerts/notify"
    method: POST
    verify_ssl: false
    headers:
      Authorization: "Bearer {token}"
    content_type: "application/json"
    payload: >-
      {{{{ {{"title": title, "message": message | default(""), "to": to | default("adults"),
          "urgent": urgent | default(false), "camera": camera | default(none)}} | tojson }}}}

# In any automation:
# action: rest_command.thomas_creek_alert
# data:
#   title: "Freezer is warm"
#   message: "{{{{ states('sensor.freezer_temperature') }}}}°F"
#   to: adults          # or everyone, kids, Dylan, or a list of names
#   urgent: false       # true rings through quiet hours
#   camera: 140_frontdoor   # optional: attach that camera's picture
"""


@router.post("/automation")
async def automation_setup(request: Request, c: Caller = Depends(caller), h: Hub = Depends(hub)) -> dict[str, Any]:
    """The Home Assistant snippet with the hub's alert token. Face ID first: the token sends alerts."""
    _admin(c)
    await require_confirm(request, h, c, "admin")
    host = request.headers.get("host") or f"{request.url.hostname}:{request.url.port}"
    url = f"https://{host}"
    return {"yaml": AUTOMATION_YAML.format(url=url, token=h.alerts.webhook_token()), "url": url}


class NotifyBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    message: str | None = Field(default=None, max_length=500)
    to: list[str] | str | None = None
    urgent: bool = False
    camera: str | None = Field(default=None, max_length=64)


@router.post("/notify")
async def notify(body: NotifyBody, request: Request, h: Hub = Depends(hub)) -> dict[str, Any]:
    """Called by Home Assistant (rest_command). Home network plus the alert token."""
    if not h.is_home(caller_ip(request, h)):
        raise error(403, "forbidden", "Only from the home network")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    expected = h.vault.get("alerts.webhook_token")
    if not expected or not hmac.compare_digest(token, str(expected)):
        raise error(401, "unauthorized", "Wrong alert token")
    cam = body.camera if body.camera and body.camera in h.frigate._info else None
    n = await h.alerts.from_automation(body.title, body.message, body.to, urgent=body.urgent, camera=cam)
    h.db.log("alerts.automation", "ok", target=body.title[:60], params={"to": body.to, "people": n}, source="home_assistant")
    return {"ok": True, "people": n}
