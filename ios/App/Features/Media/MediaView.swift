import SwiftUI

struct MediaView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var requests: [MediaRequest] = []

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
                    let waiting = requests.filter(\.waiting).count
                    SectionTile(symbol: "tray.and.arrow.down.fill", title: "Requests",
                                detail: waiting > 0 ? "\(waiting) waiting" : "\(requests.count) recent", alert: waiting > 0) { router.go(.requests) }
                    if store.me?.can(.pipeline) == true {
                        SectionTile(symbol: "arrow.down.circle.fill", title: "Downloads", detail: "Pipeline") { router.go(.pipeline) }
                    }
                }
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
        .task { await loadRequests() }
        .refreshable { await store.loadHome(); await loadRequests() }
    }

    private var speakersLine: String {
        let n = (store.home?.devices(kind: .speaker) ?? []).filter { $0["state"]?.string == "playing" }.count
        return n == 0 ? "All quiet" : "\(n) playing"
    }

    private func loadRequests() async {
        if let l: RequestList = try? await HubClient.shared.get("/v1/requests") { requests = l.items }
    }
}

// MARK: - TV remote

struct RemoteView: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String

    var body: some View {
        let d = store.home?.device(deviceID)
        ScrollView {
            VStack(spacing: 22) {
                VStack(spacing: 4) {
                    Text(d?.name ?? "TV").font(.title2.weight(.bold))
                    Text(d.map { $0.isOn ? ($0["source"]?.string ?? $0["title"]?.string ?? "On") : "Off" } ?? "").foregroundStyle(Theme.dim)
                }
                HStack(spacing: 14) {
                    key("power", "power", tint: d?.isOn == true ? Theme.crit : Theme.accent)
                    Spacer()
                    key("house.fill", "home")
                    key("arrow.uturn.backward", "back")
                }
                // D-pad
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
                HStack(spacing: 14) {
                    key("backward.fill", "prev")
                    key(d?["state"]?.string == "playing" ? "pause.fill" : "play.fill", d?["state"]?.string == "playing" ? "pause" : "play")
                    key("forward.fill", "next")
                    Spacer()
                    VStack(spacing: 10) {
                        key("speaker.plus.fill", "vol_up")
                        key("speaker.minus.fill", "vol_down")
                    }
                    key("speaker.slash.fill", "mute")
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
    @State private var query = ""
    @State private var continueItems: [MediaItem] = []
    @State private var results: [MediaItem] = []
    @State private var picked: MediaItem?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if query.isEmpty {
                    SectionHeader("Continue watching")
                    PosterRow(items: continueItems) { picked = $0 }
                } else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                        ForEach(results) { m in Poster(item: m).onTapGesture { picked = m } }
                    }
                    if results.isEmpty {
                        EmptyCard(symbol: "magnifyingglass", title: "Not in the library", detail: "Ask for it and it'll be downloaded.")
                        PrimaryButton(title: "Request \"\(query)\"", symbol: "plus") {
                            Task {
                                try? await HubClient.shared.send("/v1/requests", ["kind": "media", "text": query])
                                store.show("Requested", query)
                            }
                        }
                    }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Watch")
        .searchable(text: $query, prompt: "Movies and shows")
        .task(id: query) {
            if query.isEmpty {
                if continueItems.isEmpty, let l: MediaList = try? await HubClient.shared.get("/v1/media/continue") { continueItems = l.items }
                return
            }
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled else { return }
            if let l: MediaList = try? await HubClient.shared.get("/v1/media/search", ["q": query]) { results = l.items }
        }
        .sheet(item: $picked) { PlayOnSheet(item: $0) }
    }
}

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
                Section("Play on") {
                    ForEach(store.home?.devices(kind: .media) ?? []) { tv in
                        Button {
                            store.play(item, on: tv)
                            dismiss()
                        } label: {
                            Label(tv.name, systemImage: "tv")
                        }
                    }
                }
            }
            .navigationTitle(item.title).navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
        }
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Requests

struct RequestsView: View {
    @Environment(HomeStore.self) private var store
    @State private var items: [MediaRequest]?

    var body: some View {
        List {
            if let items {
                let waiting = items.filter(\.waiting)
                if !waiting.isEmpty {
                    Section("Waiting for you") { ForEach(waiting) { row($0, decide: store.me?.can(.mediaApprove) == true) } }
                }
                Section("Recent") { ForEach(items.filter { !$0.waiting }) { row($0, decide: false) } }
                if items.isEmpty { ContentUnavailableView("No requests", systemImage: "tray") }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Requests")
        .task { await load() }
        .refreshable { await load() }
    }

    private func row(_ r: MediaRequest, decide: Bool) -> some View {
        HStack(spacing: 12) {
            HubImage(path: r.posterUrl) { Theme.card2 }.frame(width: 44, height: 64).clipShape(RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 3) {
                Text(r.title).font(.subheadline.weight(.semibold))
                Text([r.who, RequestStatus.text(r)].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim)
                if let p = r.progress, r.status == "downloading" { ProgressBar(value: p) }
            }
            Spacer()
            if decide {
                Button { decideOn(r, "deny") } label: { Image(systemName: "xmark") }.buttonStyle(.bordered).tint(Theme.dim)
                Button { decideOn(r, "approve") } label: { Image(systemName: "checkmark") }.buttonStyle(.borderedProminent)
            }
        }
    }

    private func decideOn(_ r: MediaRequest, _ verb: String) {
        Task {
            do {
                try await HubClient.shared.send("/v1/requests/\(r.id)/\(verb)")
                store.show(verb == "approve" ? "Approved \(r.title)" : "Declined")
                await load()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func load() async {
        if let l: RequestList = try? await HubClient.shared.get("/v1/requests") { items = l.items } else if items == nil { items = [] }
    }
}

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
    var body: some View {
        List {
            if let p {
                Section("Downloads") {
                    if p.downloads.isEmpty { Text("Nothing downloading").foregroundStyle(Theme.dim) }
                    ForEach(p.downloads) { d in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack { Text(d.title).font(.subheadline.weight(.semibold)); Spacer(); Text(d.state).font(.caption).foregroundStyle(Theme.dim) }
                            ProgressBar(value: d.progress)
                            if let eta = d.etaS { Text("\(Int(eta / 60)) min left").font(.caption2).foregroundStyle(Theme.dim) }
                        }
                    }
                }
                if let f = p.failures, !f.isEmpty {
                    Section("Problems") {
                        ForEach(f) { InfoRow("exclamationmark.triangle.fill", color: Theme.crit, $0.title, $0.detail) }
                    }
                }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Downloads")
        .task { while !Task.isCancelled { p = try? await HubClient.shared.get("/v1/pipeline"); try? await Task.sleep(for: .seconds(5)) } }
    }
}
