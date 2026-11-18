"""Send logs to hub (Android phone): kept scrubbed, read by admins only, and update outcomes in the activity log."""

from __future__ import annotations

import json


def test_a_phone_sends_logs_and_an_admin_reads_them(hub, people):
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    lines = [
        "17:01:02 TCCast load Living room converted=true",
        "17:01:03 TCPlay open http://user1:secretpw@iptv.example:8080/live/user1/secretpw/10.m3u8",
        "17:01:04 TCPlay http://iptv.example/get.php?username=user1&password=secretpw",
        "17:01:05 TCHub Authorization: Bearer abcdefghijklmnop12345",
        "17:01:06 TCCast url /v1/tv/phone/cast/eyJhbGciOiJIUzI1NiJ9.abcdefghij.klmnop",
        {"t": 1795021266000, "level": "E", "tag": "TCCast", "msg": "failed code=104 on Living room"},
    ]
    r = theo.client.post("/v1/tv/phone/logs", headers=theo.h, json={
        "version_name": "0.6.0", "version_code": 7, "model": "Pixel 8", "android": "15", "mode": "managed",
        "note": "cast stayed black", "lines": lines})
    assert r.status_code == 200, r.text
    log_id = r.json()["id"]
    assert r.json()["lines"] == 6

    # A child's phone may send, only an admin reads.
    assert theo.client.get("/v1/diagnostics/phone-logs", headers=theo.h).status_code == 403
    listed = dylan.client.get("/v1/diagnostics/phone-logs", headers=dylan.h).json()["logs"]
    assert listed[0]["id"] == log_id and listed[0]["note"] == "cast stayed black" and listed[0]["person"] == "Theo"
    one = dylan.client.get(f"/v1/diagnostics/phone-logs/{log_id}", headers=dylan.h).json()
    kept = json.dumps(one)
    for secret in ("secretpw", "user1", "iptv.example", "abcdefghijklmnop12345", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in kept
    assert "TCCast load Living room" in kept and "E/TCCast failed code=104" in kept
    only = dylan.client.get(f"/v1/diagnostics/phone-logs/{log_id}", headers=dylan.h, params={"grep": "tccast"})
    assert all("TCCast" in line for line in only.json()["text"]) and len(only.json()["text"]) == 3
    assert dylan.client.get("/v1/diagnostics/phone-logs/nope", headers=dylan.h).status_code == 404
    assert theo.client.post("/v1/tv/phone/logs", json={"lines": ["x"]}).status_code == 401


def test_only_the_newest_logs_are_kept(hub, people):
    from hub.routes import phonelogs

    dylan = people["phones"]["Dylan"]
    ids = [dylan.client.post("/v1/tv/phone/logs", headers=dylan.h, json={"lines": [f"line {n}"]}).json()["id"]
           for n in range(phonelogs.KEEP + 2)]
    listed = dylan.client.get("/v1/diagnostics/phone-logs", headers=dylan.h).json()["logs"]
    assert len(listed) == phonelogs.KEEP and listed[0]["id"] == ids[-1]
    assert dylan.client.get(f"/v1/diagnostics/phone-logs/{ids[0]}", headers=dylan.h).status_code == 404


def test_update_steps_land_in_the_activity_log(hub, people):
    dylan = people["phones"]["Dylan"]
    for stage, ok, reason in (("downloaded", True, ""), ("verified", False, "signer did not match")):
        r = dylan.client.post("/v1/tv/phone/update/report", headers=dylan.h,
                              json={"version_code": 7, "stage": stage, "ok": ok, "reason": reason})
        assert r.status_code == 200
    rows = dylan.client.get("/v1/diagnostics/phone-activity", headers=dylan.h).json()["activity"]
    steps = [(row["outcome"], row["params"]["stage"]) for row in rows if row["action"] == "phoneapp.update"]
    assert ("error", "verified") in steps and ("ok", "downloaded") in steps
    assert people["phones"]["Theo"].client.get("/v1/diagnostics/phone-activity",
                                               headers=people["phones"]["Theo"].h).status_code == 403
