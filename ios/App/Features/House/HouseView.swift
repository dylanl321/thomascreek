import SwiftUI
import WebKit
import AVKit

/// Floors and rooms, plus the whole-house sections (climate, doors, cameras, appliances, holiday lights).
struct HouseView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                sections
                ForEach(store.home?.floors ?? []) { floor in
                    SectionHeader(floor.name, live: true)
                    LazyVGrid(columns: grid, spacing: 10) {
                        ForEach(store.rooms(on: floor)) { RoomTile(room: $0) }
                    }
                }
                if store.home?.floors?.isEmpty ?? true, let rooms = store.home?.rooms {
                    SectionHeader("Rooms", live: true)
                    LazyVGrid(columns: grid, spacing: 10) { ForEach(rooms) { RoomTile(room: $0) } }
                }
                if store.home == nil { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("House")
        .topBar()
        .refreshable { await store.loadHome() }
    }

    private var grid: [GridItem] { [GridItem(.adaptive(minimum: 150), spacing: 10)] }

    private var sections: some View {
        let me = store.me
        let h = store.home
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 10)], spacing: 10) {
            if me?.can(.climateAll) == true, !(h?.devices(kind: .climate).isEmpty ?? true) {
                SectionTile(symbol: "thermometer.medium", title: "Climate", detail: climateLine) { router.go(.climate) }
            }
            if me?.can(.doors) == true {
                SectionTile(symbol: "door.garage.closed", title: "Doors", detail: doorsLine, alert: doorsOpen > 0, asset: "tc.garage") { router.go(.doors) }
            }
            if me?.can(.cameras) == true, !(h?.cameras?.isEmpty ?? true) {
                SectionTile(symbol: "video.fill", title: "Cameras", detail: "\(h?.cameras?.count ?? 0) live") { router.go(.cameras) }
            }
            if me?.can(.appliances) == true {
                SectionTile(symbol: "oven.fill", title: "Appliances", detail: appliancesLine) { router.go(.appliances) }
            }
            if me?.can(.holidayLooks) == true || me?.can(.holidayFull) == true {
                SectionTile(symbol: "sparkles", title: "Holiday lights", detail: AppSettings.cachedStatus?.headline ?? "Show", asset: "tc.lights") { router.go(.holiday) }
            }
            if !(h?.devices(kind: .valve).isEmpty ?? true) {
                SectionTile(symbol: "drop.fill", title: "Outdoor", detail: "Irrigation") { router.go(.outdoor) }
            }
            if !(h?.devices(kind: .printer).isEmpty ?? true) {
                SectionTile(symbol: "printer.fill", title: "Workshop", detail: printersLine) { router.go(.workshop) }
            }
        }
    }

    private var climateLine: String {
        let temps = (store.home?.devices(kind: .climate) ?? []).compactMap { $0["currentTemp"]?.double }
        guard !temps.isEmpty else { return "–" }
        return "\(Int((temps.reduce(0, +) / Double(temps.count)).rounded()))° inside"
    }
    private var doorsOpen: Int {
        (store.home?.devices(kind: .garage) ?? []).filter { $0["open"]?.bool == true }.count
            + (store.home?.devices(kind: .lock) ?? []).filter { $0["locked"]?.bool == false }.count
    }
    private var doorsLine: String { doorsOpen == 0 ? "All closed" : "\(doorsOpen) open" }
    private var appliancesLine: String {
        let on = (store.home?.devices ?? []).filter { [.oven, .vacuum, .shower].contains($0.kind) && $0.isOn }.count
        return on == 0 ? "All off" : "\(on) on"
    }
    private var printersLine: String {
        let p = (store.home?.devices(kind: .printer) ?? []).filter { $0["state"]?.string == "printing" }.count
        return p == 0 ? "Idle" : "\(p) printing"
    }
}

struct SectionTile: View {
    let symbol: String
    let title: String
    let detail: String
    var alert = false
    /// A Thomas Creek glyph from the asset catalog (drawn from the house), used instead of the SF Symbol when set.
    var asset: String? = nil
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 8) {
                Group {
                    if let asset { Image(asset).resizable().scaledToFit().frame(width: 24, height: 24) }
                    else { Image(systemName: symbol).font(.title3) }
                }
                .foregroundStyle(alert ? Theme.warn : Theme.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                    Text(detail).font(.caption).foregroundStyle(alert ? Theme.warn : Theme.dim).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 80, alignment: .leading)
            .card(padding: 12)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Room

struct RoomDetailView: View {
    @Environment(HomeStore.self) private var store
    let roomID: String

    var body: some View {
        let room = store.home?.room(roomID)
        let devices = room.map { store.home?.devices(in: $0) ?? [] } ?? []
        let lights = devices.filter { [.light, .fan, .switch].contains($0.kind) }
        let climate = devices.filter { $0.kind == .climate }
        let media = devices.filter { [.media, .speaker].contains($0.kind) }
        let other = devices.filter { ![.light, .fan, .switch, .climate, .media, .speaker, .camera].contains($0.kind) }
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if !lights.isEmpty {
                    SectionHeader(title: "Lights", live: true) {
                        let anyOn = lights.contains { $0.isOn && $0.kind == .light }
                        Button(anyOn ? "All off" : "All on") {
                            for l in lights where l.kind == .light {
                                store.perform("light.set", target: l.id, params: ["on": !anyOn]) { $0.state["on"] = .bool(!anyOn) }
                            }
                        }
                        .font(.subheadline.weight(.medium))
                    }
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                        ForEach(lights) { DeviceTile(device: $0) }
                    }
                }
                ForEach(climate) { d in
                    SectionHeader("Climate")
                    ClimateCard(device: d)
                }
                if !media.isEmpty {
                    SectionHeader("TV and music")
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                        ForEach(media) { DeviceTile(device: $0) }
                    }
                }
                if !other.isEmpty {
                    SectionHeader("Also here")
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                        ForEach(other) { DeviceTile(device: $0) }
                    }
                }
                if devices.isEmpty { EmptyCard(symbol: "lightbulb.slash", title: "Nothing to control here yet") }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(room?.name ?? "Room")
    }
}

// MARK: - Climate, doors, lists

struct ClimateView: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                ForEach(store.home?.devices(kind: .climate) ?? []) { ClimateCard(device: $0) }
                ForEach((store.home?.devices(kind: .shower) ?? [])) { s in
                    HStack {
                        Label(s.name, systemImage: "shower.fill").font(.headline)
                        Spacer()
                        Button(s.isOn ? "Heating" : "Preheat") { store.perform("shower.preheat", target: s.id, done: "Steam shower heating") }
                            .buttonStyle(.borderedProminent).disabled(s.isOn)
                    }
                    .card()
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Climate")
    }
}

struct DoorsView: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                let garages = store.home?.devices(kind: .garage) ?? []
                let locks = store.home?.devices(kind: .lock) ?? []
                if !garages.isEmpty {
                    SectionHeader("Garage", live: true)
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(garages) { DeviceTile(device: $0, big: true) }
                    }
                }
                if !locks.isEmpty {
                    SectionHeader("Locks", live: true)
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(locks) { DeviceTile(device: $0, big: true) }
                    }
                }
                Text("Opening or unlocking asks for Face ID every time.").font(.footnote).foregroundStyle(Theme.dim).padding(.horizontal, 4)
                SectionHeader("Recent")
                DoorHistory()
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Doors")
    }
}

struct DoorHistory: View {
    @State private var items: [ActivityEntry]?
    var body: some View {
        Group {
            if let items, !items.isEmpty {
                RowsCard {
                    ForEach(items.prefix(8)) { e in
                        InfoRow("clock", e.what, [e.who, e.date.formatted(date: .omitted, time: .shortened)].compactMap { $0 }.joined(separator: " · "))
                    }
                }
            } else if items != nil {
                EmptyCard(symbol: "clock", title: "No door activity today")
            } else { LoadingCard() }
        }
        .task {
            let l: ActivityList? = try? await HubClient.shared.get("/v1/activity", ["category": "doors", "limit": "8"])
            items = l?.items ?? []
        }
    }
}

struct DeviceListView: View {
    @Environment(HomeStore.self) private var store
    let title: String
    let kinds: [DeviceKind]
    var body: some View {
        ScrollView {
            let items = (store.home?.devices ?? []).filter { kinds.contains($0.kind) }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 10)], spacing: 10) {
                ForEach(items) { DeviceTile(device: $0) }
            }
            .padding(.top, 8).screen()
            if items.isEmpty { EmptyCard(symbol: "square.dashed", title: "Nothing here yet", detail: "The hub adds these when their connector is set up.").screen() }
        }
        .background(Backdrop())
        .navigationTitle(title)
    }
}

struct IdentifiedPath: Identifiable { let path: String; var id: String { path } }

/// Event clips come from the hub with the bearer token over the pinned connection, so download first, then play.
struct ClipPlayer: View {
    let path: String
    @State private var player: AVPlayer?
    @State private var failed = false
    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let player { VideoPlayer(player: player).onAppear { player.play() } }
            else if failed { Text("Couldn't load the clip.").foregroundStyle(.white) }
            else { ProgressView().tint(.white) }
        }
        .task {
            do {
                let req = try await HubClient.shared.authorizedRequest(path)
                let (tmp, resp) = try await HubClient.streamSession.download(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else { failed = true; return }
                let dest = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
                try FileManager.default.moveItem(at: tmp, to: dest)
                player = AVPlayer(url: dest)
            } catch { failed = true }
        }
        .onDisappear { player?.pause() }
    }
}

/// A web page the hub or camera server provides (WebRTC player, the show player's 3D page and manager).
/// It trusts the hub's self-signed certificate only when it matches the pinned key.
struct WebPlayer: UIViewRepresentable {
    let url: URL
    var reload = 0
    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        let w = WKWebView(frame: .zero, configuration: cfg)
        w.navigationDelegate = context.coordinator
        w.isOpaque = false; w.backgroundColor = .black; w.scrollView.backgroundColor = .black
        w.scrollView.isScrollEnabled = false
        w.load(URLRequest(url: url))
        context.coordinator.reload = reload
        return w
    }
    func updateUIView(_ w: WKWebView, context: Context) {
        if context.coordinator.reload != reload { context.coordinator.reload = reload; w.load(URLRequest(url: url)) }
    }
    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var reload = 0
        func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                     completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            let (d, c) = HubPinning.evaluate(challenge)
            completionHandler(d, c)
        }
    }
}
