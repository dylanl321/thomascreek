import AppIntents

/// Siri phrases. The holiday-lights phrases are unchanged from the Christmas Lights app.
struct HomeShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
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
    }
}
