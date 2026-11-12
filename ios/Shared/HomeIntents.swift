import AppIntents
import Foundation

// House-wide actions for Siri, Shortcuts, widgets and Control Center. They go through the hub like
// everything else, so a child's phone gets the same answer from Siri as from the app.
// Secure-tier actions (garage, locks) are deliberately not here: they need Face ID in the app.

struct ModeEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation: TypeDisplayRepresentation { "Mode" }
    static var defaultQuery = ModeQuery()
    var id: String
    var name: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct ModeQuery: EntityStringQuery {
    func all() async -> [ModeEntity] {
        let home = (try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self)) ?? AppSettings.cachedHome
        return (home?.modes ?? []).map { ModeEntity(id: $0.id, name: $0.name) }
    }
    func entities(for identifiers: [String]) async throws -> [ModeEntity] {
        let all = await all()
        return identifiers.compactMap { id in all.first { $0.id == id } ?? ModeEntity(id: id, name: id) }
    }
    func entities(matching string: String) async throws -> [ModeEntity] {
        await all().filter { $0.name.localizedCaseInsensitiveContains(string) }
    }
    func suggestedEntities() async throws -> [ModeEntity] { await all() }
}

struct RunModeIntent: AppIntent {
    static var title: LocalizedStringResource = "Run a House Mode"
    static var description = IntentDescription("Good night, Leaving, Movie night and the other house modes.")
    @Parameter(title: "Mode") var mode: ModeEntity

    init() {}
    init(mode: ModeEntity) { self.mode = mode }

    static var parameterSummary: some ParameterSummary { Summary("Run \(\.$mode)") }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let r = try await HubClient.shared.action("mode.run", target: mode.id)
        AppSettings.refreshSurfaces()
        return .result(dialog: "\(r.message ?? "\(mode.name) is on.")")
    }
}

struct HouseStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "Is Everything OK at Home"
    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let home: HomeSnapshot = try await HubClient.shared.get("/v1/home")
        AppSettings.cachedHome = home
        let line = home.status.line
        return .result(value: line, dialog: "\(line)")
    }
}

struct MarkChoreDoneIntent: AppIntent {
    static var title: LocalizedStringResource = "Mark a Chore Done"
    @Parameter(title: "Chore id") var choreID: String
    init() {}
    init(choreID: String) { self.choreID = choreID }
    static var isDiscoverable: Bool { false }
    func perform() async throws -> some IntentResult {
        try await HubClient.shared.send("/v1/chores/\(choreID)/done")
        if let b: ChoreBoard = try? await HubClient.shared.get("/v1/chores") { AppSettings.cachedChores = b }
        AppSettings.refreshSurfaces()
        return .result()
    }
}
