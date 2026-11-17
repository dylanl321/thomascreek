import AVKit
import SwiftUI
import TVPhone

// The same rows the TV's own home screen shows (`/v1/tv/explore` and `/v1/tv/upnext`), on the phone.
// A tap plays: on this iPhone through the hub (`/v1/tv/phone/play`), or on a house TV (`tv.play`).
// The provider login stays on the hub; the phone only ever gets hub paths.

struct TVFeed: Decodable {
    var hero: TVFeedItem?
    var rows: [TVFeedRow]
}
struct TVFeedRow: Decodable, Identifiable {
    var id: String
    var title: String
    var items: [TVFeedItem]
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
        async let rows: TVFeed? = try? await HubClient.shared.get("/v1/tv/explore", query, as: TVFeed.self)
        async let shelf: TVFeed? = try? await HubClient.shared.get("/v1/tv/upnext", query, as: TVFeed.self)
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
}

struct PhoneError: Error { var message: String }

/// Full screen, with the system controls (AirPlay and picture in picture included). Starts where the person
/// stopped, reports every minute and on close, and can hand the show to a house TV from the same second.
struct PhonePlayerView: View {
    var session: PhoneSession
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store
    @State private var player: AVPlayer?
    @State private var ticker: Any?
    @State private var choosingTV = false

    private var title: String { session.request.title }

    var body: some View {
        ZStack(alignment: .top) {
            Color.black.ignoresSafeArea()
            if let player { VideoPlayer(player: player).ignoresSafeArea() } else { ProgressView().tint(.white) }
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.headline).foregroundStyle(.white).padding(12)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .accessibilityLabel("Close \(title)")
                Spacer()
                if session.request.kind != "catchup", session.request.plexId == nil {
                    Button { choosingTV = true } label: {
                        Label("Play on a TV", systemImage: "tv").font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 14).frame(minHeight: 44).background(.ultraThinMaterial, in: Capsule())
                    }
                }
            }
            .padding(16)
        }
        .onAppear(perform: start)
        .onDisappear(perform: stop)
        .confirmationDialog("Play on a TV", isPresented: $choosingTV, titleVisibility: .visible) {
            ForEach(store.houseTVs.filter { !($0.profile == "kids" && session.request.kids == false) }) { tv in
                Button(tv.name) { moveTo(tv) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text("\(title) starts there from this second.") }
        .task { if store.houseTVs.isEmpty { await store.loadHouseTVs() } }
    }

    private func start() {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        let p = AVPlayer(url: session.url)
        player = p
        if let resume = session.resumeS, resume > 0 {
            p.seek(to: CMTime(seconds: resume, preferredTimescale: 1), toleranceBefore: .zero, toleranceAfter: .positiveInfinity)
        }
        p.play()
        guard session.reports else { return }
        ticker = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 60, preferredTimescale: 1), queue: .main) { _ in
            report(p)
        }
    }

    private func stop() {
        guard let p = player else { return }
        if session.reports { report(p) }
        if let ticker { p.removeTimeObserver(ticker) }
        ticker = nil
        p.pause()
        player = nil
    }

    private func report(_ p: AVPlayer) {
        let position = p.currentTime().seconds
        let duration = p.currentItem?.duration.seconds
        guard position.isFinite else { return }
        let request = session.request
        Task { await PhonePlayback.report(request, position: position, duration: duration) }
    }

    private func moveTo(_ tv: TVPlayer) {
        var request = session.request
        if session.reports, let seconds = player?.currentTime().seconds, seconds.isFinite, seconds > 0 { request.resumeS = seconds }
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
        let page = try? await HubClient.shared.get("/v1/tv/guide", as: TVGuidePage.self)
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
