import AppIntents
import Foundation

/// "Hey Siri, ask Thomas Creek" — Siri asks what, the hub answers as this phone's person.
struct AskHomeIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Thomas Creek"
    static var description = IntentDescription("Ask the house to do or check anything you're allowed to.")
    @Parameter(title: "Request", requestValueDialog: "What should I ask the house?") var text: String

    init() {}
    init(text: String) { self.text = text }

    static var parameterSummary: some ParameterSummary { Summary("Ask Thomas Creek \(\.$text)") }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let r = try await HubClient.shared.ask(text, conversation: nil, screen: "Siri")
        var line = AskText.plain(r.reply)
        if !r.confirms.isEmpty {
            // No Face ID inside Siri: the card waits in the app.
            AppSettings.defaults.set(true, forKey: OpenAskIntent.flag)
            line += " Open Thomas Creek to confirm."
        }
        if r.changed { AppSettings.refreshSurfaces() }
        return .result(value: line, dialog: "\(line)")
    }
}

/// Opens the app straight into the quick chat, listening. Used by the Control Center button and the
/// Action Button. Runs in the app; it leaves a flag the app picks up as it comes forward.
struct OpenAskIntent: AppIntent {
    static let flag = "assistant.openListening"
    static var title: LocalizedStringResource = "Talk to Thomas Creek"
    static var description = IntentDescription("Opens Thomas Creek listening, ready for a request.")
    static var openAppWhenRun: Bool = true

    func perform() async throws -> some IntentResult {
        AppSettings.defaults.set(true, forKey: Self.flag)
        await MainActor.run { NotificationCenter.default.post(name: .openAssistant, object: nil) }
        return .result()
    }
}

extension Notification.Name {
    static let openAssistant = Notification.Name("tc.openAssistant")
}
