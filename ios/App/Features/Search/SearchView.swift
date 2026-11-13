import SwiftUI

/// One search for the whole house: rooms, lights, scenes, doors, TVs, cameras and clips, films and the kids'
/// YouTube, recipes, chores, calendar, trips, people, alerts, the inventory and every screen.
/// Rooms, devices, cameras and people answer at once from the phone; the hub adds the rest (GET /v1/search).
struct SearchView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var results: SearchResults?
    @State private var local: [SearchGroup] = []
    @State private var loading = false
    @State private var failed = false
    @State private var filter = "all"
    @State private var media: MediaItem?
    @State private var recent: [String] = SearchView.loadRecent()
    @FocusState private var focused: Bool

    private var little: Bool { store.me?.style == .little }
    private var kid: Bool { store.role == .child }
    private var tint: Color { kid ? (little ? Theme.little : Theme.kid) : Theme.accent }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if query.trimmingCharacters(in: .whitespaces).isEmpty { idle } else { found }
                }
                .padding(.top, 6).screen()
            }
            .scrollDismissesKeyboard(.immediately)
            .background(Backdrop())
            .navigationTitle(little ? "Find" : "Search")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: little ? "Find…" : kid ? "Search your things" : "Search Thomas Creek")
            .searchFocused($focused)
            .onSubmit(of: .search) { remember(query) }
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            .task(id: query) { await search() }
            .onAppear {
                if let q = router.searchQuery { query = q; router.searchQuery = nil }
                focused = query.isEmpty
            }
        }
        .tint(tint)
        .sheet(item: $media) { m in
            if kid { KidPlaySheet(item: m) } else { PlayOnSheet(item: m) }
        }
    }

    // MARK: - Empty box: recent searches and shortcuts

    @ViewBuilder private var idle: some View {
        if !recent.isEmpty {
            SectionHeader(title: "Recent") { MoreButton("Clear") { recent = []; Self.saveRecent([]) } }
            FlowChips(items: recent) { query = $0 }
        }
        SectionHeader(little ? "Try" : "Look for")
        FlowChips(items: suggestions) { query = $0 }
        if !kid {
            Text("Rooms, lights, scenes, doors, TVs, cameras and clips, films, recipes, chores, the calendar, trips, people, alerts, the inventory and settings.")
                .font(.footnote).foregroundStyle(Theme.dim).padding(.top, 8).padding(.horizontal, 4)
        }
    }

    private var suggestions: [String] {
        if little { return ["Bluey", "my lamp", "my TV", "jobs"] }
        if kid { return ["my room", "chores", "movies", "trips"] }
        var s = ["Kitchen", "Lights off", "Front door", "Garage", "Dinner", "Trips", "VPN"]
        if store.me?.can(.cameras) != true { s.removeAll { $0 == "Front door" || $0 == "Garage" } }
        return s
    }

    // MARK: - Results

    @ViewBuilder private var found: some View {
        let groups = shownGroups
        if CatDirector.shared.isOn && catWords.contains(query.trimmingCharacters(in: .whitespaces).lowercased()) { catsRow }
        if results?.ask == true && looksLikeQuestion { askRow }
        if let cmd = results?.command { commandRow(cmd) }
        if !little && groups.count > 1 { filterBar(groups) }
        if little {
            littleGrid(groups.flatMap(\.items))
        } else {
            if filter == "all", let top = topHit(groups) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Top hit").font(.caption.weight(.bold)).textCase(.uppercase).foregroundStyle(tint)
                    SearchRow(item: top, run: { run(top) }, open: { open(top) })
                }
                .padding(12)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).stroke(tint.opacity(0.5), lineWidth: 1.5))
            }
            ForEach(groups.filter { filter == "all" || $0.type == filter }) { g in
                let items = g.items.filter { $0.id != (filter == "all" ? topHit(groups)?.id : nil) }
                if !items.isEmpty {
                    SectionHeader(g.title)
                    RowsCard {
                        ForEach(filter == "all" ? Array(items.prefix(4)) : items) { item in
                            SearchRow(item: item, run: { run(item) }, open: { open(item) })
                        }
                    }
                    if filter == "all" && items.count > 4 {
                        Button("See all \(g.total ?? items.count)") { withAnimation { filter = g.type } }
                            .font(.subheadline.weight(.medium)).foregroundStyle(tint).padding(.horizontal, 4)
                    }
                }
            }
        }
        if loading {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(kid ? "Looking…" : "Looking in films, recipes, calendar, trips and alerts…")
            }
            .font(.footnote).foregroundStyle(Theme.dim).padding(.horizontal, 4).padding(.top, 4)
        } else if failed && results == nil {
            Label("Can't reach home, so only rooms, devices and cameras are searched.", systemImage: "wifi.slash")
                .font(.footnote).foregroundStyle(Theme.dim).padding(.horizontal, 4)
        }
        if !loading && groups.isEmpty && results?.command == nil {
            EmptyCard(symbol: "magnifyingglass", title: little ? "Hmm, not here" : "Nothing for \u{201C}\(query)\u{201D}",
                      detail: kid ? "Try a show, a chore or your room." : "Try a room, a camera, a film, a recipe or a setting.")
            if results?.ask == true { askRow }
        }
    }

    /// Hub results when they're in, otherwise what the phone found on its own.
    private var shownGroups: [SearchGroup] {
        guard let r = results else { return local }
        // Keep instant results for types the hub hasn't answered yet.
        let have = Set(r.groups.map(\.type))
        return r.groups + local.filter { !have.contains($0.type) && (r.pending ?? []).contains($0.type) }
    }

    private func topHit(_ groups: [SearchGroup]) -> SearchItem? {
        if let t = results?.top { return t }
        let all = groups.flatMap(\.items).sorted { ($0.score ?? 0) > ($1.score ?? 0) }
        guard let first = all.first, (first.score ?? 0) >= 6, all.count == 1 || (first.score ?? 0) > (all[1].score ?? 0) else { return nil }
        return first
    }

    // Search for the cats and they turn up.
    private var catWords: Set<String> { ["cat", "cats", "kitty", "kitten", "meow", "kiki", "bri", "kiki and bri"] }

    private var catsRow: some View {
        HStack(alignment: .bottom, spacing: 4) {
            LivingCat(kind: .kiki, pose: .sit)
            LivingCat(kind: .bri, pose: .sit)
            VStack(alignment: .leading, spacing: 2) {
                Text("Kiki and Bri").font(.headline)
                Text(query.lowercased().contains("bri") ? "Bri is probably hunting something." : "Kiki would like to know if you brought food.")
                    .font(.footnote).foregroundStyle(Theme.dim)
            }
            .padding(.bottom, 8)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(12)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .onTapGesture { UIImpactFeedbackGenerator(style: .soft).impactOccurred() }
        .accessibilityElement(children: .combine)
    }

    private var looksLikeQuestion: Bool {
        let w = query.lowercased().split(separator: " ")
        return w.count >= 5 || query.hasSuffix("?") || (w.count >= 2 && ["is", "are", "what", "when", "where", "who", "how", "did", "can", "why"].contains(String(w[0])))
    }

    private func filterBar(_ groups: [SearchGroup]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ChipButton(title: "All", active: filter == "all") { filter = "all" }
                ForEach(groups) { g in
                    ChipButton(title: "\(Self.short(g.type)) \(g.total ?? g.items.count)", active: filter == g.type) { filter = g.type }
                }
            }
        }
        .scrollClipDisabled()
    }

    private var askRow: some View {
        Button {
            remember(query)
            router.ask(query)
        } label: {
            HStack(spacing: 12) {
                IconBadge(symbol: "sparkles", color: tint, size: 38)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Ask Thomas Creek").font(.subheadline.weight(.semibold))
                    Text("\u{201C}\(query)\u{201D}").font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                }
                Spacer()
                Image(systemName: "arrow.up.circle.fill").font(.title2).foregroundStyle(tint)
            }
            .card(padding: 12, color: tint.opacity(0.12))
        }
        .buttonStyle(.plain)
    }

    private func commandRow(_ c: SearchCommand) -> some View {
        HStack(spacing: 12) {
            IconBadge(symbol: "bolt.fill", color: tint, size: 38)
            VStack(alignment: .leading, spacing: 1) {
                Text(c.title).font(.subheadline.weight(.semibold))
                if let s = c.subtitle { Text(s).font(.caption).foregroundStyle(Theme.dim) }
            }
            Spacer()
            if let a = c.action {
                Button(a.label ?? "Go") { perform(a, title: c.title) }
                    .buttonStyle(.borderedProminent).buttonBorderShape(.capsule).controlSize(.small)
            } else if let link = c.deepLink {
                Button("Open") { go(link) }.buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.small)
            }
        }
        .card(padding: 12, color: tint.opacity(0.12))
    }

    // Adeline: big pictures, one tap does the thing.
    private func littleGrid(_ items: [SearchItem]) -> some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
            ForEach(items.prefix(12)) { item in
                Button { item.action != nil || item.media != nil || item.chore != nil ? run(item) : open(item) } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        Group {
                            if let img = item.imageUrl {
                                HubImage(path: img) { Color.clear }.frame(height: 64).clipShape(RoundedRectangle(cornerRadius: 12))
                            } else {
                                Image(systemName: item.symbol ?? "star.fill").font(.system(size: 40, weight: .semibold))
                            }
                        }
                        Spacer(minLength: 0)
                        Text(item.title.replacingOccurrences(of: "Adelines ", with: "My ").replacingOccurrences(of: "Adeline's ", with: "My "))
                            .font(Theme.kidFont(18)).lineLimit(2).multilineTextAlignment(.leading)
                    }
                    .foregroundStyle(.white)
                    .padding(14).frame(maxWidth: .infinity, minHeight: 140, alignment: .topLeading)
                    .background(LinearGradient(colors: [Self.color(item.type), Self.color(item.type).opacity(0.7)], startPoint: .topLeading, endPoint: .bottomTrailing),
                                in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Doing things

    private func run(_ item: SearchItem) {
        remember(query)
        if let a = item.action {
            perform(a, title: item.title)
            if a.toggle == true { flip(item) }
            return
        }
        if let m = item.media { media = m; return }
        if let r = item.request {
            Task {
                do {
                    try await HubClient.shared.send("/v1/requests", ["kind": "media", "media_id": r.mediaId, "title": r.title,
                                                                    "poster_url": item.imageUrl ?? ""])
                    store.show("Requested", r.title)
                } catch { store.show(error.localizedDescription, isError: true) }
            }
            return
        }
        if let c = item.chore, !c.done, item.verb == "Done" {
            Task {
                do {
                    try await HubClient.shared.send("/v1/chores/\(c.id.string ?? "")/done", ["person": c.person])
                    store.show(little ? "Yay!" : "Nice! Done", item.title)
                    await search(force: true)
                } catch { store.show(error.localizedDescription, isError: true) }
            }
            return
        }
        open(item)
    }

    private func perform(_ a: SearchAction, title: String) {
        if a.action == "batch" {
            let steps = a.steps ?? []
            Task {
                var failures = 0
                for s in steps {
                    do { _ = try await HubClient.shared.action(s.action, target: s.target, params: s.body) } catch { failures += 1 }
                }
                store.show(failures == 0 ? "\(title): \(a.label?.lowercased() ?? "done")" : "\(failures) didn't respond",
                           isError: failures > 0)
                await store.loadHome()
                await search(force: true)
            }
            return
        }
        store.perform(a.action, target: a.target, params: a.body,
                      confirm: a.secure ? "\(a.label ?? "Change") \(title)" : nil,
                      optimistic: a.toggle == true ? { d in d.state["on"] = .bool(!(a.on ?? d.isOn)) } : nil,
                      done: a.toggle == true ? nil : "\(title): \(a.label?.lowercased() ?? "done")")
    }

    /// Show the light's new state right away; the next search reads the real one.
    private func flip(_ item: SearchItem) {
        guard var r = results else { return }
        func swap(_ i: SearchItem) -> SearchItem {
            guard i.id == item.id, var a = i.action, a.toggle == true else { return i }
            var n = i
            let now = !(a.on ?? false)
            a.on = now
            a.params?["on"] = .bool(!now)
            a.label = now ? "Turn off" : "Turn on"
            n.action = a
            if let s = n.subtitle {
                n.subtitle = s.replacingOccurrences(of: now ? "off" : "on", with: now ? "on" : "off")
            }
            return n
        }
        r.groups = r.groups.map { g in var g = g; g.items = g.items.map(swap); return g }
        r.top = r.top.map(swap)
        results = r
    }

    private func open(_ item: SearchItem) {
        remember(query)
        guard let link = item.deepLink else {
            if let m = item.media { media = m }
            return
        }
        go(link)
    }

    private func go(_ link: String) {
        guard let url = URL(string: link) else { return }
        dismiss()
        Task { try? await Task.sleep(for: .milliseconds(350)); router.open(url) }
    }

    // MARK: - Searching

    private func search(force: Bool = false) async {
        let q = query.trimmingCharacters(in: .whitespaces)
        filter = "all"
        guard !q.isEmpty else { results = nil; local = []; loading = false; return }
        local = LocalSearch.run(q, store: store)
        if !force {
            try? await Task.sleep(for: .milliseconds(220))
            guard !Task.isCancelled else { return }
        }
        loading = true
        failed = false
        defer { if !Task.isCancelled { loading = false } }
        do {
            var r: SearchResults = try await HubClient.shared.get("/v1/search", ["q": q])
            guard !Task.isCancelled else { return }
            results = r
            // Sources slower than the hub's budget: ask once more for just those.
            if let p = r.pending, !p.isEmpty {
                try? await Task.sleep(for: .milliseconds(900))
                guard !Task.isCancelled else { return }
                let more: SearchResults = try await HubClient.shared.get("/v1/search", ["q": q, "types": p.joined(separator: ",")])
                guard !Task.isCancelled else { return }
                let have = Set(r.groups.map(\.type))
                r.groups += more.groups.filter { !have.contains($0.type) }
                r.groups.sort { ($0.items.first?.score ?? 0) > ($1.items.first?.score ?? 0) }
                r.pending = more.pending
                results = r
            }
        } catch {
            guard !Task.isCancelled else { return }
            results = nil
            failed = true
        }
    }

    // MARK: - Recent searches (this phone only)

    private func remember(_ q: String) {
        let q = q.trimmingCharacters(in: .whitespaces)
        guard q.count > 1 else { return }
        recent = Array(([q] + recent.filter { $0.caseInsensitiveCompare(q) != .orderedSame }).prefix(8))
        Self.saveRecent(recent)
    }

    static func loadRecent() -> [String] { AppSettings.defaults.stringArray(forKey: "search.recent") ?? [] }
    static func saveRecent(_ r: [String]) { AppSettings.defaults.set(r, forKey: "search.recent") }

    static func short(_ type: String) -> String {
        switch type {
        case "room": "Rooms"; case "light": "Lights"; case "scene": "Scenes"; case "door": "Doors"
        case "climate": "Climate"; case "player": "TVs"; case "appliance": "Kitchen"; case "camera": "Cameras"
        case "event": "Clips"; case "media": "Watch"; case "youtube": "YouTube"; case "recipe": "Recipes"
        case "chore": "Chores"; case "calendar": "Calendar"; case "trip": "Trips"; case "person": "People"
        case "inventory": "Stuff"; case "alert": "Alerts"; case "screen": "Settings"
        default: type.capitalized
        }
    }

    static func color(_ type: String) -> Color {
        switch type {
        case "room", "light", "scene": Theme.warn
        case "camera", "event", "door": Theme.crit
        case "media", "youtube": Color.purple
        case "recipe", "appliance": Theme.star
        case "chore", "calendar", "person": Theme.kid
        case "alert": Theme.little
        case "screen": Theme.dim
        default: Theme.accent
        }
    }
}

/// One result: picture or icon, title, state, and the button that does the obvious thing.
struct SearchRow: View {
    let item: SearchItem
    let run: () -> Void
    let open: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Button(action: open) {
                HStack(spacing: 12) {
                    lead
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                        if let s = item.subtitle, !s.isEmpty {
                            Text(s).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                        }
                    }
                    Spacer(minLength: 4)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            trailing
        }
        .padding(.vertical, 8)
    }

    @ViewBuilder private var lead: some View {
        if item.type == "camera" || item.type == "event", let img = item.imageUrl {
            HubImage(path: img) { Theme.card2 }
                .frame(width: 64, height: 40).clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        } else if item.type == "media" || item.type == "youtube" || item.type == "recipe", let img = item.imageUrl {
            HubImage(path: img) { Theme.card2 }
                .frame(width: item.type == "media" ? 34 : 44, height: 44).clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
        } else {
            IconBadge(symbol: item.symbol ?? "magnifyingglass", color: SearchView.color(item.type), size: 38)
        }
    }

    @ViewBuilder private var trailing: some View {
        if let a = item.action, a.toggle == true {
            Toggle(item.title, isOn: Binding(get: { a.on ?? false }, set: { _ in run() }))
                .labelsHidden().tint(Theme.accent)
        } else if let verb = item.action?.label ?? item.verb {
            let primary = item.action != nil || ["Play", "Watch", "Live", "Done", "Request"].contains(verb)
            Button(verb, action: item.action != nil || item.media != nil || item.request != nil || item.chore != nil ? run : open)
                .font(.subheadline.weight(.semibold))
                .buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.small)
                .tint(primary ? Theme.accent : Theme.dim)
        }
    }
}

/// Chips that wrap onto more lines.
struct FlowChips: View {
    let items: [String]
    let tap: (String) -> Void
    var body: some View {
        WrapLayout(spacing: 8) {
            ForEach(items, id: \.self) { s in ChipButton(title: s) { tap(s) } }
        }
    }
}

struct WrapLayout: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width { y += row + spacing; x = 0; row = 0 }
            x += size.width + spacing; row = max(row, size.height); widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, width), height: y + row)
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX { y += row + spacing; x = bounds.minX; row = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing; row = max(row, size.height)
        }
    }
}

/// What the phone can answer by itself from the last house snapshot: instant, and offline.
@MainActor
enum LocalSearch {
    static func run(_ q: String, store: HomeStore) -> [SearchGroup] {
        let words = norm(q).split(separator: " ").map(String.init)
        guard !words.isEmpty, let home = store.home else { return [] }
        var groups: [String: [SearchItem]] = [:]
        func add(_ i: SearchItem) { groups[i.type, default: []].append(i) }

        for r in home.rooms ?? [] {
            let s = score(words, r.name, "room")
            if s > 0 {
                add(SearchItem(id: "room:\(r.id)", type: "room", title: r.name, subtitle: r.summary, score: s + 1.5,
                               deepLink: "thomascreek://room/\(r.id)", verb: "Open", symbol: r.symbol ?? "square.split.2x2"))
            }
        }
        for d in home.devices ?? [] {
            let roomName = home.rooms?.first { $0.id == d.roomId }?.name
            let s = score(words, d.name, "\(roomName ?? "") \(d.kind.rawValue)")
            guard s > 0 else { continue }
            var action: SearchAction?
            var type = "light", symbol = "lightbulb.fill", link = d.roomId.map { "thomascreek://room/\($0)" }, verb: String? = nil
            switch d.kind {
            case .light, .fan, .switch:
                if d.actions?.isEmpty == false {
                    action = SearchAction(action: "\(d.kind.rawValue).set", target: d.id, params: ["on": .bool(!d.isOn)],
                                          tier: "everyday", label: d.isOn ? "Turn off" : "Turn on", toggle: true, on: d.isOn)
                }
            case .garage, .lock: type = "door"; symbol = d.kind == .garage ? "door.garage.closed" : "lock.fill"; link = "thomascreek://doors"
            case .media, .speaker: type = "player"; symbol = d.kind == .media ? "tv" : "hifispeaker.fill"; link = "thomascreek://remote/\(d.id)"; verb = "Remote"
            case .climate: type = "climate"; symbol = "thermometer.medium"; link = "thomascreek://climate"
            default: continue
            }
            let sub = [roomName, d.isAvailable ? (d.kind == .light || d.kind == .fan || d.kind == .switch ? (d.isOn ? "on" : "off") : nil) : "offline"]
                .compactMap { $0 }.joined(separator: " · ")
            add(SearchItem(id: "\(type):\(d.id)", type: type, title: d.name, subtitle: sub, score: s, ref: d.id,
                           deepLink: link, action: action, verb: verb, symbol: symbol, roomId: d.roomId))
        }
        for c in home.cameras ?? [] {
            let s = score(words, c.name, "camera")
            if s > 0 {
                add(SearchItem(id: "camera:\(c.id)", type: "camera", title: c.name, subtitle: "Live", score: s,
                               deepLink: "thomascreek://camera/\(c.id)", verb: "Live", imageUrl: c.snapshotUrl, symbol: "video.fill"))
            }
        }
        for p in home.people ?? [] {
            let s = score(words, p.name, "person")
            if s > 0 {
                add(SearchItem(id: "person:\(p.id)", type: "person", title: p.name, subtitle: p.role?.rawValue.capitalized,
                               score: s, symbol: "person.crop.circle"))
            }
        }
        let order = ["room", "light", "door", "climate", "player", "camera", "person"]
        return groups.map { t, items in
            let sorted = items.sorted { ($0.score ?? 0) > ($1.score ?? 0) }
            return SearchGroup(type: t, title: title(t), total: sorted.count, items: Array(sorted.prefix(8)))
        }
        .sorted { a, b in
            let sa = a.items.first?.score ?? 0, sb = b.items.first?.score ?? 0
            return sa != sb ? sa > sb : (order.firstIndex(of: a.type) ?? 9) < (order.firstIndex(of: b.type) ?? 9)
        }
    }

    static func title(_ t: String) -> String {
        ["room": "Rooms", "light": "Lights and switches", "door": "Doors and garage", "climate": "Climate",
         "player": "TVs and speakers", "camera": "Cameras", "person": "People"][t] ?? t.capitalized
    }

    static func norm(_ s: String) -> String {
        s.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: nil)
            .replacingOccurrences(of: "'", with: "").replacingOccurrences(of: "\u{2019}", with: "")
            .components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// Same idea as the hub's scorer: every word must appear; whole words and title starts count more.
    static func score(_ words: [String], _ title: String, _ extra: String) -> Double {
        let t = norm(title), k = norm(extra)
        let tw = t.split(separator: " ").map(String.init), kw = k.split(separator: " ").map(String.init)
        func stem(_ w: String) -> String { w.count > 3 && w.hasSuffix("s") && !w.hasSuffix("ss") ? String(w.dropLast()) : w }
        var total = 0.0
        for w in words {
            if tw.contains(where: { $0 == w || stem($0) == stem(w) }) { total += 4 }
            else if tw.contains(where: { $0.hasPrefix(w) }) { total += 3 }
            else if kw.contains(where: { $0 == w || stem($0) == stem(w) }) { total += 2 }
            else if kw.contains(where: { $0.hasPrefix(w) }) { total += 1.5 }
            else if w.count >= 3 && (t.contains(w) || k.contains(w)) { total += 0.75 }
            else { return 0 }
        }
        let joined = words.joined(separator: " ")
        if t.hasPrefix(joined) { total += 2 }
        if t == joined { total += 3 }
        return total
    }
}

/// The magnifier that opens search, for every tab's top bar.
struct SearchButton: View {
    @Environment(Router.self) private var router
    var body: some View {
        Button { router.showSearch = true } label: { Image(systemName: "magnifyingglass") }
            .accessibilityLabel("Search")
    }
}
