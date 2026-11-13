import SwiftUI

// MARK: - Models (hub: /v1/media/requests, /v1/media/discover, /v1/media/title)

struct SeasonState: Codable, Hashable, Identifiable {
    var n: Int
    var status: String?
    var name: String?
    var episodes: Int?
    var id: Int { n }
}

/// A request as Jellyseerr knows it, wherever it was made (app, website, Plex watchlist).
struct TitleRequest: Codable, Hashable, Identifiable {
    var id: String
    var mediaId: String?
    var kind: String?
    var title: String
    var year: String?
    var posterUrl: String?
    var who: String?
    var mine: Bool?
    var via: String?
    var createdAt: String?
    var status: String
    var statusText: String?
    var detail: String?
    var progress: Double?
    var etaS: Int?
    var seasons: [SeasonState]?
    var plexId: String?
    var canCancel: Bool?
    var canRetry: Bool?

    var date: Date? { createdAt.flatMap { ISO8601DateFormatter.flexible.date(from: $0) } }
    var active: Bool { ["waiting", "searching", "downloading", "partial", "failed"].contains(status) }
}

struct TitleRequestList: Codable { var items: [TitleRequest]; var counts: [String: Int]? }

/// Something that can be asked for, with what's already happening to it.
struct Findable: Codable, Hashable, Identifiable {
    var mediaId: String
    var kind: String
    var title: String?
    var year: String?
    var posterUrl: String?
    var summary: String?
    var availability: String?
    var requestId: String?
    var plexId: String?
    var rating: String?
    var id: String { mediaId }
}

struct FindableList: Codable { var items: [Findable] }

struct TitleInfo: Codable, Hashable {
    var mediaId: String
    var kind: String
    var title: String?
    var year: String?
    var posterUrl: String?
    var summary: String?
    var rating: String?
    var runtimeMin: Int?
    var genres: [String]?
    var seasons: [SeasonState]?
    var availability: String?
    var requestId: String?
    var plexId: String?
}

extension ISO8601DateFormatter {
    static let flexible: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
}

// MARK: - Status chip

enum ReqStatus {
    static func color(_ s: String?) -> Color {
        switch s {
        case "waiting": Theme.warn
        case "searching", "requested": Theme.kid
        case "downloading", "partial": Theme.accent
        case "ready": Theme.accent
        case "failed", "declined": Theme.crit
        default: Theme.dim
        }
    }
    static func short(_ s: String?, progress: Double? = nil) -> String {
        switch s {
        case "waiting": "Waiting"
        case "searching": "Searching"
        case "requested": "Requested"
        case "downloading": progress.map { "\(Int(($0 * 100).rounded()))%" } ?? "Downloading"
        case "partial": "Partly ready"
        case "ready": "Ready"
        case "failed": "Couldn't add"
        case "declined": "Declined"
        case "removed": "Removed"
        default: (s ?? "").capitalized
        }
    }
    static func eta(_ s: Int?) -> String? {
        guard let s, s > 0 else { return nil }
        if s < 3600 { return "\(max(1, s / 60)) min left" }
        if s < 86400 { return "\(s / 3600) h \((s % 3600) / 60) min left" }
        return "\(s / 86400) days left"
    }
}

struct StatusChip: View {
    let status: String?
    var progress: Double? = nil
    var body: some View {
        let c = ReqStatus.color(status)
        Text(ReqStatus.short(status, progress: progress))
            .font(.caption2.weight(.bold)).padding(.horizontal, 8).padding(.vertical, 3)
            .foregroundStyle(status == "ready" ? Theme.onAccent : c)
            .background(status == "ready" ? c : c.opacity(0.16), in: Capsule())
    }
}

struct PosterArt: View {
    let url: String?
    let title: String?
    var radius: CGFloat = 10
    var body: some View {
        HubImage(path: url) {
            LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom)
                .overlay(Text(title ?? "").font(.caption2.weight(.semibold)).multilineTextAlignment(.center).padding(4))
        }
        .aspectRatio(2 / 3, contentMode: .fill)
        .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
    }
}

// MARK: - Requests list

/// Every movie and show request (from the app, the Jellyseerr site or a Plex watchlist), with live status.
/// Grown-ups also see other kinds of asks (internet time, rewards) that still need a yes.
struct RequestsView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var items: [TitleRequest]?
    @State private var asks: [MediaRequest] = []
    @State private var failed: String?
    @AppStorage("requests.scope") private var scope = "mine"
    @AppStorage("requests.filter") private var filter = "active"

    private var grownUp: Bool { store.role == .admin || store.role == .adult }

    var body: some View {
        List {
            if grownUp {
                Section {
                    Picker("Whose", selection: $scope) {
                        Text("Mine").tag("mine")
                        Text("Everyone").tag("all")
                    }
                    .pickerStyle(.segmented)
                }
                .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
            }
            Section {
                HStack(spacing: 8) {
                    ForEach([("active", "Active"), ("ready", "Ready"), ("all", "All")], id: \.0) { k, v in
                        ChipButton(title: v, active: filter == k) { filter = k }
                    }
                }
            }
            .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
            let waiting = asks.filter(\.waiting)
            if !waiting.isEmpty && store.me?.can(.mediaApprove) == true {
                Section("Waiting for you") { ForEach(waiting) { askRow($0) } }
            }
            if let items {
                let shown = items.filter { filter == "all" || (filter == "ready" ? ["ready", "partial"].contains($0.status) : $0.active) }
                Section {
                    ForEach(shown) { r in
                        Button { router.go(.mediaRequest(r.id)) } label: { RequestRow(r: r, showWho: scope == "all" || r.mine != true) }
                            .buttonStyle(.plain)
                    }
                    if shown.isEmpty {
                        VStack(spacing: 6) {
                            Text(filter == "ready" ? "Nothing ready yet" : filter == "active" ? "Nothing on the way" : "No requests yet").font(.headline)
                            Text("Tap + to ask for a movie or show.").font(.footnote).foregroundStyle(Theme.dim)
                        }
                        .frame(maxWidth: .infinity).padding(.vertical, 16)
                    }
                } footer: {
                    Text("Includes requests made on the Jellyseerr site and from Plex watchlists.")
                }
            } else if let failed {
                ContentUnavailableView("Couldn't load requests", systemImage: "tray", description: Text(failed))
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Requests")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { router.go(.askMedia) } label: { Image(systemName: "plus") }.accessibilityLabel("Ask for something")
            }
        }
        .refreshable { await load() }
        .task(id: scope) { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(10)) } }
    }

    private func load() async {
        do {
            let l: TitleRequestList = try await HubClient.shared.get("/v1/media/requests", ["scope": grownUp ? scope : "mine"])
            items = l.items; failed = nil
        } catch { if items == nil { failed = error.localizedDescription } }
        if store.me?.can(.mediaApprove) == true, let l: RequestList = try? await HubClient.shared.get("/v1/requests") {
            asks = l.items.filter { $0.kind != "media" }
        }
    }

    private func askRow(_ r: MediaRequest) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(r.title).font(.subheadline.weight(.semibold))
                Text(r.who ?? "").font(.caption).foregroundStyle(Theme.dim)
            }
            Spacer()
            Button { decide(r, "deny") } label: { Image(systemName: "xmark") }.buttonStyle(.bordered).tint(Theme.dim)
            Button { decide(r, "approve") } label: { Image(systemName: "checkmark") }.buttonStyle(.borderedProminent)
        }
    }

    private func decide(_ r: MediaRequest, _ verb: String) {
        Task {
            do {
                try await HubClient.shared.send("/v1/requests/\(r.id)/\(verb)")
                store.show(verb == "approve" ? "Approved \(r.title)" : "Declined")
                await load()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

struct RequestRow: View {
    let r: TitleRequest
    var showWho = true
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            PosterArt(url: r.posterUrl, title: r.title, radius: 7).frame(width: 46)
            VStack(alignment: .leading, spacing: 4) {
                Text(r.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                Text([showWho ? r.who : nil, r.date?.formatted(.relative(presentation: .named))].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.dim)
                HStack(spacing: 6) {
                    StatusChip(status: r.status, progress: r.progress)
                    if let d = r.detail, r.status != "ready" { Text(d).font(.caption).foregroundStyle(Theme.dim).lineLimit(1) }
                }
                if r.status == "downloading", let p = r.progress {
                    ProgressBar(value: p)
                    if let e = ReqStatus.eta(r.etaS) { Text(e).font(.caption2).foregroundStyle(Theme.dim) }
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim).padding(.top, 4)
        }
        .contentShape(Rectangle())
    }
}

// MARK: - Ask for something

struct AskMediaView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var initialQuery = ""
    @State private var query = ""
    @State private var results: [Findable]?
    @State private var failed: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                SectionHeader(query.isEmpty ? "Popular right now" : "Results")
                if let results {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 12)], spacing: 14) {
                        ForEach(results) { f in
                            Button { router.go(.mediaTitle(f.mediaId)) } label: { FindableTile(f: f) }.buttonStyle(.plain)
                        }
                    }
                    if results.isEmpty {
                        EmptyCard(symbol: "magnifyingglass", title: "No matches", detail: "Try another spelling or the year.")
                    }
                    Text("Tags show what's already on Plex or already asked for.").font(.footnote).foregroundStyle(Theme.dim)
                } else if let failed {
                    EmptyCard(symbol: "wifi.slash", title: "Couldn't search", detail: failed)
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Ask for something")
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Movies and shows")
        .onAppear { if query.isEmpty && !initialQuery.isEmpty { query = initialQuery } }
        .task(id: query) {
            if !query.isEmpty { try? await Task.sleep(for: .milliseconds(350)) }
            guard !Task.isCancelled else { return }
            do {
                let l: FindableList = try await HubClient.shared.get("/v1/media/discover", query.isEmpty ? [:] : ["q": query])
                results = l.items; failed = nil
            } catch { failed = error.localizedDescription }
        }
    }
}

struct FindableTile: View {
    let f: Findable
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            PosterArt(url: f.posterUrl, title: f.title)
                .overlay(alignment: .topTrailing) {
                    if let a = f.availability { StatusChip(status: a == "ready" ? "ready" : a).padding(5) }
                }
            Text(f.title ?? "").font(.caption.weight(.semibold)).lineLimit(1)
            Text([f.year, f.kind == "tv" ? "Show" : "Movie"].compactMap { $0 }.joined(separator: " · "))
                .font(.caption2).foregroundStyle(Theme.dim)
        }
    }
}

// MARK: - A title: request it, follow it, or play it

struct MediaTitleView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let mediaId: String
    @State private var info: TitleInfo?
    @State private var failed: String?
    @State private var picked: Set<Int> = []
    @State private var sending = false
    @State private var playing = false
    @State private var sendTo: SendTarget?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let t = info {
                    HStack(alignment: .top, spacing: 14) {
                        PosterArt(url: t.posterUrl, title: t.title).frame(width: 116)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(t.title ?? "").font(.title2.weight(.bold))
                            Text(facts(t)).font(.footnote).foregroundStyle(Theme.dim)
                            if let a = t.availability { StatusChip(status: a) }
                        }
                    }
                    if let s = t.summary, !s.isEmpty { Text(s).font(.subheadline) }
                    actions(t)
                    if store.me?.role == .admin || store.me?.role == .adult {
                        Button { sendTo = sendTarget(t) } label: {
                            Label(t.plexId != nil && (t.availability == "ready" || t.availability == "partial") ? "Send to kids' iPads" : "Send to kids' iPads when it arrives",
                                  systemImage: "ipad.and.arrow.forward").font(.headline).frame(maxWidth: .infinity, minHeight: 46)
                        }
                        .buttonStyle(.bordered)
                    }
                } else if let failed {
                    EmptyCard(symbol: "film", title: "Couldn't open this", detail: failed)
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(info?.title ?? "").navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .sheet(isPresented: $playing) {
            if let t = info, let item = plexItem(t) { PlayOnSheet(item: item) }
        }
        .sheet(item: $sendTo) { AddToIPadView(target: $0) }
    }

    private func sendTarget(_ t: TitleInfo) -> SendTarget {
        if let pid = t.plexId, t.availability == "ready" || t.availability == "partial" {
            return SendTarget(kind: t.kind == "tv" ? .show : .movie, ref: pid, title: t.title ?? "", posterUrl: t.posterUrl)
        }
        return SendTarget(kind: .request, ref: t.mediaId, title: t.title ?? "", posterUrl: t.posterUrl, isShowRequest: t.kind == "tv")
    }

    private func facts(_ t: TitleInfo) -> String {
        var bits = [t.year, t.kind == "tv" ? "Show" : "Movie", t.rating].compactMap { $0 }
        if t.kind == "tv", let n = t.seasons?.count { bits.insert("\(n) season\(n == 1 ? "" : "s")", at: 2) }
        if t.kind != "tv", let r = t.runtimeMin { bits.append("\(r / 60) h \(r % 60) min") }
        return bits.joined(separator: " · ")
    }

    private func plexItem(_ t: TitleInfo) -> MediaItem? {
        t.plexId.map { MediaItem(id: $0, title: t.title ?? "", year: t.year.flatMap(Int.init), kind: t.kind == "tv" ? "show" : "movie",
                                 posterUrl: t.posterUrl, rating: t.rating, summary: t.summary) }
    }

    @ViewBuilder private func actions(_ t: TitleInfo) -> some View {
        let missing = (t.seasons ?? []).filter { $0.status != "ready" }
        if t.plexId != nil, t.availability == "ready" || t.availability == "partial" {
            PrimaryButton(title: "Play on a TV", symbol: "play.fill") { playing = true }
        }
        if let rid = t.requestId, t.availability != nil, t.availability != "ready" {
            Button { router.go(.mediaRequest(rid)) } label: {
                Label("See the request", systemImage: "tray.full").font(.headline).frame(maxWidth: .infinity, minHeight: 46)
            }
            .buttonStyle(.bordered)
        }
        if t.availability == nil || (t.kind == "tv" && !missing.isEmpty && t.availability != "requested" && t.availability != "downloading") {
            if t.kind == "tv", !missing.isEmpty {
                SectionHeader("Seasons")
                VStack(spacing: 0) {
                    ForEach(t.seasons ?? []) { s in
                        let have = s.status == "ready"
                        Button {
                            if !have { if picked.contains(s.n) { picked.remove(s.n) } else { picked.insert(s.n) } }
                        } label: {
                            HStack {
                                Image(systemName: have ? "checkmark.circle.fill" : picked.contains(s.n) ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(have ? Theme.dim : Theme.accent)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(s.name ?? "Season \(s.n)").font(.subheadline.weight(.medium))
                                    if let e = s.episodes { Text("\(e) episodes").font(.caption).foregroundStyle(Theme.dim) }
                                }
                                Spacer()
                                if let st = s.status { StatusChip(status: st) }
                            }
                            .padding(.vertical, 10).padding(.horizontal, 14).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain).disabled(have)
                        Divider().padding(.leading, 14)
                    }
                }
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            PrimaryButton(title: sending ? "Sending…" : "Request it", symbol: "plus") { Task { await ask(t) } }
                .disabled(sending || (t.kind == "tv" && picked.isEmpty && !missing.isEmpty))
            Text("It goes straight to the download queue. You'll get a notification when it's ready to watch.")
                .font(.footnote).foregroundStyle(Theme.dim)
        }
    }

    private func load() async {
        do {
            let t: TitleInfo = try await HubClient.shared.get("/v1/media/title/\(mediaId)")
            info = t; failed = nil
            picked = Set((t.seasons ?? []).filter { $0.status == nil }.map(\.n))
        } catch { failed = error.localizedDescription }
    }

    private func ask(_ t: TitleInfo) async {
        sending = true
        defer { sending = false }
        var body: [String: Any] = ["media_id": t.mediaId]
        if t.kind == "tv" { body["seasons"] = picked.sorted() }
        do {
            let r: TitleRequest = try await HubClient.shared.post("/v1/media/requests", body)
            store.show("Requested \(t.title ?? "")", "You'll get a notification when it's ready")
            router.go(.mediaRequest(r.id))
            await load()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

// MARK: - One request: timeline, seasons, actions

struct MediaRequestView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss
    let requestID: String
    @State private var r: TitleRequest?
    @State private var failed: String?
    @State private var playing = false
    @State private var confirmCancel = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let r {
                    HStack(alignment: .top, spacing: 14) {
                        PosterArt(url: r.posterUrl, title: r.title).frame(width: 104)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(r.title).font(.title2.weight(.bold))
                            Text([r.year, r.kind == "tv" ? "Show" : "Movie"].compactMap { $0 }.joined(separator: " · "))
                                .font(.footnote).foregroundStyle(Theme.dim)
                            StatusChip(status: r.status, progress: r.progress)
                            if let d = r.detail { Text(d).font(.footnote).foregroundStyle(Theme.dim) }
                        }
                    }
                    if r.status == "downloading", let p = r.progress {
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text("\(Int((p * 100).rounded()))% downloaded").font(.subheadline.weight(.semibold))
                                Spacer()
                                if let e = ReqStatus.eta(r.etaS) { Text(e).font(.caption).foregroundStyle(Theme.dim) }
                            }
                            ProgressBar(value: p)
                        }
                        .card(padding: 14)
                    }
                    SectionHeader("Progress")
                    Timeline(r: r).card(padding: 14)
                    if let seasons = r.seasons, !seasons.isEmpty {
                        SectionHeader("Seasons")
                        RowsCard {
                            ForEach(seasons) { s in
                                HStack { Text("Season \(s.n)").font(.subheadline); Spacer(); StatusChip(status: s.status) }
                                    .padding(.vertical, 6)
                            }
                        }
                    }
                    VStack(spacing: 10) {
                        if r.plexId != nil, r.status == "ready" || r.status == "partial" {
                            PrimaryButton(title: "Play on a TV", symbol: "play.fill") { playing = true }
                        }
                        if r.canRetry == true {
                            Button { Task { await retry() } } label: {
                                Label("Search again", systemImage: "arrow.clockwise").frame(maxWidth: .infinity, minHeight: 44)
                            }
                            .buttonStyle(.bordered)
                        }
                        if r.canCancel == true {
                            Button(role: .destructive) { confirmCancel = true } label: {
                                Text("Cancel request").frame(maxWidth: .infinity, minHeight: 44)
                            }
                            .buttonStyle(.bordered)
                        }
                    }
                    .padding(.top, 4)
                } else if let failed {
                    EmptyCard(symbol: "tray", title: "Couldn't load this request", detail: failed)
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Request").navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(8)) } }
        .sheet(isPresented: $playing) {
            if let r, let id = r.plexId {
                PlayOnSheet(item: MediaItem(id: id, title: r.title, year: r.year.flatMap(Int.init), kind: r.kind == "tv" ? "show" : "movie",
                                            posterUrl: r.posterUrl))
            }
        }
        .confirmationDialog("Cancel this request?", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("Cancel request", role: .destructive) { Task { await cancel() } }
            Button("Keep it", role: .cancel) {}
        }
    }

    private func load() async {
        do { r = try await HubClient.shared.get("/v1/media/requests/\(requestID)"); failed = nil }
        catch { if r == nil { failed = error.localizedDescription } }
    }

    private func cancel() async {
        do {
            let _: Empty = try await HubClient.shared.call("DELETE", "/v1/media/requests/\(requestID)")
            store.show("Request cancelled")
            dismiss()
        } catch { store.show(error.localizedDescription, isError: true) }
    }

    private func retry() async {
        do {
            try await HubClient.shared.send("/v1/media/requests/\(requestID)/retry")
            store.show("Searching again")
            await load()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

private struct Timeline: View {
    let r: TitleRequest
    var body: some View {
        let order = ["asked", "searching", "downloading", "ready"]
        let now: String = switch r.status {
        case "waiting": "asked"
        case "searching", "failed": "searching"
        case "downloading": "downloading"
        case "ready", "partial", "removed": "ready"
        default: "asked"
        }
        let idx = order.firstIndex(of: now) ?? 0
        let finished = r.status == "ready"
        VStack(alignment: .leading, spacing: 0) {
            step(0, idx, finished, "Asked for",
                 [r.who, r.date?.formatted(date: .abbreviated, time: .shortened), r.via.map { "via \($0)" }].compactMap { $0 }.joined(separator: " · "))
            step(1, idx, finished, r.status == "failed" ? "Couldn't be added" : "Looking for a copy",
                 r.status == "failed" ? "Sonarr or Radarr refused it" : "Sonarr and Radarr search for it")
            step(2, idx, finished, "Downloading", r.status == "downloading" ? (ReqStatus.eta(r.etaS) ?? "In progress") : "")
            step(3, idx, finished, r.status == "partial" ? "Partly on Plex" : "Ready to watch", "On Plex, with a notification", last: true)
        }
    }

    private func step(_ i: Int, _ idx: Int, _ finished: Bool, _ title: String, _ sub: String, last: Bool = false) -> some View {
        let done = i < idx || (i == idx && finished)
        let current = i == idx && !finished
        return HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 0) {
                Circle()
                    .strokeBorder(done || current ? Theme.accent : Theme.line, lineWidth: current ? 5 : 2)
                    .background(Circle().fill(done ? Theme.accent : Theme.card))
                    .frame(width: 18, height: 18)
                if !last { Rectangle().fill(Theme.line).frame(width: 2).frame(maxHeight: .infinity) }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(done || current ? .semibold : .regular))
                    .foregroundStyle(done || current ? Theme.fg : Theme.dim)
                if !sub.isEmpty { Text(sub).font(.caption).foregroundStyle(Theme.dim) }
            }
            .padding(.bottom, last ? 0 : 14)
            Spacer(minLength: 0)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}
