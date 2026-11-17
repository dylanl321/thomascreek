import ActivityKit
import Foundation

/// Sends this phone's doorbell push-to-start token to the hub, and tidies up old doorbell activities.
@MainActor
final class DoorbellLive {
    static let shared = DoorbellLive()
    private var observing = false

    /// How long a ring stays on the Lock Screen. The hub ends it at the same time (household.DOORBELL_LIVE_S).
    static let ringSeconds: TimeInterval = 180

    func observe() {
        guard !observing else { return }
        observing = true
        Task {
            for await data in Activity<DoorbellAttributes>.pushToStartTokenUpdates {
                let tok = data.map { String(format: "%02x", $0) }.joined()
                AppSettings.defaults.set(tok, forKey: "doorbellStartToken")
                await send(tok)
            }
        }
        // Each ring the hub starts by push gets its own update token. The hub needs it to end the ring;
        // without it the card stayed on the Lock Screen for hours.
        Task {
            for await activity in Activity<DoorbellAttributes>.activityUpdates {
                Task {
                    for await data in activity.pushTokenUpdates {
                        let tok = data.map { String(format: "%02x", $0) }.joined()
                        await hubQuiet("doorbell activity token") {
                            try await HubClient.shared.send("/v1/devices/live-activity",
                                                            ["doorbell_activity_token": tok, "activity_id": activity.id,
                                                             "env": PushRegistration.apsEnvironment])
                        }
                    }
                }
            }
        }
        Task { await tidy() }
    }

    /// A ring older than three minutes is over, even if the hub's push to end it never came.
    func tidy() async {
        for a in Activity<DoorbellAttributes>.activities where -a.content.state.at.timeIntervalSinceNow > Self.ringSeconds {
            await a.end(nil, dismissalPolicy: .immediate)
        }
    }

    /// Re-send on launch: the hub may have dropped a dead token, or the phone was re-enrolled.
    func sync() {
        observe()
        if let tok = AppSettings.defaults.string(forKey: "doorbellStartToken") { Task { await send(tok) } }
    }

    private func send(_ token: String) async {
        guard AppSettings.deviceID != nil, HomeStore.shared.role.isGrownUp else { return }
        await hubQuiet("doorbell start token") {
            try await HubClient.shared.send("/v1/devices/live-activity",
                                            ["push_to_start_token": token, "env": PushRegistration.apsEnvironment])
        }
    }
}
