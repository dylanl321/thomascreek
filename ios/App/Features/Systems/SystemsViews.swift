import SwiftUI

/// Dylan's view of the plumbing: storage, apps, network, pipeline and backups as health cards.
struct SystemsView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        let s = store.home?.systems
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let s {
                    SystemsStrip(systems: s)
                    if let apps = s.apps, apps.crashed > 0 {
                        Button { router.go(.storage) } label: {
                            HStack(spacing: 12) {
                                IconBadge(symbol: "exclamationmark.octagon.fill", color: Theme.crit, size: 38)
                                VStack(alignment: .leading) {
                                    Text("\(apps.crashed) app\(apps.crashed == 1 ? "" : "s") crashed on the NAS").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                                    if let n = apps.crashedNames { Text(n.joined(separator: ", ")).font(.caption).foregroundStyle(Theme.dim) }
                                }
                                Spacer()
                                Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
                            }
                            .card(color: Theme.crit.opacity(0.12))
                        }
                        .buttonStyle(.plain)
                    }
                    if let b = s.backups {
                        InfoRow(b.ok ? "externaldrive.badge.checkmark" : "externaldrive.badge.exclamationmark", color: b.ok ? Theme.accent : Theme.crit,
                                b.ok ? "Backups are current" : "A backup failed", b.last.map { "Last \(clock($0))" }).card(padding: 12)
                    }
                } else { LoadingCard() }
                SectionHeader("Manage")
                RowsCard {
                    NavigationLink(value: Destination.storage) { InfoRow("internaldrive.fill", "Storage and apps", nil, chevron: true) }
                    NavigationLink(value: Destination.pipeline) { InfoRow("arrow.down.circle.fill", "Media pipeline", nil, chevron: true) }
                    NavigationLink(value: Destination.kidNet) { InfoRow("wifi", "Network and device time", nil, chevron: true) }
                    NavigationLink(value: Destination.activity) { InfoRow("clock.arrow.circlepath", "Activity log", nil, chevron: true) }
                    NavigationLink(value: Destination.people) { InfoRow("person.2.badge.key.fill", "People and permissions", nil, chevron: true) }
                }
                .buttonStyle(.plain)
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Systems")
        .topBar()
        .refreshable { await store.loadHome() }
    }
}

struct StorageView: View {
    @State private var detail: Detail?
    struct Detail: Decodable {
        var pools: [Systems.Pool]
        var apps: [AppRow]
        struct AppRow: Decodable, Identifiable, Hashable { var name: String; var state: String; var update: Bool?; var id: String { name } }
    }

    var body: some View {
        List {
            if let d = detail {
                Section("Pools") {
                    ForEach(d.pools) { p in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(p.name).font(.headline)
                                Spacer()
                                Text("\(Int(p.usedPct))% of \(p.sizeTb.map { "\(Int($0)) TB" } ?? "")").font(.caption).foregroundStyle(Theme.dim)
                            }
                            ProgressBar(value: p.usedPct / 100, color: p.usedPct > 85 ? Theme.warn : Theme.accent)
                            if let s = p.scrubPct { Text("Scrub \(Int(s))% done").font(.caption).foregroundStyle(Theme.dim) }
                        }
                    }
                }
                Section("Apps") {
                    ForEach(d.apps.sorted { ($0.state == "running" ? 1 : 0, $0.name) < ($1.state == "running" ? 1 : 0, $1.name) }) { a in
                        HStack {
                            Circle().fill(a.state == "running" ? .green : a.state == "stopped" ? Theme.dim : Theme.crit).frame(width: 9, height: 9)
                            Text(a.name)
                            Spacer()
                            if a.update == true { Pill(text: "Update", color: Theme.warn) }
                            Text(a.state).font(.caption).foregroundStyle(Theme.dim)
                        }
                    }
                }
                Section {
                    Text("Restarting apps and changing storage stay in TrueNAS on purpose.").font(.footnote).foregroundStyle(Theme.dim)
                }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Storage and apps")
        .task { detail = try? await HubClient.shared.get("/v1/systems") }
    }
}

struct ActivityView: View {
    @State private var items: [ActivityEntry] = []
    @State private var before: String?
    @State private var done = false
    @State private var filter = "all"

    var body: some View {
        List {
            Picker("Show", selection: $filter) {
                Text("All").tag("all")
                Text("People").tag("people")
                Text("Automations").tag("automation")
                Text("Doors").tag("doors")
            }
            .pickerStyle(.segmented)
            ForEach(items) { e in
                VStack(alignment: .leading, spacing: 3) {
                    Text(e.what).font(.subheadline.weight(.medium))
                    Text([e.who, e.source, e.outcome == "ok" ? nil : e.outcome].compactMap { $0 }.joined(separator: " · ")).font(.caption)
                        .foregroundStyle(e.outcome == "denied" || e.outcome == "error" ? Theme.warn : Theme.dim)
                    Text(e.date.formatted(date: .abbreviated, time: .shortened)).font(.caption2).foregroundStyle(Theme.dim)
                }
                .onAppear { if e == items.last, !done { Task { await load() } } }
            }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Activity")
        .task(id: filter) { items = []; before = nil; done = false; await load() }
    }

    private func load() async {
        var q = ["limit": "50"]
        if filter != "all" { q["category"] = filter }
        if let before { q["before"] = before }
        guard let l: ActivityList = try? await HubClient.shared.get("/v1/activity", q) else { return }
        items += l.items
        before = l.before
        done = l.before == nil || l.items.isEmpty
    }
}

/// People, their devices, new-device approvals and revoking a lost phone.
struct PeopleView: View {
    @Environment(HomeStore.self) private var store
    @State private var data: PeopleList?

    var body: some View {
        List {
            if let data {
                let pending = data.unassigned ?? []
                if !pending.isEmpty {
                    Section("New devices") {
                        ForEach(pending) { d in
                            VStack(alignment: .leading, spacing: 8) {
                                Text("\(d.name) joined").font(.headline)
                                Text(d.model ?? "").font(.caption).foregroundStyle(Theme.dim)
                                ScrollView(.horizontal, showsIndicators: false) {
                                    HStack {
                                        ForEach(data.people) { p in
                                            ChipButton(title: p.name) { assign(d, to: p.person) }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                ForEach(data.people) { p in
                    Section {
                        ForEach(p.devices ?? []) { d in
                            HStack {
                                Image(systemName: d.status == "revoked" ? "iphone.slash" : "iphone")
                                VStack(alignment: .leading) {
                                    Text(d.name)
                                    if let s = d.lastSeen { Text("Seen \(clock(s))").font(.caption).foregroundStyle(Theme.dim) }
                                }
                                Spacer()
                                if d.id == AppSettings.deviceID { Pill(text: "This phone") }
                            }
                            .swipeActions {
                                if d.status != "revoked" && d.id != AppSettings.deviceID {
                                    Button("Revoke", role: .destructive) { revoke(d) }
                                }
                            }
                        }
                    } header: {
                        HStack { Avatar(name: p.name, color: Color(hexString: p.color), size: 22); Text(p.name); Spacer(); Text((p.role ?? .pending).rawValue.capitalized) }
                    }
                }
                Section {
                    Text("Roles and per-person rules are edited on the hub for now. Revoking a phone also removes its VPN key.")
                        .font(.footnote).foregroundStyle(Theme.dim)
                }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("People")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async { data = try? await HubClient.shared.get("/v1/people") }

    private func assign(_ d: EnrolledDevice, to p: Person) {
        Task {
            do {
                try await HubClient.shared.send("/v1/devices/\(d.id)/assign", ["person_id": p.id], confirm: "Give \(d.name) to \(p.name)")
                store.show("\(d.name) is \(p.name)'s")
                await load()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func revoke(_ d: EnrolledDevice) {
        Task {
            do {
                try await HubClient.shared.send("/v1/devices/\(d.id)/revoke", [:], confirm: "Revoke \(d.name)")
                store.show("\(d.name) revoked")
                await load()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

/// Children's internet: pause, or grant time until bedtime (Firewalla, through the hub).
struct KidNetView: View {
    @Environment(HomeStore.self) private var store
    @State private var kids: [Kid] = []
    struct Kid: Decodable, Identifiable { var id: String; var name: String; var paused: Bool; var until: String?; var devices: Int? }
    private struct Reply: Decodable { var kids: [Kid] }

    var body: some View {
        List {
            ForEach(kids) { k in
                Section(k.name) {
                    LabeledContent("Internet", value: k.paused ? "Paused" : "On\(k.until.map { " until \($0)" } ?? "")")
                    if let n = k.devices { LabeledContent("Devices", value: "\(n)") }
                    HStack {
                        Button(k.paused ? "Resume" : "Pause now") { act(k.paused ? "network.grant" : "network.pause", k) }
                            .buttonStyle(.borderedProminent).tint(k.paused ? Theme.accent : Theme.warn)
                        Button("30 more minutes") { act("network.grant", k, minutes: 30) }.buttonStyle(.bordered)
                    }
                }
            }
            if kids.isEmpty { ContentUnavailableView("No children's devices yet", systemImage: "wifi") }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Device time")
        .task { await load() }
    }

    private func load() async { kids = (try? await HubClient.shared.get("/v1/network/kids", as: Reply.self))?.kids ?? [] }

    private func act(_ action: String, _ k: Kid, minutes: Int? = nil) {
        var p: [String: Any] = [:]
        if let minutes { p["minutes"] = minutes }
        store.perform(action, target: k.id, params: p, confirm: "\(action == "network.pause" ? "Pause" : "Give") \(k.name) internet")
        Task { try? await Task.sleep(for: .seconds(1)); await load() }
    }
}
