"""The Downloads page, and the short address a TV types into Downloader."""

from __future__ import annotations

import hashlib
import json
import re

from tests.test_tvapp import CERT_A, CERT_B, make_apk


def _sign_in(home_client, people) -> str:
    dylan = people["phones"]["Dylan"]
    page = home_client.get("/admin")
    code = re.search(r"class=code>([A-Z0-9-]+)<", page.text).group(1)
    pid = re.search(r"const id='([^']+)'", page.text).group(1)
    assert dylan.secure_post("/v1/admin/web-approve", {"code": code.lower()}).status_code == 200
    assert home_client.get(f"/admin/login/poll?id={pid}").json()["ok"] is True
    text = home_client.get("/admin/downloads").text
    return re.search(r"name=csrf value=([0-9a-f]+)", text).group(1)


def _info(apk: bytes, code: int, name: str) -> dict:
    return {"versionCode": code, "versionName": name, "apk": "ThomasCreekTV.apk",
            "sha256": hashlib.sha256(apk).hexdigest(), "notes": name}


def test_an_empty_page_explains_how_to_publish_and_does_not_invent_a_code(hub, people, home_client, away_client):
    assert away_client.get("/admin/downloads").status_code == 403
    _sign_in(home_client, people)
    page = home_client.get("/admin/downloads").text
    assert "No builds yet" in page and "tc-*" in page and "tvapp/inbox" in page
    assert "go.aftvnews.com" in page and "does not make one up" in page
    assert "class=codebig" not in page
    assert "Downloads" in home_client.get("/admin").text
    missing = home_client.get("/tv")
    assert missing.status_code == 404 and "No builds yet." in missing.text
    assert away_client.get("/tv").status_code == 403 and "This link only works at home." in away_client.get("/tv").text
    assert "Traceback" not in missing.text


def test_the_latest_build_is_one_address_and_older_ones_stay_folded(hub, people, home_client, away_client):
    older, newer = make_apk(CERT_A), make_apk(CERT_A)
    hub.tv_app.store_build(older, _info(older, 601, "1.4.1-tc.1"))
    hub.tv_app.store_build(newer, _info(newer, 602, "1.4.1-tc.2"))
    csrf = _sign_in(home_client, people)
    page = home_client.get("/admin/downloads").text
    assert "1.4.1-tc.2" in page and "TVs install this one overnight" in page
    assert "older build" in page and "1.4.1-tc.1" in page
    assert hashlib.sha256(newer).hexdigest() in page
    assert 'href="/tv/602"' in page
    jumped = home_client.get("/tv", follow_redirects=False)
    assert jumped.status_code == 302 and jumped.headers["location"] == "/tv/602"
    got = home_client.get("/tv/602")
    assert got.status_code == 200 and got.content == newer
    assert got.headers["content-type"].startswith("application/vnd.android.package-archive")
    assert home_client.get("/tv/601").content == older
    assert home_client.get("/tv/999").status_code == 404 and "That build is not here." in home_client.get("/tv/999").text
    assert "<svg" in page

    hub.config.tv_app.downloader_code = "nope"
    assert "class=codebig" not in home_client.get("/admin/downloads").text
    hub.config.tv_app.downloader_code = "48291"
    shown = home_client.get("/admin/downloads").text
    assert "48291" in shown and "class=codebig" in shown
    saved = home_client.post("/admin/downloads/code", data={"csrf": csrf, "downloader_code": "abc"}, follow_redirects=False)
    assert saved.status_code == 303 and "aftv.news" in saved.headers["location"]
    assert "48291" in home_client.get("/admin/downloads").text
    home_client.post("/admin/downloads/code", data={"csrf": csrf, "downloader_code": "13579"}, follow_redirects=True)
    assert "13579" in home_client.get("/admin/downloads").text
    home_client.post("/admin/downloads/code", data={"csrf": csrf, "downloader_code": ""}, follow_redirects=True)
    cleared = home_client.get("/admin/downloads").text
    assert "class=codebig" not in cleared and "13579" not in cleared and "48291" not in cleared


def test_an_upload_and_a_drop_keep_a_signed_build_and_refuse_the_rest(hub, people, home_client):
    csrf = _sign_in(home_client, people)
    apk = make_apk(CERT_A)
    info = _info(apk, 610, "1.5.0")
    bad = home_client.post("/admin/downloads/upload", data={"csrf": csrf},
                           files={"apk": ("ThomasCreekTV.apk", apk), "info": ("update.json", b"{not json")})
    assert "update.json" in bad.text and not hub.tv_app.apk_path(610).exists()
    mismatch = dict(info, sha256="0" * 64)
    refused = home_client.post("/admin/downloads/upload", data={"csrf": csrf}, files={
        "apk": ("ThomasCreekTV.apk", apk), "info": ("update.json", json.dumps(mismatch).encode()),
    })
    assert "checksum" in refused.text and not hub.tv_app.apk_path(610).exists()
    kept = home_client.post("/admin/downloads/upload", data={"csrf": csrf}, files={
        "apk": ("ThomasCreekTV.apk", apk), "info": ("update.json", json.dumps(info).encode()),
    })
    assert "Saved 1.5.0" in kept.text and hub.tv_app.apk_path(610).read_bytes() == apk

    other = make_apk(CERT_B)
    wrong = home_client.post("/admin/downloads/upload", data={"csrf": csrf}, files={
        "apk": ("ThomasCreekTV.apk", other),
        "info": ("update.json", json.dumps(_info(other, 611, "other")).encode()),
    })
    assert "different key" in wrong.text and not hub.tv_app.apk_path(611).exists()

    inbox = hub.tv_app.dir / "inbox"
    inbox.mkdir()
    dropped = make_apk(CERT_A)
    (inbox / "ThomasCreekTV.apk").write_bytes(dropped)
    (inbox / "update.json").write_text(json.dumps(_info(dropped, 612, "1.5.2")))
    page = home_client.get("/admin/downloads").text
    assert "1.5.2" in page and not (inbox / "ThomasCreekTV.apk").exists()
    assert home_client.get("/tv", follow_redirects=False).headers["location"] == "/tv/612"
