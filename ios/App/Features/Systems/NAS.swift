import SwiftUI

// MARK: - Models (GET /v1/nas, /v1/nas/apps, /v1/nas/apps/{name}, /logs, /v1/nas/jobs/{id})

struct NASOverview: Decodable {
    var system: System
    var pools: [Pool]
    var disks: [Disk]?
    var alerts: [Alert]
    var apps: Counts
    var services: [Service]?
    var truenasUrl: String?

    struct System: Decodable {
        var hostname: String?; var version: String?; var model: String?; var cpu: String?; var cores: Int?
        var memoryBytes: Double?; var ecc: Bool?; var load: [Double]?; var uptimeS: Int?; var osUpdate: String?
    }
    struct Pool: Decodable, Identifiable, Hashable {
        var name: String; var healthy: Bool?; var status: String?; var sizeBytes: Double?; var usedBytes: Double?
        var usedPct: Double?; var fragmentation: Int?; var scrub: Scrub?; var vdevs: [Vdev]?
        var id: String { name }
        struct Scrub: Decodable, Hashable { var state: String; var pct: Double?; var errors: Int?; var ended: Double?; var secsLeft: Double? }
        struct Vdev: Decodable, Hashable { var type: String?; var status: String?; var disks: [VDisk] }
        struct VDisk: Decodable, Hashable { var disk: String?; var status: String?; var errors: Int? }
    }
    struct Disk: Decodable, Hashable { var name: String?; var model: String?; var serial: String?; var sizeBytes: Double?; var type: String?; var pool: String? }
    struct Alert: Decodable, Hashable { var id: String?; var level: String?; var text: String?; var klass: String?; var at: Double? }
    struct Counts: Decodable { var total: Int; var running: Int; var crashed: [String]; var updates: Int }
    struct Service: Decodable, Hashable, Identifiable { var service: String; var title: String?; var running: Bool; var enabled: Bool?; var id: String { service } }
}

struct NASApp: Decodable, Identifiable, Hashable {
    var name: String
    var state: String
    var custom: Bool?
    var train: String?
    var version: String?
    var appVersion: String?
    var latestVersion: String?
    /// "upgrade" (catalog chart), "images" (newer images), or nil.
    var update: String?
    var containers: Int?
    var links: Links?
    /// "hub" for the hub's own app, "remote" for apps that carry remote access.
    var `protected`: String?
    var notes: String?
    var containerDetails: [Container]?
    var volumes: [Volume]?
    var upgrade: Upgrade?
    var id: String { name }

    struct Links: Decodable, Hashable { var lan: String?; var remote: String? }
    struct Container: Decodable, Hashable, Identifiable {
        var id: String; var service: String?; var image: String?; var state: String?; var ports: [Port]?
        struct Port: Decodable, Hashable { var container: Int?; var `protocol`: String?; var host: [Int]? }
    }
    struct Volume: Decodable, Hashable { var source: String?; var destination: String? }
    struct Upgrade: Decodable, Hashable { var to: String?; var toAppVersion: String?; var changelog: String? }

    var running: Bool { state == "RUNNING" }
    var crashed: Bool { state == "CRASHED" || state == "STOPPED_UNEXPECTEDLY" }
    var busy: Bool { state == "DEPLOYING" }
    var isHub: Bool { `protected` == "hub" }
    var stateLabel: String {
        switch state {
        case "RUNNING": "Running"; case "STOPPED": "Stopped"; case "DEPLOYING": "Starting"
        case "CRASHED", "STOPPED_UNEXPECTEDLY": "Crashed"; default: state.capitalized
        }
    }
    var stateColor: Color { running ? .green : crashed ? Theme.crit : busy ? Theme.warn : Theme.dim }
    var updateLine: String? {
        guard let update else { return nil }
        if update == "upgrade", let to = latestVersion { return "\(version ?? "?") → \(to)" }
        return "Newer images"
    }
}

struct NASLogs: Decodable {
    var container: String?
    var containers: [C]
    var lines: [Line]
    struct C: Decodable, Hashable, Identifiable { var id: String; var service: String?; var state: String? }
    struct Line: Decodable, Hashable { var t: String?; var text: String; var level: String? }
}

struct NASJob: Decodable { var id: Int; var state: String?; var pct: Double?; var description: String?; var error: String? }

private struct NASAppList: Decodable { var apps: [NASApp] }

private func bytes(_ v: Double?) -> String {
    guard let v else { return "–" }
    return ByteCountFormatter.string(fromByteCount: Int64(v), countStyle: .binary)
}

private func uptime(_ s: Int?) -> String {
    guard let s else { return "" }
    let d = s / 86400, h = (s % 86400) / 3600, m = (s % 3600) / 60
    return d > 0 ? "\(d) d \(h) h" : "\(h) h \(m) m"
}

// MARK: - Actions

/// Runs a nas.* action: Face ID through the hub's admin tier, the away warning, and job progress.
@MainActor
@Observable
final class NASActions {
    var running: String?
    var progress: Double?
    var awayAsk: Pending?
    struct Pending: Identifiable { let id = UUID(); var action: String; var target: String?; var params: [String: Any]; var reason: String; var message: String }

    func run(_ store: HomeStore, _ action: String, target: String?, params: [String: Any] = [:], reason: String,
             then: (() async -> Void)? = nil) {
        Task {
            running = target ?? action; progress = nil
            defer { running = nil; progress = nil }
            do {
                let r = try await HubClient.shared.action(action, target: target, params: params, confirm: reason)
                if r.dryRun == true { store.show("Practice mode", r.message ?? "Nothing was changed"); return }
                store.show(r.message ?? "Done")
                if let job = r.job { await follow(job, store) }
                await then?()
            } catch HubClient.HubError.cancelled {
            } catch HubClient.HubError.server(409, let msg) where msg.contains("away from home") {
                awayAsk = Pending(action: action, target: target, params: params.merging(["away_ok": true]) { $1 },
                                  reason: reason, message: msg)
            } catch {
                store.show(error.localizedDescription, isError: true)
            }
        }
    }

    private func follow(_ job: Int, _ store: HomeStore) async {
        for _ in 0..<180 {
            try? await Task.sleep(for: .seconds(2))
            guard let j = try? await HubClient.shared.get("/v1/nas/jobs/\(job)", as: NASJob.self) else { return }
            progress = j.pct.map { $0 / 100 }
            switch j.state {
            case "SUCCESS": return
            case "FAILED", "ABORTED": store.show("It didn't work", j.error ?? "TrueNAS stopped the job", isError: true); return
            default: continue
            }
        }
    }
}

// MARK: - Overview

/// Systems → NAS: the server, its pools and drives, alerts and the apps that need a look.
struct NASView: View {
    @Environment(HomeStore.self) private var store
    @State private var o: NASOverview?
    @State private var failed: String?
    @State private var bay: String?
    @State private var acts = NASActions()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let o {
                    ServerStage(o: o, selected: $bay)
                    headline(o)
                    SectionHeader("Storage")
                    ForEach(o.pools) { PoolCard(pool: $0, acts: acts, reload: load) }
                    SectionHeader("Apps")
                    HStack(spacing: 10) {
                        NavigationLink(value: Destination.nasApps("running")) { stat("\(o.apps.running)", "running", Theme.fg) }
                        NavigationLink(value: Destination.nasApps("problems")) { stat("\(o.apps.crashed.count)", "crashed", o.apps.crashed.isEmpty ? Theme.fg : Theme.crit) }
                        NavigationLink(value: Destination.nasUpdates) { stat("\(o.apps.updates)", "updates", o.apps.updates > 0 ? Theme.warn : Theme.fg) }
                    }
                    .buttonStyle(.plain)
                    if !o.apps.crashed.isEmpty {
                        RowsCard {
                            ForEach(o.apps.crashed, id: \.self) { n in
                                NavigationLink(value: Destination.nasApp(n)) {
                                    InfoRow("exclamationmark.octagon.fill", color: Theme.crit, n, "Crashed · tap for logs and restart", chevron: true)
                                }
                            }
                        }
                        .buttonStyle(.plain)
                    }
                    if !o.alerts.isEmpty {
                        SectionHeader("Alerts")
                        VStack(alignment: .leading, spacing: 12) {
                            ForEach(Array(o.alerts.enumerated()), id: \.offset) { _, a in AlertRow(alert: a, acts: acts, reload: load) }
                        }
                        .card(padding: 14)
                    }
                    SectionHeader("Manage")
                    RowsCard {
                        NavigationLink(value: Destination.nasApps(nil)) { InfoRow("square.grid.2x2.fill", "All apps", "\(o.apps.total) on the NAS", chevron: true) }
                        NavigationLink(value: Destination.nasUpdates) { InfoRow("arrow.down.circle.fill", color: Theme.warn, "Updates", o.apps.updates > 0 ? "\(o.apps.updates) waiting" : "Everything is current", chevron: true) }
                        NavigationLink(value: Destination.nasSettings) { InfoRow("slider.horizontal.3", "NAS settings", "Scrubs, snapshots, sharing", chevron: true) }
                        if let u = o.truenasUrl.flatMap(URL.init(string:)) {
                            Link(destination: u) { InfoRow("safari", "Open TrueNAS", store.me?.atHome == true ? nil : "Home Wi-Fi or VPN", chevron: true) }
                        }
                    }
                    .buttonStyle(.plain)
                } else if let failed {
                    ContentUnavailableView("Can't read the NAS", systemImage: "server.rack", description: Text(failed))
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(o?.system.hostname ?? "NAS")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .nasAwayAsk(acts, store)
    }

    private func load() async {
        do { o = try await HubClient.shared.get("/v1/nas", as: NASOverview.self, timeout: NASTimeout.slow); failed = nil }
        catch { failed = error.localizedDescription }
    }

    private func headline(_ o: NASOverview) -> some View {
        let unhealthy = o.pools.filter { $0.healthy == false }
        let crit = o.alerts.contains { $0.level == "CRITICAL" }
        let ok = unhealthy.isEmpty && o.apps.crashed.isEmpty && !crit
        return VStack(alignment: .leading, spacing: 4) {
            Pill(text: ok ? "All good" : "Needs a look", color: ok ? Theme.accent : (unhealthy.isEmpty ? Theme.warn : Theme.crit))
            Text(!unhealthy.isEmpty ? "\(unhealthy.map(\.name).joined(separator: ", ")) is degraded"
                 : o.apps.crashed.isEmpty ? "Pools healthy, apps running"
                 : "Pools healthy, \(o.apps.crashed.count) app\(o.apps.crashed.count == 1 ? "" : "s") crashed")
                .font(.title2.weight(.bold)).foregroundStyle(Theme.fg)
            Text(["TrueNAS \(o.system.version ?? "")", o.system.osUpdate.map { "\($0) available" }, "up \(uptime(o.system.uptimeS))"]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.footnote).foregroundStyle(Theme.dim)
        }
        .padding(.horizontal, 2)
    }

    private func stat(_ n: String, _ label: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(n).font(.title2.weight(.bold)).monospacedDigit().foregroundStyle(color)
            Text(label).font(.footnote).foregroundStyle(Theme.dim)
        }
        .card(padding: 12)
    }
}

/// The server's front panel: one bay per drive, lit green, amber with errors, flickering while a scrub runs.
struct ServerStage: View {
    let o: NASOverview
    @Binding var selected: String?
    @State private var blink = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    struct Bay: Hashable { var name: String; var pool: String?; var errors: Int; var ssd: Bool }

    private var bays: [Bay] {
        var errs: [String: Int] = [:]
        for p in o.pools { for v in p.vdevs ?? [] { for d in v.disks { if let n = d.disk { errs[n] = d.errors ?? 0 } } } }
        let pooled = (o.disks ?? []).filter { errs[$0.name ?? ""] != nil }
        return pooled.sorted { ($0.sizeBytes ?? 0, $0.name ?? "") > ($1.sizeBytes ?? 0, $1.name ?? "") }
            .map { Bay(name: $0.name ?? "?", pool: $0.pool, errors: errs[$0.name ?? ""] ?? 0, ssd: $0.type == "SSD" || ($0.sizeBytes ?? 0) < 4e12) }
    }

    private func scrubbing(_ pool: String?) -> Bool {
        o.pools.first { $0.name == pool }?.scrub?.state == "running"
    }

    var body: some View {
        let list = bays
        let hdd = list.filter { !$0.ssd }, ssd = list.filter { $0.ssd }
        ZStack {
            RadialGradient(colors: [Theme.card2, Theme.bg], center: .center, startRadius: 10, endRadius: 260)
            VStack(spacing: 6) {
                HStack {
                    Text((o.system.model ?? "SERVER").uppercased())
                    Spacer()
                    Text((o.system.hostname ?? "").uppercased())
                }
                .font(.system(size: 9, weight: .semibold, design: .rounded)).tracking(1.6).foregroundStyle(.white.opacity(0.45))
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 5), count: 4), spacing: 5) {
                    ForEach(hdd, id: \.name) { b in bayView(b, height: 30) }
                }
                if !ssd.isEmpty {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 5), count: 4), spacing: 5) {
                        ForEach(ssd, id: \.name) { b in bayView(b, height: 20) }
                    }
                }
            }
            .padding(12)
            .frame(width: 210)
            .background(LinearGradient(colors: [Color(white: 0.24), Color(white: 0.15)], startPoint: .top, endPoint: .bottom),
                        in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .shadow(color: .black.opacity(0.4), radius: 18, y: 14)
            VStack {
                HStack(alignment: .top) {
                    hud("CPU load", o.system.load?.first.map { String(format: "%.1f", $0) } ?? "–", "\(o.system.cores ?? 0) threads")
                    Spacer()
                    hud("Memory", bytes(o.system.memoryBytes), o.system.ecc == true ? "ECC" : nil)
                }
                Spacer()
                HStack {
                    if let sel = selected, let d = o.disks?.first(where: { $0.name == sel }) {
                        hud(d.model ?? sel, d.pool ?? "", "\(d.serial ?? "") · \(bytes(d.sizeBytes))")
                    } else {
                        hud("Drives", "\(hdd.count) + \(ssd.count)", "Tap a bay")
                    }
                    Spacer()
                }
            }
            .padding(12)
        }
        .frame(height: 280)
        .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        .onAppear { if !reduceMotion { withAnimation(.easeInOut(duration: 0.35).repeatForever()) { blink = true } } }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Server front panel with \(list.count) drives")
    }

    private func bayView(_ b: Bay, height: CGFloat) -> some View {
        let color: Color = b.errors > 0 ? .orange : .green
        let flicker = scrubbing(b.pool) && blink
        return Button { selected = selected == b.name ? nil : b.name } label: {
            ZStack(alignment: .bottomLeading) {
                RoundedRectangle(cornerRadius: 4).fill(Color(white: 0.09))
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(selected == b.name ? Theme.accent : .white.opacity(0.06), lineWidth: selected == b.name ? 2 : 1))
                Circle().fill(color).frame(width: 5, height: 5).shadow(color: color, radius: 3)
                    .opacity(flicker ? 0.3 : 1).padding(4)
                Text(b.name).font(.system(size: 7, design: .monospaced)).foregroundStyle(.white.opacity(0.35))
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing).padding(3)
            }
            .frame(height: height)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(b.name), \(b.pool ?? "no pool"), \(b.errors) errors")
    }

    private func hud(_ title: String, _ value: String, _ sub: String?) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title).font(.caption2.weight(.semibold)).foregroundStyle(Theme.dim)
            Text(value).font(.footnote.weight(.bold)).monospacedDigit().foregroundStyle(Theme.fg)
            if let sub { Text(sub).font(.caption2).foregroundStyle(Theme.dim).lineLimit(1) }
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

private struct PoolCard: View {
    @Environment(HomeStore.self) private var store
    let pool: NASOverview.Pool
    let acts: NASActions
    let reload: () async -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(pool.name).font(.headline)
                if pool.healthy == false { Pill(text: pool.status ?? "Degraded", color: Theme.crit) }
                Spacer()
                Text("\(bytes(pool.usedBytes)) of \(bytes(pool.sizeBytes))").font(.caption).foregroundStyle(Theme.dim)
            }
            ProgressBar(value: (pool.usedPct ?? 0) / 100, color: (pool.usedPct ?? 0) > 85 ? Theme.warn : Theme.accent)
            if let s = pool.scrub {
                HStack {
                    Text(scrubLine(s)).font(.caption).foregroundStyle(s.state == "paused" ? Theme.warn : Theme.dim)
                    Spacer()
                    if s.state == "paused" || s.state == "running" {
                        Button(s.state == "paused" ? "Resume" : "Pause") {
                            acts.run(store, "nas.scrub", target: pool.name, params: ["op": s.state == "paused" ? "start" : "pause"],
                                     reason: "\(s.state == "paused" ? "Resume" : "Pause") the \(pool.name) scrub") { await reload() }
                        }
                        .font(.caption.weight(.semibold))
                    }
                }
            }
        }
        .card(padding: 14)
    }

    private func scrubLine(_ s: NASOverview.Pool.Scrub) -> String {
        let pct = s.pct.map { "\(Int($0))%" } ?? ""
        switch s.state {
        case "paused": return "Scrub paused at \(pct)"
        case "running":
            let left = s.secsLeft.map { " · about \(Duration.seconds($0).formatted(.units(allowed: [.days, .hours], width: .wide, maximumUnitCount: 1))) left" } ?? ""
            return "Scrubbing \(pct)\(left)"
        case "finished":
            let when = s.ended.map { " " + Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .omitted) } ?? ""
            return "Scrubbed\(when) · \(s.errors ?? 0) errors"
        default: return "No scrub yet"
        }
    }
}

private struct AlertRow: View {
    @Environment(HomeStore.self) private var store
    let alert: NASOverview.Alert
    let acts: NASActions
    let reload: () async -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 2).fill(alert.level == "CRITICAL" ? Theme.crit : alert.level == "WARNING" ? Theme.warn : Theme.accent)
                .frame(width: 4)
            VStack(alignment: .leading, spacing: 6) {
                Text(plain(alert.text ?? "")).font(.subheadline).foregroundStyle(Theme.fg)
                if let id = alert.id {
                    Button("Dismiss") {
                        acts.run(store, "nas.alert", target: id, reason: "Dismiss this NAS alert") { await reload() }
                    }
                    .font(.footnote.weight(.semibold))
                }
            }
        }
    }

    /// TrueNAS formats some alerts with <br> and links.
    private func plain(_ s: String) -> String {
        s.replacingOccurrences(of: "<br>", with: "\n").replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression)
    }
}

// MARK: - Apps

struct NASAppsView: View {
    @State var filter: String?
    @State private var apps: [NASApp]?
    @State private var q = ""
    @State private var failed: String?

    private struct Filter: Hashable { var key: String?; var title: String }
    private let filters = [Filter(key: nil, title: "All"), Filter(key: "problems", title: "Problems"), Filter(key: "updates", title: "Updates"),
                           Filter(key: "running", title: "Running"), Filter(key: "stopped", title: "Stopped")]

    var body: some View {
        List {
            Section {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach(filters, id: \.self) { f in ChipButton(title: f.title, active: filter == f.key) { filter = f.key } }
                    }
                }
                .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
            }
            if let apps {
                let shown = visible(apps)
                Section("\(shown.count) shown") {
                    ForEach(shown) { a in NavigationLink(value: Destination.nasApp(a.name)) { NASAppRow(app: a) } }
                }
            } else if let failed {
                Text(failed).foregroundStyle(Theme.dim)
            } else { ProgressView().frame(maxWidth: .infinity) }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .searchable(text: $q, prompt: "Search apps")
        .navigationTitle("Apps")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        do { apps = (try await HubClient.shared.get("/v1/nas/apps", as: NASAppList.self, timeout: NASTimeout.slow)).apps; failed = nil }
        catch { failed = error.localizedDescription }
    }

    private func visible(_ all: [NASApp]) -> [NASApp] {
        let rank: (NASApp) -> Int = { $0.crashed ? 0 : $0.busy ? 1 : $0.running ? 2 : 3 }
        return all.filter { a in
            (q.isEmpty || a.name.localizedCaseInsensitiveContains(q)) && {
                switch filter {
                case "problems": a.crashed || a.busy
                case "updates": a.update != nil
                case "running": a.running
                case "stopped": a.state == "STOPPED"
                default: true
                }
            }()
        }
        .sorted { (rank($0), $0.name) < (rank($1), $1.name) }
    }
}

struct NASAppRow: View {
    let app: NASApp
    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(app.stateColor).frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 1) {
                Text(app.name).font(.body.weight(.medium)).lineLimit(1)
                Text([app.stateLabel, app.custom == true ? "custom" : nil, app.isHub ? "this hub" : nil].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.dim)
            }
            Spacer()
            if app.update != nil { Pill(text: "Update", color: Theme.warn) }
        }
    }
}

struct NASAppView: View {
    @Environment(HomeStore.self) private var store
    let name: String
    @State private var app: NASApp?
    @State private var failed: String?
    @State private var asking: Op?
    @State private var acts = NASActions()

    enum Op: String, Identifiable { case start, stop, restart, update; var id: String { rawValue } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let a = app {
                    header(a)
                    if a.isHub {
                        note("This is the hub Thomas Creek talks to. Restarting or updating it drops the app for about a minute; it reconnects on its own. It can't be stopped from here.")
                    } else if a.protected == "remote" && store.me?.atHome != true {
                        note("You're away from home. \(a.name) carries remote access, so stopping or restarting it may cut this phone off.")
                    }
                    controls(a)
                    if a.update != nil || a.isHub { updateCard(a) }
                    if let cs = a.containerDetails, !cs.isEmpty {
                        SectionHeader("Containers")
                        RowsCard {
                            ForEach(cs) { c in
                                NavigationLink(value: Destination.nasLogs(a.name, c.id)) {
                                    InfoRow(symbol: c.state == "running" ? "shippingbox.fill" : "shippingbox", color: c.state == "running" ? Theme.accent : Theme.dim,
                                            title: c.service ?? c.id, subtitle: [c.state, c.image].compactMap { $0 }.joined(separator: " · "), chevron: true) {
                                        Text("Logs").font(.footnote.weight(.semibold)).foregroundStyle(Theme.accent)
                                    }
                                }
                            }
                        }
                        .buttonStyle(.plain)
                    }
                    if let v = a.volumes, !v.isEmpty {
                        SectionHeader("Storage")
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(v, id: \.self) { m in
                                Text(m.source ?? "").font(.caption.monospaced()).foregroundStyle(Theme.dim).lineLimit(2)
                            }
                        }
                        .card(padding: 14)
                    }
                } else if let failed {
                    ContentUnavailableView("Can't read \(name)", systemImage: "shippingbox", description: Text(failed))
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog(asking.map { title($0) } ?? "", isPresented: Binding(get: { asking != nil }, set: { if !$0 { asking = nil } }),
                            titleVisibility: .visible, presenting: asking) { op in
            Button(op.rawValue.capitalized, role: op == .stop ? .destructive : nil) { act(op) }
        } message: { op in Text(detail(op)) }
        .nasAwayAsk(acts, store)
    }

    private func load() async {
        do { app = try await HubClient.shared.get("/v1/nas/apps/\(name)", as: NASApp.self, timeout: NASTimeout.slow); failed = nil }
        catch { failed = error.localizedDescription }
    }

    private func header(_ a: NASApp) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Pill(text: a.stateLabel, color: a.stateColor)
                if acts.running != nil { ProgressView().controlSize(.small) }
            }
            Text(a.name).font(.largeTitle.weight(.bold)).foregroundStyle(Theme.fg).lineLimit(1).minimumScaleFactor(0.6)
            Text(a.custom == true ? "Custom app" : "\(a.train ?? "catalog") · \(a.appVersion ?? a.version ?? "")")
                .font(.footnote).foregroundStyle(Theme.dim)
            if let p = acts.progress { ProgressBar(value: p) }
        }
        .padding(.horizontal, 2)
    }

    private func controls(_ a: NASApp) -> some View {
        let away = store.me?.atHome != true
        let link = (away ? a.links?.remote : nil) ?? a.links?.lan
        return VStack(alignment: .leading, spacing: 10) {
            if let link, let url = URL(string: link) {
                Link(destination: url) {
                    Label("Open web page", systemImage: "arrow.up.right.square").font(.headline).frame(maxWidth: .infinity, minHeight: 46)
                }
                .buttonStyle(.borderedProminent).tint(Theme.accent).buttonBorderShape(.roundedRectangle(radius: 14))
                Text(away && a.links?.remote == nil ? "\(url.host() ?? link) · needs home Wi-Fi or the VPN" : (url.host() ?? link))
                    .font(.caption).foregroundStyle(Theme.dim)
            } else {
                Text("This app has no web page.").font(.footnote).foregroundStyle(Theme.dim)
            }
            HStack(spacing: 8) {
                if a.running || a.busy || a.crashed {
                    Button { asking = .restart } label: { Text("Restart").frame(maxWidth: .infinity, minHeight: 40) }.buttonStyle(.bordered)
                    if !a.isHub {
                        Button(role: .destructive) { asking = .stop } label: { Text("Stop").frame(maxWidth: .infinity, minHeight: 40) }.buttonStyle(.bordered)
                    }
                } else {
                    Button { asking = .start } label: { Text("Start").frame(maxWidth: .infinity, minHeight: 40) }.buttonStyle(.borderedProminent).tint(Theme.accent)
                }
            }
            .disabled(acts.running != nil)
            if a.crashed, let first = a.containerDetails?.first {
                NavigationLink(value: Destination.nasLogs(a.name, first.id)) { Label("See why it crashed", systemImage: "text.alignleft") }
                    .font(.subheadline.weight(.semibold))
            }
        }
        .card(padding: 14)
    }

    private func updateCard(_ a: NASApp) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(a.isHub ? "Rebuild the hub" : "Update").font(.headline)
            if a.isHub {
                Text("Redeploys the hub from its branch on GitHub. Use it after a change has merged.").font(.footnote).foregroundStyle(Theme.dim)
            } else if let line = a.updateLine {
                Text(line).font(.subheadline.monospacedDigit())
                Text(a.update == "upgrade" ? "TrueNAS snapshots the app's storage first, so you can roll back." : "Pulls the newer images and redeploys. Settings and data stay.")
                    .font(.footnote).foregroundStyle(Theme.dim)
            }
            if let log = a.upgrade?.changelog, !log.isEmpty {
                DisclosureGroup("What's new") {
                    Text(log.replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression))
                        .font(.caption).foregroundStyle(Theme.dim).frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.subheadline)
            }
            Button { asking = .update } label: { Text(a.isHub ? "Rebuild hub" : "Update \(a.name)").frame(maxWidth: .infinity, minHeight: 40) }
                .buttonStyle(.borderedProminent).tint(Theme.warn).disabled(acts.running != nil)
        }
        .card(padding: 14)
    }

    private func note(_ s: String) -> some View {
        Text(s).font(.footnote).foregroundStyle(Theme.fg).card(padding: 12, color: Theme.warn.opacity(0.14))
    }

    private func title(_ op: Op) -> String { "\(op.rawValue.capitalized) \(name)?" }

    private func detail(_ op: Op) -> String {
        let hub = app?.isHub == true
        switch op {
        case .start: return "Brings its containers up."
        case .stop: return "Its web page and anything that uses it stop until you start it again."
        case .restart: return hub ? "Thomas Creek loses the hub for about a minute, then reconnects." : "Stops and starts its containers. Takes 10 to 60 seconds."
        case .update: return hub ? "The hub rebuilds and restarts. Thomas Creek reconnects in a minute or two." : (app?.updateLine ?? "Updates to the newest version.")
        }
    }

    private func act(_ op: Op) {
        acts.run(store, "nas.app", target: name, params: ["op": op.rawValue], reason: "\(op.rawValue.capitalized) \(name)") { await load() }
    }
}

// MARK: - Logs

struct NASLogsView: View {
    let app: String
    @State var container: String?
    @State private var logs: NASLogs?
    @State private var failed: String?
    @State private var live = true
    @State private var errorsOnly = false

    var body: some View {
        VStack(spacing: 10) {
            if let cs = logs?.containers, cs.count > 1 {
                Picker("Container", selection: Binding(get: { container ?? logs?.container ?? "" }, set: { container = $0 })) {
                    ForEach(cs) { c in Text(c.service ?? c.id).tag(c.id) }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
            }
            HStack {
                Toggle("Live", isOn: $live).toggleStyle(.button)
                Toggle("Errors only", isOn: $errorsOnly).toggleStyle(.button)
                Spacer()
                if let logs { ShareLink(item: logs.lines.map(\.text).joined(separator: "\n")) { Image(systemName: "square.and.arrow.up") } }
            }
            .font(.subheadline.weight(.semibold))
            .padding(.horizontal, 16)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        if let logs {
                            let lines = errorsOnly ? logs.lines.filter { color($0) != nil } : logs.lines
                            if lines.isEmpty { Text(errorsOnly ? "No errors or warnings." : "Nothing logged yet.").foregroundStyle(.gray) }
                            ForEach(Array(lines.enumerated()), id: \.offset) { i, l in
                                Text(l.text).foregroundStyle(color(l) ?? Color(white: 0.82)).id(i)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            Color.clear.frame(height: 1).id("end")
                        } else if let failed {
                            Text(failed).foregroundStyle(.gray)
                        } else { ProgressView().tint(.white).frame(maxWidth: .infinity) }
                    }
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(12)
                }
                .background(Color(white: 0.05), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .padding(.horizontal, 12)
                .onChange(of: logs?.lines.count) { proxy.scrollTo("end", anchor: .bottom) }
            }
        }
        .padding(.bottom, 12)
        .background(Backdrop())
        .navigationTitle("\(app) logs")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: "\(container ?? "-")|\(live)") {
            await load()
            while live && !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if live { await load() }
            }
        }
    }

    private func load() async {
        var q = ["tail": "400"]
        if let container { q["container"] = container }
        do { logs = try await HubClient.shared.get("/v1/nas/apps/\(app)/logs", q, as: NASLogs.self, timeout: NASTimeout.slow); failed = nil }
        catch HubClient.HubError.cancelled {}
        catch { failed = error.localizedDescription }
    }

    /// The hub strips terminal colour codes and sends the level they meant (or the line's ERROR/WARN words).
    private func color(_ l: NASLogs.Line) -> Color? {
        switch l.level ?? level(l.text) {
        case "error": Color(red: 1, green: 0.54, blue: 0.5)
        case "warn": Color(red: 0.96, green: 0.76, blue: 0.42)
        default: nil
        }
    }

    /// Older hubs send no level: guess it from the words.
    private func level(_ s: String) -> String? {
        let u = s.uppercased()
        if u.contains("ERROR") || u.contains("FATAL") || u.contains("PANIC") || u.contains("TRACEBACK") || u.contains("EXCEPTION") { return "error" }
        if u.contains("WARN") { return "warn" }
        return nil
    }
}

// MARK: - Updates

struct NASUpdatesView: View {
    @Environment(HomeStore.self) private var store
    @State private var apps: [NASApp]?
    @State private var confirmAll = false
    @State private var acts = NASActions()

    var body: some View {
        List {
            if let apps {
                let ups = apps.filter { $0.update != nil }
                let batch = ups.filter { $0.protected == nil }
                if ups.isEmpty {
                    ContentUnavailableView("Everything is up to date", systemImage: "checkmark.seal")
                } else {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("\(ups.count) app\(ups.count == 1 ? "" : "s") can update").font(.headline)
                            Text("One at a time, each finishing before the next starts. You'll get a notification at the end."
                                 + (ups.count > batch.count ? " Skipped: \(ups.filter { $0.protected != nil }.map(\.name).joined(separator: ", ")); update those by themselves." : ""))
                                .font(.footnote).foregroundStyle(Theme.dim)
                            Button { confirmAll = true } label: { Text("Update \(batch.count) apps").frame(maxWidth: .infinity, minHeight: 40) }
                                .buttonStyle(.borderedProminent).tint(Theme.warn).disabled(batch.isEmpty || acts.running != nil)
                        }
                        .padding(.vertical, 4)
                    }
                    let catalog = ups.filter { $0.update == "upgrade" }, images = ups.filter { $0.update != "upgrade" }
                    if !catalog.isEmpty {
                        Section("Catalog apps · \(catalog.count)") { ForEach(catalog) { row($0) } }
                    }
                    if !images.isEmpty {
                        Section("Newer images · \(images.count)") { ForEach(images) { row($0) } }
                    }
                }
                Section {
                    Text("TrueNAS's own updates restart the NAS and the hub, so they stay in TrueNAS.").font(.footnote).foregroundStyle(Theme.dim)
                }
            } else { ProgressView().frame(maxWidth: .infinity) }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Updates")
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog("Update all apps?", isPresented: $confirmAll, titleVisibility: .visible) {
            Button("Update all") { acts.run(store, "nas.update_all", target: nil, reason: "Update the NAS apps") { await load() } }
        } message: { Text("Each app restarts while it updates. This can take a while.") }
    }

    private func row(_ a: NASApp) -> some View {
        NavigationLink(value: Destination.nasApp(a.name)) {
            HStack {
                NASAppRow(app: a)
                Text(a.updateLine ?? "").font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
            }
        }
    }

    private func load() async { apps = (try? await HubClient.shared.get("/v1/nas/apps", as: NASAppList.self, timeout: NASTimeout.slow))?.apps }
}

// MARK: - Settings

struct NASSettingsView: View {
    @Environment(HomeStore.self) private var store
    @State private var o: NASOverview?
    @State private var dataset = "SSDs/Applications"
    @State private var acts = NASActions()

    var body: some View {
        List {
            if let o {
                Section("Pools") {
                    ForEach(o.pools) { p in
                        HStack {
                            VStack(alignment: .leading) {
                                Text("\(p.name) scrub")
                                Text(p.scrub.map { "\($0.state.capitalized)\($0.pct.map { " · \(Int($0))%" } ?? "")" } ?? "None yet")
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                            let st = p.scrub?.state
                            Button(st == "running" ? "Pause" : st == "paused" ? "Resume" : "Start") {
                                acts.run(store, "nas.scrub", target: p.name, params: ["op": st == "running" ? "pause" : "start"],
                                         reason: "\(st == "running" ? "Pause" : "Start") the \(p.name) scrub") { await load() }
                            }
                        }
                    }
                }
                Section {
                    TextField("Dataset", text: $dataset).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("Take a snapshot now") {
                        acts.run(store, "nas.snapshot", target: dataset, reason: "Snapshot \(dataset)")
                    }
                    .disabled(dataset.isEmpty)
                } header: { Text("Snapshot") } footer: { Text("Handy before a risky update. TrueNAS's snapshot tasks still decide how long they're kept.") }
                if let svcs = o.services, !svcs.isEmpty {
                    Section("Sharing") {
                        ForEach(svcs) { s in
                            Toggle(isOn: Binding(get: { s.running }, set: { on in
                                acts.run(store, "nas.service", target: s.service, params: ["on": on],
                                         reason: "Turn \(s.title ?? s.service) \(on ? "on" : "off")") { await load() }
                            })) {
                                VStack(alignment: .leading) {
                                    Text(s.title ?? s.service)
                                    Text(s.enabled == true ? "Starts with the NAS" : "Doesn't start with the NAS").font(.caption).foregroundStyle(Theme.dim)
                                }
                            }
                        }
                    }
                }
                Section {
                    Text("Deleting apps, datasets and pools, editing compose files, users, networking, OS updates and reboots stay in TrueNAS.")
                        .font(.footnote).foregroundStyle(Theme.dim)
                    if let u = o.truenasUrl.flatMap(URL.init(string:)) { Link("Open TrueNAS", destination: u) }
                }
            } else { ProgressView().frame(maxWidth: .infinity) }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("NAS settings")
        .task { await load() }
        .refreshable { await load() }
        .nasAwayAsk(acts, store)
    }

    private func load() async { o = try? await HubClient.shared.get("/v1/nas", as: NASOverview.self, timeout: NASTimeout.slow) }
}

// MARK: - Away warning

private struct NASAwayAsk: ViewModifier {
    @Bindable var acts: NASActions
    let store: HomeStore
    func body(content: Content) -> some View {
        content.alert("You're away from home", isPresented: Binding(get: { acts.awayAsk != nil }, set: { if !$0 { acts.awayAsk = nil } }),
                      presenting: acts.awayAsk) { p in
            Button("Do it anyway", role: .destructive) { acts.run(store, p.action, target: p.target, params: p.params, reason: p.reason) }
            Button("Cancel", role: .cancel) {}
        } message: { p in Text(p.message) }
    }
}

extension View {
    func nasAwayAsk(_ acts: NASActions, _ store: HomeStore) -> some View { modifier(NASAwayAsk(acts: acts, store: store)) }
}

/// TrueNAS answers slowly (the overview asks it for pools, disks, alerts and apps), so its screens wait longer
/// than the usual 10 seconds before saying home can't be reached.
enum NASTimeout { static let slow: TimeInterval = 45 }
