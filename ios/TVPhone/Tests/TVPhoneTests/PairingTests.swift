import XCTest
@testable import TVPhone

/// Verbatim hub JSON from `server/docs/API.md`, Zero-setup pairing (contract commit c0e44b5).
final class PairingTests: XCTestCase {
    func testSSEEvent() throws {
        let json = """
        {"type":"tv_pair","data":{"pair_id":"pair_ab12cd34ef567890abcd","install_id":"tv_office_0123456789","name":"Living room TV","model":"onn 4K","suggested_room":"living_room","status":"pending"}}
        """
        let event = try TVPairJSON.decoder.decode(TVPairEnvelope.self, from: Data(json.utf8))
        XCTAssertEqual(event.type, "tv_pair")
        XCTAssertEqual(event.data.pairId, "pair_ab12cd34ef567890abcd")
        XCTAssertEqual(event.data.installId, "tv_office_0123456789")
        XCTAssertEqual(event.data.name, "Living room TV")
        XCTAssertEqual(event.data.model, "onn 4K")
        XCTAssertEqual(event.data.suggestedRoom, "living_room")
        XCTAssertEqual(event.data.status, "pending")
        XCTAssertEqual(TVPairCopy.title(name: event.data.name), "Living room TV wants to join")
    }

    func testPendingList() throws {
        let json = """
        {"pending":[{"pair_id":"pair_ab12cd34ef567890abcd","install_id":"tv_office_0123456789","name":"Living room TV","model":"onn 4K","suggested_room":"living_room","suggested_room_name":"Living Room","expires_at":"2026-11-14T01:15:00+00:00"}]}
        """
        let list = try TVPairJSON.decoder.decode(TVPairList.self, from: Data(json.utf8))
        let ask = try XCTUnwrap(list.pending.first)
        XCTAssertEqual(ask.pairId, "pair_ab12cd34ef567890abcd")
        XCTAssertEqual(ask.installId, "tv_office_0123456789")
        XCTAssertEqual(ask.name, "Living room TV")
        XCTAssertEqual(ask.model, "onn 4K")
        XCTAssertEqual(ask.suggestedRoom, "living_room")
        XCTAssertEqual(ask.suggestedRoomName, "Living Room")
        XCTAssertEqual(ask.expiresAt, "2026-11-14T01:15:00+00:00")
        XCTAssertEqual(TVPairCopy.title(name: ask.name), "Living room TV wants to join")
        XCTAssertEqual(TVPairCopy.selectedRoom(suggested: ask.suggestedRoom, chosen: nil), "living_room")
        XCTAssertEqual(TVPairCopy.roomLabel(selected: "living_room", suggested: ask.suggestedRoom, suggestedName: ask.suggestedRoomName, rooms: []), "Living Room")
        let rooms = [TVPairRoom(id: "living_room", name: "Living Room"), TVPairRoom(id: "bedroom", name: "Bedroom")]
        XCTAssertEqual(TVPairCopy.selectedRoom(suggested: ask.suggestedRoom, chosen: "bedroom"), "bedroom")
        XCTAssertEqual(TVPairCopy.roomLabel(selected: "bedroom", suggested: ask.suggestedRoom, suggestedName: ask.suggestedRoomName, rooms: rooms), "Bedroom")
        XCTAssertEqual(TVPairCopy.roomLabel(selected: "bedroom", suggested: ask.suggestedRoom, suggestedName: ask.suggestedRoomName, rooms: []), "bedroom")
    }

    func testApproveAndDenyReplies() throws {
        let approved = try TVPairJSON.decoder.decode(TVPairReply.self, from: Data(#"{"ok":true,"status":"approved"}"#.utf8))
        XCTAssertEqual(approved.ok, true)
        XCTAssertEqual(approved.status, "approved")
        XCTAssertNil(approved.calmLine)
        XCTAssertFalse(approved.expired)

        let denied = try TVPairJSON.decoder.decode(TVPairReply.self, from: Data(#"{"ok":true,"status":"denied"}"#.utf8))
        XCTAssertEqual(denied.status, "denied")
        XCTAssertNil(denied.calmLine)

        let late = try TVPairJSON.decoder.decode(TVPairReply.self, from: Data(#"{"ok":false,"status":"expired"}"#.utf8))
        XCTAssertEqual(late.ok, false)
        XCTAssertEqual(late.status, "expired")
        XCTAssertTrue(late.expired)
        XCTAssertEqual(late.calmLine, "That TV isn't waiting anymore.")
    }

    func testApproveBodyOverridesTheRoom() throws {
        let id = "pair_ab12cd34ef567890abcd"
        XCTAssertEqual(TVPairPath.list, "/v1/tv/pair")
        XCTAssertEqual(TVPairPath.approve(id), "/v1/tv/pair/pair_ab12cd34ef567890abcd/approve")
        XCTAssertEqual(TVPairPath.deny(id), "/v1/tv/pair/pair_ab12cd34ef567890abcd/deny")
        XCTAssertEqual(try TVPairApproveBody().json(), [:])
        XCTAssertEqual(try TVPairApproveBody(room: "bedroom").json(), ["room": "bedroom"])
        XCTAssertEqual(try TVPairApproveBody(room: "living_room", profile: "kids", name: "Living room TV").json(),
                       ["name": "Living room TV", "profile": "kids", "room": "living_room"])
    }

    func testPushThreadCarriesThePairID() {
        let thread = "tv-pair:pair_ab12cd34ef567890abcd"
        XCTAssertEqual(TVPairPush.pairID(thread: thread), "pair_ab12cd34ef567890abcd")
        let info: [AnyHashable: Any] = [
            "cat": "systems",
            "ref": "thomascreek://tv",
            "aps": ["thread-id": thread, "alert": ["title": "Living room TV wants to join"]],
        ]
        XCTAssertTrue(TVPairPush.isPairing(title: "Living room TV wants to join", userInfo: info))
        XCTAssertEqual(TVPairPush.pairID(userInfo: info), "pair_ab12cd34ef567890abcd")
        XCTAssertFalse(TVPairPush.isPairing(title: "Freezer is warm", userInfo: ["cat": "house"]))
    }
}

private struct TVPairEnvelope: Decodable {
    var type: String
    var data: TVPairEvent
}
