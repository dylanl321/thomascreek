import SwiftUI
import Observation

// "Add from YouTube": search YouTube through the hub (GET /v1/youtube/search), look at a channel, playlist or
// video before adding it, then choose what comes (newest N, the whole channel, hand-picked videos), its quality,
// whether to keep following, and, for grown-ups, whether it also goes to the kids' iPads (POST /v1/sends).
// Kids get the same search (Dylan, 2026-11-13) without the quality and iPad choices. Pasting a link still works.

// MARK: - Models

struct YTFound: Codable, Identifiable, Hashable {
    var kind: String            // channel | playlist | video
    var id: String
    var title: String
    var handle: String?
    var followers: Int?
    var verified: Bool?
    var picture: String?
    var banner: String?
    var description: String?
    var videos: Int?
    var url: String?
    var channel: String?
    var channelId: String?
    var count: Int?
    var durationS: Int?
    var views: Int?
    var sourceId: String?
    var following: Bool?
    var saved: Int?             // channels: how many of its videos are on the hub
    var have: Bool?             // videos: already on the hub
    var reason: String?         // suggestions: why it's suggested

    var length: String? {
        guard let s = durationS, s > 0 else { return nil }
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }
    var subscribers: String? { followers.map { "\(compact($0)) subscribers" } }
    var viewsText: String? { views.map { "\(compact($0)) views" } }
}

private func compact(_ n: Int) -> String { n.formatted(.number.notation(.compactName)) }

struct YTSearchResults: Codable { var q: String; var channels: [YTFound]; var playlists: [YTFound]; var videos: [YTFound] }
struct YTChannelPreview: Codable { var channel: YTFound; var recent: [YTFound]; var playlists: [YTFound]; var blocked: Bool }
struct YTPlaylistPreview: Codable { var playlist: YTFound; var videos: [YTFound]; var blocked: Bool }
struct YTStart: Codable { var recent: [YTSource]; var suggested: [YTFound]; var apiKey: Bool? }

/// What the "add" step is about.
struct YTAddTarget: Hashable {
    enum Kind: String { case channel, playlist, video, picks }
    var kind: Kind
    var id: String
    var title: String
    var picture: String?
    var subtitle: String?
    var videoIds: [String] = []

    var url: String {
        switch kind {
        case .channel: "https://www.youtube.com/channel/\(id)"
        case .playlist: "https://www.youtube.com/playlist?list=\(id)"
        case .video: "https://www.youtube.com/watch?v=\(id)"
        case .picks: ""
        }
    }
}

enum YTRoute: Hashable {
    case channel(String)
    case playlist(String)
    case video(YTFound)
    case add(YTAddTarget)
}

@MainActor
@Observable
final class YTWizardModel {
    var path: [YTRoute] = []
    var close: () -> Void = {}
}

// MARK: - The wizard

struct YouTubeWizard: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var model = YTWizardModel()
    @State private var query = ""
    @State private var scope = "all"
    @State private var results: YTSearchResults?
    @State private var searching = false
    @State private var problem: String?
    @State private var start: YTStart?
    @State private var pasted: YouTubeLink?

    private var tint: Color { store.me?.style == .little ? Theme.little : store.me?.role == .child ? Theme.kid : Theme.accent }

    var body: some View {
        NavigationStack(path: $model.path) {
            List {
                if query.trimmingCharacters(in: .whitespaces).isEmpty { startContent } else { resultsContent }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Add from YouTube")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Channels, playlists, videos")
            .searchScopes($scope, activation: .onSearchPresentation) {
                Text("All").tag("all")
                Text("Channels").tag("channel")
                Text("Playlists").tag("playlist")
                Text("Videos").tag("video")
            }
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
            .navigationDestination(for: YTRoute.self) { route in
                switch route {
                case .channel(let id): YTChannelView(channelId: id, tint: tint)
                case .playlist(let id): YTPlaylistView(playlistId: id, tint: tint)
                case .video(let v): YTVideoView(video: v, tint: tint)
                case .add(let t): YTAddView(target: t, tint: tint)
                }
            }
            .task(id: "\(scope)|\(query)") { await search() }
            .task { await loadStart() }
            .sheet(item: $pasted) { YouTubeAddSheet(link: $0.text).personalStyle() }
        }
        .environment(model)
        .onAppear { model.close = { dismiss() } }
        .tint(tint)
    }

    // -- before typing: paste, recently added, suggested
    @ViewBuilder private var startContent: some View {
        Section {
            Button { pasted = YouTubeLink(text: "") } label: { Label("Paste a link", systemImage: "link") }
        } footer: {
            Text("Or share a video, playlist or channel to Thomas Creek from the YouTube app.")
        }
        if let recent = start?.recent, !recent.isEmpty {
            Section("Recently added") {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 14) {
                        ForEach(recent) { s in
                            Button {
                                if let c = s.channelId { model.path.append(.channel(c)) }
                            } label: {
                                VStack(spacing: 6) {
                                    HubImage(path: s.pictureUrl) { Circle().fill(tint.opacity(0.2)).overlay(Image(systemName: s.symbol).foregroundStyle(tint)) }
                                        .frame(width: 58, height: 58).clipShape(Circle())
                                    Text(s.title).font(.caption).lineLimit(2).multilineTextAlignment(.center).frame(width: 72)
                                }
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        if let sug = start?.suggested, !sug.isEmpty {
            Section("Suggested") {
                ForEach(sug) { c in
                    NavigationLink(value: YTRoute.channel(c.id)) { YTFoundRow(item: c, tint: tint, detail: c.reason) }
                }
            }
        }
    }

    // -- search results
    @ViewBuilder private var resultsContent: some View {
        if searching && results == nil {
            Section { HStack { Spacer(); ProgressView(); Spacer() } }
        }
        if let problem { Section { Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.warn) } }
        if let r = results {
            if !r.channels.isEmpty {
                Section("Channels") {
                    ForEach(r.channels) { c in NavigationLink(value: YTRoute.channel(c.id)) { YTFoundRow(item: c, tint: tint) } }
                }
            }
            if !r.playlists.isEmpty {
                Section("Playlists") {
                    ForEach(r.playlists) { p in NavigationLink(value: YTRoute.playlist(p.id)) { YTFoundRow(item: p, tint: tint) } }
                }
            }
            if !r.videos.isEmpty {
                Section {
                    ForEach(r.videos) { v in NavigationLink(value: YTRoute.video(v)) { YTFoundRow(item: v, tint: tint) } }
                } header: { Text("Videos") } footer: { Text("Live streams are left out, since they can't be saved.") }
            }
            if r.channels.isEmpty && r.playlists.isEmpty && r.videos.isEmpty && !searching {
                Section { Text("Nothing found for “\(r.q)”.").foregroundStyle(Theme.dim) }
            }
        }
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        problem = nil
        guard !q.isEmpty else { results = nil; return }
        // A pasted link goes straight to its preview instead of a search.
        if q.contains("youtu") && q.contains("/") {
            pasted = YouTubeLink(text: q); query = ""; return
        }
        try? await Task.sleep(for: .milliseconds(450))
        if Task.isCancelled { return }
        searching = true
        defer { searching = false }
        do {
            let r: YTSearchResults = try await HubClient.shared.call("GET", "/v1/youtube/search", query: ["q": q, "kind": scope, "limit": "10"])
            if !Task.isCancelled { results = r }
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func loadStart() async {
        start = try? await HubClient.shared.call("GET", "/v1/youtube/start")
    }
}

// MARK: - Rows and tiles

struct YTFoundRow: View {
    let item: YTFound
    var tint: Color = Theme.accent
    var detail: String? = nil

    var body: some View {
        HStack(spacing: 12) {
            if item.kind == "channel" {
                HubImage(path: item.picture) { Circle().fill(tint.opacity(0.2)).overlay(Text(String(item.title.prefix(1))).font(.headline).foregroundStyle(tint)) }
                    .frame(width: 48, height: 48).clipShape(Circle())
            } else {
                YTThumb(path: item.picture, length: item.length, playlist: item.kind == "playlist" ? (item.count ?? 0) : nil)
                    .frame(width: 104)
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(item.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                    if item.verified == true { Image(systemName: "checkmark.seal.fill").font(.caption2).foregroundStyle(Theme.dim) }
                }
                Text(detail ?? line).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
                if let mark { Text(mark).font(.caption2.weight(.bold)).foregroundStyle(tint) }
            }
        }
        .padding(.vertical, 2)
    }

    private var line: String {
        switch item.kind {
        case "channel": return [item.subscribers, item.videos.map { "\($0.formatted()) videos" }].compactMap { $0 }.joined(separator: " · ")
        case "playlist": return [item.channel, item.count.map { "\($0) videos" }].compactMap { $0 }.joined(separator: " · ")
        default: return [item.channel, item.viewsText].compactMap { $0 }.joined(separator: " · ")
        }
    }

    private var mark: String? {
        if item.following == true { return "Following" }
        if item.sourceId != nil { return "Added" }
        if let n = item.saved, n > 0 { return "\(n) saved" }
        if item.have == true { return "Saved" }
        return nil
    }
}

struct YTThumb: View {
    let path: String?
    var length: String? = nil
    var playlist: Int? = nil
    var selected: Bool? = nil

    var body: some View {
        HubImage(path: path) { LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom) }
            .aspectRatio(16 / 9, contentMode: .fill)
            .frame(minWidth: 0, maxWidth: .infinity)
            .aspectRatio(16 / 9, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(alignment: .bottomTrailing) {
                if let length {
                    Text(length).font(.caption2.weight(.semibold).monospacedDigit()).foregroundStyle(.white)
                        .padding(.horizontal, 4).padding(.vertical, 1).background(.black.opacity(0.75), in: RoundedRectangle(cornerRadius: 4))
                        .padding(4)
                }
            }
            .overlay(alignment: .trailing) {
                if let playlist {
                    VStack(spacing: 2) {
                        Image(systemName: "list.bullet")
                        if playlist > 0 { Text("\(playlist)").font(.caption2.weight(.bold)) }
                    }
                    .foregroundStyle(.white).frame(maxHeight: .infinity).frame(width: 34).background(.black.opacity(0.6))
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(alignment: .topTrailing) {
                if let selected {
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .font(.title3).foregroundStyle(.white, selected ? Theme.accent : .black.opacity(0.35)).padding(6)
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(selected == true ? Theme.accent : .clear, lineWidth: 3))
    }
}

/// A grid of videos; in select mode a tap ticks a video instead of opening it.
struct YTVideoGrid: View {
    let videos: [YTFound]
    var selecting: Bool
    @Binding var picked: Set<String>

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 14) {
            ForEach(videos) { v in
                let tile = VStack(alignment: .leading, spacing: 4) {
                    YTThumb(path: v.picture, length: v.length, selected: selecting ? picked.contains(v.id) : nil)
                    Text(v.title).font(.caption.weight(.semibold)).lineLimit(2).multilineTextAlignment(.leading)
                    Text([v.viewsText, v.have == true ? "saved" : nil].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption2).foregroundStyle(Theme.dim)
                }
                .contentShape(Rectangle())
                if selecting {
                    Button { if picked.contains(v.id) { picked.remove(v.id) } else { picked.insert(v.id) } } label: { tile }
                        .buttonStyle(.plain)
                } else {
                    NavigationLink(value: YTRoute.video(v)) { tile }.buttonStyle(.plain)
                }
            }
        }
    }
}

// MARK: - Channel preview

struct YTChannelView: View {
    @Environment(HomeStore.self) private var store
    let channelId: String
    var tint: Color = Theme.accent
    @State private var data: YTChannelPreview?
    @State private var problem: String?
    @State private var tab = "recent"
    @State private var selecting = false
    @State private var picked: Set<String> = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let d = data {
                    header(d.channel)
                    if d.blocked {
                        Label("A grown-up turned this channel off.", systemImage: "hand.raised.fill").foregroundStyle(Theme.warn)
                    }
                    Picker("", selection: $tab) {
                        Text("Recent uploads").tag("recent")
                        Text(d.playlists.isEmpty ? "Playlists" : "Playlists (\(d.playlists.count))").tag("playlists")
                    }
                    .pickerStyle(.segmented)
                    if tab == "recent" {
                        if d.recent.isEmpty { Text("No videos that can be saved.").foregroundStyle(Theme.dim) }
                        YTVideoGrid(videos: d.recent, selecting: selecting, picked: $picked)
                    } else {
                        if d.playlists.isEmpty { Text("No playlists.").foregroundStyle(Theme.dim) }
                        ForEach(d.playlists) { p in
                            NavigationLink(value: YTRoute.playlist(p.id)) { YTFoundRow(item: p, tint: tint) }.buttonStyle(.plain)
                        }
                    }
                } else if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.warn)
                } else {
                    ProgressView("Looking it up…").frame(maxWidth: .infinity).padding(.top, 60)
                }
            }
            .padding(16)
        }
        .navigationTitle(data?.channel.title ?? "Channel")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if data != nil && tab == "recent" {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(selecting ? "Cancel" : "Select") { selecting.toggle(); if !selecting { picked = [] } }
                }
            }
        }
        .safeAreaInset(edge: .bottom) {
            if let d = data, !d.blocked || store.role.isGrownUp {
                let ch = d.channel
                Group {
                    if selecting {
                        NavigationLink(value: YTRoute.add(YTAddTarget(kind: .picks, id: ch.id, title: ch.title, picture: ch.picture,
                                                                     subtitle: "\(picked.count) videos", videoIds: d.recent.map(\.id).filter(picked.contains)))) {
                            Text(picked.isEmpty ? "Tap the videos you want" : "Add \(picked.count) video\(picked.count == 1 ? "" : "s")…")
                                .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                        }
                        .disabled(picked.isEmpty)
                    } else {
                        NavigationLink(value: YTRoute.add(YTAddTarget(kind: .channel, id: ch.id, title: ch.title, picture: ch.picture,
                                                                     subtitle: ch.subscribers))) {
                            Text(ch.following == true ? "Change what comes…" : "Add this channel…").font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                        }
                    }
                }
                .buttonStyle(.borderedProminent).buttonBorderShape(.roundedRectangle(radius: 16)).tint(tint)
                .padding(.horizontal, 16).padding(.bottom, 8).background(.bar)
            }
        }
        .task { await load() }
    }

    @ViewBuilder private func header(_ c: YTFound) -> some View {
        VStack(spacing: 6) {
            HubImage(path: c.banner) { LinearGradient(colors: [tint.opacity(0.35), tint.opacity(0.15)], startPoint: .topLeading, endPoint: .bottomTrailing) }
                .frame(height: 80).frame(maxWidth: .infinity).clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            HubImage(path: c.picture) { Circle().fill(tint.opacity(0.25)).overlay(Text(String(c.title.prefix(1))).font(.title.bold()).foregroundStyle(tint)) }
                .frame(width: 72, height: 72).clipShape(Circle())
                .overlay(Circle().stroke(Theme.bg, lineWidth: 3)).padding(.top, -40)
            HStack(spacing: 4) {
                Text(c.title).font(.title3.bold()).multilineTextAlignment(.center)
                if c.verified == true { Image(systemName: "checkmark.seal.fill").foregroundStyle(Theme.dim) }
            }
            Text([c.handle, c.subscribers, c.videos.map { "\($0.formatted()) videos" }].compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            if let d = c.description, !d.isEmpty {
                Text(d).font(.footnote).foregroundStyle(Theme.dim).lineLimit(3).multilineTextAlignment(.center)
            }
            if c.following == true {
                Text("Following").font(.caption.weight(.bold)).foregroundStyle(tint)
            } else if let n = c.saved, n > 0 {
                Text("\(n) of its videos are already saved").font(.caption.weight(.bold)).foregroundStyle(tint)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func load() async {
        guard data == nil else { return }
        do { data = try await HubClient.shared.call("GET", "/v1/youtube/channel/\(channelId)") }
        catch { problem = error.localizedDescription }
    }
}

// MARK: - Playlist preview

struct YTPlaylistView: View {
    @Environment(HomeStore.self) private var store
    let playlistId: String
    var tint: Color = Theme.accent
    @State private var data: YTPlaylistPreview?
    @State private var problem: String?
    @State private var selecting = false
    @State private var picked: Set<String> = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let d = data {
                    let p = d.playlist
                    YTThumb(path: p.picture)
                    Text(p.title).font(.title3.bold())
                    if let cid = p.channelId, let name = p.channel {
                        NavigationLink(value: YTRoute.channel(cid)) { Label(name, systemImage: "person.crop.circle") }.font(.subheadline)
                    }
                    Text("\(d.videos.count) videos that can be saved" + (p.following == true ? " · following" : ""))
                        .font(.caption).foregroundStyle(Theme.dim)
                    if d.blocked { Label("A grown-up turned this channel off.", systemImage: "hand.raised.fill").foregroundStyle(Theme.warn) }
                    YTVideoGrid(videos: d.videos, selecting: selecting, picked: $picked)
                } else if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.warn)
                } else {
                    ProgressView("Looking it up…").frame(maxWidth: .infinity).padding(.top, 60)
                }
            }
            .padding(16)
        }
        .navigationTitle("Playlist")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if data != nil {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(selecting ? "Cancel" : "Select") { selecting.toggle(); if !selecting { picked = [] } }
                }
            }
        }
        .safeAreaInset(edge: .bottom) {
            if let d = data, !d.blocked || store.role.isGrownUp {
                let p = d.playlist
                let target = selecting
                    ? YTAddTarget(kind: .picks, id: p.id, title: p.title, picture: p.picture, subtitle: "\(picked.count) videos",
                                  videoIds: d.videos.map(\.id).filter(picked.contains))
                    : YTAddTarget(kind: .playlist, id: p.id, title: p.title, picture: p.picture, subtitle: p.channel)
                NavigationLink(value: YTRoute.add(target)) {
                    Text(selecting ? (picked.isEmpty ? "Tap the videos you want" : "Add \(picked.count) video\(picked.count == 1 ? "" : "s")…") : "Add this playlist…")
                        .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                }
                .disabled(selecting && picked.isEmpty)
                .buttonStyle(.borderedProminent).buttonBorderShape(.roundedRectangle(radius: 16)).tint(tint)
                .padding(.horizontal, 16).padding(.bottom, 8).background(.bar)
            }
        }
        .task {
            guard data == nil else { return }
            do { data = try await HubClient.shared.call("GET", "/v1/youtube/playlist/\(playlistId)") }
            catch { problem = error.localizedDescription }
        }
    }
}

// MARK: - One video

struct YTVideoView: View {
    let video: YTFound
    var tint: Color = Theme.accent

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                YTThumb(path: video.picture, length: video.length)
                Text(video.title).font(.title3.bold())
                if let cid = video.channelId, let name = video.channel {
                    NavigationLink(value: YTRoute.channel(cid)) { Label(name, systemImage: "person.crop.circle") }.font(.subheadline)
                }
                Text([video.length, video.viewsText].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim)
                if video.have == true {
                    Label("Already saved on the hub", systemImage: "checkmark.circle.fill").font(.subheadline).foregroundStyle(tint)
                }
            }
            .padding(16)
        }
        .navigationTitle("Video")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            NavigationLink(value: YTRoute.add(YTAddTarget(kind: .video, id: video.id, title: video.title, picture: video.picture, subtitle: video.channel))) {
                Text(video.have == true ? "Send it somewhere…" : "Get this video…").font(.headline).frame(maxWidth: .infinity, minHeight: 50)
            }
            .buttonStyle(.borderedProminent).buttonBorderShape(.roundedRectangle(radius: 16)).tint(tint)
            .padding(.horizontal, 16).padding(.bottom, 8).background(.bar)
        }
    }
}

// MARK: - Choosing what comes, and where

struct YTAddView: View {
    @Environment(HomeStore.self) private var store
    @Environment(YTWizardModel.self) private var model
    let target: YTAddTarget
    var tint: Color = Theme.accent

    @State private var amount = "newest"     // newest | whole (channels)
    @State private var count = 10
    @State private var follow = true
    @State private var quality = 1080
    @State private var toIPads = false
    @State private var kids: [SendsOverview.P] = []
    @State private var who: Set<String> = []
    @State private var keepOnIPad = 10
    @State private var sending = false
    @State private var problem: String?
    @State private var added: YTSource?

    private var grownUp: Bool { store.role.isGrownUp }
    private var followable: Bool { target.kind == .channel || target.kind == .playlist }

    var body: some View {
        Group {
            if let added { done(added) } else { form }
        }
        .navigationTitle(added == nil ? target.title : "")
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(added != nil)
    }

    private var form: some View {
        Form {
            Section {
                HStack(spacing: 12) {
                    HubImage(path: target.picture) { RoundedRectangle(cornerRadius: 8).fill(tint.opacity(0.2)) }
                        .frame(width: target.kind == .channel ? 44 : 72, height: 44)
                        .clipShape(target.kind == .channel ? AnyShape(Circle()) : AnyShape(RoundedRectangle(cornerRadius: 8, style: .continuous)))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(target.title).font(.headline).lineLimit(2)
                        Text([kindName, target.subtitle].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim)
                    }
                }
            }
            if target.kind == .channel {
                Section {
                    Picker("What", selection: $amount) {
                        Text("The newest videos").tag("newest")
                        if grownUp { Text("The whole channel").tag("whole") }
                    }
                    .pickerStyle(.inline).labelsHidden()
                    if amount == "newest" { Stepper("Newest \(count) video\(count == 1 ? "" : "s")", value: $count, in: 1...50) }
                } header: { Text("What do you want?") } footer: {
                    Text(amount == "whole" ? "Up to 300 videos, newest first. That can be a lot of space on the hub; the oldest watched go first when it's full."
                         : "To choose videos one by one, go back and tap Select.")
                }
            }
            if followable {
                Section {
                    Toggle("Keep getting new ones", isOn: $follow)
                } footer: { Text("The hub checks every 3 hours and saves anything new.") }
            }
            if grownUp {
                Section {
                    Picker("Quality", selection: $quality) {
                        Text("Best 1080p").tag(1080)
                        Text("Good 720p").tag(720)
                        Text("Small 480p").tag(480)
                    }
                    .pickerStyle(.segmented)
                } header: { Text("Quality") } footer: {
                    Text(quality == 1080 ? "Sharpest on the TV." : quality == 720 ? "About half the space. Looks the same on an iPad." : "A quarter of the space. Fine for phones and long trips.")
                }
                Section {
                    Toggle("Also put on the kids' iPads", isOn: $toIPads)
                    if toIPads {
                        if kids.isEmpty { Text("No kids set up yet.").foregroundStyle(Theme.dim) }
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 10) {
                                ForEach(kids, id: \.id) { p in
                                    let on = who.contains(p.id)
                                    Button { if on { who.remove(p.id) } else { who.insert(p.id) } } label: {
                                        VStack(spacing: 6) {
                                            Avatar(name: p.name, color: p.uiStyle == "little" ? Theme.little : Theme.kid, size: 40)
                                            Text(p.name).font(.subheadline.weight(.semibold))
                                        }
                                        .padding(10).frame(minWidth: 80)
                                        .background(on ? Theme.accent.opacity(0.15) : Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(on ? Theme.accent : .clear, lineWidth: 2))
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                            .padding(.vertical, 4)
                        }
                        if followable { Stepper("Keep the newest \(keepOnIPad) on each iPad", value: $keepOnIPad, in: 1...50) }
                    }
                } header: { Text("Where it goes") } footer: {
                    Text(toIPads ? "It's saved on the hub first, then each iPad downloads it by itself on home Wi-Fi."
                         : "It's saved on the hub for everyone to watch in the app.")
                }
            }
            if let problem { Section { Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.warn) } }
            Section {
                PrimaryButton(title: sending ? "Adding…" : addTitle, symbol: "arrow.down.circle.fill", color: tint) { Task { await add() } }
                    .disabled(sending || (toIPads && who.isEmpty))
                    .listRowInsets(EdgeInsets()).listRowBackground(Color.clear)
            }
        }
        .task { await loadKids() }
    }

    private var kindName: String {
        switch target.kind {
        case .channel: "Channel"
        case .playlist: "Playlist"
        case .video: "Video"
        case .picks: "Hand-picked"
        }
    }

    private var addTitle: String {
        switch target.kind {
        case .channel: amount == "whole" ? "Add the whole channel" : "Add the newest \(count)"
        case .playlist: "Add the playlist"
        case .video: "Get it"
        case .picks: "Add \(target.videoIds.count) video\(target.videoIds.count == 1 ? "" : "s")"
        }
    }

    @ViewBuilder private func done(_ s: YTSource) -> some View {
        VStack(spacing: 16) {
            Spacer()
            HubImage(path: s.pictureUrl ?? target.picture) { Circle().fill(tint.opacity(0.2)).overlay(Image(systemName: "checkmark").font(.largeTitle.bold()).foregroundStyle(tint)) }
                .frame(width: 88, height: 88).clipShape(Circle())
            Text("\(s.title) is on its way").font(store.me?.role == .child ? Theme.kidFont(24) : .title2.bold()).multilineTextAlignment(.center)
            Text(doneDetail(s)).font(.body).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            Spacer()
            PrimaryButton(title: "Done", color: tint) { model.close() }
            Button("Add something else") { model.path = [] }.padding(.bottom, 8)
        }
        .padding(24)
    }

    private func doneDetail(_ s: YTSource) -> String {
        let n = s.waiting + s.count
        var t = n == 1 ? "It's downloading to the hub" : "\(n) videos are downloading to the hub"
        if s.follow { t += ", and new ones will come by themselves" }
        t += "."
        if toIPads {
            let names = kids.filter { who.contains($0.id) }.map(\.name).joined(separator: " and ")
            if !names.isEmpty { t += " Then \(n == 1 ? "it goes" : "they go") to \(names)'s iPad." }
        }
        return t
    }

    private func loadKids() async {
        guard grownUp, kids.isEmpty else { return }
        if let o: SendsOverview = try? await HubClient.shared.get("/v1/sends") {
            kids = o.people.map(\.person).filter { $0.role == "child" }
            if who.isEmpty { who = Set(kids.map(\.id)) }
        }
    }

    private func add() async {
        sending = true
        problem = nil
        defer { sending = false }
        var body: [String: Any] = [:]
        if target.kind == .picks {
            body["video_ids"] = target.videoIds
            body["title"] = target.videoIds.count == 1 ? target.title : "\(target.videoIds.count) from \(target.title)"
        } else {
            body["url"] = target.url
            body["follow"] = followable && follow
        }
        if target.kind == .channel {
            if amount == "whole" { body["whole"] = true } else { body["latest"] = count }
        }
        if grownUp { body["quality"] = quality }
        do {
            let s: YTSource = try await HubClient.shared.post("/v1/youtube", body)
            if toIPads && !who.isEmpty {
                await sendToIPads(s)
            }
            await YouTubeStore.shared.load()
            added = s
        } catch { problem = error.localizedDescription }
    }

    private func sendToIPads(_ s: YTSource) async {
        var body: [String: Any] = ["person_ids": Array(who), "title": s.title, "quality": quality >= 720 ? "high" : "small"]
        if target.kind == .video {
            body["kind"] = "title"; body["ref"] = "yt-\(target.id)"
        } else {
            body["kind"] = "youtube"; body["ref"] = s.id
            body["count"] = target.kind == .picks ? target.videoIds.count : keepOnIPad
        }
        if let p = s.pictureUrl { body["poster_url"] = p }
        do { try await HubClient.shared.send("/v1/sends", body) }
        catch { store.show("Added, but couldn't send it to the iPads", error.localizedDescription, isError: true) }
    }
}
