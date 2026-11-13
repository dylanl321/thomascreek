import SwiftUI

/// Home for grown-ups: "is the house OK, and what do I want right now", in one screen.
struct HomeView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    private var me: Me? { store.me }
    private var home: HomeSnapshot? { store.home }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HeroHeader(title: greeting(me?.firstName), subtitle: dateLine, subtitleLink: home?.weather?.temp == nil ? nil : .weather, show3D: true)
                VPNNotice()
                ForEach(sections) { section($0) }
                if !store.reachable, store.role == .admin, !sections.contains(.status) { OfflineCard() }
                Button { router.showEditHome = true } label: {
                    Label("Edit Home", systemImage: "slider.horizontal.3").font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                }
                .buttonStyle(.plain).foregroundStyle(Theme.accent).padding(.top, 4)
            }
            .screen()
        }
        .background(Backdrop())
        // With the navigation bar hidden nothing covers the status bar, so headings scrolled under the clock.
        .safeAreaInset(edge: .top, spacing: 0) { Color.clear.frame(height: 0).background(.bar) }
        .refreshable { await store.refreshAll(); await loadTrips(force: true) }
        .toolbar(.hidden, for: .navigationBar)
        .task { await loadTrips() }
    }

    private func loadTrips(force: Bool = false) async {
        if me?.can(.tripsView) == true { await TripStore.shared.load(force: force) }
    }

    /// What this person can see on Home, in the default order; `SettingsStore` applies their own order.
    static func sections(for me: Me?) -> [HomeSection] {
        HomeSection.allCases.filter { s in
            switch s {
            case .whosHome: true
            case .printer: me?.can(.appliances) == true
            case .kids: me?.can(.networkManage) == true
            case .cameras: me?.can(.cameras) == true
            case .holiday: me?.can(.holidayLooks) == true || me?.can(.holidayFull) == true
            case .trip: me?.can(.tripsView) == true
            case .systems: me?.can(.systems) == true
            default: true
            }
        }
    }

    private var sections: [HomeSection] { SettingsStore.shared.homeSections(default: Self.sections(for: me)) }

    @ViewBuilder private func section(_ s: HomeSection) -> some View {
        switch s {
        case .whosHome: WhosHomeRow()
        case .rightNow: RightNowSection()
        case .weather: HomeWeatherCard()
        case .status:
            if !store.reachable {
                // Everyone sees the offline strip; Dylan also gets the likely cause.
                if store.role == .admin { OfflineCard() }
            } else { StatusBlock() }
        case .favorites: FavoritesSection()
        case .modes:
            if let modes = home?.modes, !modes.isEmpty {
                SectionHeader("Modes")
                ModesRow(modes: modes)
            }
        case .kids:
            if !kids.isEmpty {
                SectionHeader(title: "Kids online") { MoreButton("Manage") { router.go(.kidNet) } }
                KidsOnlineRow(kids: kids)
            }
        case .playing:
            if let np = home?.nowPlaying?.first {
                SectionHeader("Now playing", live: true)
                HouseNowPlayingCard(item: np)
            }
        // Printers (PR #34): `ForEach(home?.activePrinters ?? []) { PrinterNowCard(printer: $0) }`
        case .printer: ForEach(home?.activePrinters ?? []) { PrinterNowCard(printer: $0) }
        case .cameras:
            if !(home?.cameras?.isEmpty ?? true) {
                SectionHeader(title: "Recent at the cameras") { MoreButton { router.show(.house, .cameras) } }
                RecentEventsStrip(events: store.recentEvents)
            }
        case .holiday:
            if store.holidaySeason {
                SectionHeader("Tonight's show")
                HolidayHomeCard()
            }
        case .trip: NextTripCard()
        case .today: TodaySection()
        case .systems:
            if let sys = home?.systems {
                SectionHeader(title: "Systems") { MoreButton { router.show(.systems) } }
                SystemsStrip(systems: sys)
            }
        }
    }

    private var kids: [Person] { (home?.people ?? []).filter { $0.role == .child } }

    private var dateLine: String {
        var s = Date.now.formatted(.dateTime.weekday(.wide).month(.abbreviated).day())
        if let w = home?.weather, let t = w.temp { s += " · \(Int(t.rounded()))° \(w.condition ?? "")" }
        return s
    }
}

// MARK: - Hero

/// The house art with the profile avatar, the 3D chip and the inbox bell over it.
/// The date and greeting sit up in the sky, under the buttons, so they never cover the house.
struct HeroHeader: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let title: String
    let subtitle: String
    /// Where the subtitle leads when tapped (the date line carries the temperature, so Weather).
    var subtitleLink: Destination? = nil
    var show3D = false
    var height: CGFloat = 230

    var body: some View {
        ZStack(alignment: .topLeading) {
            HubImage(path: "/v1/assets/header") {
                Image("HomeHeader").resizable().scaledToFill()
            }
            .frame(height: height).frame(maxWidth: .infinity).clipped()
            .onLongPressGesture(minimumDuration: 0.7) { CatDirector.shared.perform(.briDash) }
            LinearGradient(colors: [.black.opacity(0.35), .clear], startPoint: .top, endPoint: .center)
            VStack(alignment: .leading, spacing: 0) {
                if store.me?.dryRun == true {
                    Text("Practice mode · nothing really changes").font(.caption2.weight(.bold)).foregroundStyle(.black)
                        .padding(.horizontal, 8).padding(.vertical, 3).background(Theme.warn, in: Capsule()).padding(.bottom, 4)
                }
                Text(subtitle).font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.8))
                    .links(to: subtitleLink, "Weather")
                Text(title).font(.system(.title, design: .rounded, weight: .bold)).foregroundStyle(.white)
                    .lineLimit(1).minimumScaleFactor(0.6)
                    .shadow(color: .black.opacity(0.35), radius: 6, y: 1)
            }
            .padding(.horizontal, 18)
            .padding(.top, 62) // clears the avatar / 3D / bell row
        }
        .catLedge()
        .overlay(alignment: .top) {
            HStack {
                Button { router.showProfile = true } label: {
                    Avatar(name: store.me?.person.name ?? "?", color: Color(hexString: store.me?.person.color), size: 36)
                        .overlay(Circle().stroke(.white.opacity(0.5), lineWidth: 1))
                }
                .accessibilityLabel("Profile")
                Spacer()
                if show3D {
                    Button { router.show(.house, .house3D) } label: {
                        Label("View in 3D", systemImage: "cube.transparent").font(.caption.weight(.semibold))
                            .padding(.horizontal, 10).padding(.vertical, 7)
                            .background(.ultraThinMaterial, in: Capsule())
                    }
                    .foregroundStyle(.white)
                }
                SearchButton().padding(9).background(.ultraThinMaterial, in: Circle()).foregroundStyle(.white)
                BellButton().padding(9).background(.ultraThinMaterial, in: Circle()).foregroundStyle(.white)
            }
            .padding(14)
        }
        .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
        .padding(.top, 6)
    }
}

// MARK: - Status

/// One calm line, or the specific things that need attention with a button to fix each.
struct StatusBlock: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        let status = store.home?.status
        let items = status?.items ?? []
        if status == nil || (status?.level == "ok" && items.isEmpty) {
            HStack(spacing: 12) {
                IconBadge(symbol: "checkmark", color: Theme.accent, size: 38)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Everything's fine").font(.headline)
                    Text(status?.line ?? "Checking the house…").font(.subheadline).foregroundStyle(Theme.dim)
                }
                Spacer(minLength: 0)
            }
            .card()
        } else {
            VStack(spacing: 8) {
                if items.isEmpty, let status {
                    alertRow(.init(id: "line", level: status.level, title: status.line))
                }
                ForEach(items) { alertRow($0) }
            }
        }
    }

    private func alertRow(_ item: HomeSnapshot.StatusItem) -> some View {
        let color = Theme.level(item.level)
        let link = item.ref == nil ? Destination.forTarget(item.deviceId) : nil
        return Button {
            if let ref = item.ref, let url = URL(string: ref) { router.follow(url) }
            else if let link, router.allows(link) { router.follow(link) }
        } label: {
            HStack(spacing: 12) {
                IconBadge(symbol: item.level == "critical" ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill", color: color, size: 38)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                    if let d = item.detail { Text(d).font(.footnote).foregroundStyle(Theme.dim) }
                }
                Spacer(minLength: 0)
                if let action = item.action {
                    Button(item.actionTitle ?? "Fix") {
                        store.perform(action, target: item.deviceId, confirm: SecureAction.reason(for: action, name: item.title))
                    }
                    .buttonStyle(.borderedProminent).tint(color).controlSize(.small)
                } else if item.ref != nil || link.map { router.allows($0) } == true {
                    Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
                }
            }
            .card(color: color.opacity(0.12))
        }
        .buttonStyle(.plain)
    }
}

/// Face ID wording for Secure-tier actions; nil for everyday ones.
enum SecureAction {
    static let secure: Set<String> = ["garage.set", "lock.set", "oven.set", "network.pause", "network.grant", "camera.away"]
    static func reason(for action: String, name: String) -> String? {
        guard secure.contains(action) || action.hasPrefix("garage") || action.hasPrefix("lock") else { return nil }
        return "Confirm: \(name)"
    }
}

struct OfflineCard: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        let since = store.unreachableSince ?? store.lastUpdate
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                IconBadge(symbol: "icloud.slash", color: Theme.warn, size: 38)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Can't reach home").font(.headline)
                    if let since { Text("Since \(since.formatted(date: .omitted, time: .shortened))").font(.footnote).foregroundStyle(Theme.dim) }
                }
            }
            if store.role == .admin {
                InfoRow("iphone", "This \(UIDevice.current.model) is online", "Showing what the house last reported")
                if VPN.shared.isAvailable && VPN.shared.mode != .off { InfoRow("lock.shield", color: Theme.warn, "Home tunnel isn't answering", "It connects by itself when home is back") }
                InfoRow("wifi.exclamationmark", color: Theme.warn, "Likely cause: home internet or the hub", "Light switches, the wall tablets and the Home Assistant app keep working")
            } else {
                Text("Showing what the house last said. Light switches and the wall tablets still work.")
                    .font(.subheadline).foregroundStyle(Theme.dim)
            }
        }
        .card(color: Theme.card2)
    }
}

// MARK: - Rows and tiles

struct ModesRow: View {
    @Environment(HomeStore.self) private var store
    let modes: [Mode]
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(modes) { m in
                    Button { store.perform("mode.run", target: m.id, done: "\(m.name) is on") } label: {
                        VStack(spacing: 8) {
                            Image(systemName: m.symbol ?? "sparkles").font(.title3)
                                .foregroundStyle(m.active == true ? Theme.onAccent : Theme.accent)
                                .frame(width: 46, height: 46)
                                .background(m.active == true ? Theme.accent : Theme.accent.opacity(0.14), in: Circle())
                            Text(m.name).font(.caption.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                        }
                        .frame(width: 86).padding(.vertical, 12)
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .favoriteMenu(.mode, m.id, name: m.name)
                }
            }
        }
        .scrollClipDisabled()
    }
}

struct RoomTile: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let room: Room
    var body: some View {
        let n = store.lightsOn(in: room)
        StateTile(symbol: room.symbol ?? (n > 0 ? "lightbulb.fill" : "lightbulb"), title: room.name,
                  detail: room.summary ?? (n > 0 ? "\(n) light\(n == 1 ? "" : "s") on" : "Lights off"), on: n > 0) {
            router.go(.room(room.id))
        }
        .favoriteMenu(.room, room.id, name: room.name)
    }
}

struct KidsOnlineRow: View {
    @Environment(Router.self) private var router
    let kids: [Person]
    var body: some View {
        HStack(spacing: 10) {
            ForEach(kids) { k in
                Button { router.go(.kidNet) } label: {
                    HStack(spacing: 10) {
                        Avatar(name: k.name, color: Color(hexString: k.color), size: 34)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(k.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                            Text(k.home == false ? "Away" : "Online").font(.caption).foregroundStyle(Theme.dim)
                        }
                        Spacer(minLength: 0)
                    }
                    .card(padding: 12)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

struct HouseNowPlayingCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let item: NowPlaying
    var body: some View {
        let room = store.home?.room(item.roomId)?.name
        let dev = store.home?.device(item.deviceId)
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                HubImage(path: item.artUrl) { Theme.card2 }
                    .frame(width: 54, height: 54).clipShape(RoundedRectangle(cornerRadius: 12))
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.title).font(.headline).lineLimit(1)
                    Text([room, dev?.name, item.subtitle].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                        .links(to: item.roomId.map { .room($0) }, room)
                    if let p = item.progress { ProgressBar(value: p).frame(width: 150) }
                }
            }
            HStack(spacing: 8) {
                ChipButton(title: "", symbol: item.state == "playing" ? "pause.fill" : "play.fill") {
                    store.perform("media.command", target: item.deviceId, params: ["command": item.state == "playing" ? "pause" : "play"])
                }
                ChipButton(title: "Remote", symbol: "appletvremote.gen4") { router.show(.media, .remote(item.deviceId)) }
                if let v = item.volume { ChipButton(title: "\(Int(v))%", symbol: "hifispeaker.fill") { router.show(.media, .music) } }
            }
        }
        .card()
    }
}

/// Newest detections, newest first. Tapping one opens its clip; "See all" opens the Cameras section.
struct RecentEventsStrip: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    let events: [CameraEvent]?
    var body: some View {
        if let events, !events.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(events.prefix(12)) { e in
                        Button { router.show(.house, .cameraEvent(e.id)) } label: { EventCard(event: e).frame(width: 156) }
                            .buttonStyle(.plain)
                    }
                }
            }
            .scrollClipDisabled()
        } else if events == nil {
            LoadingCard().task { await store.loadRecentEvents() }
        } else if let err = store.recentEventsError {
            EmptyCard(symbol: "exclamationmark.triangle", title: "Couldn't load camera events", detail: err)
        } else {
            EmptyCard(symbol: "video.badge.checkmark", title: "Quiet so far today", detail: "People, cars and animals the cameras spot show up here.")
        }
    }
}

struct CameraThumb: View {
    let camera: Camera
    /// Snapshots change; a per-minute key keeps the image cache from showing an old frame.
    static func fresh(_ path: String) -> String {
        path + (path.contains("?") ? "&" : "?") + "m=\(Int(Date().timeIntervalSince1970 / 60))"
    }
    var body: some View {
        ZStack(alignment: .bottomLeading) {
            // The snapshot sits in an overlay so its pixel size never widens the tile (it fills, then clips).
            Color.clear.overlay {
                HubImage(path: camera.snapshotUrl.map { CameraThumb.fresh($0) }) {
                    LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom)
                        .overlay(Image(systemName: "video.fill").foregroundStyle(Theme.dim))
                }
            }
            .clipped()
            LinearGradient(colors: [.clear, .black.opacity(0.6)], startPoint: .center, endPoint: .bottom)
            HStack {
                Text(camera.name).font(.caption.weight(.semibold))
                Spacer()
                if let e = camera.event { Text(e).font(.caption2.weight(.bold)).padding(.horizontal, 6).padding(.vertical, 2).background(Theme.warn, in: Capsule()) }
            }
            .foregroundStyle(.white).padding(8)
        }
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

struct TodayCard: View {
    /// Room for "10:30 AM" at the person's text size, so times never wrap.
    @ScaledMetric(relativeTo: .subheadline) private var timeWidth: CGFloat = 70
    @Environment(Router.self) private var router
    let today: Today
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach((today.events ?? []).sorted { ($0.isAllDay ? 0 : 1, ISO.parse($0.start) ?? .distantFuture) < ($1.isAllDay ? 0 : 1, ISO.parse($1.start) ?? .distantFuture) }, id: \.stableID) { e in
                HStack(alignment: .top, spacing: 12) {
                    Text(e.isAllDay ? "All day" : clock(e.start)).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.dim)
                        .lineLimit(1).minimumScaleFactor(0.7).frame(width: timeWidth, alignment: .leading)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(e.title).font(.subheadline.weight(.medium))
                        if let w = e.who { Text(w).font(.caption).foregroundStyle(Theme.dim) }
                    }
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 8)
                Divider().overlay(Theme.line)
            }
            if let d = today.dinner {
                Button { router.show(.family, d.recipeId.map { .recipe($0) } ?? .meals) } label: {
                    HStack(spacing: 12) {
                        Text(d.emoji ?? "🍽️").font(.system(size: 30))
                        VStack(alignment: .leading, spacing: 1) {
                            Text("Dinner").font(.caption).foregroundStyle(Theme.dim)
                            Text(d.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
                    }
                    .padding(.top, 10)
                }
                .buttonStyle(.plain)
            }
            if (today.events ?? []).isEmpty && today.dinner == nil {
                Text("Nothing on the calendar today.").font(.subheadline).foregroundStyle(Theme.dim)
            }
        }
        .card()
    }
}

struct SystemsStrip: View {
    @Environment(Router.self) private var router
    let systems: Systems
    var body: some View {
        let pool = systems.storage?.first
        HStack(spacing: 10) {
            tile("Storage", pool.map { "\(Int($0.used))%" } ?? "–", pool?.scrubPct.map { "Scrub \(Int($0))%" }, bar: pool.map { $0.used / 100 }) { router.show(.systems, .storage) }
            let apps = systems.apps
            tile("Apps", apps.flatMap { a in a.total.map { "\($0 - a.crashed)/\($0)" } } ?? "–",
                 apps.map { $0.crashed > 0 ? "\($0.crashed) crashed" : "Running" }, bad: (apps?.crashed ?? 0) > 0) { router.show(.systems, .storage) }
            tile("Internet", systems.internet.map { $0.up ? "Up" : "Down" } ?? "–",
                 systems.internet?.downMbps.map { "\(Int($0)) Mbps" }, bad: systems.internet?.up == false) { router.show(.systems) }
        }
    }

    private func tile(_ title: String, _ value: String, _ detail: String?, bar: Double? = nil, bad: Bool = false, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.caption).foregroundStyle(Theme.dim)
                Text(value).font(.title3.weight(.bold).monospacedDigit()).foregroundStyle(bad ? Theme.crit : Theme.fg)
                if let bar { ProgressBar(value: bar) }
                if let detail { Text(detail).font(.caption2).foregroundStyle(bad ? Theme.crit : Theme.dim) }
            }
            .card(padding: 12)
        }
        .buttonStyle(.plain)
    }
}


/// Always on Home (VPN builds): connected or not, and one button to connect or disconnect.
struct VPNNotice: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    @State private var busy = false
    var body: some View {
        let vpn = VPN.shared
        let connected = vpn.status == .connected
        let working = busy || vpn.status == .connecting || vpn.status == .reasserting || vpn.status == .disconnecting
        if vpn.isAvailable {
            HStack(spacing: 12) {
                Image(systemName: connected ? "lock.shield.fill" : "lock.open")
                    .font(.title3).foregroundStyle(connected ? Theme.accent : Theme.dim)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title(vpn)).font(.subheadline.weight(.semibold))
                    Text(detail(vpn)).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
                }
                Spacer(minLength: 8)
                if working {
                    ProgressView().frame(width: 96)
                } else {
                    Button(connected ? "Disconnect" : "Connect") {
                        busy = true
                        Task {
                            defer { busy = false }
                            if connected { await vpn.disconnectNow() }
                            else {
                                do { try await vpn.connectNow() }
                                catch { store.show(error.localizedDescription, isError: true) }
                            }
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(connected ? Theme.crit : Theme.accent)
                    .controlSize(.small)
                    .frame(minWidth: 96)
                }
            }
            .padding(12)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(connected ? Theme.accent : Theme.line, lineWidth: connected ? 1.5 : 1))
            .contentShape(Rectangle())
            .onTapGesture { router.showVPN = true }
            .task { await vpn.refreshStatus() }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("VPN, \(title(vpn))")
        }
    }

    private func title(_ vpn: VPN) -> String {
        switch vpn.status {
        case .connected: "VPN connected"
        case .connecting, .reasserting: "VPN connecting…"
        case .disconnecting: "VPN disconnecting…"
        default: "VPN not connected"
        }
    }
    private func detail(_ vpn: VPN) -> String {
        if let p = vpn.problem { return p }
        if vpn.status == .connected {
            return vpn.exit == .home ? "Home · home traffic goes through the tunnel" : "Private · PIA \(vpn.regionName ?? "nearest region")"
        }
        return switch vpn.mode {
        case .off: vpn.exit == .private ? "Private · tap Connect" : (vpn.isSetUp ? "Home · tap Connect to reach home from anywhere" : "Tap Connect once at home to set it up")
        case .away: "\(vpn.exit.title) · connects by itself when you leave home Wi-Fi"
        case .always: "\(vpn.exit.title) · always on; it should connect shortly"
        }
    }
}
