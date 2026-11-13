import Foundation
import ActivityKit

/// Live Activity for the doorbell. The hub starts it with a push-to-start notification
/// (hub/household.py doorbell_live), so these names must match the JSON keys it sends. Times are Unix seconds.
struct DoorbellAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        var status: String          // ringing | answered
        var atUnix: Double
        var answeredBy: String?
        var at: Date { Date(timeIntervalSince1970: atUnix) }
    }
    var camera: String
    var cameraName: String
    var door: String
}
