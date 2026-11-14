import Foundation

/// Zero-setup TV pairing, as `server/docs/API.md` describes it.
/// A new TV asks once. An admin phone hears one event and one push, then approves with one tap.

public enum TVPairJSON {
    public static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    public static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()
}

public enum TVPairPath {
    public static let list = "/v1/tv/pair"
    public static func approve(_ pairID: String) -> String { "/v1/tv/pair/\(pairID)/approve" }
    public static func deny(_ pairID: String) -> String { "/v1/tv/pair/\(pairID)/deny" }
}

/// `GET /v1/tv/pair` with an admin bearer.
public struct TVPairList: Decodable, Equatable, Sendable {
    public var pending: [TVPairAsk]
    public init(pending: [TVPairAsk]) { self.pending = pending }
}

public struct TVPairAsk: Decodable, Equatable, Identifiable, Sendable {
    public var pairId: String
    public var installId: String
    public var name: String?
    public var model: String?
    public var suggestedRoom: String?
    public var suggestedRoomName: String?
    public var expiresAt: String?
    public var id: String { pairId }

    public init(pairId: String, installId: String, name: String?, model: String?, suggestedRoom: String?,
                suggestedRoomName: String?, expiresAt: String?) {
        self.pairId = pairId
        self.installId = installId
        self.name = name
        self.model = model
        self.suggestedRoom = suggestedRoom
        self.suggestedRoomName = suggestedRoomName
        self.expiresAt = expiresAt
    }
}

/// Admin phone SSE `{"type":"tv_pair","data":{...}}`. One event per new TV.
public struct TVPairEvent: Decodable, Equatable, Sendable {
    public var pairId: String
    public var installId: String
    public var name: String?
    public var model: String?
    public var suggestedRoom: String?
    public var status: String
}

/// Approve and deny both answer 200. A late tap is `ok: false`, `status: "expired"`, and is not an error.
public struct TVPairReply: Decodable, Equatable, Sendable {
    public var ok: Bool
    public var status: String

    public var expired: Bool { status == "expired" || ok == false }

    /// A sentence for a late tap. Nil when the tap did what it said.
    public var calmLine: String? {
        expired ? "That TV isn't waiting anymore." : nil
    }
}

public struct TVPairRoom: Equatable, Sendable {
    public var id: String
    public var name: String
    public init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

public enum TVPairCopy {
    /// "Living room TV wants to join". The hub uses the same sentence as the push title.
    public static func title(name: String?) -> String {
        let trimmed = name?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if trimmed.isEmpty { return "A TV wants to join" }
        return "\(trimmed) wants to join"
    }

    /// The room the banner shows as already chosen. An override wins; otherwise the hub's suggestion.
    public static func selectedRoom(suggested: String?, chosen: String?) -> String? {
        if let chosen, !chosen.isEmpty { return chosen }
        if let suggested, !suggested.isEmpty { return suggested }
        return nil
    }

    public static func roomLabel(selected: String?, suggested: String?, suggestedName: String?, rooms: [TVPairRoom]) -> String {
        if let selected, let match = rooms.first(where: { $0.id == selected }) { return match.name }
        if selected == nil || selected == suggested, let suggestedName, !suggestedName.isEmpty { return suggestedName }
        if let selected, !selected.isEmpty { return selected.replacingOccurrences(of: "_", with: " ") }
        return "This room"
    }
}

/// `POST /v1/tv/pair/{pair_id}/approve` body. `room` is an area id. Omit it to keep the suggestion.
public struct TVPairApproveBody: Encodable, Equatable, Sendable {
    public var room: String?
    public var profile: String?
    public var name: String?

    public init(room: String? = nil, profile: String? = nil, name: String? = nil) {
        self.room = room
        self.profile = profile
        self.name = name
    }

    public func json() throws -> [String: String] {
        let data = try TVPairJSON.encoder.encode(self)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: String]
        return object ?? [:]
    }
}

public enum TVPairPush {
    public static let category = "TV_PAIR"
    public static let approve = "TV_PAIR_APPROVE"
    public static let notNow = "TV_PAIR_NOT_NOW"
    public static let ref = "thomascreek://tv"

    /// The hub's collapse id is `tv-pair:{pair_id}` and arrives as `aps.thread-id`.
    public static func pairID(thread: String?) -> String? {
        guard let thread, thread.hasPrefix("tv-pair:") else { return nil }
        let id = String(thread.dropFirst("tv-pair:".count))
        guard id.hasPrefix("pair_"), id.count > "pair_".count else { return nil }
        return id
    }

    public static func pairID(userInfo: [AnyHashable: Any]) -> String? {
        if let id = userInfo["pair_id"] as? String, id.hasPrefix("pair_") { return id }
        let aps = userInfo["aps"] as? [AnyHashable: Any]
        let thread = (aps?["thread-id"] as? String) ?? (userInfo["thread-id"] as? String)
        return pairID(thread: thread)
    }

    public static func isPairing(title: String, userInfo: [AnyHashable: Any]) -> Bool {
        if userInfo["ref"] as? String == ref { return true }
        if title.hasSuffix(" wants to join"), userInfo["cat"] as? String == "systems" { return true }
        return false
    }
}

public extension TVPairAsk {
    /// An SSE event has no room name yet. The list fills that in a moment later.
    init(_ event: TVPairEvent) {
        self.init(pairId: event.pairId, installId: event.installId, name: event.name, model: event.model,
                  suggestedRoom: event.suggestedRoom, suggestedRoomName: nil, expiresAt: nil)
    }
}
