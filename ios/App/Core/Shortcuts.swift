import AppIntents

/// Siri phrases. The holiday-lights phrases are unchanged from the Christmas Lights app.
struct HomeShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskHomeIntent(), phrases: ["Ask \(.applicationName)", "Tell \(.applicationName)", "Talk to \(.applicationName)"],
                    shortTitle: "Ask Thomas Creek", systemImageName: "mic.fill")
        AppShortcut(intent: RunModeIntent(), phrases: ["Run \(\.$mode) in \(.applicationName)", "\(.applicationName) \(\.$mode)"],
                    shortTitle: "Run Mode", systemImageName: "house.fill")
        AppShortcut(intent: HouseStatusIntent(), phrases: ["Is everything OK at \(.applicationName)", "\(.applicationName) house status"],
                    shortTitle: "House Status", systemImageName: "checkmark.circle")
        AppShortcut(intent: SetLookIntent(), phrases: ["Set \(.applicationName) to \(\.$look)", "Make the \(.applicationName) \(\.$look)",
                                                         "Turn \(.applicationName) to \(\.$look)"],
                    shortTitle: "Set Look", systemImageName: "lightbulb.fill")
        AppShortcut(intent: StartShowIntent(), phrases: ["Start the \(.applicationName) show", "Start \(.applicationName)"],
                    shortTitle: "Start Show", systemImageName: "play.fill")
        AppShortcut(intent: NextSceneIntent(), phrases: ["Next scene on \(.applicationName)", "Skip the \(.applicationName) scene"],
                    shortTitle: "Next Scene", systemImageName: "forward.fill")
        AppShortcut(intent: PlaySceneIntent(), phrases: ["Play \(\.$scene) on \(.applicationName)"],
                    shortTitle: "Play Scene", systemImageName: "film")
        AppShortcut(intent: LightsStatusIntent(), phrases: ["What's on the \(.applicationName)", "\(.applicationName) status"],
                    shortTitle: "Lights Status", systemImageName: "info.circle")
        AppShortcut(intent: SkipTonightIntent(), phrases: ["Skip \(.applicationName) tonight"],
                    shortTitle: "Skip Tonight", systemImageName: "moon.zzz")
        AppShortcut(intent: SearchHomeIntent(), phrases: ["Search \(.applicationName)", "Find something in \(.applicationName)"],
                    shortTitle: "Search", systemImageName: "magnifyingglass")
        // Play/Move/Turn-off TV intents stay available in the Shortcuts app. iOS caps App Shortcuts at 10
        // and only allows an AppEntity/AppEnum (one per phrase) as a phrase parameter, so they have no Siri phrase here.
    }
}

/// "Search Thomas Creek": opens the app's search, with the words if Siri or Shortcuts passed any.
struct SearchHomeIntent: AppIntent {
    static var title: LocalizedStringResource = "Search Thomas Creek"
    static var description = IntentDescription("Find a room, light, camera, film, recipe, trip or setting.")
    static var openAppWhenRun = true
    @Parameter(title: "Search for") var query: String?

    static var parameterSummary: some ParameterSummary { Summary("Search for \(\.$query)") }

    @MainActor
    func perform() async throws -> some IntentResult {
        Router.shared.startSearch(query)
        return .result()
    }
}
