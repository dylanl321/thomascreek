import SwiftUI

// Grown-ups send films, shows and YouTube to the kids' iPads (hub: /v1/sends). The iPads download
// them on their own (`SendsSync`) and keep them fresh: a show's next episodes, a channel's newest
// videos. "Kids' iPads" shows what's on each device so you know what to add or take off.

// MARK: - Models

struct SendsOverview: Codable {
    var people: [Kid]

    struct Kid: Codable, Identifiable, Hashable {
        var person: P
        var devices: [Device]
        var sends: [Send]
        var id: String { person.id }
    }
    struct P: Codable, Hashable { var id: String; var name: String; var role: String?; var uiStyle: String? }
    struct Device: Codable, Identifiable, Hashable {
        var id: String
        var name: String
        var model: String?
        var reportedAt: String?
        var freeBytes: Int64?
        var totalBytes: Int64?
        var usedBytes: Int64
        var sentBytes: Int64
        var holdings: [Holding]
        var canWake: Bool
    }
    struct Holding: Codable, Hashable { var itemId: String; var title: String?; var state: String; var progress: Double?; var bytes: Int64?; var watched: Bool?; var sent: Bool? }
    struct Send: Codable, Identifiable, Hashable {
        var id: String
        var kind: String
        var ref: String
        var title: String
        var posterUrl: String?
        var scope: String
        var count: Int?
        var season: Int?
        var quality: String
        var from: String?
        var state: String
        var detail: String?
        var error: String?
        var items: [Item]
    }
    struct Item: Codable, Hashable, Identifiable {
        var itemId: String
        var title: String?
        var season: Int?
        var episode: Int?
        var ready: Bool?
        var watched: Bool?
        var on: [String: String]
        var id: String { itemId }
    }
}

/// What a send is about, before the who and how much are picked.
struct SendTarget: Hashable, Identifiable {
    enum Kind: String { case movie, show, youtube, request }
    var kind: Kind
    var ref: String           // Plex id, "yt-<id>", a YouTube link or source id, or "movie:123" / "tv:123"
    var title: String
    var posterUrl: String?
    var isShowRequest = false  // a "tv:123" request: it gets show choices
    var id: String { "\(kind.rawValue):\(ref)" }

    var showChoices: Bool { kind == .show || isShowRequest }
    var apiKind: String {
        switch kind {
        case .movie: "title"
        case .show: "show"
        case .youtube: ref.hasPrefix("yt-") ? "title" : "youtube"
        case .request: "request"
        }
    }

    static func from(_ m: MediaItem) -> SendTarget {
        if m.id.hasPrefix("yt-") { return SendTarget(kind: .youtube, ref: m.id, title: m.title, posterUrl: m.posterUrl) }
        return SendTarget(kind: m.kind == "show" ? .show : .movie, ref: m.id, title: m.title, posterUrl: m.posterUrl)
    }
}

// MARK: - Kids' iPads (grown-ups)

struct KidsIPadsView: View {
    @Environment(HomeStore.self) private var store
    @State private var data: SendsOverview?
    @State private var failed: String?
    @State private var adding = false
    @State private var editing: SendsOverview.Send?
    @State private var toppingUp = false

    var body: some View {
        List {
            if let data {
                if data.people.isEmpty {
                    EmptyCard(symbol: "ipad", title: "No kids set up", detail: "Give a child their own person in Systems → People.")
                        .listRowBackground(Color.clear)
                }
                ForEach(data.people) { kid in kidSection(kid) }
            } else if let failed {
                EmptyCard(symbol: "wifi.slash", title: "Couldn't load", detail: failed).listRowBackground(Color.clear)
            } else {
                LoadingCard().listRowBackground(Color.clear)
            }
        }
        .navigationTitle("Kids' iPads")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { adding = true } label: { Label("Add", systemImage: "plus") }
            }
            ToolbarItem(placement: .secondaryAction) {
                Button { Task { await topUp() } } label: { Label("Top up now", systemImage: "arrow.clockwise.icloud") }
                    .disabled(toppingUp)
            }
        }
        .task { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(15)) } }
        .refreshable { await load() }
        .sheet(isPresented: $adding, onDismiss: { Task { await load() } }) {
            AddToIPadView(target: nil, kids: data?.people ?? [])
        }
        .sheet(item: $editing, onDismiss: { Task { await load() } }) { s in EditSendView(send: s) }
    }

    @ViewBuilder private func kidSection(_ kid: SendsOverview.Kid) -> some View {
        Section {
            ForEach(kid.devices) { d in deviceRow(d) }
            if kid.devices.isEmpty {
                Text("No device yet. Open Thomas Creek on their iPad once at home.").font(.footnote).foregroundStyle(Theme.dim)
            }
            ForEach(kid.sends) { s in
                Button { editing = s } label: { sendRow(s, devices: kid.devices) }
                    .buttonStyle(.plain)
                    .swipeActions {
                        Button("Take off", role: .destructive) { Task { await remove(s) } }
                    }
            }
            Button { adding = true } label: { Label("Add to \(kid.person.name)'s iPad", systemImage: "plus.circle") }
        } header: {
            HStack(spacing: 8) {
                Avatar(name: kid.person.name, color: kid.person.uiStyle == "little" ? Theme.little : Theme.kid, size: 26)
                Text(kid.person.name).font(.headline).foregroundStyle(Theme.fg).textCase(nil)
            }
        } footer: {
            if !kid.sends.isEmpty {
                Text("Shows and channels top themselves up: watched episodes and old videos go, the next ones come. Swipe to take something off.")
            }
        }
    }

    private func deviceRow(_ d: SendsOverview.Device) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: (d.model ?? d.name).lowercased().contains("ipad") ? "ipad" : "iphone").foregroundStyle(Theme.accent)
                Text(d.name).font(.subheadline.weight(.semibold))
                Spacer()
                Text(seen(d)).font(.caption).foregroundStyle(Theme.dim)
            }
            let free = d.freeBytes ?? 0
            let total = Double(d.usedBytes + free)
            ProgressBar(value: total > 0 ? Double(d.usedBytes) / total : 0)
            let busy = d.holdings.filter { $0.state == "downloading" || $0.state == "preparing" }.count
            Text([ "\(byteSize(d.usedBytes)) of downloads", d.freeBytes.map { "\(byteSize($0)) free" },
                   busy > 0 ? "\(busy) downloading" : nil, d.canWake ? nil : "can't be woken (notifications off)" ]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(Theme.dim)
        }
        .padding(.vertical, 4)
    }

    private func seen(_ d: SendsOverview.Device) -> String {
        guard let s = d.reportedAt, let date = ISO8601DateFormatter().date(from: s) else { return "Not checked in yet" }
        return "Checked " + Freshness.text(date)
    }

    private func sendRow(_ s: SendsOverview.Send, devices: [SendsOverview.Device]) -> some View {
        HStack(spacing: 12) {
            HubImage(path: s.posterUrl) { Theme.card2.overlay(Image(systemName: icon(s)).foregroundStyle(Theme.dim)) }
                .frame(width: 44, height: 64).clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(s.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                Text(describe(s)).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
                status(s, devices: devices)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.dim)
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }

    @ViewBuilder private func status(_ s: SendsOverview.Send, devices: [SendsOverview.Device]) -> some View {
        if s.state == "waiting" {
            Label(s.detail ?? "Waiting", systemImage: "hourglass").font(.caption.weight(.medium)).foregroundStyle(Theme.warn)
        } else if s.state == "gone" || s.state == "error" || s.state == "empty" {
            Label(s.detail ?? "Needs a look", systemImage: "exclamationmark.triangle").font(.caption.weight(.medium)).foregroundStyle(Theme.warn)
        } else if s.state == "done" {
            Label(s.detail ?? "All watched", systemImage: "checkmark.seal").font(.caption.weight(.medium)).foregroundStyle(Theme.accent)
        } else {
            let total = s.items.count
            let onAll = s.items.filter { i in !devices.isEmpty && devices.allSatisfy { i.on[$0.id] == "ready" } }.count
            let moving = s.items.filter { i in i.on.values.contains { $0 == "downloading" || $0 == "preparing" } }.count
            Label(onAll == total ? "All \(total) on the iPad" : "\(onAll) of \(total) on the iPad" + (moving > 0 ? " · \(moving) downloading" : ""),
                  systemImage: onAll == total ? "checkmark.circle.fill" : "arrow.down.circle")
                .font(.caption.weight(.medium)).foregroundStyle(onAll == total ? Theme.accent : Theme.dim)
        }
    }

    private func icon(_ s: SendsOverview.Send) -> String {
        ["show": "tv", "youtube": "play.rectangle", "request": "hourglass"][s.kind] ?? "film"
    }

    private func load() async {
        do { data = try await HubClient.shared.get("/v1/sends"); failed = nil }
        catch { if data == nil { failed = error.localizedDescription } }
    }

    private func remove(_ s: SendsOverview.Send) async {
        do {
            let _: Empty = try await HubClient.shared.call("DELETE", "/v1/sends/\(s.id)")
            store.show("Taking \(s.title) off", "It's removed the next time the iPad checks in")
            await load()
        } catch { store.show(error.localizedDescription, isError: true) }
    }

    private func topUp() async {
        toppingUp = true
        defer { toppingUp = false }
        do {
            try await HubClient.shared.send("/v1/sends/sync")
            store.show("Topping up", "The iPads were asked to check now")
            await load()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

/// "Next 10 episodes, topped up as they watch", "Season 2", "Newest 15 videos".
func describe(_ s: SendsOverview.Send) -> String {
    let by = s.from.map { " · from \($0)" } ?? ""
    let size = ["original": "Original", "high": "Good (720p)", "small": "Small (480p)"][s.quality] ?? s.quality
    switch (s.kind, s.scope) {
    case ("show", "next"), ("request", "next"): return "Next \(s.count ?? 10) episodes, topped up as they watch" + by
    case ("show", "last"), ("request", "last"): return "Newest \(s.count ?? 10) episodes" + by
    case ("show", "season"), ("request", "season"): return (s.season.map { "Season \($0)" } ?? "Latest season") + by
    case ("show", "all"), ("request", "all"): return "Every episode · \(size)" + by
    case ("youtube", _): return "Newest \(s.count ?? 10) videos, kept fresh" + by
    case ("request", _): return "Movie · sent when it reaches Plex" + by
    default: return (s.ref.hasPrefix("yt-") ? "YouTube video" : "Movie · \(size)") + by
    }
}

// MARK: - Add to iPad (who, what, how much)

struct AddToIPadView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    /// Set when opened from a title; nil starts from "what do you want to add?".
    let target: SendTarget?
    var kids: [SendsOverview.Kid] = []

    @State private var people: [SendsOverview.P] = []
    @State private var who: Set<String> = []
    @State private var what: SendTarget.Kind = .show
    @State private var picked: SendTarget?
    @State private var scope = "next"
    @State private var count = 10
    @State private var season: Int?
    @State private var quality = "high"
    @State private var choices: Choices?
    @State private var sending = false

    struct Choices: Codable {
        struct Season: Codable, Hashable { var season: Int; var episodes: Int }
        var episodes: Int?
        var seasons: [Season]?
        var videos: Int?
        var bytesPerEpisode: [String: Int64?]?
    }

    private var current: SendTarget? { target ?? picked }

    var body: some View {
        NavigationStack {
            Form {
                Section("Who's it for?") {
                    if people.isEmpty { Text("No kids set up yet.").foregroundStyle(Theme.dim) }
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 10) {
                            ForEach(people, id: \.id) { p in
                                let on = who.contains(p.id)
                                Button {
                                    if on { who.remove(p.id) } else { who.insert(p.id) }
                                } label: {
                                    VStack(spacing: 6) {
                                        Avatar(name: p.name, color: p.uiStyle == "little" ? Theme.little : Theme.kid, size: 44)
                                        Text(p.name).font(.subheadline.weight(.semibold))
                                    }
                                    .padding(10).frame(minWidth: 84)
                                    .background(on ? Theme.accent.opacity(0.15) : Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                    .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(on ? Theme.accent : .clear, lineWidth: 2))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                }
                if target == nil {
                    Section("What do you want to add?") {
                        Picker("Kind", selection: $what) {
                            Label("Movie", systemImage: "film").tag(SendTarget.Kind.movie)
                            Label("TV show", systemImage: "tv").tag(SendTarget.Kind.show)
                            Label("YouTube", systemImage: "play.rectangle").tag(SendTarget.Kind.youtube)
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: what) { picked = nil; choices = nil }
                        NavigationLink {
                            SendPickerView(kind: what) { picked = $0 }
                        } label: {
                            HStack {
                                Text(picked?.title ?? pickPrompt).foregroundStyle(picked == nil ? Theme.accent : Theme.fg)
                                Spacer()
                            }
                        }
                    }
                } else if let t = target {
                    Section {
                        HStack(spacing: 12) {
                            HubImage(path: t.posterUrl) { Theme.card2 }
                                .frame(width: 40, height: 58).clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                            VStack(alignment: .leading) {
                                Text(t.title).font(.headline)
                                Text(kindLabel(t)).font(.caption).foregroundStyle(Theme.dim)
                            }
                        }
                    }
                }
                if let t = current { howMuch(t) }
                if let t = current, t.kind != .youtube {
                    Section {
                        Picker("Size", selection: $quality) {
                            Text("Good (720p)").tag("high")
                            Text("Small (480p)").tag("small")
                            Text("Original").tag("original")
                        }
                    } footer: {
                        Text("Good fits about three films in 5 GB. Small is half that, fine for a phone or a long trip.")
                    }
                }
                if let t = current, let est = estimate(t) {
                    Section {
                        LabeledContent("About", value: est)
                        if let low = lowSpace(t) { Text(low).font(.footnote).foregroundStyle(Theme.warn) }
                    } footer: {
                        Text("It downloads on its own on home Wi-Fi. Nobody has to tap anything on the iPad.")
                    }
                }
            }
            .navigationTitle(target == nil ? "Add to iPad" : "Send to iPad")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(sending ? "Sending…" : "Send") { Task { await send() } }
                        .disabled(sending || who.isEmpty || current == nil)
                }
            }
            .task { await loadPeople() }
            .task(id: current?.id) { await loadChoices() }
        }
    }

    private var pickPrompt: String {
        switch what {
        case .movie: "Choose a movie"
        case .show, .request: "Choose a show"
        case .youtube: "Choose a channel or video"
        }
    }

    private func kindLabel(_ t: SendTarget) -> String {
        switch t.kind {
        case .movie: "Movie"
        case .show: "TV show"
        case .youtube: t.ref.hasPrefix("yt-") ? "YouTube video" : "YouTube channel"
        case .request: t.isShowRequest ? "TV show · not on Plex yet" : "Movie · not on Plex yet"
        }
    }

    @ViewBuilder private func howMuch(_ t: SendTarget) -> some View {
        if t.showChoices {
            Section {
                Picker("Which episodes", selection: $scope) {
                    Text("The next ones they haven't seen").tag("next")
                    Text("The newest episodes").tag("last")
                    Text("One season").tag("season")
                    Text("All seasons").tag("all")
                }
                .pickerStyle(.inline).labelsHidden()
                if scope == "next" || scope == "last" {
                    Stepper("\(count) episodes", value: $count, in: 1...60)
                }
                if scope == "season" {
                    let seasons = choices?.seasons?.filter { $0.season > 0 } ?? []
                    Picker("Season", selection: $season) {
                        Text("Latest").tag(Int?.none)
                        ForEach(seasons, id: \.season) { s in Text("Season \(s.season) · \(s.episodes) episodes").tag(Int?.some(s.season)) }
                    }
                }
            } header: {
                Text("How much?")
            } footer: {
                Text(scope == "next" ? "Keeps that many ready. As they watch one, it's removed and the next one comes." :
                        scope == "last" ? "Follows the show: new episodes replace the oldest." : "Stays until you take it off.")
            }
        } else if t.kind == .youtube && !t.ref.hasPrefix("yt-") {
            Section {
                Stepper("Newest \(count) videos", value: $count, in: 1...50)
            } header: {
                Text("How much?")
            } footer: {
                Text("The hub keeps checking the channel. New videos replace the oldest, so it stays fresh.")
            }
        }
    }

    private func episodes(_ t: SendTarget) -> Int? {
        if t.kind == .youtube { return t.ref.hasPrefix("yt-") ? 1 : count }
        guard t.showChoices else { return 1 }
        switch scope {
        case "next", "last": return count
        case "season":
            let s = choices?.seasons?.filter { $0.season > 0 } ?? []
            return (season.flatMap { n in s.first { $0.season == n } } ?? s.last)?.episodes
        default: return choices?.episodes
        }
    }

    private func perItem(_ t: SendTarget) -> Int64? {
        guard let b = choices?.bytesPerEpisode else { return nil }
        let key = t.kind == .youtube ? "original" : quality
        return (b[key] ?? nil) ?? (b["small"] ?? nil) ?? (b["original"] ?? nil)
    }

    private func estimate(_ t: SendTarget) -> String? {
        guard let n = episodes(t) else { return nil }
        let what = t.showChoices ? "\(n) episode\(n == 1 ? "" : "s")" : t.kind == .youtube && !t.ref.hasPrefix("yt-") ? "\(n) videos" : "1 title"
        guard let each = perItem(t) else { return what }
        return "\(what) · \(byteSize(each * Int64(n))) on each iPad"
    }

    private func lowSpace(_ t: SendTarget) -> String? {
        guard let n = episodes(t), let each = perItem(t) else { return nil }
        let need = each * Int64(n)
        let short = kids.filter { who.contains($0.id) }.compactMap { k -> String? in
            let free = k.devices.compactMap(\.freeBytes).min()
            return free.map { $0 < need + 1_000_000_000 ? k.person.name : nil } ?? nil
        }
        return short.isEmpty ? nil : "\(short.joined(separator: " and "))'s iPad is low on space. Take something off first, or pick Small."
    }

    private func loadPeople() async {
        if !kids.isEmpty { people = kids.map(\.person) }
        else if let o: SendsOverview = try? await HubClient.shared.get("/v1/sends") { people = o.people.map(\.person) }
        if who.isEmpty { who = Set(people.filter { $0.role == "child" }.map(\.id)) }
    }

    private func loadChoices() async {
        choices = nil
        guard let t = current else { return }
        var q: [String: String] = [:]
        switch t.kind {
        case .show: q = ["kind": "show", "ref": t.ref]
        case .movie: q = ["kind": "title", "ref": t.ref]
        case .youtube: q = t.ref.hasPrefix("yt-") ? ["kind": "title", "ref": t.ref] : (t.ref.hasPrefix("yts_") ? [:] : ["kind": "youtube", "ref": t.ref])
        case .request: return
        }
        guard !q.isEmpty else { return }
        choices = try? await HubClient.shared.call("GET", "/v1/sends/choices", query: q)
    }

    private func send() async {
        guard let t = current else { return }
        sending = true
        defer { sending = false }
        var body: [String: Any] = ["person_ids": Array(who), "kind": t.apiKind, "ref": t.ref, "title": t.title, "quality": quality]
        if let p = t.posterUrl { body["poster_url"] = p }
        if t.showChoices {
            body["scope"] = scope
            if scope == "next" || scope == "last" { body["count"] = count }
            if scope == "season", let season { body["season"] = season }
        } else if t.kind == .youtube && !t.ref.hasPrefix("yt-") {
            body["count"] = count
        }
        do {
            try await HubClient.shared.send("/v1/sends", body)
            let names = people.filter { who.contains($0.id) }.map(\.name).joined(separator: " and ")
            store.show("Sending to \(names)", t.kind == .request ? "When it reaches Plex" : "It downloads by itself")
            dismiss()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

// MARK: - Picking a movie, show or YouTube channel

struct SendPickerView: View {
    @Environment(\.dismiss) private var dismiss
    let kind: SendTarget.Kind
    let pick: (SendTarget) -> Void
    @State private var query = ""
    @State private var library: [MediaItem] = []
    @State private var results: [MediaItem] = []
    @State private var requestable: [Findable] = []
    @State private var link = ""
    private let yt = YouTubeStore.shared

    var body: some View {
        List {
            if kind == .youtube {
                Section {
                    TextField("Paste a YouTube channel or playlist link", text: $link)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    if !link.isEmpty {
                        Button("Use this link") { choose(SendTarget(kind: .youtube, ref: link.trimmingCharacters(in: .whitespaces), title: "YouTube channel")) }
                    }
                } footer: { Text("Or share a channel from the YouTube app to Thomas Creek, then send it from here.") }
                let sources = (yt.data?.sources ?? []).filter { $0.kind != "video" }
                if !sources.isEmpty {
                    Section("Channels and playlists on the hub") {
                        ForEach(sources) { s in
                            Button { choose(SendTarget(kind: .youtube, ref: s.id, title: s.title, posterUrl: s.pictureUrl)) } label: {
                                InfoRow(s.symbol, color: .red, s.title, "\(s.kindLabel) · \(s.count) saved")
                            }
                        }
                    }
                }
                let videos = yt.ready.prefix(40)
                if !videos.isEmpty {
                    Section("Single videos") {
                        ForEach(Array(videos)) { v in
                            Button { choose(SendTarget(kind: .youtube, ref: v.id, title: v.title, posterUrl: v.posterUrl)) } label: {
                                InfoRow("play.rectangle.fill", color: .red, v.title, [v.channel, v.length].compactMap { $0 }.joined(separator: " · "))
                            }
                        }
                    }
                }
            } else {
                let shown = query.isEmpty ? library : results.filter { ($0.kind == "show") == (kind == .show) }
                Section(query.isEmpty ? (kind == .show ? "Shows on Plex" : "Movies on Plex") : "On Plex") {
                    ForEach(shown) { m in
                        Button { choose(SendTarget.from(m)) } label: { row(m.title, m.year.map(String.init), m.posterUrl, m.rating) }
                    }
                }
                let wantKind = kind == .show ? "tv" : "movie"
                let notYet = requestable.filter { $0.kind == wantKind }
                if !notYet.isEmpty {
                    Section {
                        ForEach(notYet) { f in
                            Button {
                                if let pid = f.plexId, f.availability == "ready" || f.availability == "partial" {
                                    choose(SendTarget(kind: kind == .show ? .show : .movie, ref: pid, title: f.title ?? "", posterUrl: f.posterUrl))
                                } else {
                                    choose(SendTarget(kind: .request, ref: f.mediaId, title: f.title ?? "", posterUrl: f.posterUrl, isShowRequest: f.kind == "tv"))
                                }
                            } label: { row(f.title ?? "", [f.year, f.availability.map(availability)].compactMap { $0 }.joined(separator: " · "), f.posterUrl, f.rating) }
                        }
                    } header: { Text("Not on Plex yet") } footer: {
                        Text("Request it first if it isn't on the way. It goes to the iPad when it arrives.")
                    }
                }
            }
        }
        .navigationTitle(kind == .youtube ? "YouTube" : kind == .show ? "Pick a show" : "Pick a movie")
        .searchable(text: $query, prompt: kind == .show ? "Shows" : kind == .movie ? "Movies" : "Search")
        .task {
            if kind == .youtube { if yt.data == nil { await yt.load() }; return }
            library = (try? await HubClient.shared.get("/v1/media/library", ["kind": kind == .show ? "show" : "movie"], as: MediaList.self))?.items ?? []
        }
        .task(id: query) {
            guard kind != .youtube, !query.isEmpty else { return }
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled else { return }
            if let l: WatchSearch = try? await HubClient.shared.get("/v1/media/search", ["q": query]) {
                results = l.items; requestable = l.requestable ?? []
            }
        }
    }

    private func availability(_ a: String) -> String {
        ["requested": "requested", "downloading": "downloading", "partial": "partly on Plex", "ready": "on Plex"][a] ?? a
    }

    private func row(_ title: String, _ sub: String?, _ poster: String?, _ rating: String?) -> some View {
        HStack(spacing: 12) {
            HubImage(path: poster) { Theme.card2 }
                .frame(width: 36, height: 52).clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                Text([sub, rating].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim)
            }
        }
    }

    private func choose(_ t: SendTarget) { pick(t); dismiss() }
}

// MARK: - Changing how much of a send goes

struct EditSendView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let send: SendsOverview.Send
    @State private var scope = "next"
    @State private var count = 10
    @State private var quality = "high"
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section { Text(describe(send)).font(.subheadline).foregroundStyle(Theme.dim) }
                if send.kind == "show" || (send.kind == "request" && send.scope != "one") {
                    Section("How much?") {
                        Picker("Which episodes", selection: $scope) {
                            Text("The next ones they haven't seen").tag("next")
                            Text("The newest episodes").tag("last")
                            Text("One season").tag("season")
                            Text("All seasons").tag("all")
                        }
                        .pickerStyle(.inline).labelsHidden()
                        if scope == "next" || scope == "last" { Stepper("\(count) episodes", value: $count, in: 1...60) }
                    }
                } else if send.kind == "youtube" {
                    Section("How much?") { Stepper("Newest \(count) videos", value: $count, in: 1...50) }
                }
                if !send.ref.hasPrefix("yt-") && send.kind != "youtube" {
                    Section {
                        Picker("Size", selection: $quality) {
                            Text("Good (720p)").tag("high"); Text("Small (480p)").tag("small"); Text("Original").tag("original")
                        }
                    }
                }
                if !send.items.isEmpty {
                    Section("On the iPad") {
                        ForEach(send.items) { i in
                            let states = Set(i.on.values)
                            HStack {
                                Text(i.title ?? i.itemId).font(.subheadline).lineLimit(1)
                                Spacer()
                                Image(systemName: states == ["ready"] ? "checkmark.circle.fill" : states.contains("downloading") || states.contains("preparing") ? "arrow.down.circle" : "circle.dotted")
                                    .foregroundStyle(states == ["ready"] ? Theme.accent : Theme.dim)
                            }
                        }
                    }
                }
                Section {
                    Button("Take it off the iPad", role: .destructive) { Task { await remove() } }
                }
            }
            .navigationTitle(send.title).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } }.disabled(saving) }
            }
            .onAppear { scope = send.scope; count = send.count ?? 10; quality = send.quality }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        var body: [String: Any] = ["quality": quality, "count": count]
        if send.kind == "show" || send.kind == "request" { body["scope"] = scope }
        do {
            let _: Empty = try await HubClient.shared.call("PATCH", "/v1/sends/\(send.id)", body: body)
            store.show("Saved", "The iPad catches up next time it checks")
            dismiss()
        } catch { store.show(error.localizedDescription, isError: true) }
    }

    private func remove() async {
        do {
            let _: Empty = try await HubClient.shared.call("DELETE", "/v1/sends/\(send.id)")
            store.show("Taking \(send.title) off")
            dismiss()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

// MARK: - The kid's side: "From Mom & Dad"

/// What grown-ups sent, ready to play with no connection. Picture-first for the little one.
struct SentShelf: View {
    @Environment(HomeStore.self) private var store
    var tint: Color = Theme.kid
    @State private var playing: Downloads.Title?
    private let downloads = Downloads.shared

    var body: some View {
        let sent = downloads.mine.filter(\.sent)
        let ready = sent.filter { $0.state == .ready }.sorted { ($0.watchedAt != nil ? 1 : 0, $0.createdAt) < ($1.watchedAt != nil ? 1 : 0, $1.createdAt) }
        let coming = sent.filter { $0.state == .preparing || $0.state == .downloading }
        if !ready.isEmpty || !coming.isEmpty {
            let little = store.me?.style == .little
            VStack(alignment: .leading, spacing: 10) {
                Text(fromLine(sent)).font(Theme.kidFont(little ? 24 : 20)).padding(.top, 6)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 12) {
                        ForEach(ready) { t in
                            Button { playing = t } label: { tile(t, little: little) }.buttonStyle(.plain)
                        }
                        ForEach(coming) { t in tile(t, little: little).opacity(0.55) }
                    }
                }
                .scrollClipDisabled()
            }
            .fullScreenCover(item: $playing) { OfflinePlayer(title: $0) }
        }
    }

    private func fromLine(_ sent: [Downloads.Title]) -> String {
        let names = Array(Set(sent.compactMap(\.sentBy))).sorted()
        return names.isEmpty ? "Sent for you" : "From \(names.joined(separator: " & "))"
    }

    private func tile(_ t: Downloads.Title, little: Bool) -> some View {
        let w: CGFloat = little ? 150 : 120
        return VStack(alignment: .leading, spacing: 6) {
            ZStack(alignment: .topLeading) {
                Group {
                    if let img = UIImage(contentsOfFile: downloads.posterURL(t).path) {
                        Image(uiImage: img).resizable().aspectRatio(contentMode: .fill)
                    } else {
                        LinearGradient(colors: [tint, tint.opacity(0.6)], startPoint: .top, endPoint: .bottom)
                            .overlay(Text(t.show ?? t.title).font(Theme.kidFont(16)).foregroundStyle(.white).multilineTextAlignment(.center).padding(8))
                    }
                }
                .frame(width: w, height: w * 1.45).clipped()
                .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                if t.state == .ready && t.watchedAt == nil && t.positionS < 30 {
                    Text("NEW").font(.caption2.weight(.heavy)).padding(.horizontal, 7).padding(.vertical, 3)
                        .background(.white, in: Capsule()).foregroundStyle(.black).padding(8)
                }
                if t.state != .ready {
                    ProgressRing(value: t.progress, lineWidth: 4, color: .white).frame(width: 26, height: 26).padding(8)
                        .frame(maxWidth: .infinity, alignment: .topTrailing)
                }
            }
            Text(caption(t)).font(Theme.kidFont(13, .semibold)).foregroundStyle(Theme.dim).lineLimit(1).frame(width: w, alignment: .leading)
        }
    }

    private func caption(_ t: Downloads.Title) -> String {
        if t.state != .ready { return "Coming…" }
        if let s = t.season, let e = t.episode { return "Season \(s) · Episode \(e)" }
        if t.kind == "youtube" { return "YouTube" }
        return t.title
    }
}
