import Foundation

/// One dollhouse the app can show. Adding a room is a folder plus a row in `Rooms/catalog.json`.
struct DollhouseRoom: Identifiable {
    var id: String
    var title: String
    var symbol: String
    var model: String
    var devicesName: String
    var mappingName: String
    var appearanceName: String
    var devices: DollhouseDevices
    var mapping: HubMapping
    var appearance: DollhouseAppearance
    var views: DollhouseViews?

    func presetIDs() -> [String] {
        let preferred = ["all_off", "all_on", "evening", "movie_night", "party", "bedtime", "reading", "laundry_running"]
        let extra = devices.presets.keys.filter { !preferred.contains($0) }.sorted()
        return preferred.filter { devices.presets[$0] != nil } + extra
    }

    /// Every device at a 0...1 level, the way devices.json writes a preset. A missing name is off.
    func preset(_ id: String) -> [String: Double] {
        let levels = devices.presets[id] ?? [:]
        return Dictionary(uniqueKeysWithValues: devices.devices.map { device in
            (device.name, min(1, max(0, levels[device.name] ?? 0)))
        })
    }

    func device(_ name: String) -> DollhouseDevice? { devices.devices.first { $0.name == name } }

    func entities(for name: String) -> [MappedEntity] { mapping.devices[name]?.entities ?? [] }

    func isConfigured(_ name: String) -> Bool {
        entities(for: name).contains { $0.id?.isEmpty == false }
    }

    func control(for name: String) -> DollhouseControl {
        if let raw = mapping.devices[name]?.control, let control = DollhouseControl(rawValue: raw) { return control }
        switch device(name)?.type {
        case .appliance: return .status
        case .media: return .media
        case .fireplace: return .switch
        case .fan: return .fan
        default: return .light
        }
    }

    static func presetTitle(_ id: String) -> String {
        switch id {
        case "all_off": "All off"
        case "all_on": "All on"
        case "evening": "Evening"
        case "movie_night": "Movie night"
        case "party": "Party"
        case "bedtime": "Bedtime"
        case "reading": "Reading"
        case "laundry_running": "Laundry running"
        default: id.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

struct DollhouseDevices: Decodable {
    var presets: [String: [String: Double]]
    var devices: [DollhouseDevice]
}

struct DollhouseDevice: Decodable, Identifiable {
    var name: String
    var label: String
    var type: DollhouseDeviceType
    var anchorNode: String
    var groupNode: String?
    var lightNodes: [String]
    var lightOnIntensity: [Double]
    var glowDecals: [String]
    var meshNodes: [String]
    var onState: OnState
    var id: String { name }

    struct OnState: Decodable { var shows: [String] }

    /// Names a tap on the model should treat as this device.
    var hitNames: [String] {
        meshNodes + glowDecals + lightNodes + onState.shows + [name, anchorNode] + (groupNode.map { [$0] } ?? [])
    }

    var symbol: String {
        switch name {
        case "soundbar": "hifispeaker.fill"
        case "washer": "washer.fill"
        case "dryer": "dryer.fill"
        default: type.symbol
        }
    }
}

enum DollhouseDeviceType: String, Decodable {
    case light, fireplace, media, appliance, fan, other
    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = DollhouseDeviceType(rawValue: raw) ?? .other
    }

    var symbol: String {
        switch self {
        case .light: "lightbulb.fill"
        case .fireplace: "flame.fill"
        case .media: "tv.fill"
        case .appliance: "washer.fill"
        case .fan: "fan.fill"
        case .other: "circle.grid.2x2"
        }
    }
}

struct HubMapping: Decodable {
    var roomId: String
    var title: String
    var climateEntityId: String?
    var devices: [String: MappedDevice]
    var relatedScenes: [String: String?]?
    var also: [AlsoControl]?
    var notInModel: [ExtraHubDevice]?
}

struct MappedDevice: Decodable {
    /// light, switch, media, select, or status. Empty means the page picks from the device type.
    var control: String?
    var note: String?
    var entities: [MappedEntity]
}

struct MappedEntity: Decodable, Hashable {
    /// Null until someone fills in the hub id. The control is already chosen.
    var id: String?
    var action: String
    var command: String?
    var options: [String]?
    var onOption: String?
    var offOption: String?
    var note: String?
}

/// A real hub device that is in the room but not in the cutaway.
struct AlsoControl: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var action: String
    var note: String?
}

struct ExtraHubDevice: Decodable, Hashable {
    var id: String
    var name: String
    var note: String?
}

enum DollhouseControl: String {
    case light, `switch`, media, select, status, fan
}

struct DollhouseAppearance: Decodable {
    var nodes: [String: LitNode]
    var lights: [String: LitLight]
}

struct LitNode: Decodable {
    var kind: String
    var baseSrgb: [Double]
    var opacity: Double
    var emissiveSrgb: [Double]
    var strength: Double
    var roughness: Double
    var metallic: Double
    var doubleSided: Bool
    var baseTexture: String?
    var emissiveTexture: String?
}

struct LitLight: Decodable {
    var type: String
    var colorSrgb: [Double]
    var intensity: Double
    var range: Double
    var innerDeg: Double?
    var outerDeg: Double?
}

struct DollhouseShotSpec: Decodable {
    var position: [Double]
    var target: [Double]
    var fov: Double
}

struct DollhouseViews: Decodable {
    var dollhouse: DollhouseShotSpec
    var close: DollhouseShotSpec
    var top: DollhouseShotSpec
}

enum DollhouseCatalog {
    static let rooms: [DollhouseRoom] = load()

    static func room(_ id: String) -> DollhouseRoom? { rooms.first { $0.id == id } }

    private struct Index: Decodable {
        struct Row: Decodable {
            var id: String
            var title: String
            var symbol: String
            var model: String
            var devices: String
            var mapping: String
            var appearance: String
            var views: DollhouseViews?
        }
        var rooms: [Row]
    }

    private static func load() -> [DollhouseRoom] {
        guard let url = DollhouseFiles.url("catalog.json"),
              let data = try? Data(contentsOf: url),
              let index = try? decoder.decode(Index.self, from: data) else { return [] }
        return index.rooms.compactMap { row in
            guard let devicesURL = DollhouseFiles.url(row.devices),
                  let mappingURL = DollhouseFiles.url(row.mapping),
                  let appearanceURL = DollhouseFiles.url(row.appearance),
                  let devices = try? decoder.decode(DollhouseDevices.self, from: Data(contentsOf: devicesURL)),
                  let mapping = try? decoder.decode(HubMapping.self, from: Data(contentsOf: mappingURL)),
                  let appearance = try? decoder.decode(DollhouseAppearance.self, from: Data(contentsOf: appearanceURL))
            else { return nil }
            return DollhouseRoom(id: row.id, title: row.title, symbol: row.symbol, model: row.model,
                                 devicesName: row.devices, mappingName: row.mapping, appearanceName: row.appearance,
                                 devices: devices, mapping: mapping, appearance: appearance, views: row.views)
        }
    }

    private static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }
}

enum DollhouseFiles {
    /// `relative` is a path inside `ios/Rooms`, such as `LivingRoom/room_corner.usdz`.
    /// XcodeGen copies that folder either as a folder reference (`Rooms/…` in the bundle) or flat.
    static func url(_ relative: String) -> URL? {
        if let root = Bundle.main.resourceURL {
            for prefix in ["Rooms/", ""] {
                let candidate = root.appendingPathComponent(prefix + relative)
                if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
            }
        }
        let ns = relative as NSString
        let ext = ns.pathExtension
        let base = ns.deletingPathExtension
        let name = (base as NSString).lastPathComponent
        let dir = (base as NSString).deletingLastPathComponent
        let subs = dir.isEmpty ? ["Rooms", ""] : ["Rooms/\(dir)", dir]
        for sub in subs {
            if let url = Bundle.main.url(forResource: name, withExtension: ext.isEmpty ? nil : ext,
                                          subdirectory: sub.isEmpty ? nil : sub) {
                return url
            }
        }
        return nil
    }
}
