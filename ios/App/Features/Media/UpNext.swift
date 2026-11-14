import SwiftUI

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

struct UpNextStrip: View {
    @Environment(HomeStore.self) private var store
    @State private var rows: [UpNextRow] = []
    @State private var play: TVPlayRequest?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(rows) { row in
                VStack(alignment: .leading, spacing: 8) {
                    SectionHeader(row.title)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 10) {
                            ForEach(row.items) { item in
                                Button {
                                    UIImpactFeedbackGenerator(style: .soft).impactOccurred()
                                    open(item)
                                } label: { tile(item) }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    .scrollClipDisabled()
                }
            }
        }
        .animation(.snappy, value: rows.map(\.id))
        .task { await load() }
        .sheet(item: $play) { PlayOnTVSheet(request: $0) }
    }

    private func tile(_ item: UpNextItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HubImage(path: item.posterUrl) {
                Image(systemName: symbol(item.kind)).font(.title3).foregroundStyle(Theme.dim)
                    .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card2)
            }
            .frame(width: 112, height: 64)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(alignment: .bottom) {
                if let progress = item.progress {
                    ProgressBar(value: progress).padding(.horizontal, 6).padding(.bottom, 4)
                }
            }
            Text(item.title).font(.caption.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1).frame(width: 112, alignment: .leading)
            if let subtitle = item.subtitle {
                Text(subtitle).font(.caption2).foregroundStyle(Theme.dim).lineLimit(1).frame(width: 112, alignment: .leading)
            }
        }
    }

    private func symbol(_ kind: String) -> String {
        switch kind {
        case "movie": "film"
        case "episode": "tv"
        case "recording": "record.circle"
        default: "play.tv"
        }
    }

    private func open(_ item: UpNextItem) {
        if item.recordingId != nil && item.movieId == nil && item.episodeId == nil {
            store.show("That's saved on the TV")
            return
        }
        var request = TVPlayRequest(title: item.title)
        if let episode = item.episodeId {
            request.kind = "episode"
            request.episodeId = episode
            request.seriesId = item.seriesId
        } else if let movie = item.movieId {
            request.kind = "movie"
            request.movieId = movie
        } else if let channel = item.channelKey {
            request.channelKey = channel
        } else {
            return
        }
        if store.houseTVs.count == 1, let only = store.houseTVs.first {
            var params: [String: Any] = ["title": item.title]
            if let channel = request.channelKey { params["channel_key"] = channel }
            if let movie = request.movieId { params["movie_id"] = movie }
            if let episode = request.episodeId { params["episode_id"] = episode }
            if let series = request.seriesId { params["series_id"] = series }
            store.show("Playing \(item.title) on \(only.name)")
            store.perform("tv.play", target: only.id, params: params)
            return
        }
        play = request
    }

    private func load() async {
        guard let page: UpNextPage = try? await HubClient.shared.get("/v1/tv/upnext") else { return }
        rows = page.rows
    }
}
