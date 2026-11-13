import SwiftUI

/// One card from GET /v1/titles/search: where a film or show lives, and what you can do with it.
struct TitlePage: Decodable { var query: String; var titles: [TitleHit] }

struct TitleHit: Decodable, Identifiable {
    var id: String
    var title: String
    var year: Int?
    var kind: String
    var posterUrl: String?
    var offers: [TitleOffer]
    var request: TitleAsk?
}

struct TitleOffer: Decodable, Hashable, Identifiable {
    var source: String
    var label: String
    var plexId: String?
    var playOnPhone: Bool?
    var playOnTv: Bool?
    var seriesId: String?
    var params: [String: JSONValue]?
    var id: String { source + label }
}

struct TitleAsk: Decodable {
    var mediaId: String
    var status: String?
    var canRequest: Bool?
    var askParent: Bool?
    var requestId: String?
}

struct TitleResultCard: View {
    var card: TitleHit
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var plex: MediaItem?
    @State private var onTV: TVPlayRequest?
    @State private var asking = false
    @State private var status: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                art
                VStack(alignment: .leading, spacing: 4) {
                    Text(card.title).font(.headline).foregroundStyle(Theme.fg).lineLimit(2)
                    if let year = card.year { Text(String(year)).font(.caption).foregroundStyle(Theme.dim) }
                    if let status { Text(statusText(status)).font(.caption.weight(.semibold)).foregroundStyle(Theme.accent) }
                    ForEach(card.offers) { offer in
                        Text(offer.label).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
                    }
                }
            }
            HStack(spacing: 8) {
                if iptvOffer != nil {
                    Button("Play on TV") { onTV = playRequest }
                        .buttonStyle(.borderedProminent).controlSize(.small)
                }
                if let plexId = plexOffer?.plexId {
                    Button(plexOffer?.playOnPhone == true ? "Play on phone" : "Play") {
                        plex = MediaItem(id: plexId, title: card.title, posterUrl: card.posterUrl)
                    }
                    .buttonStyle(.bordered).controlSize(.small)
                }
                if let series = card.offers.compactMap(\.seriesId).first {
                    Button("Episodes") { router.go(.tvSeries(series)) }
                        .buttonStyle(.bordered).controlSize(.small)
                }
                if card.request?.canRequest == true {
                    Button(card.request?.askParent == true ? "Ask a grown-up" : "Request") { Task { await ask() } }
                        .buttonStyle(.bordered).controlSize(.small)
                        .disabled(asking)
                }
            }
        }
        .card()
        .sheet(item: $onTV) { PlayOnTVSheet(request: $0) }
        .sheet(item: $plex) { item in
            if store.role == .child { KidPlaySheet(item: item) } else { PlayOnSheet(item: item) }
        }
        .onAppear { status = card.request?.status }
    }

    private var art: some View {
        Group {
            HubImage(path: card.posterUrl) {
                Image(systemName: card.kind == "channel" ? "tv" : "film").foregroundStyle(Theme.dim)
            }
        }
        .frame(width: 52, height: 74)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var iptvOffer: TitleOffer? { card.offers.first { $0.playOnTv == true && $0.plexId == nil } }
    private var plexOffer: TitleOffer? { card.offers.first { $0.plexId != nil } }

    private var playRequest: TVPlayRequest {
        let params = iptvOffer?.params ?? [:]
        let movie = params["movie_id"]?.string
        let episode = params["episode_id"]?.string
        let kind = params["kind"]?.string ?? (movie != nil ? "movie" : episode != nil ? "episode" : "live")
        return TVPlayRequest(title: card.title, kind: kind, channelKey: params["channel_key"]?.string,
                             start: params["start"]?.string, movieId: movie, seriesId: iptvOffer?.seriesId, episodeId: episode)
    }

    private func ask() async {
        guard let mediaId = card.request?.mediaId else { return }
        asking = true
        defer { asking = false }
        do {
            let reply: TitleAsk = try await HubClient.shared.post("/v1/titles/request", ["media_id": mediaId])
            status = reply.status
            store.show(reply.status == "waiting" ? "Asked a grown-up" : "Requested", card.title)
        } catch {
            store.show(error.localizedDescription, isError: true)
        }
    }

    private func statusText(_ status: String) -> String {
        switch status {
        case "waiting": "Asked — waiting for a grown-up"
        case "requested": "Requested"
        case "downloading": "Downloading"
        case "available": "Available"
        default: status.capitalized
        }
    }
}
