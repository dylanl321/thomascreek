import Foundation
import ActivityKit

/// Live Activity for tonight's show. The player pushes ContentState (push.py / live_state() in show_player.py),
/// so the field names here must match the JSON keys it sends. Times are Unix seconds.
struct ShowAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        var phase: String
        var look: String?
        var scene: String?
        var next: String?
        var sceneStart: Double
        var sceneLength: Double
        var start: Double
        var calm: Double
        var off: Double
        var brightness: Int
        var skip: Bool
        var playing: Bool

        var phaseValue: ShowPhase { ShowPhase(rawValue: phase) ?? .day }
        var calmDate: Date { Date(timeIntervalSince1970: calm) }
        var offDate: Date { Date(timeIntervalSince1970: off) }
        var startDate: Date { Date(timeIntervalSince1970: start) }
        var sceneRange: ClosedRange<Date>? {
            guard playing, scene != nil, sceneStart > 0, sceneLength > 0 else { return nil }
            let a = Date(timeIntervalSince1970: sceneStart)
            return a...a.addingTimeInterval(sceneLength)
        }
        var title: String {
            if playing, let scene { return scene }
            if let l = look.flatMap(Look.init(rawValue:)) { return l == .off ? "Lights off" : l.title }
            return phaseValue.title
        }
    }
    var name: String
}
