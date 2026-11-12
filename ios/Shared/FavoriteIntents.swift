import AppIntents
import Foundation

// A person's starred lights, switches and fans for widgets, Control Center and Siri. They read the
// App Group copy of the person's favorites, so each family member's Control Center offers their own.
// Locks and garage doors are never toggled from here: they need Face ID in the app.

/// Which hub action turns a device on or off, or nil if it can't be done without the app.
enum FavoriteToggle {
    static func action(for d: DeviceItem) -> String? {
        switch d.kind {
        case .light: "light.set"
        case .switch: "switch.set"
        case .fan: "fan.set"
        default: nil
        }
    }

    /// Starred devices that can be switched from a widget, in the person's order.
    static func starred(home: HomeSnapshot?, settings: PersonSettings?) -> [DeviceItem] {
        let devices = home?.devices ?? []
        return (settings?.favorites(.device) ?? []).compactMap { f in devices.first { $0.id == f.id } }
    }
}

struct FavoriteDeviceEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation: TypeDisplayRepresentation { "Light or switch" }
    static var defaultQuery = FavoriteDeviceQuery()
    var id: String
    var name: String
    var action: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct FavoriteDeviceQuery: EntityQuery {
    /// Starred ones first, then every other light, switch and fan this person may use.
    func all() async -> [FavoriteDeviceEntity] {
        let home = (try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self)) ?? AppSettings.cachedHome
        let starred = FavoriteToggle.starred(home: home, settings: PersonSettings.cached)
        let rest = (home?.devices ?? []).filter { d in !starred.contains { $0.id == d.id } }
        return (starred + rest).compactMap { d in
            FavoriteToggle.action(for: d).map { FavoriteDeviceEntity(id: d.id, name: d.name, action: $0) }
        }
    }
    func entities(for identifiers: [String]) async throws -> [FavoriteDeviceEntity] {
        let all = await all()
        return identifiers.compactMap { id in all.first { $0.id == id } }
    }
    func suggestedEntities() async throws -> [FavoriteDeviceEntity] { await all() }
}

/// Turn one light, switch or fan on or off. Used by the Favorites widget and the Control Center toggle.
struct ToggleFavoriteIntent: SetValueIntent {
    static var title: LocalizedStringResource = "Turn a Favorite On or Off"
    static var isDiscoverable: Bool { false }
    @Parameter(title: "Device id") var deviceID: String
    @Parameter(title: "Action") var action: String
    @Parameter(title: "On") var value: Bool

    init() {}
    init(deviceID: String, action: String, on: Bool = false) {
        self.deviceID = deviceID; self.action = action; self.value = on
    }

    func perform() async throws -> some IntentResult {
        _ = try await HubClient.shared.action(action, target: deviceID, params: ["on": value])
        if let h = try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self) { AppSettings.cachedHome = h }
        AppSettings.refreshSurfaces()
        return .result()
    }
}
