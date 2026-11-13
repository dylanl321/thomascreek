import SwiftUI

struct MediaView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var requests: [TitleRequest] = []

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
                DownloadsEntry()
                YouTubeShelf()
                let tvs = store.home?.devices(kind: .media) ?? []
                if !tvs.isEmpty {
                    SectionHeader("TVs", live: true)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                        ForEach(tvs) { DeviceTile(device: $0) }
                    }
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Media")
        .topBar()
        .task { while !Task.isCancelled { await loadRequests(); try? await Task.sleep(for: .seconds(20)) } }
        .refreshable { await store.loadHome(); await loadRequests() }
    }

    private var speakersLine: String {
        let n = (store.home?.devices(kind: .speaker) ?? []).filter { $0["state"]?.string == "playing" }.count
        return n == 0 ? "All quiet" : "\(n) playing"
    }

    private func loadRequests() async {
        if let l: TitleRequestList = try? await HubClient.shared.get("/v1/media/requests", ["scope": "mine"]) { requests = l.items }
    }
}

// MARK: - TV remote

/// Talks to the TV through Home Assistant (media_player and remote services, via the hub).
/// Only the buttons the TV actually supports are shown; the hub lists them in `commands`.
struct RemoteView: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    @State private var volume: Double?
    @State private var volumeTask: Task<Void, Never>?

    var body: some View {
        let d = store.home?.device(deviceID)
        let cmds: Set<String>? = d?["commands"]?.array.map { Set($0.compactMap(\.string)) }
        let has: (String) -> Bool = { cmds?.contains($0) ?? true }
        let sources = d?["sources"]?.array?.compactMap(\.string) ?? []
        let playing = d?["state"]?.string == "playing"
        ScrollView {
            VStack(spacing: 22) {
                VStack(spacing: 4) {
                    Text(d?.name ?? "TV").font(.title2.weight(.bold))
                    Text(d.map { $0.isOn ? ($0["title"]?.string ?? $0["source"]?.string ?? "On") : "Off" } ?? "").foregroundStyle(Theme.dim)
                }
                HStack(spacing: 14) {
                    if has("power") || has("turn_on") { key("power", "power", tint: d?.isOn == true ? Theme.crit : Theme.accent) }
                    Spacer()
                    if has("home") { key("house.fill", "home") }
                    if has("back") { key("arrow.uturn.backward", "back") }
                }
                if !sources.isEmpty {
                    Menu {
                        ForEach(sources, id: \.self) { src in
                            Button { store.perform("media.command", target: deviceID, params: ["command": "source", "source": src], done: "Switched to \(src)") } label: {
                                if d?["source"]?.string == src { Label(src, systemImage: "checkmark") } else { Text(src) }
                            }
                        }
                    } label: {
                        HStack {
                            Image(systemName: "rectangle.on.rectangle")
                            Text(d?["source"]?.string ?? "Choose input")
                            Spacer()
                            Image(systemName: "chevron.up.chevron.down").font(.caption)
                        }
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                        .padding(.horizontal, 16).frame(minHeight: 48)
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                }
                if ["up", "down", "left", "right", "select"].contains(where: has) {
                    ZStack {
                        Circle().fill(Theme.card).frame(width: 240, height: 240)
                        VStack(spacing: 0) {
                            key("chevron.up", "up", plain: true)
                            HStack(spacing: 0) {
                                key("chevron.left", "left", plain: true)
                                Button { send("select") } label: {
                                    Text("OK").font(.headline).frame(width: 84, height: 84).background(Theme.card2, in: Circle())
                                }
                                .buttonStyle(.plain)
                                key("chevron.right", "right", plain: true)
                            }
                            key("chevron.down", "down", plain: true)
                        }
                    }
                }
                HStack(spacing: 14) {
                    if has("prev") { key("backward.fill", "prev") }
                    if has("play") || has("pause") || has("play_pause") {
                        key(playing ? "pause.fill" : "play.fill", has("play_pause") ? "play_pause" : (playing ? "pause" : "play"))
                    }
                    if has("next") { key("forward.fill", "next") }
                }
                .frame(maxWidth: .infinity)
                if let level = d?["volume"]?.double {
                    HStack(spacing: 12) {
                        Button { send("vol_down") } label: { Image(systemName: "speaker.minus.fill") }.buttonStyle(.plain)
                        Slider(value: Binding(get: { volume ?? level }, set: { setVolume($0) }), in: 0...100, step: 1)
                            .tint(Theme.accent)
                        Button { send("vol_up") } label: { Image(systemName: "speaker.plus.fill") }.buttonStyle(.plain)
                        if has("mute") {
                            Button { send("mute") } label: {
                                Image(systemName: d?["muted"]?.bool == true ? "speaker.slash.fill" : "speaker.fill")
                                    .foregroundStyle(d?["muted"]?.bool == true ? Theme.crit : Theme.fg)
                            }
                            .buttonStyle(.plain).padding(.leading, 6).accessibilityLabel("Mute")
                        }
                    }
                    .foregroundStyle(Theme.fg)
                    .padding(.horizontal, 16).frame(minHeight: 52)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                } else if has("vol_up") || has("vol_down") {
                    HStack(spacing: 14) {
                        key("speaker.minus.fill", "vol_down")
                        key("speaker.plus.fill", "vol_up")
                        if has("mute") { key(d?["muted"]?.bool == true ? "speaker.slash.fill" : "speaker.fill", "mute") }
                    }
                    .frame(maxWidth: .infinity)
                }
                if let apps = d?["apps"]?.array?.compactMap(\.string), !apps.isEmpty {
                    SectionHeader("Apps")
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 10)], spacing: 10) {
                        ForEach(apps, id: \.self) { a in
                            ChipButton(title: a) { store.perform("media.command", target: deviceID, params: ["command": "app", "app": a]) }
                        }
                    }
                }
            }
            .padding(.top, 12).screen()
        }
        .background(Backdrop())
        .navigationTitle("Remote").navigationBarTitleDisplayMode(.inline)
    }

    /// Slider moves are coalesced so Home Assistant gets one volume_set, not dozens.
    private func setVolume(_ v: Double) {
        volume = v
        volumeTask?.cancel()
        volumeTask = Task {
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            store.perform("media.command", target: deviceID, params: ["command": "volume", "volume": Int(v)])
            try? await Task.sleep(for: .seconds(2))
            if !Task.isCancelled { volume = nil }
        }
    }

    private func send(_ cmd: String) {
        store.perform("media.command", target: deviceID, params: ["command": cmd])
    }

    private func key(_ symbol: String, _ cmd: String, tint: Color = Theme.fg, plain: Bool = false) -> some View {
        Button { send(cmd) } label: {
            Image(systemName: symbol).font(.title3.weight(.semibold)).foregroundStyle(tint)
                .frame(width: plain ? 64 : 58, height: plain ? 64 : 58)
                .background(plain ? Color.clear : Theme.card, in: Circle())
        }
        .buttonStyle(.plain)
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
    var body: some View {
        NavigationStack {
            List {
                Section {
                    if let s = item.summary { Text(s).font(.subheadline).foregroundStyle(Theme.dim) }
                }
                Section {
                    DownloadButton(item: item)
                } footer: { Text("Downloads play here, with no connection.") }
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
