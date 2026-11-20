import AVKit
import SwiftUI
import TVPhone

// The same rows the TV's own home screen shows (`/v1/tv/explore` and `/v1/tv/upnext`), on the phone.
// A tap plays: on this iPhone through the hub (`/v1/tv/phone/play`), or on a house TV (`tv.play`).
// The provider login stays on the hub; the phone only ever gets hub paths.

/// The For you rows: Keep watching first, then new, popular and acclaimed.
struct TVForYouRows: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    /// "kids" shows a kids' TV's rows. Nil: this person's own.
    var profile: String?
    var play: (TVPlayRequest) -> Void
    @State private var feed: TVFeed?
    @State private var next: TVFeed?
    @State private var failed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let feed {
                if let hero = feed.hero ?? feed.rows.first?.items.first {
                    HeroCard(item: hero) { open(hero) }
                }
                ForEach((next?.rows ?? []) + feed.rows) { row in
                    if !row.items.isEmpty {
                        SectionHeader(row.title)
                        if let reason = row.reason, !reason.isEmpty {
                            Text(reason).font(.footnote).foregroundStyle(.secondary).lineLimit(1).padding(.top, -10)
                        }
                        ScrollView(.horizontal, showsIndicators: false) {
                            LazyHStack(alignment: .top, spacing: 12) {
                                ForEach(row.items) { item in
                                    PosterCard(item: item) { open(item) }
                                }
                            }
                            .padding(.horizontal, 2)
                        }
                    }
                }
                if feed.rows.isEmpty && (next?.rows ?? []).isEmpty {
                    EmptyCard(symbol: "sparkles.tv", title: "Nothing here yet",
                              detail: "The hub fills these rows once the TV library has loaded.")
                }
            } else if failed {
                VStack(spacing: 10) {
                    EmptyCard(symbol: "wifi.exclamationmark", title: "The hub didn't answer",
                              detail: "The TV rows couldn't load. Try again in a moment.")
                    Button("Try again") { Task { await load() } }.buttonStyle(.bordered)
                }
            } else { LoadingCard() }
        }
        .task(id: profile) { await load() }
    }

    private func load() async {
        failed = false
        let query = profile.map { ["profile": $0] } ?? [:]
        async let rows: TVFeed? = await hubQuiet("tv/explore", { try await HubClient.shared.get("/v1/tv/explore", query, as: TVFeed.self) })
        async let shelf: TVFeed? = await hubQuiet("tv/upnext", { try await HubClient.shared.get("/v1/tv/upnext", query, as: TVFeed.self) })
        let (r, s) = await (rows, shelf)
        next = s
        if let r { feed = r } else if feed == nil { failed = true }
    }

    private func open(_ item: TVFeedItem) {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        if item.availability == "request" {
            Task { await ask(item) }
        } else if let request = item.playRequest {
            play(request)
        } else if let series = item.seriesId, item.plexId == nil {
            router.go(.tvSeries(series))
        } else if let plex = item.plexId {
            play(TVPlayRequest(title: item.title, kind: "movie", plexId: plex, kids: item.kids))
        }
    }

    private struct AskReply: Decodable { var ok: Bool?; var message: String?; var status: String? }

    private func ask(_ item: TVFeedItem) async {
        guard let tmdb = item.tmdbId else { return }
        do {
            let reply: AskReply = try await HubClient.shared.post("/v1/tv/explore/request",
                                                                  ["tmdb_id": tmdb, "kind": item.kind ?? "movie"])
            store.show(reply.message ?? (reply.status == "waiting" ? "Already asked for" : "Asked for it"), item.title)
        } catch {
            store.show(error.localizedDescription, item.title, isError: true)
        }
    }
}

private struct HeroCard: View {
    var item: TVFeedItem
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            ZStack(alignment: .bottomLeading) {
                HubImage(path: item.backdropUrl ?? item.posterUrl) {
                    LinearGradient(colors: [Theme.card2, Theme.card], startPoint: .top, endPoint: .bottom)
                }
                .frame(maxWidth: .infinity).frame(height: 200).clipped()
                LinearGradient(colors: [.clear, .black.opacity(0.8)], startPoint: .center, endPoint: .bottom)
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.reason ?? "Popular now").font(.caption.weight(.bold)).foregroundStyle(.white.opacity(0.8))
                    Text(item.title).font(.title2.weight(.bold)).foregroundStyle(.white).lineLimit(2)
                    Label(item.availability == "request" ? "Ask for it" : "Play", systemImage: item.availability == "request" ? "plus" : "play.fill")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(.black)
                        .padding(.horizontal, 14).padding(.vertical, 8).background(.white, in: Capsule())
                        .padding(.top, 4)
                }
                .padding(16)
            }
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(item.title). \(item.availability == "request" ? "Ask for it" : "Play")")
    }
}

private struct PosterCard: View {
    var item: TVFeedItem
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                ZStack(alignment: .bottom) {
                    HubImage(path: item.posterUrl ?? item.backdropUrl) {
                        Image(systemName: item.channelKey != nil ? "tv" : "film").font(.title2).foregroundStyle(Theme.dim)
                            .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card)
                    }
                    .frame(width: 112, height: 164)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    if let p = item.progress, p > 0 {
                        GeometryReader { g in
                            Capsule().fill(Theme.accent).frame(width: g.size.width * min(max(p, 0), 1), height: 4)
                        }
                        .frame(height: 4).padding(.horizontal, 8).padding(.bottom, 8)
                    }
                    if item.availability == "request" {
                        Text("Ask for it").font(.caption2.weight(.bold)).foregroundStyle(.black)
                            .padding(.horizontal, 8).padding(.vertical, 3).background(.white, in: Capsule())
                            .padding(.bottom, 8)
                    }
                }
                Text(item.title).font(.caption.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(2)
                    .frame(width: 112, alignment: .leading)
                if let sub = item.subtitle ?? item.year.map(String.init), !sub.isEmpty {
                    Text(sub).font(.caption2).foregroundStyle(Theme.dim).lineLimit(1).frame(width: 112, alignment: .leading)
                }
            }
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Playing on this iPhone

struct PhonePlayReply: Decodable {
    var url: String; var kind: String?; var title: String?
    /// Where this person stopped on any screen (or the second asked for with start_s). Nil: from the start.
    var resumeS: Double?
    var seriesId: String?
}
private struct PlexStreamReply: Decodable { var hlsUrl: String }

/// One play on this iPhone: the address, where to start, and what to report back to the hub.
struct PhoneSession: Identifiable {
    var id: String { url.absoluteString }
    var url: URL
    var request: TVPlayRequest
    var resumeS: Double?
    /// Library films and episodes report progress so Continue watching follows the person. Plex and live don't.
    var reports: Bool { request.plexId == nil && (request.kind == "movie" || request.kind == "episode") }
}

enum PhonePlayback {
    /// A hub path for the phone's own player, or a sentence saying why not. `startS` starts from that second
    /// ("Continue on iPhone" from a TV); otherwise the hub answers with the person's own place.
    static func start(_ request: TVPlayRequest, startS: Double? = nil) async -> Result<PhoneSession, PhoneError> {
        do {
            let path: String
            var resume: Double? = nil
            var request = request
            if let plex = request.plexId, request.movieId?.hasPrefix("vod:thomas-creek:") != true {
                let reply: PlexStreamReply = try await HubClient.shared.get("/v1/media/\(plex)/stream")
                path = reply.hlsUrl
            } else {
                var body: [String: Any] = [:]
                switch request.kind {
                case "movie": body["movie_id"] = request.movieId ?? ""
                case "episode": body["episode_id"] = request.episodeId ?? ""
                case "live": body["channel_key"] = request.channelKey ?? ""
                default: return .failure(PhoneError(message: "Start over only plays on a TV"))
                }
                if let startS, startS > 0 { body["start_s"] = Int(startS) }
                let reply: PhonePlayReply = try await HubClient.shared.post("/v1/tv/phone/play", body)
                path = reply.url
                resume = reply.resumeS
                if request.seriesId == nil { request.seriesId = reply.seriesId }
            }
            guard let url = await playable(path) else { return .failure(PhoneError(message: "Can't reach the house right now.")) }
            request.plexId = request.movieId?.hasPrefix("vod:thomas-creek:") == true ? nil : request.plexId
            return .success(PhoneSession(url: url, request: request, resumeS: resume))
        } catch {
            return .failure(PhoneError(message: error.localizedDescription))
        }
    }

    /// The same record a TV's heartbeat makes, kept for this person (`POST /v1/tv/phone/progress`).
    static func report(_ request: TVPlayRequest, position: Double, duration: Double?) async {
        guard position >= 1 else { return }
        var body: [String: Any] = ["position_s": Int(position)]
        if request.kind == "episode" { body["episode_id"] = request.episodeId ?? "" } else { body["movie_id"] = request.movieId ?? "" }
        if let duration, duration.isFinite, duration >= 60 { body["duration_s"] = Int(duration) }
        do { try await HubClient.shared.send("/v1/tv/phone/progress", body) }
        catch { print("phone progress not saved: \(error.localizedDescription)") }
    }

    /// The phone's player can't use the hub's pinned home certificate, so it goes through the published
    /// address (a normal certificate) when there is one.
    private static func playable(_ path: String) async -> URL? {
        if let remote = await HubClient.shared.remoteBase() {
            return URL(string: path, relativeTo: remote)?.absoluteURL
        }
        return await HubClient.shared.resolve(path)
    }

    static func canPlay(_ request: TVPlayRequest) -> Bool { request.kind != "catchup" }

    // The hub's next episode, the person's "Play next" queue and the Skip sections. Nil: the hub did not answer
    // (or is an older hub), and the player simply goes without.

    static func next(after episodeId: String?) async -> PlayNextEpisode? {
        guard let episodeId, !episodeId.isEmpty else { return nil }
        let reply = try? await HubClient.shared.get("/v1/tv/phone/next", ["episode_id": episodeId], as: PlayNextReply.self)
        return reply?.next
    }

    static func queue() async -> [PlayQueueItem]? {
        let reply = try? await HubClient.shared.get("/v1/tv/phone/queue", as: PlayQueue.self)
        return reply.map { $0.items ?? [] }
    }

    /// One change (`add` with `first`, `remove`, `clear`); the new queue, or nil when the hub refused it.
    static func changeQueue(_ body: [String: Any]) async -> [PlayQueueItem]? {
        let reply = try? await HubClient.shared.post("/v1/tv/phone/queue", body, as: PlayQueue.self)
        return reply.map { $0.items ?? [] }
    }

    static func segments(_ request: TVPlayRequest, duration: Double) async -> [PlaySegment] {
        var query: [String: String] = [:]
        if request.kind == "episode" { query["episode_id"] = request.episodeId ?? "" } else { query["movie_id"] = request.movieId ?? "" }
        if duration.isFinite, duration > 0 { query["duration_s"] = String(Int(duration)) }
        let reply = try? await HubClient.shared.get("/v1/tv/phone/segments", query, as: PlaySegments.self)
        return reply?.segments ?? []
    }
}

/// "Play next" and "Add to queue" for a film's page or an episode row: the queue is the person's, on the hub.
@MainActor
func addToPlayQueue(_ id: String, title: String, first: Bool, store: HomeStore) {
    Task { @MainActor in
        var body: [String: Any] = ["add": id]
        if first { body["first"] = true }
        if await PhonePlayback.changeQueue(body) != nil {
            store.show(first ? "Plays next" : "Added to your queue", title)
        } else {
            store.show("That couldn't be queued", "Try again in a moment.", isError: true)
        }
    }
}

struct PhoneError: Error { var message: String }

/// Full screen, with the system controls (AirPlay and picture in picture included). Starts where the person
/// stopped, reports every minute and on close, and can hand the show to a house TV from the same second. For a
/// library film or episode the hub adds: Skip for the recap, intro and credits it knows, the next episode, and the
/// person's "Play next" queue, which plays before the next episode.
struct PhonePlayerView: View {
    var session: PhoneSession
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store
    /// What plays now when it is no longer what the player was opened with (the next episode, a queued title).
    @State private var followed: PhoneSession?
    @State private var player: AVPlayer?
    @State private var ticker: Any?
    @State private var clock: Any?
    @State private var choosingTV = false
    @State private var position: Double = 0
    @State private var length: Double = 0
    @State private var segments: [PlaySegment] = []
    @State private var segmentsAsked = false
    @State private var next: PlayNextEpisode?
    @State private var queue: [PlayQueueItem] = []
    @State private var nextDismissed = false
    @State private var startedByItself = 0
    @State private var stillAsking = false
    @State private var showingQueue = false
    @State private var problem: String?
    @State private var run = 0

    private enum Following {
        case queued(PlayQueueItem)
        case episode(PlayNextEpisode)
    }

    private var now: PhoneSession { followed ?? session }
    private var title: String { now.request.title }
    private var playingId: String {
        (now.request.kind == "episode" ? now.request.episodeId : now.request.movieId) ?? ""
    }

    /// What plays when this one ends: the front of the queue, else the hub's next episode.
    private var upNext: Following? {
        guard now.reports else { return nil }
        if let item = queue.first(where: { $0.id != playingId }) { return .queued(item) }
        return next.map { .episode($0) }
    }

    /// The recap, intro or credits the picture is in. No "Skip credits" when something plays next: its card is there.
    private var skippable: PlaySegment? {
        segments.first { position >= $0.startS && position < $0.endS - 1 && !($0.kind == "credits" && upNext != nil) }
    }

    /// The "next" card: from the credits the hub knows, else the last 20 seconds.
    private var nextCardUp: Bool {
        guard upNext != nil, !nextDismissed, !stillAsking, length > 0 else { return false }
        let credits = segments.last { $0.kind == "credits" && $0.startS < length }?.startS
        return position >= (credits ?? length - 20)
    }

    var body: some View {
        ZStack(alignment: .top) {
            Color.black.ignoresSafeArea()
            if let player { VideoPlayer(player: player).ignoresSafeArea() } else if problem == nil { ProgressView().tint(.white) }
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.headline).foregroundStyle(.white).padding(12)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .accessibilityLabel("Close \(title)")
                Spacer()
                if now.reports {
                    Button { startedByItself = 0; showingQueue = true } label: {
                        Label("Up next", systemImage: "list.bullet").font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 14).frame(minHeight: 44).background(.ultraThinMaterial, in: Capsule())
                    }
                }
                if now.request.kind != "catchup", now.request.plexId == nil {
                    Button { choosingTV = true } label: {
                        Label("Play on a TV", systemImage: "tv").font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 14).frame(minHeight: 44).background(.ultraThinMaterial, in: Capsule())
                    }
                }
            }
            .padding(16)
            corner
            if stillAsking { stillWatching }
            if let problem { failed(problem) }
        }
        .onAppear(perform: begin)
        .onDisappear(perform: halt)
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemDidPlayToEndTime)) { note in
            if let item = note.object as? AVPlayerItem, item === player?.currentItem { ended() }
        }
        .confirmationDialog("Play on a TV", isPresented: $choosingTV, titleVisibility: .visible) {
            ForEach(store.houseTVs.filter { !($0.profile == "kids" && now.request.kids == false) }) { tv in
                Button(tv.name) { moveTo(tv) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text("\(title) starts there from this second.") }
        .sheet(isPresented: $showingQueue) { queueSheet }
        .task { if store.houseTVs.isEmpty { await store.loadHouseTVs() } }
    }

    // MARK: Skip and what is next

    /// Above the system's time bar, on the right: Skip, or the card for what plays next.
    @ViewBuilder private var corner: some View {
        VStack(alignment: .trailing, spacing: 8) {
            if nextCardUp, let item = upNext {
                HStack(spacing: 8) {
                    Button { nextDismissed = true } label: {
                        Text("Not now").font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 14).frame(minHeight: 44).background(.ultraThinMaterial, in: Capsule())
                    }
                    Button { follow(item, byItself: false) } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(heading(item)).font(.caption.weight(.semibold)).foregroundStyle(.black.opacity(0.7))
                            Text(name(item)).font(.subheadline.weight(.semibold)).foregroundStyle(.black).lineLimit(1)
                        }
                        .padding(.horizontal, 16).frame(minHeight: 48).background(.white, in: RoundedRectangle(cornerRadius: 14))
                    }
                    .accessibilityLabel("Play \(name(item)) now")
                }
            } else if let part = skippable {
                Button { skip(part) } label: {
                    Label(part.label, systemImage: "forward.end.fill").font(.subheadline.weight(.semibold)).foregroundStyle(.black)
                        .padding(.horizontal, 16).frame(minHeight: 44).background(.white, in: Capsule())
                }
            }
        }
        .padding(.trailing, 16).padding(.bottom, 110)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing)
    }

    private func heading(_ item: Following) -> String {
        if case .queued = item { return "From your queue" }
        return "Next episode"
    }

    private func name(_ item: Following) -> String {
        switch item {
        case .queued(let q): return q.isEpisode ? [q.line, q.title].compactMap { $0 }.joined(separator: " · ") : q.title
        case .episode(let e): return e.line
        }
    }

    private var stillWatching: some View {
        VStack(spacing: 14) {
            Text("Still watching?").font(.title2.weight(.bold)).foregroundStyle(.white)
            if let item = upNext { Text(name(item)).font(.subheadline).foregroundStyle(.white.opacity(0.75)).lineLimit(1) }
            HStack(spacing: 10) {
                Button("Stop") { dismiss() }.buttonStyle(.bordered).tint(.white)
                Button("Keep watching") {
                    stillAsking = false
                    if let item = upNext { follow(item, byItself: false) }
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black.opacity(0.75).ignoresSafeArea())
    }

    private func failed(_ message: String) -> some View {
        VStack(spacing: 12) {
            Text("That didn't start").font(.headline).foregroundStyle(.white)
            Text(message).font(.subheadline).foregroundStyle(.white.opacity(0.75)).multilineTextAlignment(.center)
            Button("Close") { dismiss() }.buttonStyle(.borderedProminent)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// The person's queue and the next episode. A tap plays that one now; a swipe takes a title out.
    private var queueSheet: some View {
        NavigationStack {
            List {
                if queue.isEmpty && next == nil {
                    Text("Nothing is queued. Play next and Add to queue on a film or an episode put it here, from any screen.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                if !queue.isEmpty {
                    Section("Your queue") {
                        ForEach(queue) { item in
                            Button { showingQueue = false; follow(.queued(item), byItself: false) } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                                    if let line = item.line { Text(line).font(.caption).foregroundStyle(.secondary) }
                                }
                            }
                        }
                        .onDelete { rows in
                            let ids = rows.map { queue[$0].id }
                            queue.remove(atOffsets: rows)
                            Task { @MainActor in
                                for id in ids { if let left = await PhonePlayback.changeQueue(["remove": id]) { queue = left } }
                            }
                        }
                    }
                }
                if let next {
                    Section("Next episode") {
                        Button { showingQueue = false; follow(.episode(next), byItself: false) } label: {
                            Text(next.line).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                        }
                    }
                }
            }
            .navigationTitle("Up next")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { showingQueue = false } }
                if !queue.isEmpty {
                    ToolbarItem(placement: .destructiveAction) {
                        Button("Clear") {
                            queue = []
                            Task { @MainActor in if let left = await PhonePlayback.changeQueue(["clear": true]) { queue = left } }
                        }
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: Playing

    private func begin() {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        let playing = now
        let p = AVPlayer(url: playing.url)
        player = p
        run += 1
        position = 0
        length = 0
        segments = []
        segmentsAsked = false
        next = nil
        nextDismissed = false
        stillAsking = false
        problem = nil
        if let resume = playing.resumeS, resume > 0 {
            p.seek(to: CMTime(seconds: resume, preferredTimescale: 1), toleranceBefore: .zero, toleranceAfter: .positiveInfinity)
        }
        p.play()
        guard playing.reports else { return }
        ticker = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 60, preferredTimescale: 1), queue: .main) { _ in
            report(p)
        }
        clock = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 10), queue: .main) { time in
            tick(p, time.seconds)
        }
        loadHub()
    }

    private func tick(_ p: AVPlayer, _ seconds: Double) {
        guard seconds.isFinite else { return }
        position = seconds
        if let total = p.currentItem?.duration.seconds, total.isFinite, total > 0 { length = total }
        if !segmentsAsked, length > 0 {
            segmentsAsked = true
            loadSegments()
        }
        // A pause means someone is there: the "Still watching?" count starts again
        if p.timeControlStatus == .paused, length > 0, seconds < length - 2 { startedByItself = 0 }
    }

    /// The hub's next episode and the queue. What plays now leaves the queue.
    private func loadHub() {
        let mine = run, id = playingId
        let episode = now.request.kind == "episode" ? now.request.episodeId : nil
        Task { @MainActor in
            let following = await PhonePlayback.next(after: episode)
            var rows = await PhonePlayback.queue()
            if let held = rows, held.contains(where: { $0.id == id }) {
                rows = await PhonePlayback.changeQueue(["remove": id]) ?? held.filter { $0.id != id }
            }
            guard mine == run else { return }
            next = following
            if let rows { queue = rows }
        }
    }

    /// Asked once, when the player knows the length (the hub checks the sections against it).
    private func loadSegments() {
        let mine = run, request = now.request, total = length
        Task { @MainActor in
            let parts = await PhonePlayback.segments(request, duration: total)
            guard mine == run else { return }
            segments = parts
        }
    }

    private func skip(_ part: PlaySegment) {
        startedByItself = 0
        let target = length > 0 ? min(part.endS, length - 2) : part.endS
        player?.seek(to: CMTime(seconds: target, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    /// The film or episode ended: the queue's front or the next episode starts, unless "Not now" was pressed.
    private func ended() {
        guard !nextDismissed, !stillAsking, let item = upNext else { return }
        // Three in a row with nobody pressing anything: ask before playing on
        if startedByItself >= 3 {
            stillAsking = true
            return
        }
        follow(item, byItself: true)
    }

    private func follow(_ item: Following, byItself: Bool) {
        let request: TVPlayRequest
        switch item {
        case .queued(let q):
            queue.removeAll { $0.id == q.id }
            request = q.isEpisode
                ? TVPlayRequest(title: name(item), kind: "episode", seriesId: q.seriesId, episodeId: q.id)
                : TVPlayRequest(title: q.title, kind: "movie", movieId: q.id)
        case .episode(let e):
            request = TVPlayRequest(title: e.line, kind: "episode", seriesId: e.seriesId ?? now.request.seriesId,
                                    episodeId: e.episodeId, kids: now.request.kids)
        }
        halt()
        startedByItself = byItself ? startedByItself + 1 : 0
        Task { @MainActor in
            switch await PhonePlayback.start(request) {
            case .success(let started):
                followed = started
                begin()
            case .failure(let error):
                problem = error.message
            }
        }
    }

    private func halt() {
        guard let p = player else { return }
        if now.reports { report(p) }
        if let ticker { p.removeTimeObserver(ticker) }
        if let clock { p.removeTimeObserver(clock) }
        ticker = nil
        clock = nil
        p.pause()
        player = nil
    }

    private func report(_ p: AVPlayer) {
        let position = p.currentTime().seconds
        let duration = p.currentItem?.duration.seconds
        guard position.isFinite else { return }
        let request = now.request
        Task { await PhonePlayback.report(request, position: position, duration: duration) }
    }

    private func moveTo(_ tv: TVPlayer) {
        var request = now.request
        if now.reports, let seconds = player?.currentTime().seconds, seconds.isFinite, seconds > 0 { request.resumeS = seconds }
        store.perform("tv.play", target: tv.id, params: request.tvParams, done: "Playing on \(tv.name)")
        dismiss()
    }
}

// MARK: - One TV: Watch, Remote, Apps

/// Everything for one house TV on one screen: what's on, then Watch (tap to play on this TV), Remote and Apps.
struct TVHomeView: View {
    enum Part: String { case watch, remote, apps }
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let tvID: String
    @State var part: Part = .watch
    @State private var paused = false
    @State private var moving = false
    @State private var channels: [TVGuideChannel]?
    @State private var startingHere = false
    @State private var phoneSession: PhoneSession?
    @State private var query = ""
    @FocusState private var typing: Bool

    private var tv: TVPlayer? { store.houseTVs.first { $0.id == tvID || $0.mediaEntity == tvID } }
    private var deviceID: String { tv?.id ?? tvID }
    private var entity: String? { tv?.mediaEntity ?? (store.home?.device(tvID) != nil ? tvID : nil) }
    private var fullRemote: Bool {
        guard let entity, let d = store.home?.device(entity), case .object(let o)? = d["remote"] else { return false }
        return !(o["keys"]?.array ?? []).isEmpty
    }
    private var hasApps: Bool {
        guard let entity, let d = store.home?.device(entity), case .object(let o)? = d["remote"] else { return false }
        return !(o["apps"]?.array ?? []).isEmpty
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                nowCard
                Picker("", selection: $part) {
                    Text("Watch").tag(Part.watch)
                    Text("Remote").tag(Part.remote)
                    if hasApps { Text("Apps").tag(Part.apps) }
                }
                .pickerStyle(.segmented)
                switch part {
                case .watch: watch
                case .remote: remote
                case .apps:
                    if let entity { RemoteView(deviceID: entity, part: .apps, embedded: true) }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(tv?.name ?? "TV")
        .navigationBarTitleDisplayMode(.inline)
        .task { if store.houseTVs.isEmpty { await store.loadHouseTVs() } }
        .sheet(isPresented: $moving) { MoveToSheet(from: deviceID, title: tv?.playing?.title ?? tv?.playing?.name ?? "It") }
        .fullScreenCover(item: $phoneSession) { PhonePlayerView(session: $0) }
    }

    // MARK: what's on

    private var nowCard: some View {
        let playing = tv?.playing
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                HubImage(path: playing?.posterUrl) {
                    Image(systemName: "tv").font(.title2).foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card2)
                }
                .frame(width: 96, height: 64)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(tv?.online == false ? "Asleep" : (playing == nil ? "Nothing playing" : "Now playing"))
                        .font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                    Text(playing?.title ?? playing?.name ?? tv?.name ?? "TV").font(.headline).foregroundStyle(Theme.fg).lineLimit(2)
                    if let name = playing?.name, playing?.title != nil {
                        Text([playing?.number.map { "\($0)" }, name].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                    }
                    if let line = tv?.watching?.line, tv?.online != false {
                        Label(line, systemImage: "person.fill").font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            if let tv, tv.online != false { ImWatchingChip(tv: tv) }
            if let resume = continueHere {
                Button { Task { await continueOnPhone(resume) } } label: {
                    Label(startingHere ? "Starting…" : "Continue on iPhone", systemImage: "iphone")
                        .font(.subheadline.weight(.semibold)).padding(.horizontal, 14).frame(minHeight: 44)
                        .background(Theme.card2, in: Capsule())
                }
                .buttonStyle(.plain).disabled(startingHere)
            }
            if playing != nil {
                HStack(spacing: 12) {
                    key(paused ? "play.fill" : "pause.fill", paused ? "Play" : "Pause") { toggle() }
                    if playing?.channelKey != nil {
                        key("chevron.down", "Channel down") { send(TVRemoteBody.channelDown(deviceID)) }
                        key("chevron.up", "Channel up") { send(TVRemoteBody.channelUp(deviceID)) }
                    }
                    Spacer()
                    Button { moving = true } label: {
                        Label("Move", systemImage: "arrow.right.square").font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 14).frame(minHeight: 44).background(Theme.card2, in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .card()
    }

    /// A film or episode playing on this TV, as a request the phone can open at the TV's second.
    private var continueHere: TVPlayRequest? {
        guard tv?.online != false, let p = tv?.playing else { return nil }
        let title = p.title ?? p.name ?? "It"
        if let movie = p.movieId { return TVPlayRequest(title: title, kind: "movie", movieId: movie) }
        if let episode = p.episodeId {
            return TVPlayRequest(title: title, kind: "episode", seriesId: p.seriesId, episodeId: episode)
        }
        return nil
    }

    private func continueOnPhone(_ request: TVPlayRequest) async {
        startingHere = true
        defer { startingHere = false }
        switch await PhonePlayback.start(request, startS: tv?.playing?.positionS) {
        case .success(let session): phoneSession = session
        case .failure(let why): store.show(why.message, request.title, isError: true)
        }
    }

    // MARK: Watch

    @ViewBuilder private var watch: some View {
        TVForYouRows(profile: tv?.profile == "kids" ? "kids" : nil) { playHere($0) }
        if let channels, !channels.isEmpty {
            SectionHeader("On TV now")
            ForEach(channels.prefix(12)) { ch in
                Button { playHere(TVPlayRequest(title: ch.now?.title ?? ch.name, channelKey: ch.key)) } label: {
                    HStack(spacing: 10) {
                        HubImage(path: ch.logoUrl ?? ch.logo) {
                            Image(systemName: "tv").foregroundStyle(Theme.dim)
                                .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card2)
                        }
                        .frame(width: 44, height: 44).clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(ch.now?.title ?? ch.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                            Text([ch.number.map { "\($0)" }, ch.name].compactMap { $0 }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                        }
                        Spacer()
                        Image(systemName: "play.fill").foregroundStyle(Theme.accent)
                    }
                }
                .buttonStyle(.plain).card()
            }
        }
        Color.clear.frame(height: 1).task(id: tvID) { await loadChannels() }
    }

    private func loadChannels() async {
        guard channels == nil else { return }
        let page = await hubQuiet("tv/guide", { try await HubClient.shared.get("/v1/tv/guide", as: TVGuidePage.self) })
        channels = (page?.channels ?? []).filter { $0.now != nil }
    }

    private func playHere(_ request: TVPlayRequest) {
        if tv?.profile == "kids", request.kids == false {
            store.show("That isn't for a kids' TV", request.title, isError: true)
            return
        }
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        store.perform("tv.play", target: deviceID, params: request.tvParams, done: "Playing on \(tv?.name ?? "the TV")")
    }

    // MARK: Remote

    @ViewBuilder private var remote: some View {
        if let entity, fullRemote {
            RemoteView(deviceID: entity, part: .remote, embedded: true)
        } else {
            simpleRemote
        }
    }

    /// Thomas Creek TV on its own (no Home Assistant remote): the buttons its app answers.
    private var simpleRemote: some View {
        VStack(spacing: 18) {
            HStack(spacing: 22) {
                key("arrow.uturn.backward", "Back") { send(TVRemoteBody.back(deviceID)) }
                key("house.fill", "Home") { send(TVRemoteBody.home(deviceID)) }
                key(paused ? "play.fill" : "pause.fill", paused ? "Play" : "Pause") { toggle() }
            }
            HStack(spacing: 12) {
                VStack(spacing: 6) {
                    key("chevron.up", "Channel up") { send(TVRemoteBody.channelUp(deviceID)) }
                    Text("CH").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                    key("chevron.down", "Channel down") { send(TVRemoteBody.channelDown(deviceID)) }
                }
                .padding(10).background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
            HStack(spacing: 10) {
                TextField("Search on the TV", text: $query)
                    .textFieldStyle(.roundedBorder).submitLabel(.search).focused($typing).onSubmit(type)
                Button(action: type) {
                    Image(systemName: "magnifyingglass").frame(width: 44, height: 44).background(Theme.card, in: Circle())
                }
                .buttonStyle(.plain).accessibilityLabel("Search on the TV")
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func key(_ symbol: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.title3.weight(.semibold)).foregroundStyle(Theme.fg)
                .frame(width: 56, height: 56).background(Theme.card2, in: Circle())
        }
        .buttonStyle(.plain).accessibilityLabel(label)
    }

    private func toggle() {
        let was = paused
        paused.toggle()
        let body = paused ? TVRemoteBody.pause(deviceID) : TVRemoteBody.play(deviceID)
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        Task { if let line = await store.press(body) { paused = was; store.show(line) } }
    }

    private func type() {
        guard let body = TVRemoteBody.type(deviceID, query) else { return }
        query = ""
        typing = false
        send(body)
    }

    private func send(_ body: TVRemoteBody) {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        Task { if let line = await store.press(body) { store.show(line) } }
    }
}

/// `.remote(id)`: a house TV with Thomas Creek TV gets its one screen (opened on Remote); any other box keeps
/// the plain remote.
struct RemoteRoute: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    var body: some View {
        if store.houseTVs.contains(where: { $0.mediaEntity == deviceID || $0.id == deviceID }) {
            TVHomeView(tvID: deviceID, part: .remote)
        } else {
            RemoteView(deviceID: deviceID)
        }
    }
}

extension TVPlayRequest {
    /// The `tv.play` params for a house TV.
    var tvParams: [String: Any] {
        var params: [String: Any] = ["title": title]
        switch kind {
        case "catchup":
            params["kind"] = "catchup"
            params["channel_key"] = channelKey ?? ""
            if let start { params["start"] = start }
        case "movie":
            params["movie_id"] = movieId ?? ""
        case "episode":
            params["episode_id"] = episodeId ?? ""
            params["series_id"] = seriesId ?? ""
        default:
            params["channel_key"] = channelKey ?? ""
        }
        if let resumeS, resumeS > 0, kind == "movie" || kind == "episode" { params["resume"] = Int(resumeS) }
        return params
    }
}
