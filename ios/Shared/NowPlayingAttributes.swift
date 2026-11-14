import ActivityKit
import Foundation

/// Lock Screen and Dynamic Island for whatever the house TV is playing. The phone starts it;
/// the hub does not need a push token for this one.
struct NowPlayingAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        var title: String
        var room: String
    }
    var deviceId: String
}
