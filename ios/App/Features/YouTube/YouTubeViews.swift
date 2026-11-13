import SwiftUI
import Observation

// Kids' YouTube. A child shares a video, playlist or channel; the hub fetches it with yt-dlp onto the
// NAS (no approval, parents get a note). Each saved video is a media item ("yt-<id>"), so it downloads
// and plays offline through `Downloads` like a film. App only: these never show in Plex.

struct YTSource: Codable, Identifiable, Hashable {
    var id: String
    var kind: String
    var title: String
    var channel: String?
    var channelId: String?
    var url: String
    var follow: Bool
    var addedBy: String?
    var addedById: String?
    var pictureUrl: String?
    var count: Int
    var waiting: Int
    var bytes: Int64?
    var lastError: String?

    var kindLabel: String { ["channel": "Channel", "playlist": "Playlist"][kind] ?? "Video" }
    var symbol: String { ["channel": "person.crop.square.fill", "playlist": "list.bullet.rectangle.fill"][kind] ?? "play.rectangle.fill" }
}

struct YTVideo: Codable, Identifiable, Hashable {
    var id: String
    var title: String
    var year: Int?
    var posterUrl: String?
    var durationS: Int?
    var videoId: String
    var channel: String?
    var channelId: String?
    var status: String
    var progress: Double?
    var bytes: Int64?
    var height: Int?
    var error: String?
    var watched: Bool?
    var sourceIds: [String]

    var ready: Bool { status == "ready" }
    var media: MediaItem { MediaItem(id: id, title: title, year: year, kind: "youtube", posterUrl: posterUrl, rating: nil, summary: channel) }
    var length: String? {
        guard let s = durationS, s > 0 else { return nil }
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }
}

struct YTOverview: Codable {
    struct Storage: Codable { var bytes: Int64; var capBytes: Int64; var ready: Int; var waiting: Int; var failed: Int; var ytDlp: String? }
    struct Blocked: Codable, Hashable, Identifiable { var channelId: String; var name: String?; var id: String { channelId } }
    var sources: [YTSource]
    var videos: [YTVideo]
    var storage: Storage?
    var blocked: [Blocked]?
}

struct YTPreview: Codable {
    var kind: String
    var title: String
    var channel: String?
    var picture: String?
    var followers: Int?
    var count: Int
    var newest: String?
    var followable: Bool
}

@MainActor
@Observable
final class YouTubeStore {
    static let shared = YouTubeStore()
    var data: YTOverview?
    var loading = false
    private var poller: Task<Void, Never>?

    var ready: [YTVideo] { data?.videos.filter(\.ready) ?? [] }
    var waiting: Int { data?.videos.filter { $0.status == "queued" || $0.status == "fetching" }.count ?? 0 }
    func videos(of s: YTSource) -> [YTVideo] { data?.videos.filter { $0.sourceIds.contains(s.id) } ?? [] }

    func load() async {
        loading = true
        defer { loading = false }
        if let d: YTOverview = try? await HubClient.shared.get("/v1/youtube") { data = d }
        // While the hub is still fetching, look again every few seconds so progress moves.
        if waiting > 0, poller == nil {
            poller = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(4))
                    guard let self else { return }
                    if let d: YTOverview = try? await HubClient.shared.get("/v1/youtube") { self.data = d }
                    if self.waiting == 0 { break }
                }
                self?.poller = nil
            }
        }
    }

    func delete(_ path: String) async -> Bool {
        do { _ = try await HubClient.shared.raw("DELETE", path); await load(); return true }
        catch { HomeStore.shared.show(error.localizedDescription, isError: true); return false }
    }
}

// MARK: - Shelf on the Watch screens

struct YouTubeShelf: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var tint: Color = Theme.accent
    @State private var picked: YTVideo?
    private let yt = YouTubeStore.shared

    var body: some View {
        let little = store.me?.style == .little
        VStack(alignment: .leading, spacing: 12) {
            if little {
                Button { router.youtubeAdd = "" } label: {
                    Label("Add from YouTube", systemImage: "plus").font(Theme.kidFont(20, .bold)).frame(maxWidth: .infinity, minHeight: 58)
                }
                .buttonStyle(.borderedProminent).tint(tint).buttonBorderShape(.roundedRectangle(radius: 18))
                .disabled(!store.reachable)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
                    ForEach(yt.ready) { v in YouTubeThumb(video: v, big: true).onTapGesture { picked = v } }
                }
            } else {
                SectionHeader(title: "YouTube") {
                    Button { router.youtubeAdd = "" } label: { Label("Add", systemImage: "plus").font(.subheadline.weight(.semibold)) }
                        .tint(tint).disabled(!store.reachable)
                }
                if yt.ready.isEmpty {
                    Text(yt.waiting > 0 ? "Getting \(yt.waiting) video\(yt.waiting == 1 ? "" : "s") from YouTube…" :
                            "Share a video, playlist or channel from the YouTube app, or tap Add.")
                        .font(.subheadline).foregroundStyle(Theme.dim).card(padding: 14)
                } else {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(alignment: .top, spacing: 12) {
                            ForEach(yt.ready.prefix(30)) { v in
                                VStack(alignment: .leading, spacing: 4) {
                                    YouTubeThumb(video: v)
                                    Text(v.title).font(.caption.weight(.semibold)).lineLimit(2)
                                    Text(v.channel ?? "").font(.caption2).foregroundStyle(Theme.dim).lineLimit(1)
                                }
                                .frame(width: 168, alignment: .leading)
                                .onTapGesture { picked = v }
                            }
                        }
                    }
                    .scrollClipDisabled()
                }
                let followed = (yt.data?.sources ?? []).filter { $0.kind != "video" }
                if !followed.isEmpty {
                    RowsCard {
                        ForEach(followed) { s in
                            NavigationLink { YouTubeSourceView(source: s, tint: tint) } label: { SourceRow(source: s, tint: tint) }
                                .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .task { await yt.load() }
        .sheet(item: $picked) { YouTubeVideoSheet(video: $0, tint: tint) }
    }
}

struct SourceRow: View {
    let source: YTSource
    var tint: Color = Theme.accent
    var body: some View {
        HStack(spacing: 12) {
            HubImage(path: source.pictureUrl) {
                Circle().fill(tint.opacity(0.2)).overlay(Image(systemName: source.symbol).foregroundStyle(tint))
            }
            .frame(width: 44, height: 44).clipShape(Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(source.title).font(.headline).lineLimit(1)
                Text(detail).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                if source.waiting > 0 {
                    Text("Getting \(source.waiting) more from YouTube…").font(.caption).foregroundStyle(tint)
                }
            }
            Spacer()
            Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
        }
        .padding(.vertical, 10).padding(.horizontal, 14)
        .contentShape(Rectangle())
    }

    private var detail: String {
        var parts = [source.kindLabel, "\(source.count) video\(source.count == 1 ? "" : "s")"]
        if let who = source.addedBy { parts.append("added by \(who)") }
        return parts.joined(separator: " · ")
    }
}

/// A 16:9 picture with the length, and a tick when it's on this device.
struct YouTubeThumb: View {
    let video: YTVideo
    var big = false
    private let downloads = Downloads.shared

    var body: some View {
        HubImage(path: video.posterUrl) {
            LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom)
                .overlay(Text(video.title).font(.caption.weight(.semibold)).multilineTextAlignment(.center).padding(6))
        }
        .aspectRatio(16 / 9, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: big ? 16 : 12, style: .continuous))
        .overlay(alignment: .bottomTrailing) {
            if let l = video.length {
                Text(l).font(.caption2.weight(.semibold).monospacedDigit()).foregroundStyle(.white)
                    .padding(.horizontal, 5).padding(.vertical, 2).background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 4))
                    .padding(6)
            }
        }
        .overlay(alignment: .topTrailing) {
            if downloads.title(for: video.id)?.state == .ready {
                Image(systemName: "checkmark.circle.fill").font(big ? .title2 : .body).foregroundStyle(.white, .green).padding(6)
                    .accessibilityLabel("On this device")
            }
        }
        .overlay(alignment: .bottom) {
            if !video.ready {
                ProgressBar(value: video.progress ?? 0).padding(6)
            }
        }
    }
}

// MARK: - One video

struct YouTubeVideoSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let video: YTVideo
    var tint: Color = Theme.accent
    @State private var playing: Downloads.Title?
    @State private var wantsToPlay = false
    @State private var confirmBlock = false
    private let downloads = Downloads.shared

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    YouTubeThumb(video: video, big: true)
                    Text(video.title).font(store.me?.role == .child ? Theme.kidFont(22) : .title3.weight(.semibold))
                    Text([video.channel, video.height.map { "\($0)p" }, video.bytes.map(byteSize)].compactMap { $0 }.joined(separator: " · "))
                        .font(.subheadline).foregroundStyle(Theme.dim)
                    watch
                    if store.me?.role.isGrownUp == true && store.me?.role != .tablet {
                        HStack {
                            Button(role: .destructive) { Task { if await YouTubeStore.shared.delete("/v1/youtube/videos/\(video.videoId)") { dismiss() } } } label: {
                                Label("Remove", systemImage: "trash")
                            }
                            if video.channelId != nil {
                                Button(role: .destructive) { confirmBlock = true } label: { Label("Block channel", systemImage: "nosign") }
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                }
                .padding(20)
            }
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.large])
        .fullScreenCover(item: $playing) { OfflinePlayer(title: $0) }
        .onChange(of: downloads.title(for: video.id)?.state) { _, s in
            if s == .ready, wantsToPlay, let t = downloads.title(for: video.id) { wantsToPlay = false; playing = t }
        }
        .confirmationDialog("Block \(video.channel ?? "this channel")?", isPresented: $confirmBlock, titleVisibility: .visible) {
            Button("Block and remove its videos", role: .destructive) {
                Task {
                    do {
                        try await HubClient.shared.send("/v1/youtube/block", ["channel_id": video.channelId ?? "", "name": video.channel ?? ""])
                        await YouTubeStore.shared.load()
                        dismiss()
                    } catch { store.show(error.localizedDescription, isError: true) }
                }
            }
        } message: { Text("Nobody can add it again until you unblock it in Systems.") }
    }

    @ViewBuilder private var watch: some View {
        if !video.ready {
            Label(video.status == "failed" ? (video.error ?? "YouTube didn't give it to the hub.") : "Coming from YouTube…",
                  systemImage: video.status == "failed" ? "exclamationmark.triangle" : "hourglass")
                .font(.subheadline).foregroundStyle(video.status == "failed" ? Theme.warn : Theme.dim)
        } else if let t = downloads.title(for: video.id) {
            switch t.state {
            case .ready:
                PrimaryButton(title: t.positionS > 30 ? "Keep watching" : "Watch", symbol: "play.fill", color: tint) { playing = t }
                Text("It's on this device, so it plays in the car too.").font(.caption).foregroundStyle(Theme.dim)
                Button("Remove from this device", role: .destructive) { downloads.remove(t) }.font(.footnote)
            case .preparing, .downloading:
                VStack(alignment: .leading, spacing: 6) {
                    HStack { Text(wantsToPlay ? "Starting…" : "Saving to this device").font(.subheadline.weight(.semibold)); Spacer()
                        Text(t.progress, format: .percent.precision(.fractionLength(0))).font(.caption.monospacedDigit()).foregroundStyle(Theme.dim) }
                    ProgressBar(value: t.progress, color: tint)
                }
                .card(padding: 12, color: Theme.card2)
            case .failed:
                Text(t.error ?? "The download stopped.").font(.footnote).foregroundStyle(Theme.warn)
                Button("Try again") { downloads.retry(t) }.buttonStyle(.bordered)
            }
        } else {
            PrimaryButton(title: "Watch", symbol: "play.fill", color: tint) { Task { await start(play: true) } }
                .disabled(!store.reachable)
            Button { Task { await start(play: false) } } label: {
                Label("Save for the car", systemImage: "arrow.down.circle").frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.bordered).tint(tint).disabled(!store.reachable)
            Text("Watching saves it to this device first. It only takes a moment at home.").font(.caption).foregroundStyle(Theme.dim)
        }
    }

    /// The hub's own copy, straight to the device (no converting), then play it.
    private func start(play: Bool) async {
        do {
            let opts = try await downloads.options(for: video.media)
            guard let o = opts.first(where: { $0.quality == "original" }) ?? opts.first else { return }
            wantsToPlay = play
            try await downloads.start(video.media, o)
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

// MARK: - A channel or playlist

struct YouTubeSourceView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let source: YTSource
    var tint: Color = Theme.accent
    @State private var picked: YTVideo?
    @State private var follow: Bool
    private let yt = YouTubeStore.shared

    init(source: YTSource, tint: Color = Theme.accent) {
        self.source = source; self.tint = tint; _follow = State(initialValue: source.follow)
    }

    private var mayChange: Bool {
        (store.me?.role == .admin || store.me?.role == .adult) || source.addedById == store.me?.person.id
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                SourceRow(source: source, tint: tint).card(padding: 4)
                if source.kind != "video", mayChange {
                    Toggle(isOn: $follow) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Get new videos").font(.headline)
                            Text(source.kind == "channel" ? "Each new upload, checked every few hours" : "New videos added to the playlist")
                                .font(.caption).foregroundStyle(Theme.dim)
                        }
                    }
                    .tint(tint).card(padding: 14)
                    .onChange(of: follow) { _, f in
                        Task { _ = try? await HubClient.shared.send("/v1/youtube/sources/\(source.id)", ["follow": f]); await yt.load() }
                    }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 12)], spacing: 14) {
                    ForEach(yt.videos(of: source)) { v in
                        VStack(alignment: .leading, spacing: 4) {
                            YouTubeThumb(video: v)
                            Text(v.title).font(.caption.weight(.semibold)).lineLimit(2)
                            if !v.ready {
                                Text(v.status == "failed" ? (v.error ?? "Couldn't get it") : "Coming from YouTube…")
                                    .font(.caption2).foregroundStyle(v.status == "failed" ? Theme.warn : Theme.dim)
                            }
                        }
                        .onTapGesture { picked = v }
                    }
                }
                if mayChange {
                    Button(role: .destructive) {
                        Task { if await yt.delete("/v1/youtube/sources/\(source.id)") { dismiss() } }
                    } label: { Label("Remove \(source.kindLabel.lowercased())", systemImage: "trash").frame(maxWidth: .infinity, minHeight: 44) }
                    .buttonStyle(.bordered)
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(source.title)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await yt.load() }
        .sheet(item: $picked) { YouTubeVideoSheet(video: $0, tint: tint) }
    }
}

// MARK: - Adding

struct YouTubeAddSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State var link: String
    @State private var preview: YTPreview?
    @State private var problem: String?
    @State private var looking = false
    @State private var follow = true
    @State private var sending = false
    @State private var added: YTSource?

    private var tint: Color { store.me?.style == .little ? Theme.little : store.me?.role == .child ? Theme.kid : Theme.accent }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                if let added {
                    Label("Got it!", systemImage: "checkmark.circle.fill").font(Theme.kidFont(24)).foregroundStyle(tint)
                    Text(added.kind == "video" ? "\(added.title) is on its way." :
                            "The videos from \(added.title) are on their way\(added.follow ? ", and new ones will come by themselves" : "").")
                        .font(.body)
                    Spacer()
                    PrimaryButton(title: "Done", color: tint) { dismiss() }
                } else {
                    HStack(spacing: 8) {
                        Image(systemName: "link").foregroundStyle(Theme.dim)
                        TextField("YouTube link", text: $link).textInputAutocapitalization(.never).autocorrectionDisabled()
                            .keyboardType(.URL).submitLabel(.go)
                        PasteButton(payloadType: String.self) { s in if let s = s.first { link = s } }
                            .labelStyle(.iconOnly).buttonBorderShape(.capsule).tint(tint)
                    }
                    .padding(12).background(Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    if looking { ProgressView().frame(maxWidth: .infinity) }
                    if let problem { Label(problem, systemImage: "exclamationmark.triangle").font(.subheadline).foregroundStyle(Theme.warn) }
                    if let p = preview {
                        HStack(spacing: 12) {
                            HubImage(path: p.picture) { Circle().fill(tint.opacity(0.2)) }
                                .frame(width: 56, height: 56).clipShape(p.kind == "channel" ? AnyShape(Circle()) : AnyShape(RoundedRectangle(cornerRadius: 10)))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.title).font(.headline).lineLimit(2)
                                Text(previewLine(p)).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
                            }
                        }
                        if p.followable {
                            Picker("", selection: $follow) {
                                Text("Keep getting new ones").tag(true)
                                Text("Just these").tag(false)
                            }
                            .pickerStyle(.segmented)
                        }
                        PrimaryButton(title: sending ? "Adding…" : "Get it", symbol: "arrow.down.circle.fill", color: tint) { Task { await add() } }
                            .disabled(sending)
                    } else if !looking {
                        Label("Easiest: in the YouTube app tap Share, then Copy link, and come back here and paste.", systemImage: "square.and.arrow.up")
                            .font(.footnote).foregroundStyle(Theme.dim)
                    }
                    Spacer()
                }
            }
            .padding(20)
            .navigationTitle(added == nil ? "Add from YouTube" : "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button(added == nil ? "Cancel" : "Close") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
        .task(id: link) { await look() }
    }

    private func previewLine(_ p: YTPreview) -> String {
        switch p.kind {
        case "channel": return ["Channel", p.followers.map { "\($0.formatted(.number.notation(.compactName))) subscribers" }, p.newest.map { "newest: \($0)" }].compactMap { $0 }.joined(separator: " · ")
        case "playlist": return "Playlist · \(p.count) videos" + (p.channel.map { " · \($0)" } ?? "")
        default: return ["Video", p.channel].compactMap { $0 }.joined(separator: " · ")
        }
    }

    private func look() async {
        preview = nil; problem = nil
        let text = link.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count > 8 else { return }
        try? await Task.sleep(for: .milliseconds(500))
        if Task.isCancelled { return }
        looking = true
        defer { looking = false }
        do {
            let p: YTPreview = try await HubClient.shared.get("/v1/youtube/preview", ["url": text])
            if !Task.isCancelled { preview = p }
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func add() async {
        sending = true
        defer { sending = false }
        do {
            let s: YTSource = try await HubClient.shared.post("/v1/youtube", ["url": link, "follow": follow])
            added = s
            await YouTubeStore.shared.load()
        } catch { problem = error.localizedDescription }
    }
}

// MARK: - Parents: Systems → Kids' YouTube

struct YouTubeManageView: View {
    @Environment(HomeStore.self) private var store
    @State private var refreshing = false
    @State private var sendTo: SendTarget?
    private let yt = YouTubeStore.shared

    var body: some View {
        List {
            if let s = yt.data?.storage {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(byteSize(s.bytes)) of \(byteSize(s.capBytes))").font(.headline)
                        ProgressBar(value: s.capBytes > 0 ? Double(s.bytes) / Double(s.capBytes) : 0, color: .red)
                        Text("\(s.ready) videos\(s.waiting > 0 ? " · \(s.waiting) on the way" : "")\(s.failed > 0 ? " · \(s.failed) failed" : ""). Oldest watched go first when it's full.")
                            .font(.caption).foregroundStyle(Theme.dim)
                        if let v = s.ytDlp { Text("yt-dlp \(v), updated daily").font(.caption2).foregroundStyle(Theme.dim) }
                    }
                }
            }
            Section("Added") {
                if (yt.data?.sources ?? []).isEmpty { Text("Nothing yet.").foregroundStyle(Theme.dim) }
                ForEach(yt.data?.sources ?? []) { s in
                    NavigationLink { YouTubeSourceView(source: s) } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s.title).font(.headline)
                            Text([s.kindLabel, s.addedBy, "\(s.count)", s.bytes.map(byteSize), s.follow ? "following" : nil].compactMap { $0 }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(Theme.dim)
                            if let e = s.lastError { Text(e).font(.caption).foregroundStyle(Theme.warn) }
                        }
                    }
                    .swipeActions(edge: .leading) {
                        Button { sendTo = SendTarget(kind: .youtube, ref: s.kind == "video" ? "yt-\(yt.videos(of: s).first?.videoId ?? "")" : s.id,
                                                    title: s.title, posterUrl: s.pictureUrl) } label: {
                            Label("Send to iPads", systemImage: "ipad.and.arrow.forward")
                        }
                        .tint(Theme.kid)
                    }
                    .swipeActions {
                        Button("Remove", role: .destructive) { Task { _ = await yt.delete("/v1/youtube/sources/\(s.id)") } }
                    }
                }
            }
            let failed = (yt.data?.videos ?? []).filter { $0.status == "failed" }
            if !failed.isEmpty {
                Section("Couldn't get") {
                    ForEach(failed) { v in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(v.title).font(.subheadline)
                            Text(v.error ?? "").font(.caption).foregroundStyle(Theme.warn)
                        }
                        .swipeActions {
                            Button("Try again") { Task { try? await HubClient.shared.send("/v1/youtube/videos/\(v.videoId)/retry"); await yt.load() } }.tint(.blue)
                            Button("Remove", role: .destructive) { Task { _ = await yt.delete("/v1/youtube/videos/\(v.videoId)") } }
                        }
                    }
                }
            }
            Section {
                ForEach(yt.data?.blocked ?? []) { b in
                    Text(b.name ?? b.channelId)
                        .swipeActions { Button("Unblock") { Task { _ = await yt.delete("/v1/youtube/block/\(b.channelId)") } }.tint(.green) }
                }
                if (yt.data?.blocked ?? []).isEmpty { Text("Nothing blocked.").foregroundStyle(Theme.dim) }
            } header: { Text("Blocked channels") } footer: {
                Text("Kids add without asking; you get a note each time. Block a channel from one of its videos.")
            }
        }
        .navigationTitle("Kids' YouTube")
        .sheet(item: $sendTo) { AddToIPadView(target: $0) }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { Task { refreshing = true; _ = try? await HubClient.shared.send("/v1/youtube/refresh"); await yt.load(); refreshing = false } } label: {
                    if refreshing { ProgressView() } else { Image(systemName: "arrow.clockwise") }
                }
                .accessibilityLabel("Look for new videos")
            }
        }
        .task { await yt.load() }
        .refreshable { await yt.load() }
    }
}

struct YouTubeLink: Identifiable { let text: String; var id: String { text } }
