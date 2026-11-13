import Foundation

/// What can be starred. The hub checks each star against the person's role.
enum FavoriteKind: String, CaseIterable, Codable, Hashable {
    case room, device, camera, mode, media, recipe, youtube
}

/// One favorite. `title`, `subtitle` and `image` let a screen or widget draw it without another fetch.
struct FavoriteRef: Codable, Hashable, Identifiable {
    var id: String
    var title: String?
    var subtitle: String?
    var image: String?
}

/// A person's settings and favorites as the hub keeps them (GET /v1/me/settings). They follow the person
/// to every device they use. Keys are dotted ("downloads.cellular"), so this is decoded WITHOUT the
/// snake-case conversion the rest of the API uses.
struct PersonSettings: Codable, Hashable {
    var version: Int = 0
    var settings: [String: JSONValue] = [:]
    var favorites: [String: [FavoriteRef]] = [:]

    static let decoder = JSONDecoder()

    func favorites(_ kind: FavoriteKind) -> [FavoriteRef] { favorites[kind.rawValue] ?? [] }
    func isStarred(_ kind: FavoriteKind, _ id: String) -> Bool { favorites(kind).contains { $0.id == id } }

    func bool(_ key: String, _ fallback: Bool) -> Bool { settings[key]?.bool ?? fallback }
    func string(_ key: String) -> String? { settings[key]?.string }
    func strings(_ key: String) -> [String] {
        if case .array(let a)? = settings[key] { return a.compactMap(\.string) }
        return []
    }

    /// The App Group copy, so widgets, Control Center and Siri see the same favorites with no network.
    static var cached: PersonSettings? {
        get { AppSettings.defaults.data(forKey: "personSettings").flatMap { try? decoder.decode(PersonSettings.self, from: $0) } }
        set { AppSettings.defaults.set(newValue.flatMap { try? JSONEncoder().encode($0) }, forKey: "personSettings") }
    }
}

/// Setting keys the app uses (the hub's list is in server/hub/settings.py).
enum SettingKey {
    static let homeOrder = "home.order"
    static let homeHidden = "home.hidden"
    static let appearance = "appearance"
    static let simpleMode = "simple_mode"
    static let textSize = "text_size"
    static let playOn = "defaults.play_on"
    static let speaker = "defaults.speaker"
    static let oven = "defaults.oven"
    static let camerasLayout = "cameras.layout"
    static let camerasHome = "cameras.home"
    static let downloadQuality = "downloads.quality"
    static let downloadCellular = "downloads.cellular"
    static let removeWatched = "downloads.remove_watched"
    static let liveActivityAuto = "live_activity.auto"
    /// How often Kiki and Bri come out: off, rarely, sometimes, often (an app-only key the hub stores as is).
    static let cats = "app.cats"
}
