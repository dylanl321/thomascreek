import ActivityKit
import Foundation

/// Sends this phone's doorbell push-to-start token to the hub, and tidies up old doorbell activities.
@MainActor
final class DoorbellLive {
    static let shared = DoorbellLive()
    private var observing = false

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
        // A ring older than ten minutes is over even if the push to end it never came.
        for a in Activity<DoorbellAttributes>.activities where -a.content.state.at.timeIntervalSinceNow > 600 {
            Task { await a.end(nil, dismissalPolicy: .immediate) }
        }
    }

    /// Re-send on launch: the hub may have dropped a dead token, or the phone was re-enrolled.
    func sync() {
        observe()
        if let tok = AppSettings.defaults.string(forKey: "doorbellStartToken") { Task { await send(tok) } }
    }

    private func send(_ token: String) async {
        guard AppSettings.deviceID != nil, HomeStore.shared.role.isGrownUp else { return }
        try? await HubClient.shared.send("/v1/devices/live-activity",
                                         ["push_to_start_token": token, "env": PushRegistration.apsEnvironment])
    }
}
