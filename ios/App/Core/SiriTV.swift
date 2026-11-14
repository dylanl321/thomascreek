import AppIntents
import Foundation

/// "Play Bluey in the living room", "Move the TV to the bedroom", "Turn off the TV in the kitchen".
/// Each one is a single phrase. The hub already knows the rooms and what's on.

struct PlayInRoomIntent: AppIntent {
    static var title: LocalizedStringResource = "Play in a Room"
    static var description = IntentDescription("Play a show, movie, or channel on the TV in a room.")
    static var openAppWhenRun = false

    @Parameter(title: "Show") var title: String
    @Parameter(title: "Room") var room: String

    static var parameterSummary: some ParameterSummary { Summary("Play \(\.$title) in \(\.$room)") }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        do {
            let spoken = title.trimmingCharacters(in: .whitespacesAndNewlines)
            guard spoken.count >= 2 else { return .result(dialog: "Tell me what to play.") }
            guard let choice = try await SiriTV.find(spoken) else {
                return .result(dialog: "I couldn't find \(spoken).")
            }
            let tvs = try await SiriTV.devices()
            guard let tv = SiriTV.destination(room, among: tvs) else {
                return .result(dialog: tvs.isEmpty ? "There isn't a TV set up yet." : "There isn't a TV in \(room).")
            }
            let reply = try await HubClient.shared.action("tv.play", target: tv.id, params: choice.params)
            let line = reply.message ?? "Playing \(choice.title) on \(tv.name)."
            return .result(dialog: "\(line)")
        } catch {
            return .result(dialog: "\(SiriTV.spoken(error))")
        }
    }
}

struct MoveTVIntent: AppIntent {
    static var title: LocalizedStringResource = "Move the TV to a Room"
    static var description = IntentDescription("Keep watching on the TV in another room, from the same moment.")
    static var openAppWhenRun = false

    @Parameter(title: "Room") var room: String

    static var parameterSummary: some ParameterSummary { Summary("Move the TV to \(\.$room)") }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        do {
            let tvs = try await SiriTV.devices()
            guard let dest = SiriTV.destination(room, among: tvs) else {
                return .result(dialog: tvs.isEmpty ? "There isn't a TV set up yet." : "There isn't a TV in \(room).")
            }
            guard let source = tvs.first(where: { $0.id != dest.id && $0.playing != nil }) else {
                if tvs.contains(where: { $0.id == dest.id && $0.playing != nil }) {
                    return .result(dialog: "It's already on in \(dest.name).")
                }
                return .result(dialog: "Nothing is playing.")
            }
            let reply: TVHandoffReply = try await HubClient.shared.post("/v1/tv/handoff", [
                "from_device": source.id, "to_device": dest.id,
            ])
            return .result(dialog: "\(reply.message ?? "Moved it to \(dest.name).")")
        } catch {
            return .result(dialog: "\(SiriTV.spoken(error))")
        }
    }
}

struct TurnOffTVIntent: AppIntent {
    static var title: LocalizedStringResource = "Turn Off the TV in a Room"
    static var description = IntentDescription("Turn off the TV in a room.")
    static var openAppWhenRun = false

    @Parameter(title: "Room") var room: String

    static var parameterSummary: some ParameterSummary { Summary("Turn off the TV in \(\.$room)") }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        do {
            let tvs = try await SiriTV.devices()
            guard let dest = SiriTV.destination(room, among: tvs) else {
                return .result(dialog: tvs.isEmpty ? "There isn't a TV set up yet." : "There isn't a TV in \(room).")
            }
            _ = try await HubClient.shared.action("tv.stop", target: dest.id)
            return .result(dialog: "Turned off \(dest.name).")
        } catch {
            return .result(dialog: "\(SiriTV.spoken(error))")
        }
    }
}

enum SiriTV {
    struct Choice {
        var title: String
        var params: [String: Any]
    }

    static func devices() async throws -> [TVPlayer] {
        try await HubClient.shared.get("/v1/tv/devices", as: TVDeviceList.self).devices
    }

    /// A room name, a TV name, or the only TV in the house.
    static func destination(_ spoken: String, among tvs: [TVPlayer]) -> TVPlayer? {
        let q = words(spoken)
        if q.isEmpty { return tvs.count == 1 ? tvs.first : tvs.first { $0.usual == true } ?? (tvs.count == 1 ? tvs.first : nil) }
        let hits = tvs.filter { matches($0, q) }
        if hits.count == 1 { return hits[0] }
        return hits.first { $0.playing != nil } ?? hits.first
    }

    /// `/v1/search` first. A channel, programme, or movie the house search doesn't know falls through to the TV library.
    static func find(_ query: String) async throws -> Choice? {
        if let found = try? await HubClient.shared.get("/v1/search", ["q": query], as: SearchResults.self) {
            if let top = found.top, let action = top.action, action.action == "tv.play" {
                return Choice(title: top.title, params: action.body)
            }
            for group in found.groups {
                if let item = group.items.first(where: { $0.action?.action == "tv.play" }), let action = item.action {
                    return Choice(title: item.title, params: action.body)
                }
            }
        }
        guard query.trimmingCharacters(in: .whitespacesAndNewlines).count >= 2,
              let library = try? await HubClient.shared.get("/v1/tv/library/search", ["q": query], as: TVLibrarySearch.self)
        else { return nil }
        let needle = words(query)
        if let channel = library.channels?.first(where: { words($0.name) == needle }) ?? library.channels?.first {
            return Choice(title: channel.name, params: ["channel_key": channel.key, "title": channel.name])
        }
        if let show = library.programmes?.first, let key = show.channelKey {
            return Choice(title: show.title, params: ["channel_key": key, "title": show.title])
        }
        if let movie = library.movies?.first {
            return Choice(title: movie.name, params: ["movie_id": movie.id, "title": movie.name])
        }
        return nil
    }

    static func spoken(_ error: Error) -> String {
        if let hub = error as? HubClient.HubError, let line = hub.errorDescription, !line.isEmpty { return line }
        return "That didn't work. Try again in a moment."
    }

    private static func matches(_ tv: TVPlayer, _ q: String) -> Bool {
        let name = tv.name.lowercased()
        let room = (tv.room ?? "").replacingOccurrences(of: "_", with: " ")
        let roomName = (tv.roomName ?? "").lowercased()
        return name == q || room == q || roomName == q || name.contains(q) || (!room.isEmpty && (room.contains(q) || q.contains(room)))
            || (!roomName.isEmpty && (roomName.contains(q) || q.contains(roomName)))
    }

    private static func words(_ spoken: String) -> String {
        var text = spoken.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        for prefix in ["the ", "our "] where text.hasPrefix(prefix) {
            text.removeFirst(prefix.count)
        }
        return text
    }
}
