import ActivityKit
import Foundation

/// Keeps one Live Activity in step with the TV that's on. A failure here changes nothing on screen.
enum NowPlayingActivity {
    @MainActor
    static func sync(_ devices: [TVPlayer]) {
        let on = devices.filter { tv in
            let title = tv.playing?.title ?? tv.playing?.name
            return title?.isEmpty == false
        }
        guard let first = on.first else {
            for activity in Activity<NowPlayingAttributes>.activities {
                Task { await activity.end(nil, dismissalPolicy: .immediate) }
            }
            return
        }
        let state = NowPlayingAttributes.ContentState(
            title: first.playing?.title ?? first.playing?.name ?? "Playing",
            room: first.name)
        let content = ActivityContent(state: state, staleDate: nil)
        if let existing = Activity<NowPlayingAttributes>.activities.first {
            Task { await existing.update(content) }
            return
        }
        Task { _ = try? Activity.request(attributes: NowPlayingAttributes(deviceId: first.id), content: content) }
    }
}
