import AVKit
import SwiftUI

// The phone's view of the household IPTV library. The hub holds the provider login;
// this screen only asks for guide rows, series, movies and which TVs this person may use.


struct TVGuideView: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    @State private var section = "foryou"
    @State private var loadFailed = false
    @State private var query = ""
    @State private var guide: [TVGuideChannel]?
    @State private var shows: [TVShow]?
    @State private var films: [TVShow]?
    @State private var recordings: [DvrItem]?
    @State private var found: TVLibrarySearch?
    @State private var play: TVPlayRequest?
    @State private var playback: URL?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Picker("Library", selection: $section) {
                    Text("For you").tag("foryou")
                    Text("Guide").tag("guide")
                    Text("Shows").tag("shows")
                    Text("Movies").tag("movies")
                    Text("Saved").tag("saved")
                }
                .pickerStyle(.segmented)
                if query.count >= 2 {
                    searchResults
                } else if section == "foryou" {
                    TVForYouRows(profile: nil) { play = $0 }
                } else if loadFailed {
                    EmptyCard(symbol: "wifi.exclamationmark", title: "The hub didn't answer",
                              detail: "Pull down to try again.")
                } else if section == "guide" {
                    guideList
                } else if section == "shows" {
                    posterGrid(shows, empty: "No shows") { router.go(.tvSeries($0.id)) }
                } else if section == "saved" {
                    savedList
                } else {
                    posterGrid(films, empty: "No movies") { router.go(.tvMovie($0.id)) }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("TV")
        .searchable(text: $query, prompt: "Channels, shows and movies")
        .topBar()
        .task(id: section) { await loadSection() }
        .task(id: query) { await search() }
        .refreshable { await loadSection() }
        .sheet(item: $play) { PlayOnTVSheet(request: $0) }
        .sheet(isPresented: Binding(get: { playback != nil }, set: { if !$0 { playback = nil } })) {
            if let playback {
                NavigationStack {
                    VideoPlayer(player: AVPlayer(url: playback)).ignoresSafeArea()
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { self.playback = nil } } }
                }
            }
        }
    }

    @ViewBuilder private var savedList: some View {
        if let recordings {
            if recordings.isEmpty {
                EmptyCard(symbol: "recordingtape", title: "No recordings", detail: "Record a programme from the guide.")
            }
            ForEach(recordings) { item in
                Button { Task { await playSaved(item) } } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title ?? "Recording").font(.headline).foregroundStyle(Theme.fg)
                            Text(item.status).font(.caption).foregroundStyle(Theme.dim)
                        }
                        Spacer()
                        if item.status == "done" || item.status == "recording" {
                            Image(systemName: "play.fill").foregroundStyle(Theme.accent)
                        }
                    }
                }
                .buttonStyle(.plain).card()
            }
        } else { LoadingCard() }
    }

    @ViewBuilder private var guideList: some View {
        if let guide {
            if guide.isEmpty {
                EmptyCard(symbol: "tv", title: "Nothing in the guide", detail: "The hub hasn't loaded the TV source yet.")
            }
            ForEach(guide) { channel in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        TVArt(url: channel.logoUrl ?? channel.logo, symbol: "tv")
                        VStack(alignment: .leading, spacing: 2) {
                            Text(channel.name).font(.headline)
                            if let now = channel.now {
                                Text(now.title).font(.subheadline).foregroundStyle(Theme.dim).lineLimit(1)
                            }
                        }
                        Spacer()
                        if let number = channel.number {
                            Text("\(number)").font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
                        }
                    }
                    if let now = channel.now {
                        programmeButton(now, channel: channel, live: true)
                        if now.catchup == true {
                            programmeButton(now, channel: channel, live: false)
                        }
                    }
                    if let next = channel.next {
                        programmeButton(next, channel: channel, live: false)
                    }
                }
                .card()
            }
        } else { LoadingCard() }
    }

    private func programmeButton(_ item: TVProgramme, channel: TVGuideChannel, live: Bool) -> some View {
        HStack {
            Button {
                play = TVPlayRequest(
                    title: item.title,
                    kind: item.catchup == true && !live ? "catchup" : "live",
                    channelKey: channel.key,
                    start: item.catchup == true && !live ? item.start : nil)
            } label: {
                HStack {
                    TVArt(url: item.posterUrl ?? item.backdropUrl ?? item.icon, symbol: "film").frame(width: 36, height: 36)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                        Text(live ? "On now" : (item.catchup == true ? "Start over · \(tvClock(item.start))" : tvClock(item.start)))
                            .font(.caption).foregroundStyle(Theme.dim)
                    }
                    Spacer()
                    Image(systemName: "play.tv").foregroundStyle(Theme.accent)
                }
            }
            .buttonStyle(.plain)
            if item.end != nil {
                Button { Task { await record(item, channel) } } label: {
                    Image(systemName: "record.circle").font(.title3).foregroundStyle(Theme.warn)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Record")
            }
        }
    }

    private func record(_ item: TVProgramme, _ channel: TVGuideChannel) async {
        guard let end = item.end else { return }
        do {
            let _: DvrItem = try await HubClient.shared.post("/v1/dvr/recordings", [
                "channel_key": channel.key, "start": item.start, "end": end, "title": item.title,
            ])
            store.show("Recording", item.title)
        } catch {
            store.show(error.localizedDescription, item.title, isError: true)
        }
    }

    private func playSaved(_ item: DvrItem) async {
        guard let path = item.streamUrl, item.status == "done" || item.status == "recording" else { return }
        do {
            let request = try await HubClient.shared.authorizedRequest(path)
            let (data, response) = try await HubClient.session.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard (200..<300).contains(code), !data.isEmpty else { return }
            let file = FileManager.default.temporaryDirectory.appendingPathComponent("\(item.id).ts")
            try data.write(to: file)
            playback = file
        } catch {
            store.show(error.localizedDescription, item.title, isError: true)
        }
    }

    private func posterGrid(_ items: [TVShow]?, empty: String, open: @escaping (TVShow) -> Void) -> some View {
        Group {
            if let items {
                if items.isEmpty { EmptyCard(symbol: "film", title: empty, detail: nil) }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                    ForEach(items) { item in
                        Button { open(item) } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                TVArt(url: item.posterUrl ?? item.cover ?? item.backdropUrl ?? item.backdrop, symbol: "film")
                                    .frame(maxWidth: .infinity).frame(height: 150)
                                Text(item.name).font(.caption.weight(.semibold)).lineLimit(2).foregroundStyle(Theme.fg)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
            } else { LoadingCard() }
        }
    }

    @ViewBuilder private var searchResults: some View {
        if let found {
            if let channels = found.channels, !channels.isEmpty {
                SectionHeader("Channels")
                ForEach(channels) { channel in
                    Button { play = TVPlayRequest(title: channel.now?.title ?? channel.name, channelKey: channel.key) } label: {
                        HStack {
                            TVArt(url: channel.logoUrl ?? channel.logo, symbol: "tv")
                            Text(channel.name).foregroundStyle(Theme.fg)
                            Spacer()
                            Image(systemName: "play.tv").foregroundStyle(Theme.accent)
                        }
                    }
                    .buttonStyle(.plain).card()
                }
            }
            if let programmes = found.programmes, !programmes.isEmpty {
                SectionHeader("On the guide")
                ForEach(Array(programmes.enumerated()), id: \.offset) { _, item in
                    Button {
                        play = TVPlayRequest(title: item.title, kind: item.catchup == true ? "catchup" : "live",
                                             channelKey: item.channelKey, start: item.catchup == true ? item.start : nil)
                    } label: {
                        HStack(spacing: 10) {
                            TVArt(url: item.posterUrl ?? item.backdropUrl ?? item.icon, symbol: "film").frame(width: 36, height: 36)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                                Text([item.channelName, tvClock(item.start)].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .buttonStyle(.plain).card()
                }
            }
            if let series = found.series, !series.isEmpty {
                SectionHeader("Shows")
                posterGrid(series, empty: "") { router.go(.tvSeries($0.id)) }
            }
            if let movies = found.movies, !movies.isEmpty {
                SectionHeader("Movies")
                posterGrid(movies, empty: "") { router.go(.tvMovie($0.id)) }
            }
            if (found.channels ?? []).isEmpty && (found.programmes ?? []).isEmpty
                && (found.series ?? []).isEmpty && (found.movies ?? []).isEmpty {
                EmptyCard(symbol: "magnifyingglass", title: "No matches", detail: "Try another spelling.")
            }
        } else { LoadingCard() }
    }

    /// A failed load says so (it used to look like an empty library).
    private func loadSection() async {
        loadFailed = false
        do {
            if section == "foryou" {
                return
            } else if section == "guide" {
                guide = try await HubClient.shared.get("/v1/tv/guide", as: TVGuidePage.self).channels
            } else if section == "shows" {
                shows = try await HubClient.shared.get("/v1/tv/series", as: TVSeriesPage.self).series
            } else if section == "saved" {
                recordings = try await HubClient.shared.get("/v1/dvr/recordings", as: DvrPage.self).recordings
            } else {
                films = try await HubClient.shared.get("/v1/tv/movies", as: TVMoviePage.self).movies
            }
        } catch is CancellationError {
        } catch HubClient.HubError.cancelled {
        } catch {
            loadFailed = true
        }
    }

    private func search() async {
        guard query.count >= 2 else { found = nil; return }
        try? await Task.sleep(for: .milliseconds(300))
        guard !Task.isCancelled else { return }
        found = await hubQuiet("tv/library/search", { try await HubClient.shared.get("/v1/tv/library/search", ["q": query]) })
    }
}

struct TVSeriesView: View {
    var seriesID: String
    @State private var show: TVShow?
    @State private var missing = false
    @State private var play: TVPlayRequest?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let show {
                    header(show.name, show.plot, show.posterUrl ?? show.cover ?? show.backdropUrl ?? show.backdrop)
                    ForEach(show.seasons ?? []) { season in
                        SectionHeader("Season \(season.number)")
                        ForEach(season.episodes) { episode in
                            Button {
                                play = TVPlayRequest(title: episode.title ?? show.name, kind: "episode",
                                                     seriesId: show.id, episodeId: episode.id, kids: show.kids)
                            } label: {
                                HStack(spacing: 10) {
                                    TVArt(url: episode.stillUrl ?? episode.still ?? show.posterUrl ?? show.cover, symbol: "film")
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(episode.title ?? "Episode").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                                        if let n = episode.number { Text("Episode \(n)").font(.caption).foregroundStyle(Theme.dim) }
                                    }
                                    Spacer()
                                    Image(systemName: "play.tv").foregroundStyle(Theme.accent)
                                }
                            }
                            .buttonStyle(.plain).card()
                        }
                    }
                } else if missing {
                    EmptyCard(symbol: "film", title: "That show isn't available", detail: nil)
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(show?.name ?? "Show")
        .topBar()
        .task { await load() }
        .sheet(item: $play) { PlayOnTVSheet(request: $0) }
    }

    private func load() async {
        do { show = try await HubClient.shared.get("/v1/tv/series/\(seriesID)") }
        catch { missing = true }
    }
}

struct TVMovieView: View {
    var movieID: String
    @State private var movie: TVShow?
    @State private var missing = false
    @State private var play: TVPlayRequest?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let movie {
                    header(movie.name, movie.plot, movie.posterUrl ?? movie.cover ?? movie.backdropUrl ?? movie.backdrop)
                    Button { play = TVPlayRequest(title: movie.name, kind: "movie", movieId: movie.id, kids: movie.kids) } label: {
                        Label("Play", systemImage: "play.fill").font(.headline).frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(.borderedProminent)
                } else if missing {
                    EmptyCard(symbol: "film", title: "That movie isn't available", detail: nil)
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(movie?.name ?? "Movie")
        .topBar()
        .task { await load() }
        .sheet(item: $play) { PlayOnTVSheet(request: $0) }
    }

    private func load() async {
        do { movie = try await HubClient.shared.get("/v1/tv/movies/\(movieID)") }
        catch { missing = true }
    }
}

struct PlayOnTVSheet: View {
    var request: TVPlayRequest
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store
    @State private var tvs: [TVPlayer]?
    @State private var failed = false
    @State private var starting = false
    @State private var phone: PhoneSession?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text(request.title).font(.headline).padding(.bottom, 4)
                    if PhonePlayback.canPlay(request) {
                        Button { Task { await playOnPhone() } } label: {
                            HStack {
                                Image(systemName: "iphone").font(.title3).foregroundStyle(Theme.accent).frame(width: 28)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("This iPhone").font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                                    Text("Watch here").font(.caption).foregroundStyle(Theme.dim)
                                }
                                Spacer()
                                if starting { ProgressView() } else { Image(systemName: "play.fill").foregroundStyle(Theme.accent) }
                            }
                        }
                        .buttonStyle(.plain).card().disabled(starting)
                        SectionHeader("On a TV")
                    }
                    if let tvs {
                        if tvs.isEmpty {
                            EmptyCard(symbol: "tv.slash", title: "No TV to play on",
                                      detail: failed ? "The guide didn't answer." : "None of the house TVs are yours to use.")
                        }
                        ForEach(tvs) { tv in
                            let blocked = tv.profile == "kids" && request.kids == false
                            Button { send(tv) } label: {
                                HStack {
                                    Image(systemName: "tv").font(.title3).foregroundStyle(blocked ? Theme.dim : Theme.accent).frame(width: 28)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(tv.name).font(.body.weight(.semibold)).foregroundStyle(blocked ? Theme.dim : Theme.fg)
                                        Text(blocked ? "Kids' TV · not for this one" : line(tv))
                                            .font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                                    }
                                    Spacer()
                                    Image(systemName: blocked ? "lock.fill" : "play.fill").foregroundStyle(blocked ? Theme.dim : Theme.accent)
                                }
                            }
                            .buttonStyle(.plain).card().disabled(blocked)
                        }
                    } else { LoadingCard() }
                }
                .padding(16)
            }
            .background(Backdrop())
            .navigationTitle("Play on")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
        }
        .task { await load() }
        .fullScreenCover(isPresented: Binding(get: { phone != nil }, set: { if !$0 { phone = nil; dismiss() } })) {
            if let phone { PhonePlayerView(session: phone) }
        }
    }

    private func playOnPhone() async {
        starting = true
        defer { starting = false }
        switch await PhonePlayback.start(request) {
        case .success(let session): phone = session
        case .failure(let why): store.show(why.message, request.title, isError: true)
        }
    }

    private func line(_ tv: TVPlayer) -> String {
        let where_ = tv.roomName ?? (tv.online == true ? "On" : "Asleep")
        if let title = tv.playing?.title ?? tv.playing?.name, !title.isEmpty { return "\(where_) · \(title)" }
        return tv.online == true ? "\(where_) · nothing playing" : "\(where_) · asleep"
    }

    private func load() async {
        do { tvs = try await HubClient.shared.get("/v1/tv/players", as: TVPlayerList.self).tvs }
        catch { tvs = []; failed = true }
    }

    private func send(_ tv: TVPlayer) {
        store.perform("tv.play", target: tv.id, params: request.tvParams, done: "Playing on \(tv.name)")
        dismiss()
    }
}

/// Pick another room. The hub already ordered them: the room this person last used is first.
struct MoveToSheet: View {
    var from: String
    var title: String
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store

    private var others: [TVPlayer] {
        let mine = store.houseTVs.first { $0.id == from || $0.mediaEntity == from }?.id
        return store.houseTVs.filter { $0.id != mine }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text(title).font(.headline).padding(.bottom, 4)
                    if store.tvsLoadedAt == nil && store.houseTVs.isEmpty {
                        LoadingCard()
                    } else if others.isEmpty {
                        EmptyCard(symbol: "tv", title: "Nowhere else to play it",
                                  detail: "This is the only TV in the house right now.")
                    } else {
                        ForEach(others) { tv in
                            Button {
                                UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                                store.moveTV(from: from, to: tv, title: title)
                                dismiss()
                            } label: {
                                HStack(spacing: 12) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(tv.name).font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                                        Text(caption(tv)).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                                    }
                                    Spacer()
                                    Image(systemName: "arrow.right.square").font(.title3).foregroundStyle(Theme.accent)
                                }
                            }
                            .buttonStyle(.plain).card()
                        }
                    }
                }
                .padding(16)
                .animation(.snappy, value: others.map(\.id))
            }
            .background(Backdrop())
            .navigationTitle("Move to")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
        .task { await store.loadHouseTVs() }
    }

    private func caption(_ tv: TVPlayer) -> String {
        if tv.usual == true { return "Where you usually move it" }
        if tv.online == false { return "Asleep. It'll wake up." }
        if let on = tv.playing?.title ?? tv.playing?.name, !on.isEmpty { return "On · \(on)" }
        return "Ready"
    }
}

private struct TVArt: View {
    var url: String?
    var symbol: String
    var body: some View {
        HubImage(path: url) { placeholder }
        .frame(width: 56, height: 56)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
    private var placeholder: some View {
        Image(systemName: symbol).font(.title3).foregroundStyle(Theme.dim)
            .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card)
    }
}

private func header(_ title: String, _ plot: String?, _ art: String?) -> some View {
    VStack(alignment: .leading, spacing: 10) {
        TVArt(url: art, symbol: "film").frame(width: 120, height: 170)
        Text(title).font(.title2.weight(.semibold))
        if let plot, !plot.isEmpty { Text(plot).font(.subheadline).foregroundStyle(Theme.dim) }
    }
}

private func tvClock(_ iso: String?) -> String {
    guard let iso, let date = ISO.parse(iso) else { return "" }
    return date.formatted(date: .omitted, time: .shortened)
}
