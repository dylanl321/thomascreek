import AppIntents
import Foundation

// Garage and front door from widgets, Control Center, the Lock Screen, CarPlay's widget dashboard and the Watch.
// Every intent here needs an unlocked iPhone (authenticationPolicy). Opening the garage is sent "quick", so the
// hub skips its Face ID step for it (hub.quick_actions, Dylan's choice 2026-11-12). Front-door unlocking is not
// offered here: it still needs Face ID in the app.

struct GarageEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation: TypeDisplayRepresentation { "Garage door" }
    static var defaultQuery = GarageQuery()
    var id: String
    var name: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct GarageQuery: EntityQuery {
    func all() async -> [GarageEntity] {
        let home = (try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self)) ?? AppSettings.cachedHome
        return (home?.devices(kind: .garage) ?? []).map { GarageEntity(id: $0.id, name: $0.name) }
    }
    func entities(for identifiers: [String]) async throws -> [GarageEntity] {
        let all = await all()
        return identifiers.compactMap { id in all.first { $0.id == id } ?? GarageEntity(id: id, name: "Garage") }
    }
    func suggestedEntities() async throws -> [GarageEntity] { await all() }
    func defaultResult() async -> GarageEntity? { await all().first }
}

enum QuickHouse {
    static func garageOpen(_ id: String) -> Bool {
        AppSettings.cachedHome?.devices(kind: .garage).first { $0.id == id }?["open"]?.bool ?? false
    }
    static var frontDoor: DeviceItem? {
        let locks = AppSettings.cachedHome?.devices(kind: .lock) ?? []
        return locks.first { $0.name.localizedCaseInsensitiveContains("front") } ?? locks.first
    }
    static func refresh() async {
        if let h = try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self) { AppSettings.cachedHome = h }
        AppSettings.refreshSurfaces()
    }
}

struct SetGarageIntent: SetValueIntent {
    static var title: LocalizedStringResource = "Open or Close the Garage"
    static var description = IntentDescription("Opens or closes a garage door at Thomas Creek.")
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    @Parameter(title: "Garage door") var garage: GarageEntity
    @Parameter(title: "Open") var value: Bool

    init() {}
    init(garage: GarageEntity, open: Bool) { self.garage = garage; self.value = open }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let r = try await HubClient.shared.action("garage.set", target: garage.id, params: ["open": value], quick: true)
        await QuickHouse.refresh()
        return .result(dialog: "\(r.message ?? (value ? "Opening \(garage.name)" : "Closing \(garage.name)"))")
    }
}

struct ToggleGarageIntent: AppIntent {
    static var title: LocalizedStringResource = "Toggle the Garage"
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    @Parameter(title: "Garage door") var garage: GarageEntity

    init() {}
    init(garage: GarageEntity) { self.garage = garage }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let open = !QuickHouse.garageOpen(garage.id)
        let r = try await HubClient.shared.action("garage.set", target: garage.id, params: ["open": open], quick: true)
        await QuickHouse.refresh()
        return .result(dialog: "\(r.message ?? (open ? "Opening" : "Closing"))")
    }
}

struct LockFrontDoorIntent: AppIntent {
    static var title: LocalizedStringResource = "Lock the Front Door"
    static var description = IntentDescription("Locks the front door at Thomas Creek.")
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let door = QuickHouse.frontDoor else { return .result(dialog: "No door lock found.") }
        let r = try await HubClient.shared.action("lock.set", target: door.id, params: ["locked": true])
        await QuickHouse.refresh()
        return .result(dialog: "\(r.message ?? "\(door.name) locked")")
    }
}

/// "Last one out?": Siri answers with what's still open.
struct LeavingCheckIntent: AppIntent {
    static var title: LocalizedStringResource = "Is the House Locked Up"
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d: Departure = try await HubClient.shared.get("/v1/departure")
        return .result(dialog: "\(d.line ?? (d.allClear ? "Everything's locked up." : "Some things are still open."))")
    }
}
