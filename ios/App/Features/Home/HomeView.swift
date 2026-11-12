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
                HeroHeader(title: greeting(me?.firstName), subtitle: dateLine, show3D: true)
                if !store.reachable {
                    // Everyone sees the offline strip; Dylan also gets the likely cause.
                    if store.role == .admin { OfflineCard() }
                } else { StatusBlock() }

                if let modes = home?.modes, !modes.isEmpty {
                    SectionHeader("Modes")
                    ModesRow(modes: modes)
                }

                if me?.can(.networkManage) == true, !kids.isEmpty {
                    SectionHeader(title: "Kids online") { MoreButton("Manage") { router.go(.kidNet) } }
                    KidsOnlineRow(kids: kids)
                }

                let favs = store.favoriteRooms
                if !favs.isEmpty {
                    SectionHeader(title: "Favorites", live: true) { MoreButton("All rooms") { router.show(.house) } }
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(favs) { RoomTile(room: $0) }
                    }
                }

                if let np = home?.nowPlaying?.first {
                    SectionHeader("Now playing", live: true)
                    HouseNowPlayingCard(item: np)
                }

                if me?.can(.cameras) == true, !(home?.cameras?.isEmpty ?? true) {
                    SectionHeader(title: "Recent at the cameras") { MoreButton { router.show(.house, .cameras) } }
                    RecentEventsStrip(events: store.recentEvents)
                }

                if store.holidaySeason, me?.can(.holidayLooks) == true || me?.can(.holidayFull) == true {
                    SectionHeader("Tonight's show")
                    HolidayHomeCard()
                }

                if me?.can(.tripsView) == true { NextTripCard() }

                if let today = home?.today {
                    SectionHeader(title: "Today") { MoreButton("Calendar") { router.show(.family, .calendar) } }
                    TodayCard(today: today)
                }

                if me?.can(.systems) == true, let sys = home?.systems {
                    SectionHeader(title: "Systems") { MoreButton { router.show(.systems) } }
                    SystemsStrip(systems: sys)
                }
            }
            .screen()
        }
        .background(Backdrop())
        .refreshable { await store.refreshAll(); await loadTrips(force: true) }
        .toolbar(.hidden, for: .navigationBar)
        .task { await loadTrips() }
    }

    private func loadTrips(force: Bool = false) async {
        if me?.can(.tripsView) == true { await TripStore.shared.load(force: force) }
    }

    private var kids: [Person] { (home?.people ?? []).filter { $0.role == .child } }

    private var dateLine: String {
        var s = Date.now.formatted(.dateTime.weekday(.wide).month(.abbreviated).day())
        if let w = home?.weather, let t = w.temp { s += " · \(Int(t.rounded()))° \(w.condition ?? "")" }
        return s
    }
}

// MARK: - Hero

/// The house art with the greeting, the profile avatar, the 3D chip and the inbox bell over it.
struct HeroHeader: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let title: String
    let subtitle: String
    var show3D = false
    var height: CGFloat = 230

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            HubImage(path: "/v1/assets/header") {
                Image("HomeHeader").resizable().scaledToFill()
            }
            .frame(height: height).frame(maxWidth: .infinity).clipped()
            LinearGradient(colors: [.clear, .black.opacity(0.55)], startPoint: .center, endPoint: .bottom)
            VStack(alignment: .leading, spacing: 2) {
                if store.me?.dryRun == true {
                    Text("Practice mode · nothing really changes").font(.caption2.weight(.bold)).foregroundStyle(.black)
                        .padding(.horizontal, 8).padding(.vertical, 3).background(Theme.warn, in: Capsule()).padding(.bottom, 4)
                }
                Text(subtitle).font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.8))
                Text(title).font(.system(.largeTitle, design: .rounded, weight: .bold)).foregroundStyle(.white)
                    .lineLimit(1).minimumScaleFactor(0.7)
            }
            .padding(18)
        }
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
        return Button {
            if let ref = item.ref, let url = URL(string: ref) { router.open(url) }
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
                } else if item.ref != nil {
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
                if VPN.shared.isAvailable { InfoRow("lock.shield", color: Theme.warn, "Home tunnel isn't answering", "It connects by itself when home is back") }
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
            LoadingCard()
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
            HubImage(path: camera.snapshotUrl.map { CameraThumb.fresh($0) }) {
                LinearGradient(colors: [Theme.card2, Theme.off], startPoint: .top, endPoint: .bottom)
                    .overlay(Image(systemName: "video.fill").foregroundStyle(Theme.dim))
            }
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
    @Environment(Router.self) private var router
    let today: Today
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(today.events ?? [], id: \.stableID) { e in
                HStack(alignment: .top, spacing: 12) {
                    Text(e.allDay == true ? "All day" : clock(e.start)).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.dim)
                        .frame(width: 70, alignment: .leading)
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
