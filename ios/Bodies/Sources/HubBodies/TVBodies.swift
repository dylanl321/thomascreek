import Foundation

// The TV bodies the iPhone reads from the hub (/v1/tv/explore, /v1/tv/upnext, /v1/tv/guide, /v1/tv/players,
// /v1/tv/series, /v1/tv/movies, /v1/tv/library/search, /v1/dvr/recordings). Foundation only, so the decode tests
// in ios/Bodies run with `swift test` on the Mac without a simulator (#206).

// MARK: For you and Up next

struct TVFeed: Decodable {
    var hero: TVFeedItem?
    var rows: [TVFeedRow]
}
struct TVFeedRow: Decodable, Identifiable {
    var id: String
    var title: String
    var items: [TVFeedItem]
    /// The hub's one line on why this row is here for this person (hub #201); older hubs send none.
    var reason: String?
}
struct TVFeedItem: Decodable, Identifiable, Hashable {
    var kind: String?
    var title: String
    var subtitle: String?
    var reason: String?
    var year: Int?
    var overview: String?
    var posterUrl: String?
    var backdropUrl: String?
    var availability: String?
    var kids: Bool?
    var movieId: String?
    var seriesId: String?
    var episodeId: String?
    var channelKey: String?
    var plexId: String?
    var tmdbId: Int?
    var progress: Double?
    var id: String { episodeId ?? movieId ?? seriesId ?? channelKey ?? plexId ?? tmdbId.map { "tmdb:\($0)" } ?? title }

    /// Something to play right away. A show with no episode opens its page instead.
    var playRequest: TVPlayRequest? {
        if let episodeId {
            return TVPlayRequest(title: title, kind: "episode", seriesId: seriesId, episodeId: episodeId, kids: kids)
        }
        if let movieId, kind != "series" {
            return TVPlayRequest(title: title, kind: "movie", movieId: movieId, plexId: plexId, kids: kids)
        }
        if let channelKey { return TVPlayRequest(title: title, channelKey: channelKey, kids: kids) }
        return nil
    }
}

/// What to put on next. The hub ranks it from what this house watches. An empty answer hides the row.
struct UpNextPage: Decodable { var rows: [UpNextRow] }
struct UpNextRow: Decodable, Identifiable {
    var id: String
    var title: String
    var items: [UpNextItem]
}
struct UpNextItem: Decodable, Identifiable {
    var kind: String
    var title: String
    var subtitle: String?
    var reason: String?
    var posterUrl: String?
    var channelKey: String?
    var movieId: String?
    var seriesId: String?
    var episodeId: String?
    var recordingId: String?
    var progress: Double?
    var id: String { (episodeId ?? movieId ?? recordingId ?? channelKey ?? title) + (reason ?? "") }
}

// MARK: Guide, players, library

struct TVPlayerList: Decodable { var tvs: [TVPlayer] }
struct TVPlayer: Decodable, Identifiable {
    var id: String
    var name: String
    var room: String? = nil
    var roomName: String? = nil
    var profile: String? = nil
    var mediaEntity: String? = nil
    var online: Bool? = nil
    var playing: TVPlaying? = nil
    /// The room this person moved a show to last. The hub lists that one first.
    var usual: Bool? = nil
    /// Who the hub thinks is watching (hub/tvwho.py). Older hubs leave it out.
    var watching: TVWatching? = nil
}
/// `watching` on a TV: person_id is nil when the hub can't tell (the TV keeps its own history then).
struct TVWatching: Decodable, Hashable {
    var personId: String? = nil
    var name: String? = nil
    var role: String? = nil
    /// pinned, claimed, phone (a phone sent the show) or home (the only one home).
    var reason: String? = nil
    var line: String? = nil
}
struct TVDeviceList: Decodable { var devices: [TVPlayer] }
struct TVHandoffReply: Decodable { var ok: Bool?; var moved: Bool?; var message: String? }
struct TVPlaying: Decodable, Hashable {
    var channelKey: String? = nil
    var title: String? = nil
    var name: String? = nil
    var positionS: Double? = nil
    var posterUrl: String? = nil
    var number: Int? = nil
    var kind: String? = nil
    var movieId: String? = nil
    var seriesId: String? = nil
    var episodeId: String? = nil
}
/// A `tv` event: what's on one house TV right now.
struct TVWatch: Decodable {
    var id: String
    var name: String?
    var room: String?
    var mediaEntity: String?
    var playing: TVPlaying?
    var watching: TVWatching? = nil
}

struct TVGuidePage: Decodable { var channels: [TVGuideChannel] }
struct TVGuideChannel: Decodable, Identifiable {
    var key: String
    var name: String
    var number: Int?
    var logo: String?
    var logoUrl: String?
    var now: TVProgramme?
    var next: TVProgramme?
    var programmes: [TVProgramme]?
    var id: String { key }
}
struct TVProgramme: Decodable, Hashable {
    var start: String
    var end: String?
    var title: String
    var subtitle: String?
    var desc: String?
    var icon: String?
    var posterUrl: String?
    var backdropUrl: String?
    var catchup: Bool?
    var channelKey: String?
    var channelName: String?
}

struct TVSeriesPage: Decodable { var series: [TVShow] }
struct TVMoviePage: Decodable { var movies: [TVShow] }
struct TVShow: Decodable, Identifiable {
    var id: String
    var name: String
    var cover: String?
    var posterUrl: String?
    var year: String?
    var genre: String?
    var category: String?
    var plot: String?
    var backdrop: String?
    var backdropUrl: String?
    var seasons: [TVSeason]?
    var durationS: Int?
    var kids: Bool?
}
struct TVSeason: Decodable, Identifiable {
    var number: Int
    var episodes: [TVEpisode]
    var id: Int { number }
}
struct TVEpisode: Decodable, Identifiable {
    var id: String
    var number: Int?
    var title: String?
    var still: String?
    var stillUrl: String?
    var durationS: Int?
}
struct TVLibrarySearch: Decodable {
    var channels: [TVGuideChannel]?
    var programmes: [TVProgramme]?
    var series: [TVShow]?
    var movies: [TVShow]?
}

// MARK: The player on this iPhone (/v1/tv/phone/next, /queue, /segments)

/// The episode the hub plays after this one (its order; a kids' episode only for a child).
struct PlayNextEpisode: Decodable {
    var episodeId: String
    var seriesId: String?
    var title: String?
    var season: Int?
    var number: Int?
    var stillUrl: String?
    /// "S1 E2 · Second", or the title alone when the hub sent no numbers.
    var line: String {
        let name = title ?? "Next episode"
        if let season, let number { return "S\(season) E\(number) · \(name)" }
        return name
    }
}
struct PlayNextReply: Decodable { var next: PlayNextEpisode? }

/// A recap, intro or credits section the hub knows.
struct PlaySegment: Decodable {
    var kind: String
    var startS: Double
    var endS: Double
    var label: String { kind == "recap" ? "Skip recap" : (kind == "intro" ? "Skip intro" : "Skip credits") }
}
struct PlaySegments: Decodable { var segments: [PlaySegment]? }

/// One entry of the person's "Play next" queue, which follows them to every screen.
struct PlayQueueItem: Decodable, Identifiable {
    var id: String
    var kind: String?
    var title: String
    var subtitle: String?
    var season: Int?
    var number: Int?
    var seriesId: String?
    var imageUrl: String?
    var durationS: Int?
    var isEpisode: Bool { id.hasPrefix("ep:") }
    /// "Cartoon Show · S1 E2" for an episode, the hub's subtitle for a film, or nil.
    var line: String? {
        if isEpisode, let season, let number { return [subtitle, "S\(season) E\(number)"].compactMap { $0 }.joined(separator: " · ") }
        return subtitle
    }
}
struct PlayQueue: Decodable { var items: [PlayQueueItem]? }

struct TVPlayRequest: Identifiable {
    var id: String { title + (channelKey ?? "") + (start ?? "") + (movieId ?? "") + (episodeId ?? "") }
    var title: String
    var kind: String = "live"
    var channelKey: String?
    var start: String?
    var movieId: String?
    var seriesId: String?
    var episodeId: String?
    /// A Plex title (played on the phone through the hub's Plex stream).
    var plexId: String? = nil
    /// False: an adult title, which a kids' TV refuses. Nil when not known (the hub still checks).
    var kids: Bool? = nil
    /// Start a TV here (seconds), e.g. "Play on a TV" from the phone's player.
    var resumeS: Double? = nil
}

struct DvrPage: Decodable { var recordings: [DvrItem] }
struct DvrItem: Decodable, Identifiable {
    var id: String
    var title: String?
    var status: String
    var start: String?
    var streamUrl: String?
}
