import ActivityKit
import Foundation

/// Lock Screen / Dynamic Island show tracker (holiday lights). The hub, on behalf of the show player, starts it at sunset with a push-to-start token
/// (when "Start automatically" is on), keeps it current with update pushes, and ends it at sunrise.
@MainActor
final class LiveActivities: ObservableObject {
    static let shared = LiveActivities()
    @Published var active = false
    private var observing = false
    private var watched = Set<String>()

    var enabled: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    func observe() {
        guard !observing else { return }
        observing = true
        // Token the player uses to start an activity remotely (iOS 17.2+).
        Task {
            for await data in Activity<ShowAttributes>.pushToStartTokenUpdates {
                let tok = data.map { String(format: "%02x", $0) }.joined()
                AppSettings.defaults.set(tok, forKey: "startToken")
                await sendStartToken()
            }
        }
        // Every activity, whether the app or the player started it.
        Task {
            for await activity in Activity<ShowAttributes>.activityUpdates { watch(activity) }
        }
        Activity<ShowAttributes>.activities.forEach(watch)
        active = !Activity<ShowAttributes>.activities.isEmpty
    }

    private func watch(_ activity: Activity<ShowAttributes>) {
        guard !watched.contains(activity.id) else { return }
        watched.insert(activity.id)
        active = true
        Task {
            for await data in activity.pushTokenUpdates {
                let tok = data.map { String(format: "%02x", $0) }.joined()
                try? await HubClient.shared.send("/v1/devices/activity", ["activity_id": activity.id, "token": tok])
            }
        }
        Task {
            for await state in activity.activityStateUpdates where state == .ended || state == .dismissed {
                try? await HubClient.shared.send("/v1/devices/activity", ["activity_id": activity.id, "token": ""])
                self.active = !Activity<ShowAttributes>.activities.filter { $0.activityState == .active }.isEmpty
            }
        }
    }

    func sendStartToken() async {
        let tok = AppSettings.autoLiveActivity ? (AppSettings.defaults.string(forKey: "startToken") ?? "") : ""
        guard AppSettings.deviceID != nil else { return }
        try? await HubClient.shared.send("/v1/devices/activity", ["start_token": tok])
    }

    /// Re-send tokens when the app opens (the player may have been reinstalled or restarted without devices.json).
    func sync() {
        observe()
        Task { await sendStartToken() }
    }

    /// Start one now from the app.
    func start() async throws {
        let state = try await LightsAPI.live()
        if let a = Activity<ShowAttributes>.activities.first {
            await a.update(ActivityContent(state: state, staleDate: nil)); return
        }
        let a = try Activity.request(attributes: ShowAttributes(name: "Christmas Lights"),
                                     content: ActivityContent(state: state, staleDate: nil), pushType: .token)
        watch(a)
    }

    func endAll() async {
        for a in Activity<ShowAttributes>.activities { await a.end(nil, dismissalPolicy: .immediate) }
        active = false
    }
}
