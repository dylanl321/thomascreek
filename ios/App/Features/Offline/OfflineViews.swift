import SwiftUI
import AVKit

extension Freshness {
    /// "just now", "3 hr ago", "Tuesday": how old a saved copy is, in words.
    static func text(_ d: Date) -> String {
        let age = Date.now.timeIntervalSince(d)
        if age < 90 { return "just now" }
        if age < 6 * 86400 { return d.formatted(.relative(presentation: .named, unitsStyle: .abbreviated)) }
        return d.formatted(.dateTime.month(.abbreviated).day())
    }
}

// MARK: - The offline strip (every tab)

/// One line under the title on every screen while home can't be reached.
struct OfflineStrip: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        if !store.reachable {
            Button { router.showOffline = true } label: {
                HStack(spacing: 8) {
                    Image(systemName: "wifi.slash").foregroundStyle(Theme.warn)
                    VStack(alignment: .leading, spacing: 0) {
                        Text("Offline").font(.footnote.weight(.semibold))
                        Text(line).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
                }
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Theme.card2, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .padding(.horizontal, 16).padding(.bottom, 4)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Offline. \(line)")
            .transition(.move(edge: .top).combined(with: .opacity))
        }
    }

    private var line: String {
        let when = store.lastUpdate.map { "Saved \(Freshness.text($0))" } ?? "Showing what was saved"
        let n = Downloads.shared.mine.filter { $0.state == .ready }.count
        return n > 0 ? "\(when) · \(n) download\(n == 1 ? "" : "s") ready" : when
    }
}

extension View {
    /// The offline strip pinned under the navigation bar.
    func offlineStrip() -> some View { safeAreaInset(edge: .top, spacing: 0) { OfflineStrip() } }
}

// MARK: - Download button (title sheets)

/// "Download", then "Getting ready", "Downloading 40%", "Downloaded · Play".
struct DownloadButton: View {
    @Environment(HomeStore.self) private var store
    let item: MediaItem
    var tint: Color = Theme.accent
    @State private var options: [Downloads.Option]?
    @State private var asking = false
    @State private var playing: Downloads.Title?
    private let downloads = Downloads.shared

    var body: some View {
        Group {
            if let t = downloads.title(for: item.id) {
                switch t.state {
                case .ready:
                    PrimaryButton(title: t.positionS > 30 ? "Resume download" : "Play download", symbol: "play.fill", color: tint) { playing = t }
                case .preparing:
                    status("Getting it ready", t.progress, detail: "Home is preparing \(t.qualityLabel.lowercased()) quality")
                case .downloading:
                    status("Downloading", t.progress, detail: AppSettings.downloadOnCellular ? nil : "Waits for Wi-Fi")
                case .failed:
                    VStack(alignment: .leading, spacing: 6) {
                        Text(t.error ?? "The download stopped.").font(.footnote).foregroundStyle(Theme.warn)
                        HStack {
                            Button("Try again") { downloads.retry(t) }.buttonStyle(.bordered)
                            Button("Remove", role: .destructive) { downloads.remove(t) }.buttonStyle(.bordered)
                        }
                    }
                }
            } else if item.kind == "show" {
                NavigationLink { EpisodesView(show: item) } label: {
                    Label("Download episodes", systemImage: "arrow.down.circle").font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                }
                .buttonStyle(.bordered).tint(tint).buttonBorderShape(.roundedRectangle(radius: 16))
            } else {
                Button { Task { await ask() } } label: {
                    Label("Download", systemImage: "arrow.down.circle").font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                }
                .buttonStyle(.bordered).tint(tint).buttonBorderShape(.roundedRectangle(radius: 16))
                .disabled(!store.reachable)
            }
        }
        .sheet(isPresented: $asking) {
            DownloadOptionsSheet(item: item, options: options ?? []).presentationDetents([.medium])
        }
        .fullScreenCover(item: $playing) { OfflinePlayer(title: $0) }
    }

    private func status(_ title: String, _ p: Double, detail: String?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.subheadline.weight(.semibold))
                Spacer()
                Text(p, format: .percent.precision(.fractionLength(0))).font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
            }
            ProgressBar(value: p, color: tint)
            if let detail { Text(detail).font(.caption).foregroundStyle(Theme.dim) }
        }
        .card(padding: 12, color: Theme.card2)
    }

    private func ask() async {
        do { options = try await downloads.options(for: item); asking = true }
        catch { store.show(error.localizedDescription, isError: true) }
    }
}

/// Original (fastest) or converted by Plex, with rough sizes.
struct DownloadOptionsSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let item: MediaItem
    let options: [Downloads.Option]
    @State private var pick: String = AppSettings.downloadQuality

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(options) { o in
                    Button { pick = o.quality } label: {
                        HStack(spacing: 12) {
                            Image(systemName: pick == o.quality ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(pick == o.quality ? Theme.accent : Theme.dim)
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: 6) {
                                    Text(o.label).font(.headline)
                                    if o.instant { Pill(text: "Starts now") }
                                }
                                Text([o.detail, o.bytes.map(size)].compactMap { $0 }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                        }
                        .card(padding: 12, color: Theme.card2)
                    }
                    .buttonStyle(.plain)
                }
                Text(AppSettings.downloadOnCellular ? "Downloads in the background, on any connection." : "Downloads in the background on Wi-Fi. You can change that in Storage.")
                    .font(.caption).foregroundStyle(Theme.dim)
                Spacer(minLength: 0)
                PrimaryButton(title: "Download", symbol: "arrow.down.circle.fill") {
                    guard let o = options.first(where: { $0.quality == pick }) ?? options.first else { return }
                    Task {
                        do { try await Downloads.shared.start(item, o); store.show(o.instant ? "Downloading" : "Getting it ready", item.title) }
                        catch { store.show(error.localizedDescription, isError: true) }
                    }
                    dismiss()
                }
            }
            .padding(16)
            .navigationTitle("Download \(item.title)").navigationBarTitleDisplayMode(.inline)
            .onAppear { if !options.contains(where: { $0.quality == pick }) { pick = options.first?.quality ?? "high" } }
        }
    }
}

func size(_ b: Int64) -> String { ByteCountFormatter.string(fromByteCount: b, countStyle: .file) }

/// A show's episodes, each with its own download button.
struct EpisodesView: View {
    @Environment(HomeStore.self) private var store
    let show: MediaItem
    @State private var episodes: [MediaItem]?

    var body: some View {
        List {
            if let episodes {
                ForEach(episodes) { e in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(e.title.replacingOccurrences(of: "\(show.title): ", with: "")).font(.subheadline.weight(.semibold))
                        DownloadButton(item: e)
                    }
                    .padding(.vertical, 4)
                }
                if episodes.isEmpty { Text("No episodes found.").foregroundStyle(Theme.dim) }
            } else { ProgressView() }
        }
        .navigationTitle(show.title).navigationBarTitleDisplayMode(.inline)
        .task {
            do { episodes = (try await HubClient.shared.get("/v1/media/\(show.id)/episodes", as: MediaList.self)).items }
            catch { episodes = []; store.show(error.localizedDescription, isError: true) }
        }
    }
}

// MARK: - Downloads list

struct DownloadsView: View {
    @State private var playing: Downloads.Title?
    private let downloads = Downloads.shared

    var body: some View {
        List {
            let all = downloads.mine
            let ready = all.filter { $0.state == .ready }
            let busy = all.filter { $0.state != .ready }
            if all.isEmpty {
                Section {
                    VStack(spacing: 8) {
                        Image(systemName: "arrow.down.circle").font(.largeTitle).foregroundStyle(Theme.dim)
                        Text("Nothing downloaded yet").font(.headline)
                        Text("Open a film or show and tap Download to watch it with no connection, like on a plane.")
                            .font(.footnote).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 20)
                }
            }
            if !ready.isEmpty {
                Section("Ready to watch") {
                    ForEach(ready) { t in Button { playing = t } label: { DownloadRow(title: t) }.buttonStyle(.plain) }
                        .onDelete { ix in ix.map { ready[$0] }.forEach(downloads.remove) }
                }
            }
            if !busy.isEmpty {
                Section("In progress") {
                    ForEach(busy) { t in DownloadRow(title: t) }
                        .onDelete { ix in ix.map { busy[$0] }.forEach(downloads.remove) }
                }
            }
        }
        .navigationTitle("Downloads")
        .fullScreenCover(item: $playing) { OfflinePlayer(title: $0) }
    }
}

struct DownloadRow: View {
    let title: Downloads.Title
    private let downloads = Downloads.shared

    var body: some View {
        HStack(spacing: 12) {
            poster
            VStack(alignment: .leading, spacing: 4) {
                Text(title.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                Text(line).font(.caption).foregroundStyle(title.state == .failed ? Theme.warn : Theme.dim).lineLimit(2)
                if title.state == .preparing || title.state == .downloading { ProgressBar(value: title.progress) }
                if title.state == .ready, let d = title.durationS, d > 0, title.positionS > 0 { ProgressBar(value: title.positionS / d, color: Theme.dim) }
            }
            Spacer(minLength: 0)
            if title.state == .ready { Image(systemName: "play.circle.fill").font(.title2).foregroundStyle(Theme.accent) }
            if title.state == .failed { Button("Retry") { downloads.retry(title) }.buttonStyle(.bordered).controlSize(.small) }
        }
        .padding(.vertical, 2)
    }

    private var poster: some View {
        Group {
            if let d = try? Data(contentsOf: downloads.posterURL(title)), let img = UIImage(data: d) {
                Image(uiImage: img).resizable().scaledToFill()
            } else {
                LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom)
                    .overlay(Image(systemName: "film").foregroundStyle(Theme.dim))
            }
        }
        .frame(width: 46, height: 68).clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var line: String {
        let parts: [String?] = [title.rating, title.qualityLabel, title.bytes.map(size)]
        let base = parts.compactMap { $0 }.joined(separator: " · ")
        switch title.state {
        case .ready: return title.watchedAt != nil ? "\(base) · Watched" : base
        case .preparing: return "Home is getting it ready · \(base)"
        case .downloading: return AppSettings.downloadOnCellular ? "Downloading · \(base)" : "Downloading on Wi-Fi · \(base)"
        case .failed: return title.error ?? "Stopped"
        }
    }
}

/// A card that opens Downloads (Watch tab, kids' Watch tab).
struct DownloadsEntry: View {
    var tint: Color = Theme.accent
    private let downloads = Downloads.shared
    var body: some View {
        let mine = downloads.mine
        let ready = mine.filter { $0.state == .ready }.count
        let busy = mine.count - ready
        NavigationLink { DownloadsView() } label: {
            HStack(spacing: 12) {
                IconBadge(symbol: "arrow.down.circle.fill", color: tint, size: 38)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Downloads").font(.headline)
                    Text(ready == 0 && busy == 0 ? "Watch with no connection" :
                            "\(ready) ready to watch" + (busy > 0 ? " · \(busy) on the way" : ""))
                        .font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
            }
            .card(padding: 12)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Player (downloaded titles only)

/// Plays a downloaded file. Live titles still go to a TV; this is for the car and the plane.
struct OfflinePlayer: View {
    @Environment(\.dismiss) private var dismiss
    let title: Downloads.Title
    @State private var player: AVPlayer?
    @State private var observer: Any?

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.black.ignoresSafeArea()
            if let player { VideoPlayer(player: player).ignoresSafeArea() }
            Button { close() } label: {
                Image(systemName: "xmark").font(.headline).foregroundStyle(.white).padding(12)
                    .background(.ultraThinMaterial, in: Circle())
            }
            .padding(16)
            .accessibilityLabel("Close")
        }
        .onAppear(perform: start)
        .onDisappear { save(); if let observer { player?.removeTimeObserver(observer) } }
    }

    private func start() {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        let p = AVPlayer(url: Downloads.shared.fileURL(title))
        if title.positionS > 5 { p.seek(to: CMTime(seconds: title.positionS, preferredTimescale: 600)) }
        observer = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 15, preferredTimescale: 1), queue: .main) { _ in
            Task { @MainActor in save(post: false) }
        }
        player = p
        p.play()
    }

    private func close() { save(); dismiss() }

    /// Remember where it stopped. Only the last position goes to Plex, when home is reachable.
    private func save(post: Bool = true) {
        guard let p = player else { return }
        let at = p.currentTime().seconds
        let dur = p.currentItem?.duration.seconds
        guard at.isFinite, at > 0 else { return }
        if post { Downloads.shared.played(title, at: at, duration: dur?.isFinite == true ? dur : nil) }
    }
}

// MARK: - Storage and downloads

/// What's saved on this phone, how big and how fresh, and the switches for it.
struct OfflineStorageView: View {
    @Environment(HomeStore.self) private var store
    @State private var usage: [OfflineCache.Usage] = []
    @State private var cellular = AppSettings.downloadOnCellular
    @State private var removeWatched = AppSettings.removeWatchedDownloads
    @State private var confirmClear = false
    private let downloads = Downloads.shared
    private let trips = TripStore.shared
    private let outbox = Outbox.shared

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(size(total)).font(.title2.weight(.bold))
                        Spacer()
                        Text(limitLine).font(.caption).foregroundStyle(Theme.dim)
                    }
                    UsageBar(usage: shown, total: max(total, 1))
                    LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading), GridItem(.flexible(), alignment: .leading)], spacing: 4) {
                        ForEach(shown, id: \.category) { u in
                            HStack(spacing: 6) {
                                Circle().fill(Self.color(u.category)).frame(width: 8, height: 8)
                                Text("\(u.category.title.components(separatedBy: ",").first ?? "") · \(size(u.bytes))").font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                            }
                        }
                    }
                }
                .padding(.vertical, 4)
            }

            Section {
                NavigationLink { DownloadsView() } label: {
                    row("film.stack", "Downloads", "\(downloads.mine.count) title\(downloads.mine.count == 1 ? "" : "s") · \(size(downloads.bytesUsed))")
                }
                ForEach(trips.keptTrips) { t in
                    HStack {
                        row("airplane", t.title, OfflineCache.savedAt(path: "/v1/trips/\(t.id)").map { "Saved \(Freshness.text($0))" } ?? "Saving…")
                        if canChangeTrips {
                            Toggle("Keep", isOn: Binding(get: { trips.isKept(t.id) }, set: { trips.setKept(t.id, $0); refresh() })).labelsHidden()
                        }
                    }
                }
                if let u = usage.first(where: { $0.category == .house }), u.bytes > 0 {
                    row("cube.transparent", "3D house", "Saved \(u.newest.map(Freshness.text) ?? "") · \(size(u.bytes))")
                }
            } header: { Text("Kept offline") } footer: {
                Text("Trips are kept by themselves from two weeks before until a week after. Downloads stay until you remove them\(removeWatched ? " or a week after they're watched" : "").")
            }

            Section("Recent, trimmed by itself") {
                row("house", "Home, calendar, meals, lists", (usage.first { $0.category == .home }?.newest).map { "Saved \(Freshness.text($0))" } ?? "Nothing saved yet")
                if store.me?.can(.cameras) == true {
                    row("video", "Camera pictures", "Last 24 hours only · \(size(usage.first { $0.category == .cameras }?.bytes ?? 0))")
                }
                row("photo", "Posters and photos", size(usage.first { $0.category == .pictures }?.bytes ?? 0))
            }

            if !outbox.visible.isEmpty {
                Section {
                    ForEach(outbox.visible) { i in row("clock", i.label ?? "", "Done \(Freshness.text(i.createdAt))") }
                        .onDelete { ix in ix.map { outbox.visible[$0] }.forEach(outbox.discard) }
                } header: { Text("Waiting to send") } footer: {
                    Text("Sent by itself when home is back. Lights, locks and doors are never saved up like this.")
                }
            }

            Section {
                Toggle(isOn: $cellular) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Download on cellular")
                        Text(cellular ? "Films download on any connection" : "Films wait for Wi-Fi").font(.caption).foregroundStyle(Theme.dim)
                    }
                }
                .onChange(of: cellular) { _, v in AppSettings.downloadOnCellular = v }
                Toggle(isOn: $removeWatched) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Remove watched downloads")
                        Text("A week after finishing").font(.caption).foregroundStyle(Theme.dim)
                    }
                }
                .onChange(of: removeWatched) { _, v in AppSettings.removeWatchedDownloads = v }
                Button("Clear saved copies", role: .destructive) { confirmClear = true }
            } header: { Text("Settings") } footer: {
                Text("Clearing keeps downloads and trips. Saved copies are only for this person and are wiped if the phone is given to someone else.")
            }
        }
        .navigationTitle("Storage")
        .onAppear(perform: refresh)
        .refreshable { await trips.load(force: true); refresh() }
        .confirmationDialog("Clear saved copies?", isPresented: $confirmClear, titleVisibility: .visible) {
            Button("Clear", role: .destructive) { OfflineCache.clearRecent(); refresh(); store.show("Saved copies cleared", "Downloads and trips kept") }
        } message: { Text("Screens fill in again next time home answers.") }
        .task { await trips.load() }
    }

    private var canChangeTrips: Bool { store.role == .admin || store.role == .adult }
    private var shown: [OfflineCache.Usage] { usage.filter { $0.bytes > 0 } }
    private var total: Int64 { usage.reduce(0) { $0 + $1.bytes } }
    private var limitLine: String {
        if store.role == .child { return "of \(size(Downloads.childLimit)) for downloads" }
        let free = (try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?.volumeAvailableCapacityForImportantUsage
        return free.map { "\(size($0)) free on this \(UIDevice.current.model)" } ?? ""
    }

    private func refresh() { usage = OfflineCache.usage() }

    private func row(_ symbol: String, _ title: String, _ detail: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).frame(width: 28).foregroundStyle(Theme.accent)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).lineLimit(1)
                Text(detail).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
            }
        }
    }

    static func color(_ c: OfflineCache.Category) -> Color {
        switch c {
        case .downloads: Theme.accent
        case .trips: .cyan
        case .cameras: Theme.warn
        case .house: Theme.kid
        case .home: Theme.dim
        case .pictures: Theme.little
        }
    }
}

private struct UsageBar: View {
    let usage: [OfflineCache.Usage]
    let total: Int64
    var body: some View {
        GeometryReader { g in
            HStack(spacing: 1) {
                ForEach(usage, id: \.category) { u in
                    OfflineStorageView.color(u.category)
                        .frame(width: max(3, g.size.width * CGFloat(Double(u.bytes) / Double(total))))
                }
                Spacer(minLength: 0)
            }
        }
        .frame(height: 12)
        .background(Theme.card2)
        .clipShape(Capsule())
    }
}
