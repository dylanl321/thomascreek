import AppIntents
import Foundation

/// Pause one TV from the Now playing Live Activity. The hub checks the person may control that TV.
struct PauseTVIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Pause a TV"
    static var isDiscoverable: Bool { false }
    @Parameter(title: "TV id") var tvID: String
    init() {}
    init(tvID: String) { self.tvID = tvID }

    func perform() async throws -> some IntentResult {
        try await HubClient.shared.send("/v1/tv/remote", ["command": "pause", "device": tvID])
        return .result()
    }
}
