import SwiftUI

// The phone's view of the household IPTV library. The hub holds the provider login;
// this screen only asks for guide rows, series, movies and which TVs this person may use.

struct TVPlayerList: Decodable { var tvs: [TVPlayer] }
struct TVPlayer: Decodable, Identifiable {
    var id: String
    var name: String
    var roomName: String?
    var profile: String?
    var online: Bool?
    var playing: TVPlaying?
}
struct TVPlaying: Decodable, Hashable {
    var channelKey: String?
    var title: String?
    var name: String?
}

struct TVGuidePage: Decodable { var channels: [TVGuideChannel] }
struct TVGuideChannel: Decodable, Identifiable {
    var key: String
    var name: String
    var number: Int?
    var logo: String?
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
    var year: String?
    var genre: String?
    var category: String?
    var plot: String?
    var backdrop: String?
    var seasons: [TVSeason]?
    var durationS: Int?
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
    var durationS: Int?
}
struct TVLibrarySearch: Decodable {
    var channels: [TVGuideChannel]?
    var programmes: [TVProgramme]?
    var series: [TVShow]?
    var movies: [TVShow]?
}

struct TVPlayRequest: Identifiable {
    var id: String { title + (channelKey ?? "") + (start ?? "") + (movieId ?? "") + (episodeId ?? "") }
    var title: String
    var kind: String = "live"
    var channelKey: String?
    var start: String?
    var movieId: String?
    var seriesId: String?
    var episodeId: String?
}

struct TVGuideView: View {
    @Environment(Router.self) private var router
    @State private var section = "guide"
    @State private var query = ""
    @State private var guide: [TVGuideChannel]?
    @State private var shows: [TVShow]?
    @State private var films: [TVShow]?
    @State private var found: TVLibrarySearch?
    @State private var play: TVPlayRequest?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Picker("Library", selection: $section) {
                    Text("Guide").tag("guide")
                    Text("Shows").tag("shows")
                    Text("Movies").tag("movies")
                }
                .pickerStyle(.segmented)
                if query.count >= 2 {
                    searchResults
                } else if section == "guide" {
                    guideList
                } else if section == "shows" {
                    posterGrid(shows, empty: "No shows") { router.go(.tvSeries($0.id)) }
                } else {
                    posterGrid(films, empty: "No movies") { router.go(.tvMovie($0.id)) }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("TV guide")
        .searchable(text: $query, prompt: "Channels, shows and movies")
        .topBar()
        .task(id: section) { await loadSection() }
        .task(id: query) { await search() }
        .refreshable { await loadSection() }
        .sheet(item: $play) { PlayOnTVSheet(request: $0) }
    }

    @ViewBuilder private var guideList: some View {
        if let guide {
            if guide.isEmpty {
                EmptyCard(symbol: "tv", title: "Nothing in the guide", detail: "The hub hasn't loaded the TV source yet.")
            }
            ForEach(guide) { channel in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        TVArt(url: channel.logo, symbol: "tv")
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
        Button {
            play = TVPlayRequest(
                title: item.title,
                kind: item.catchup == true && !live ? "catchup" : "live",
                channelKey: channel.key,
                start: item.catchup == true && !live ? item.start : nil)
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                    Text(live ? "On now" : (item.catchup == true ? "Start over · \(clock(item.start))" : clock(item.start)))
                        .font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
                Image(systemName: "play.tv").foregroundStyle(Theme.accent)
            }
        }
        .buttonStyle(.plain)
    }

    private func posterGrid(_ items: [TVShow]?, empty: String, open: @escaping (TVShow) -> Void) -> some View {
        Group {
            if let items {
                if items.isEmpty { EmptyCard(symbol: "film", title: empty, detail: nil) }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                    ForEach(items) { item in
                        Button { open(item) } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                TVArt(url: item.cover, symbol: "film")
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
                            TVArt(url: channel.logo, symbol: "tv")
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
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                            Text([item.channelName, clock(item.start)].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(Theme.dim)
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

    private func loadSection() async {
        if section == "guide" {
            guide = (try? await HubClient.shared.get("/v1/tv/guide", as: TVGuidePage.self))?.channels ?? []
        } else if section == "shows" {
            shows = (try? await HubClient.shared.get("/v1/tv/series", as: TVSeriesPage.self))?.series ?? []
        } else {
            films = (try? await HubClient.shared.get("/v1/tv/movies", as: TVMoviePage.self))?.movies ?? []
        }
    }

    private func search() async {
        guard query.count >= 2 else { found = nil; return }
        try? await Task.sleep(for: .milliseconds(300))
        guard !Task.isCancelled else { return }
        found = try? await HubClient.shared.get("/v1/tv/library/search", ["q": query])
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
                    header(show.name, show.plot, show.cover ?? show.backdrop)
                    ForEach(show.seasons ?? []) { season in
                        SectionHeader("Season \(season.number)")
                        ForEach(season.episodes) { episode in
                            Button {
                                play = TVPlayRequest(title: episode.title ?? show.name, kind: "episode",
                                                     seriesId: show.id, episodeId: episode.id)
                            } label: {
                                HStack(spacing: 10) {
                                    TVArt(url: episode.still ?? show.cover, symbol: "film")
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
                    header(movie.name, movie.plot, movie.cover ?? movie.backdrop)
                    Button { play = TVPlayRequest(title: movie.name, kind: "movie", movieId: movie.id) } label: {
                        Label("Play on a TV", systemImage: "play.tv").font(.headline).frame(maxWidth: .infinity, minHeight: 48)
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

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text(request.title).font(.headline).padding(.bottom, 4)
                    if let tvs {
                        if tvs.isEmpty {
                            EmptyCard(symbol: "tv.slash", title: "No TV to play on",
                                      detail: failed ? "The guide didn't answer." : "None of the house TVs are yours to use.")
                        }
                        ForEach(tvs) { tv in
                            Button { send(tv) } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(tv.name).font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                                        Text(line(tv)).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                                    }
                                    Spacer()
                                    Image(systemName: "play.fill").foregroundStyle(Theme.accent)
                                }
                            }
                            .buttonStyle(.plain).card()
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
        var params: [String: Any] = ["title": request.title]
        switch request.kind {
        case "catchup":
            params["kind"] = "catchup"
            params["channel_key"] = request.channelKey ?? ""
            if let start = request.start { params["start"] = start }
        case "movie":
            params["movie_id"] = request.movieId ?? ""
        case "episode":
            params["episode_id"] = request.episodeId ?? ""
            params["series_id"] = request.seriesId ?? ""
        default:
            params["channel_key"] = request.channelKey ?? ""
        }
        store.perform("tv.play", target: tv.id, params: params, done: "Sent to \(tv.name)")
        dismiss()
    }
}

private struct TVArt: View {
    var url: String?
    var symbol: String
    var body: some View {
        Group {
            if let url, let u = URL(string: url) {
                AsyncImage(url: u) { image in image.resizable().scaledToFill() } placeholder: { placeholder }
            } else { placeholder }
        }
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

private func clock(_ iso: String?) -> String {
    guard let iso, let date = ISO.parse(iso) else { return "" }
    return date.formatted(date: .omitted, time: .shortened)
}
