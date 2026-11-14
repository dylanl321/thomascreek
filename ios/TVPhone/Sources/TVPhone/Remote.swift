import Foundation

/// The iPhone remote, as `server/docs/API.md` describes it.
/// Cards come from `GET /v1/tv/devices`. Buttons are `POST /v1/tv/remote`.
/// Moving a show is still `POST /v1/tv/handoff`.

public enum TVRemotePath {
    public static let devices = "/v1/tv/devices"
    public static let remote = "/v1/tv/remote"
    public static let handoff = "/v1/tv/handoff"
}

public struct HouseDeviceList: Decodable, Equatable, Sendable {
    public var devices: [HouseDevice]
    public init(devices: [HouseDevice]) { self.devices = devices }
}

public struct HouseDevice: Decodable, Equatable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var room: String?
    public var online: Bool?
    public var usual: Bool?
    public var mediaEntity: String?
    public var playing: HousePlaying?
    public init(id: String, name: String, room: String?, online: Bool?, usual: Bool?, mediaEntity: String?, playing: HousePlaying?) {
        self.id = id
        self.name = name
        self.room = room
        self.online = online
        self.usual = usual
        self.mediaEntity = mediaEntity
        self.playing = playing
    }
}

public struct HousePlaying: Decodable, Equatable, Sendable {
    public var channelKey: String?
    public var title: String?
    public var name: String?
    public var number: Int?
    public var positionS: Double?
    public var posterUrl: String?
    public init(channelKey: String?, title: String?, name: String?, number: Int?, positionS: Double?, posterUrl: String?) {
        self.channelKey = channelKey
        self.title = title
        self.name = name
        self.number = number
        self.positionS = positionS
        self.posterUrl = posterUrl
    }
}

public struct TVHandoffBody: Encodable, Equatable, Sendable {
    public var fromDevice: String
    public var toDevice: String
    public init(fromDevice: String, toDevice: String) {
        self.fromDevice = fromDevice
        self.toDevice = toDevice
    }

    public func json() throws -> [String: Any] {
        let data = try TVPairJSON.encoder.encode(self)
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
}

public struct HouseHandoffReply: Decodable, Equatable, Sendable {
    public var ok: Bool
    public var moved: Bool?
    public var message: String?
    public var from: String?
    public var to: String?
    public var dryRun: Bool?
    public var commandId: String?
    public var stopCommandId: String?

    /// Nothing moved. The hub's `message` is the sentence to show.
    public var stayed: Bool { ok == false || moved == false || dryRun == true }
}

public struct TVRemoteBody: Encodable, Equatable, Sendable {
    public var device: String
    public var command: String
    public var positionS: Int?
    public var channelKey: String?
    public var text: String?

    public static func play(_ device: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "play", positionS: nil, channelKey: nil, text: nil)
    }
    public static func pause(_ device: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "pause", positionS: nil, channelKey: nil, text: nil)
    }
    /// Whole seconds, 0 to 86400.
    public static func seek(_ device: String, position: Double) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "seek", positionS: clamped(position), channelKey: nil, text: nil)
    }
    public static func channelUp(_ device: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "channel_up", positionS: nil, channelKey: nil, text: nil)
    }
    public static func channelDown(_ device: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "channel_down", positionS: nil, channelKey: nil, text: nil)
    }
    public static func tune(_ device: String, channelKey: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "tune", positionS: nil, channelKey: channelKey, text: nil)
    }
    /// Empty text is not a command. Longer text is cut at 80 characters.
    public static func type(_ device: String, _ text: String) -> TVRemoteBody? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return TVRemoteBody(device: device, command: "text", positionS: nil, channelKey: nil, text: String(trimmed.prefix(80)))
    }
    public static func back(_ device: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "back", positionS: nil, channelKey: nil, text: nil)
    }
    public static func home(_ device: String) -> TVRemoteBody {
        TVRemoteBody(device: device, command: "home", positionS: nil, channelKey: nil, text: nil)
    }

    public static func clamped(_ position: Double) -> Int {
        Int(min(86400, max(0, position)).rounded())
    }

    public func json() throws -> [String: Any] {
        let data = try TVPairJSON.encoder.encode(self)
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
}

public struct TVRemoteReply: Decodable, Equatable, Sendable {
    public var ok: Bool
    public var id: String?
    public var message: String?

    /// A sentence when the hub did not send the button. Nil when it did.
    public var calmLine: String? { ok ? nil : (message?.isEmpty == false ? message : "That didn't go through.") }
}

/// What the card says, and how far the scrub bar can move. `position_s` is the playhead.
public enum TVRemoteCopy {
    public static func title(_ playing: HousePlaying?) -> String {
        if let title = playing?.title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty { return title }
        if let name = playing?.name?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty { return name }
        return "Nothing on"
    }

    public static func status(online: Bool?, playing: HousePlaying?) -> String? {
        if online == false, playing == nil { return "Asleep" }
        return nil
    }

    /// The playhead, and the end of the bar. A day is as far as a seek can go.
    public static func scrub(position: Double?) -> (value: Double, upper: Double) {
        let value = min(86400, max(0, position ?? 0))
        let upper = min(86400, max(value, 3600))
        return (value, upper)
    }

    public static func clock(_ seconds: Double) -> String {
        let whole = TVRemoteBody.clamped(seconds)
        return String(format: "%d:%02d", whole / 60, whole % 60)
    }
}

public extension HouseDevice {
    /// The show moves to `destination` before the hub answers. Same room, or nothing on, stays put.
    func handing(to destination: HouseDevice) -> (HouseDevice, HouseDevice)? {
        guard id != destination.id, playing != nil else { return nil }
        var source = self
        var dest = destination
        dest.playing = playing
        source.playing = nil
        return (source, dest)
    }
}
