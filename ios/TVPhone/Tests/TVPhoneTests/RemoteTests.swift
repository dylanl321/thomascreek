import XCTest
@testable import TVPhone

/// Verbatim hub JSON from `server/docs/API.md`, iPhone remote (PR #108).
final class RemoteTests: XCTestCase {
    func testDevices() throws {
        let json = """
        {"devices":[
          {"id":"tv_bed_0123456789abcd","name":"Bedroom","room":"bedroom","online":true,"usual":true,
           "playing":null},
          {"id":"tv_office_0123456789","name":"Living room","room":"living_room","online":true,
           "media_entity":"media_player.living",
           "playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20,"position_s":125,"poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f"}}
        ]}
        """
        let list = try TVPairJSON.decoder.decode(HouseDeviceList.self, from: Data(json.utf8))
        XCTAssertEqual(list.devices.count, 2)
        let bed = list.devices[0]
        XCTAssertEqual(bed.id, "tv_bed_0123456789abcd")
        XCTAssertEqual(bed.name, "Bedroom")
        XCTAssertEqual(bed.room, "bedroom")
        XCTAssertEqual(bed.online, true)
        XCTAssertEqual(bed.usual, true)
        XCTAssertNil(bed.playing)
        XCTAssertEqual(TVRemoteCopy.title(bed.playing), "Nothing on")
        XCTAssertNil(TVRemoteCopy.status(online: bed.online, playing: bed.playing))

        let living = list.devices[1]
        XCTAssertEqual(living.mediaEntity, "media_player.living")
        XCTAssertEqual(living.playing?.channelKey, "xc:thomas-creek:10")
        XCTAssertEqual(living.playing?.title, "Newsroom")
        XCTAssertEqual(living.playing?.name, "CNN")
        XCTAssertEqual(living.playing?.number, 20)
        XCTAssertEqual(living.playing?.positionS, 125)
        XCTAssertEqual(living.playing?.posterUrl, "/v1/art/poster/prog:0b49a5b26ac1e2d3739f")
        XCTAssertEqual(TVRemoteCopy.title(living.playing), "Newsroom")
        let scrub = TVRemoteCopy.scrub(position: living.playing?.positionS)
        XCTAssertEqual(scrub.value, 125)
        XCTAssertEqual(scrub.upper, 3600)
        XCTAssertEqual(TVRemoteCopy.clock(125), "2:05")

        let moved = try XCTUnwrap(living.handing(to: bed))
        XCTAssertNil(moved.0.playing)
        XCTAssertEqual(moved.1.playing?.title, "Newsroom")
        XCTAssertEqual(moved.1.playing?.positionS, 125)
        XCTAssertNil(bed.handing(to: living))
    }

    func testAsleepCard() {
        XCTAssertEqual(TVRemoteCopy.status(online: false, playing: nil), "Asleep")
        XCTAssertNil(TVRemoteCopy.status(online: false, playing: HousePlaying(channelKey: nil, title: "Newsroom", name: nil, number: nil, positionS: nil, posterUrl: nil)))
    }

    func testHandoff() throws {
        let still = """
        {"ok":false,"moved":false,"message":"Nothing is playing on Living room","from":"tv_office_0123456789","to":"tv_bed_0123456789abcd"}
        """
        let stayed = try TVPairJSON.decoder.decode(HouseHandoffReply.self, from: Data(still.utf8))
        XCTAssertTrue(stayed.stayed)
        XCTAssertEqual(stayed.message, "Nothing is playing on Living room")

        let moved = """
        {"ok":true,"moved":true,"message":"Moved Newsroom to Bedroom","from":"tv_office_0123456789","to":"tv_bed_0123456789abcd","dry_run":false,"command_id":"tvc_ab12","stop_command_id":"tvc_ab13"}
        """
        let reply = try TVPairJSON.decoder.decode(HouseHandoffReply.self, from: Data(moved.utf8))
        XCTAssertEqual(reply.ok, true)
        XCTAssertEqual(reply.moved, true)
        XCTAssertEqual(reply.dryRun, false)
        XCTAssertEqual(reply.commandId, "tvc_ab12")
        XCTAssertEqual(reply.stopCommandId, "tvc_ab13")
        XCTAssertFalse(reply.stayed)
        XCTAssertEqual(reply.message, "Moved Newsroom to Bedroom")

        let body = try json(TVHandoffBody(fromDevice: "tv_office_0123456789", toDevice: "tv_bed_0123456789abcd"))
        XCTAssertEqual(body["from_device"] as? String, "tv_office_0123456789")
        XCTAssertEqual(body["to_device"] as? String, "tv_bed_0123456789abcd")
        XCTAssertEqual(body.count, 2)
    }

    func testRemoteButtons() throws {
        let pause = try json(TVRemoteBody.pause("tv_office_0123456789"))
        XCTAssertEqual(pause["device"] as? String, "tv_office_0123456789")
        XCTAssertEqual(pause["command"] as? String, "pause")
        XCTAssertEqual(pause.count, 2)

        XCTAssertEqual(try json(TVRemoteBody.play("tv_office_0123456789"))["command"] as? String, "play")
        let seek = try json(TVRemoteBody.seek("tv_office_0123456789", position: 125))
        XCTAssertEqual(seek["command"] as? String, "seek")
        XCTAssertEqual(number(seek["position_s"]), 125)
        XCTAssertEqual(number(try json(TVRemoteBody.seek("tv", position: -4))["position_s"]), 0)
        XCTAssertEqual(number(try json(TVRemoteBody.seek("tv", position: 90000))["position_s"]), 86400)

        XCTAssertEqual(try json(TVRemoteBody.channelUp("tv_office_0123456789"))["command"] as? String, "channel_up")
        XCTAssertEqual(try json(TVRemoteBody.channelDown("tv_office_0123456789"))["command"] as? String, "channel_down")
        XCTAssertEqual(try json(TVRemoteBody.back("tv_office_0123456789"))["command"] as? String, "back")
        XCTAssertEqual(try json(TVRemoteBody.home("tv_office_0123456789"))["command"] as? String, "home")

        let tune = try json(TVRemoteBody.tune("tv_office_0123456789", channelKey: "xc:thomas-creek:10"))
        XCTAssertEqual(tune["command"] as? String, "tune")
        XCTAssertEqual(tune["channel_key"] as? String, "xc:thomas-creek:10")

        let typed = try XCTUnwrap(TVRemoteBody.type("tv_office_0123456789", " news "))
        XCTAssertEqual(try json(typed)["text"] as? String, "news")
        XCTAssertEqual(try json(typed)["command"] as? String, "text")
        XCTAssertNil(TVRemoteBody.type("tv_office_0123456789", "   "))
        let long = String(repeating: "a", count: 90)
        XCTAssertEqual(try XCTUnwrap(TVRemoteBody.type("tv", long)).text?.count, 80)

        let sent = try TVPairJSON.decoder.decode(TVRemoteReply.self, from: Data(#"{"ok":true,"id":"tvr_ab12cd34ef567890abcd"}"#.utf8))
        XCTAssertEqual(sent.ok, true)
        XCTAssertEqual(sent.id, "tvr_ab12cd34ef567890abcd")
        XCTAssertNil(sent.calmLine)

        let pick = try TVPairJSON.decoder.decode(TVRemoteReply.self, from: Data(#"{"ok":false,"message":"Pick a room."}"#.utf8))
        XCTAssertEqual(pick.calmLine, "Pick a room.")
        let fast = try TVPairJSON.decoder.decode(TVRemoteReply.self, from: Data(#"{"ok":false,"message":"That's as fast as it goes."}"#.utf8))
        XCTAssertEqual(fast.calmLine, "That's as fast as it goes.")
    }

    func testRemoteEvent() throws {
        let json = """
        {"type":"tv_remote","data":{"id":"tvr_ab12cd34ef567890abcd","install_id":"tv_office_0123456789","command":"seek","position_s":125,"at":"2026-11-14T01:30:00+00:00"}}
        """
        let event = try TVPairJSON.decoder.decode(TVRemoteEvent.self, from: Data(json.utf8))
        XCTAssertEqual(event.type, "tv_remote")
        XCTAssertEqual(event.data.id, "tvr_ab12cd34ef567890abcd")
        XCTAssertEqual(event.data.installId, "tv_office_0123456789")
        XCTAssertEqual(event.data.command, "seek")
        XCTAssertEqual(event.data.positionS, 125)
        XCTAssertEqual(event.data.at, "2026-11-14T01:30:00+00:00")
    }

    private func json<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try TVPairJSON.encoder.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func number(_ value: Any?) -> Int? {
        if let int = value as? Int { return int }
        if let number = value as? NSNumber { return number.intValue }
        return nil
    }
}

private struct TVRemoteEvent: Decodable {
    var type: String
    var data: Payload
    struct Payload: Decodable {
        var id: String
        var installId: String
        var command: String
        var positionS: Double?
        var at: String?
    }
}
