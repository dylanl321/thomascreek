"""#212: parent approvals from the family board, answered from the iPhone by adults only."""

from __future__ import annotations

import asyncio
import json

import httpx
import respx

from hub import familyapprovals

BOARD = "http://fcc.test"
SNAP = {
    "people": [{"id": "theo-b", "name": "Theo B"}],
    "items": [{"id": "dishes", "title": "Empty the dishwasher", "points": 5}],
    "rewards": [{"id": "ice", "title": "Ice cream", "cost": 20}],
    "reports": [
        {"id": "rep1", "item_id": "dishes", "person_id": "theo-b", "date": "2026-11-17", "status": "pending",
         "note": "all of it", "created_at": "2026-11-17T18:00:00Z"},
        {"id": "rep0", "item_id": "dishes", "person_id": "theo-b", "status": "approved"},
    ],
    "redemptions": [{"id": "red1", "reward_id": "ice", "person_id": "theo-b", "state": "pending", "cost": 20,
                     "created_at": "2026-11-17T18:05:00Z"}],
}


def _board(hub, people, key: bool = True):
    hub.config.family_board.enabled = True
    hub.config.family_board.url = BOARD
    hub.db.update("people", "id", people["theo"]["id"], {"family_board_id": "theo-b"})
    if key:
        hub.vault.set("family_board.admin_token", "board-pin-123")


def test_adults_list_and_answer_approvals_and_a_child_is_refused(hub, people, home_client):
    _board(hub, people)
    dylan, theo = people["phones"]["Dylan"], people["phones"]["Theo"]
    with respx.mock(assert_all_called=False) as mock:
        admin = mock.get(f"{BOARD}/api/admin").mock(return_value=httpx.Response(200, json=SNAP))
        approve = mock.post(f"{BOARD}/api/completions/rep1/approve").mock(return_value=httpx.Response(200, json={}))
        decline = mock.post(f"{BOARD}/api/redemptions/red1/review").mock(return_value=httpx.Response(200, json={}))

        got = home_client.get("/v1/family/approvals", headers=dylan.h)
        assert got.status_code == 200, got.text
        items = got.json()["items"]
        assert [(i["kind"], i["id"], i["person"], i["title"]) for i in items] == [
            ("chore", "rep1", "Theo", "Empty the dishwasher"), ("reward", "red1", "Theo", "Ice cream")]
        assert admin.calls[0].request.headers["X-Admin-Token"] == "board-pin-123"
        assert "board-pin-123" not in got.text

        # A child can neither see nor answer them.
        assert home_client.get("/v1/family/approvals", headers=theo.h).status_code == 403
        assert home_client.post("/v1/family/approvals/chore/rep1", headers=theo.h,
                                json={"approve": True}).status_code == 403
        assert not approve.called

        # Practice mode first, then live.
        assert home_client.post("/v1/family/approvals/chore/rep1", headers=dylan.h,
                                json={"approve": True}).json()["dry_run"] is True
        assert not approve.called
        hub.config.hub.live_actions = ["chores.approve"]
        assert home_client.post("/v1/family/approvals/chore/rep1", headers=dylan.h,
                                json={"approve": True}).json() == {"ok": True}
        assert approve.called
        assert home_client.post("/v1/family/approvals/reward/red1", headers=dylan.h,
                                json={"approve": False}).json() == {"ok": True}
        assert json.loads(decline.calls[0].request.content) == {"approved": False}
        assert home_client.post("/v1/family/approvals/other/x", headers=dylan.h,
                                json={"approve": True}).status_code == 404


def test_without_the_board_key_the_app_is_told_what_to_do(hub, people, home_client):
    _board(hub, people, key=False)
    dylan = people["phones"]["Dylan"]
    r = home_client.get("/v1/family/approvals", headers=dylan.h)
    assert r.status_code == 409 and r.json()["error"] == "board_key_missing"
    assert "admin page" in r.json()["message"]


def test_a_new_approval_pushes_the_adults_and_approve_works_from_the_notification(hub, people, home_client):
    _board(hub, people)
    hub.config.hub.live_actions = ["chores.approve"]
    sent = []
    orig = hub.notify.send

    async def send(**kw):
        sent.append(kw)
        return await orig(**kw)
    hub.notify.send = send  # type: ignore[method-assign]
    first = {**SNAP, "reports": [], "redemptions": []}
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{BOARD}/api/admin").mock(return_value=httpx.Response(200, json=first))
        approve = mock.post(f"{BOARD}/api/completions/rep1/approve").mock(return_value=httpx.Response(200, json={}))
        # The first look only remembers what is waiting.
        assert asyncio.run(familyapprovals.check(hub)) == 0
        route.mock(return_value=httpx.Response(200, json={**SNAP, "redemptions": []}))
        assert asyncio.run(familyapprovals.check(hub)) == 1
        assert asyncio.run(familyapprovals.check(hub)) == 0  # not twice
        push = sent[0]
        assert push["push_category"] == "APPROVAL" and push["roles"] == ("admin", "adult")
        assert push["title"] == "Theo says they did Empty the dishwasher"
        assert [a["id"] for a in push["actions"]] == ["approve", "deny"]

        # The kids get nothing; Dylan answers from the notification's Approve button.
        assert not hub.db.all("SELECT * FROM inbox WHERE person_id=?", [people["theo"]["id"]])
        dylan = people["phones"]["Dylan"]
        row = hub.db.one("SELECT * FROM inbox WHERE person_id=? AND group_key=?", [people["dylan"]["id"], "board:chore:rep1"])
        r = home_client.post(f"/v1/inbox/{row['id']}/act", headers=dylan.h, json={"action": "approve"})
        assert r.status_code == 200, r.text
        assert approve.called
        avery_row = hub.db.one("SELECT * FROM inbox WHERE person_id=? AND group_key=?",
                               [people["avery"]["id"], "board:chore:rep1"])
        assert avery_row["acked_at"]  # answered once clears it for every adult

        # Answered on the board itself: the notice clears at the next look.
        route.mock(return_value=httpx.Response(200, json=first))
        asyncio.run(familyapprovals.check(hub))
