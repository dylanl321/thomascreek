import ActivityKit
import Foundation

/// Lock Screen and Dynamic Island for what the TVs are playing. The hub starts, updates and ends it by
/// push (hub/nowplaying_live.py) and lists every TV that is on that this person may control.
struct NowPlayingAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        /// The first TV's title and name, kept for the card's one-line form and older hub builds.
        var title: String
        var room: String
        /// Every TV that is on (newer hubs).
        var tvs: [NowPlayingTV]?
        var count: Int?

        var shown: [NowPlayingTV] { tvs ?? [NowPlayingTV(id: "", name: room, title: title, kind: nil)] }
    }
    var deviceId: String
}

struct NowPlayingTV: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var title: String
    var kind: String?
}
