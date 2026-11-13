import SwiftUI

struct MediaView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var requests: [TitleRequest] = []
    @State private var watchAnywhere = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let np = store.home?.nowPlaying, !np.isEmpty {
                    SectionHeader("Now playing", live: true)
                    ForEach(np) { HouseNowPlayingCard(item: $0) }
                }
                HStack(spacing: 10) {
                    SectionTile(symbol: "play.rectangle.fill", title: "Watch", detail: "Search the library") { router.go(.watch) }
                    SectionTile(symbol: "hifispeaker.2.fill", title: "Music", detail: speakersLine) { router.go(.music) }
                }
                .padding(.top, 8)
                if !(store.home?.devices(kind: .media) ?? []).isEmpty {
                    SectionTile(symbol: "tv.badge.wifi", title: "Watch on a TV", detail: "YouTube, Netflix, Plex and more, on any TV") {
                        watchAnywhere = true
                    }
                }
                HStack(spacing: 10) {
                    let onWay = requests.filter { ["searching", "downloading", "waiting", "failed"].contains($0.status) }.count
                    SectionTile(symbol: "tray.and.arrow.down.fill", title: "Requests",
                                detail: onWay > 0 ? "\(onWay) on the way" : requests.isEmpty ? "Track what you asked for" : "\(requests.count) recent") { router.go(.requests) }
                    if store.me?.can(.mediaRequest) == true {
                        SectionTile(symbol: "plus.circle.fill", title: "Ask for something", detail: "Movies and shows") { router.go(.askMedia) }
                    }
                }
                MyShowsRow()
                let moving = requests.filter { ["downloading", "searching"].contains($0.status) }.prefix(4)
                if !moving.isEmpty {
                    SectionHeader("On the way", live: true)
                    VStack(spacing: 0) {
                        ForEach(Array(moving)) { r in
                            Button { router.go(.mediaRequest(r.id)) } label: { RequestRow(r: r, showWho: false).padding(.vertical, 10) }
                                .buttonStyle(.plain)
                            if r.id != moving.last?.id { Divider().overlay(Theme.line) }
                        }
                    }
                    .padding(.horizontal, 14)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                }
                if store.me?.role == .admin || store.me?.role == .adult {
                    SectionTile(symbol: "ipad.and.arrow.forward", title: "Kids' iPads", detail: "Send shows and YouTube, see what's on them") { router.go(.kidsIPads) }
                }
                DownloadsEntry()
                YouTubeShelf()
                let tvs = store.home?.devices(kind: .media) ?? []
                if !tvs.isEmpty {
                    SectionHeader("TVs", live: true)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                        ForEach(tvs) { DeviceTile(device: $0) }
                    }
                }
                if store.me?.can(.remotesSetup) == true {
                    SectionTile(symbol: "plus.rectangle.on.rectangle", title: "Add a TV or box",
                                detail: "Android TV and Apple TV, added once for everyone") { router.go(.tvSetup) }
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Media")
        .topBar()
        .task { while !Task.isCancelled { await loadRequests(); try? await Task.sleep(for: .seconds(20)) } }
        .refreshable { await store.loadHome(); await loadRequests() }
        .sheet(isPresented: $watchAnywhere) { WatchAnywhereSheet() }
    }

    private var speakersLine: String {
        let n = (store.home?.devices(kind: .speaker) ?? []).filter { $0["state"]?.string == "playing" }.count
        return n == 0 ? "All quiet" : "\(n) playing"
    }

    private func loadRequests() async {
        if let l: TitleRequestList = try? await HubClient.shared.get("/v1/media/requests", ["scope": "mine"]) { requests = l.items }
    }
}

// MARK: - Music (Sonos)

struct MusicView: View {
    @Environment(HomeStore.self) private var store
    @State private var announce = ""
    @State private var volumes: [String: Double] = [:]

    var body: some View {
        let speakers = store.home?.devices(kind: .speaker) ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(speakers) { s in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(s.name).font(.headline)
                                Text(s["state"]?.string == "playing" ? [s["title"]?.string, s["artist"]?.string].compactMap { $0 }.joined(separator: " · ") : "Idle")
                                    .font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                            }
                            Spacer()
                            Button {
                                store.perform("media.command", target: s.id, params: ["command": s["state"]?.string == "playing" ? "pause" : "play"])
                            } label: {
                                Image(systemName: s["state"]?.string == "playing" ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 34))
                            }
                            .buttonStyle(.plain).foregroundStyle(Theme.accent)
                        }
                        HStack {
                            Image(systemName: "speaker.fill").foregroundStyle(Theme.dim)
                            Slider(value: Binding(get: { volumes[s.id] ?? s["volume"]?.double ?? 20 }, set: { volumes[s.id] = $0 }), in: 0...100) { editing in
                                if !editing, let v = volumes[s.id] { store.perform("media.volume", target: s.id, params: ["volume": Int(v)]) }
                            }
                            Image(systemName: "speaker.wave.3.fill").foregroundStyle(Theme.dim)
                        }
                    }
                    .card()
                }
                if speakers.isEmpty { EmptyCard(symbol: "hifispeaker", title: "No speakers found") }
                SectionHeader("Announce")
                VStack(alignment: .leading, spacing: 10) {
                    TextField("Dinner in five!", text: $announce).textFieldStyle(.roundedBorder)
                    PrimaryButton(title: "Play on every speaker", symbol: "megaphone.fill") {
                        let t = announce.trimmingCharacters(in: .whitespaces)
                        guard !t.isEmpty else { return }
                        store.perform("announce", params: ["text": t, "rooms": speakers.map(\.id)], done: "Announced")
                        announce = ""
                    }
                }
                .card()
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Music")
    }
}

// MARK: - Watch (browse Plex, then hand off to a TV)

struct WatchView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var query = ""
    @State private var continueItems: [MediaItem] = []
    @State private var library: [MediaItem]?
    @State private var results: [MediaItem] = []
    @State private var requestable: [Findable] = []
    @State private var searched = false
    @State private var picked: MediaItem?
    @AppStorage("watch.kind") private var kind = "all"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if query.isEmpty {
                    if !continueItems.isEmpty {
                        SectionHeader("Continue watching")
                        PosterRow(items: continueItems) { picked = $0 }
                    }
                    Picker("Kind", selection: $kind) {
                        Text("All").tag("all"); Text("Movies").tag("movie"); Text("Shows").tag("show")
                    }
                    .pickerStyle(.segmented).padding(.top, 6)
                    if let library {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                            ForEach(library) { m in Poster(item: m).onTapGesture { picked = m } }
                        }
                        if library.isEmpty {
                            EmptyCard(symbol: store.reachable ? "film.stack" : "wifi.slash",
                                      title: store.reachable ? "Nothing here yet" : "Library needs home",
                                      detail: store.reachable ? nil : "Downloads still play. Open Downloads on the Media tab.")
                        }
                    } else { LoadingCard() }
                    if store.me?.can(.mediaRequest) == true {
                        Button { router.go(.askMedia) } label: {
                            Label("Not here? Ask for it", systemImage: "plus.circle").font(.headline).frame(maxWidth: .infinity, minHeight: 46)
                        }
                        .buttonStyle(.bordered).padding(.top, 4)
                    }
                } else {
                    if !results.isEmpty {
                        SectionHeader("On Plex")
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                            ForEach(results) { m in Poster(item: m).onTapGesture { picked = m } }
                        }
                    }
                    if !requestable.isEmpty {
                        SectionHeader("Not on Plex yet")
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                            ForEach(requestable) { f in
                                Button { router.go(.mediaTitle(f.mediaId)) } label: { FindableTile(f: f) }.buttonStyle(.plain)
                            }
                        }
                    }
                    if searched && results.isEmpty && requestable.isEmpty {
                        EmptyCard(symbol: "magnifyingglass", title: "No matches", detail: "Try another spelling.")
                    }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Watch")
        .searchable(text: $query, prompt: "Movies and shows")
        .task { if continueItems.isEmpty, let l: MediaList = try? await HubClient.shared.get("/v1/media/continue") { continueItems = l.items } }
        .task(id: kind) {
            library = nil
            let q = kind == "all" ? [:] : ["kind": kind]
            library = (try? await HubClient.shared.get("/v1/media/library", q, as: MediaList.self))?.items ?? []
        }
        .task(id: query) {
            guard !query.isEmpty else { searched = false; return }
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled else { return }
            if let l: WatchSearch = try? await HubClient.shared.get("/v1/media/search", ["q": query]) {
                results = l.items; requestable = l.requestable ?? []
            }
            searched = true
        }
        .sheet(item: $picked) { PlayOnSheet(item: $0) }
    }
}

struct WatchSearch: Codable { var items: [MediaItem]; var requestable: [Findable]? }

struct PosterRow: View {
    let items: [MediaItem]
    let tap: (MediaItem) -> Void
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 12) {
                ForEach(items) { m in Poster(item: m).frame(width: 112).onTapGesture { tap(m) } }
            }
        }
        .scrollClipDisabled()
    }
}

struct Poster: View {
    let item: MediaItem
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HubImage(path: item.posterUrl) {
                LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom)
                    .overlay(Text(item.title).font(.caption.weight(.semibold)).multilineTextAlignment(.center).padding(6))
            }
            .aspectRatio(2 / 3, contentMode: .fill)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(alignment: .bottom) {
                if let p = item.progress { ProgressBar(value: p).padding(6) }
            }
            Text(item.title).font(.caption.weight(.semibold)).lineLimit(1)
            if let y = item.year { Text(String(y)).font(.caption2).foregroundStyle(Theme.dim) }
        }
    }
}

/// "Play on" picker: hand the title to a TV.
struct PlayOnSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let item: MediaItem
    @State private var sending: SendTarget?
    var body: some View {
        NavigationStack {
            List {
                Section {
                    if let s = item.summary { Text(s).font(.subheadline).foregroundStyle(Theme.dim) }
                    if store.me?.role == .admin || store.me?.role == .adult {
                        Button { sending = SendTarget.from(item) } label: { Label("Send to kids' iPads", systemImage: "ipad.and.arrow.forward") }
                            .sheet(item: $sending) { AddToIPadView(target: $0) }
                    }
                }
                Section {
                    DownloadButton(item: item)
                } footer: { Text("Downloads play here, with no connection.") }
                Section {
                    Button { store.fixLanguage(item, on: usual) } label: {
                        Label("Not in English? Fix audio and subtitles", systemImage: "captions.bubble")
                    }
                } footer: { Text("Picks English audio, or the original audio with English subtitles.") }
                Section("Play on") {
                    ForEach(tvs) { tv in
                        Button {
                            store.play(item, on: tv)
                            // The TV someone picks becomes their usual one, on every device they use.
                            if tv.id != usual { SettingsStore.shared.set(SettingKey.playOn, tv.id) }
                            dismiss()
                        } label: {
                            LabeledContent {
                                if tv.id == usual { Text("Your usual").font(.caption) }
                            } label: { Label(tv.name, systemImage: "tv") }
                        }
                    }
                }
            }
            .navigationTitle(item.title).navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
        }
        .presentationDetents([.medium, .large])
    }

    private var usual: String? { SettingsStore.shared.optionalString(SettingKey.playOn) }
    private var tvs: [DeviceItem] {
        let all = store.home?.devices(kind: .media) ?? []
        return all.filter { $0.id == usual } + all.filter { $0.id != usual }
    }
}

/// Shows and movies this person starred, on every device they use. Long-press to remove.
struct MyShowsRow: View {
    @Environment(Router.self) private var router
    var body: some View {
        let mine = SettingsStore.shared.favorites(.media)
        if !mine.isEmpty {
            SectionHeader("My shows")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(mine) { f in
                        Button { router.go(.mediaTitle(f.id)) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                PosterArt(url: f.image, title: f.title).frame(width: 96)
                                Text(f.title ?? "").font(.caption.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                            }
                            .frame(width: 96)
                        }
                        .buttonStyle(.plain)
                        .favoriteMenu(.media, f.id, name: f.title ?? "this", label: f)
                    }
                }
            }
            .scrollClipDisabled()
        }
    }
}

// MARK: - Requests (see MediaRequestsViews.swift)

enum RequestStatus {
    static func text(_ r: MediaRequest) -> String {
        switch r.status {
        case "waiting": "Waiting for a parent"
        case "approved": "Approved, finding it"
        case "downloading": "Downloading"
        case "ready": "Ready to watch"
        case "denied": "Not this time"
        default: r.status.capitalized
        }
    }
}

struct PipelineView: View {
    @State private var p: Pipeline?
    @State private var failed = false
    var body: some View {
        List {
            if let p {
                if let f = p.failures, !f.isEmpty {
                    Section("Problems") {
                        ForEach(f) { x in
                            InfoRow("exclamationmark.triangle.fill", color: Theme.crit, (x.source ?? x.title ?? "Service").capitalized, x.error ?? x.detail)
                        }
                    }
                }
                Section("Requests") {
                    let r = p.requests ?? []
                    if r.isEmpty { Text("No open requests").foregroundStyle(Theme.dim) }
                    ForEach(r, id: \.stableID) { q in
                        HStack(spacing: 12) {
                            HubImage(path: q.posterUrl) { Theme.card2.overlay(Image(systemName: q.kind == "tv" ? "tv" : "film").foregroundStyle(Theme.dim)) }
                                .frame(width: 40, height: 60).clipShape(RoundedRectangle(cornerRadius: 6))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(q.title + (q.year.map { " (\($0))" } ?? "")).font(.subheadline.weight(.semibold))
                                Text([q.requestedBy.map { "Asked by \($0)" }, q.mediaStatus ?? q.status].compactMap { $0 }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                        }
                    }
                }
                Section("Downloading") {
                    let d = p.downloads ?? []
                    if d.isEmpty { Text("Nothing downloading").foregroundStyle(Theme.dim) }
                    ForEach(d) { d in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack { Text(d.title).font(.subheadline.weight(.semibold)).lineLimit(2); Spacer(); Text(d.state ?? "").font(.caption).foregroundStyle(Theme.dim) }
                            if let pr = d.progress { ProgressBar(value: pr > 1 ? pr / 100 : pr) }
                            if let e = d.error { Text(e).font(.caption2).foregroundStyle(Theme.warn) }
                            else if let eta = d.etaS, eta > 0 { Text(eta < 3600 ? "\(Int(eta / 60)) min left" : "\(Int(eta / 3600)) h left").font(.caption2).foregroundStyle(Theme.dim) }
                        }
                    }
                }
                Section("Coming up") {
                    let u = p.upcoming ?? []
                    if u.isEmpty { Text("Nothing scheduled").foregroundStyle(Theme.dim) }
                    ForEach(u) { item($0, upcoming: true) }
                }
                Section("Recently added") {
                    let r = p.recent ?? []
                    if r.isEmpty { Text("Nothing new yet").foregroundStyle(Theme.dim) }
                    ForEach(r) { item($0, upcoming: false) }
                }
            } else if failed {
                ContentUnavailableView("Couldn't load the pipeline", systemImage: "arrow.down.circle", description: Text("The hub didn't answer. Pull to try again."))
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Media pipeline")
        .refreshable { await load() }
        .task { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(8)) } }
    }

    private func load() async {
        do { p = try await HubClient.shared.get("/v1/pipeline"); failed = false } catch { if p == nil { failed = true } }
    }

    private func item(_ i: Pipeline.Item, upcoming: Bool) -> some View {
        HStack(spacing: 12) {
            Image(systemName: i.kind == "episode" || i.kind == "tv" ? "tv" : i.kind == "music" || i.kind == "album" ? "music.note" : "film")
                .foregroundStyle(Theme.accent).frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(i.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                if let d = i.detail { Text(d).font(.caption).foregroundStyle(Theme.dim).lineLimit(2) }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                if let date = i.date { Text(date.formatted(.relative(presentation: .named))).font(.caption2).foregroundStyle(Theme.dim) }
                if upcoming, i.hasFile == true { Image(systemName: "checkmark.circle.fill").font(.caption).foregroundStyle(Theme.accent) }
            }
        }
    }
}
