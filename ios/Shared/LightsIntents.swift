import AppIntents
import Foundation

// Actions shared by Siri / Shortcuts, widget buttons, Live Activity buttons, Control Center and notification actions.

extension Look: AppEnum {
    static var typeDisplayRepresentation: TypeDisplayRepresentation { "Look" }
    static var caseDisplayRepresentations: [Look: DisplayRepresentation] {
        [.elegant: DisplayRepresentation(title: "Elegant", subtitle: "Warm white", image: .init(systemName: "lightbulb.fill")),
         .twinkle: DisplayRepresentation(title: "Twinkle", subtitle: "Warm white with slow twinkles", image: .init(systemName: "sparkle")),
         .festive: DisplayRepresentation(title: "Festive", subtitle: "Red, green and gold", image: .init(systemName: "gift.fill")),
         .calm: DisplayRepresentation(title: "Calm", subtitle: "Steady warm white", image: .init(systemName: "moon.fill")),
         .off: DisplayRepresentation(title: "Off", subtitle: "All lights off", image: .init(systemName: "power"))]
    }
}

struct SetLookIntent: AppIntent {
    static var title: LocalizedStringResource = "Set Christmas Lights Look"
    static var description = IntentDescription("Stops the loop and puts every controller on a look.")
    @Parameter(title: "Look") var look: Look

    init() {}
    init(look: Look) { self.look = look }

    static var parameterSummary: some ParameterSummary { Summary("Set the lights to \(\.$look)") }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        _ = try await LightsAPI.look(look)
        return .result(dialog: look == .off ? "The Christmas lights are off." : "The lights are on \(look.title).")
    }
}

struct StartShowIntent: AppIntent {
    static var title: LocalizedStringResource = "Start the Light Show"
    static var description = IntentDescription("Starts the scene loop.")
    func perform() async throws -> some IntentResult & ProvidesDialog {
        _ = try await LightsAPI.start()
        return .result(dialog: "The light show is starting.")
    }
}

struct NextSceneIntent: AppIntent {
    static var title: LocalizedStringResource = "Next Scene"
    static var description = IntentDescription("Skips to the next scene in the loop.")
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let r = try await LightsAPI.next()
        if r == "not running" { return .result(dialog: "The loop isn't playing right now.") }
        try? await Task.sleep(for: .milliseconds(700))
        let s = try? await LightsAPI.status()
        return .result(dialog: s?.scene.map { "Now playing \($0)." } ?? "Skipped.")
    }
}

struct SetBrightnessIntent: AppIntent {
    static var title: LocalizedStringResource = "Set Christmas Lights Brightness"
    @Parameter(title: "Brightness", description: "Percent", inclusiveRange: (1, 100)) var percent: Int
    init() {}
    init(percent: Int) { self.percent = percent }
    static var parameterSummary: some ParameterSummary { Summary("Set brightness to \(\.$percent)%") }
    func perform() async throws -> some IntentResult & ProvidesDialog {
        _ = try await LightsAPI.brightness(percent: percent)
        return .result(dialog: "Brightness is \(percent) percent.")
    }
}

struct SkipTonightIntent: AppIntent {
    static var title: LocalizedStringResource = "Skip Tonight's Show"
    static var description = IntentDescription("The schedule leaves the lights alone tonight. It's back tomorrow at sunset.")
    @Parameter(title: "Skip", default: true) var skip: Bool
    init() {}
    init(skip: Bool) { self.skip = skip }
    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await LightsAPI.skipTonight(skip)
        return .result(dialog: skip ? "Tonight's show is skipped." : "Tonight's show is back on.")
    }
}

struct LightsStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "What's on the Christmas Lights"
    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let s = try await LightsAPI.status()
        var line = s.running ? "Playing \(s.scene ?? "the loop")." : "\(s.headline)."
        if let sc = s.schedule {
            if sc.skip { line += " Tonight is skipped." }
            else if sc.phase == "show" { line += " Calm at \(sc.calm)." }
            else if sc.phase == "day" { line += " The show starts at \(sc.start)." }
        }
        if s.controllersDown > 0 { line += " \(s.controllersDown) controller\(s.controllersDown == 1 ? " isn't" : "s aren't") responding." }
        return .result(value: line, dialog: "\(line)")
    }
}

// MARK: scenes as Shortcuts entities ("Play Snowfall")

struct SceneEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation: TypeDisplayRepresentation { "Scene" }
    static var defaultQuery = SceneQuery()
    var id: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(id)") }
}

struct SceneQuery: EntityStringQuery {
    func all() async -> [SceneEntity] {
        let s = (try? await LightsAPI.status()) ?? AppSettings.cachedStatus
        return (s?.scenes ?? []).map(SceneEntity.init(id:))
    }
    func entities(for identifiers: [String]) async throws -> [SceneEntity] { identifiers.map(SceneEntity.init(id:)) }
    func entities(matching string: String) async throws -> [SceneEntity] {
        await all().filter { $0.id.localizedCaseInsensitiveContains(string) }
    }
    func suggestedEntities() async throws -> [SceneEntity] { await all() }
}

struct PlaySceneIntent: AppIntent {
    static var title: LocalizedStringResource = "Play a Scene"
    @Parameter(title: "Scene") var scene: SceneEntity
    init() {}
    init(scene: String) { self.scene = SceneEntity(id: scene) }
    static var parameterSummary: some ParameterSummary { Summary("Play \(\.$scene)") }
    func perform() async throws -> some IntentResult & ProvidesDialog {
        _ = try await LightsAPI.play(scene.id)
        return .result(dialog: "Playing \(scene.id).")
    }
}

// MARK: Control Center toggle (iOS 18)

struct ShowLoopToggleIntent: SetValueIntent {
    static var title: LocalizedStringResource = "Light Show Loop"
    @Parameter(title: "Playing") var value: Bool
    func perform() async throws -> some IntentResult {
        if value { _ = try await LightsAPI.start() } else { _ = try await LightsAPI.look(.calm) }
        return .result()
    }
}
