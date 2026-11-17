import ActivityKit
import Foundation

/// The Now playing Live Activity is started, updated and ended by the hub (#204), so it appears on a
/// locked phone that hasn't opened the app. The phone only hands the hub its tokens.
enum NowPlayingActivity {
    /// Kept for callers that pass the TV list: the hub decides now, so this only makes sure the tokens went.
    @MainActor
    static func sync(_ devices: [TVPlayer]) {
        NowPlayingLive.shared.observe()
    }
}

/// Sends the Now playing push-to-start token, and each running activity's update token, to the hub.
@MainActor
final class NowPlayingLive {
    static let shared = NowPlayingLive()
    private var observing = false
    private static let key = "nowPlayingStartToken"

    func observe() {
        guard !observing else { return }
        observing = true
        Task {
            for await data in Activity<NowPlayingAttributes>.pushToStartTokenUpdates {
                let tok = data.map { String(format: "%02x", $0) }.joined()
                AppSettings.defaults.set(tok, forKey: Self.key)
                await send(["now_playing_start_token": tok])
            }
        }
        Task {
            for await activity in Activity<NowPlayingAttributes>.activityUpdates {
                Task {
                    for await data in activity.pushTokenUpdates {
                        let tok = data.map { String(format: "%02x", $0) }.joined()
                        await send(["now_playing_activity_token": tok, "activity_id": activity.id])
                    }
                }
            }
        }
    }

    /// On launch: the hub may have dropped a dead token, or the phone was re-enrolled.
    func sync() {
        observe()
        if let tok = AppSettings.defaults.string(forKey: Self.key) { Task { await send(["now_playing_start_token": tok]) } }
    }

    private func send(_ body: [String: Any]) async {
        guard AppSettings.deviceID != nil else { return }
        var body = body
        body["env"] = PushRegistration.apsEnvironment
        await hubQuiet("now playing token") { try await HubClient.shared.send("/v1/devices/live-activity", body) }
    }
}
