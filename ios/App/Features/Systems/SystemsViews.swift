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
                    NavigationLink(value: Destination.storage) { InfoRow("server.rack", "NAS and apps", "Pools, apps, updates, logs", chevron: true) }
                    NavigationLink(value: Destination.pipeline) { InfoRow("arrow.down.circle.fill", "Media pipeline", nil, chevron: true) }
                    NavigationLink(value: Destination.vpnPool) { InfoRow("lock.shield", "Away-from-home VPN", nil, chevron: true) }
                    NavigationLink(value: Destination.activity) { InfoRow("clock.arrow.circlepath", "Activity log", nil, chevron: true) }
                    NavigationLink(value: Destination.people) { InfoRow("person.2.badge.key.fill", "People and permissions", nil, chevron: true) }
                    if store.role == .admin {
                        NavigationLink(value: Destination.hubWeb) { InfoRow("globe", "Hub web page", "Status and keys in a browser", chevron: true) }
                    }
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

struct ActivityView: View {
    @State private var items: [ActivityEntry] = []
    @State private var before: String?
    @State private var done = false
    @State private var filter = "all"
    @State private var failed = false

    var body: some View {
        List {
            Picker("Show", selection: $filter) {
                Text("All").tag("all")
                Text("People").tag("people")
                Text("Automations").tag("automation")
                Text("Doors").tag("doors")
            }
            .pickerStyle(.segmented)
            if !done && items.isEmpty { ProgressView().frame(maxWidth: .infinity).padding() }
            if done && items.isEmpty {
                Text(failed ? "Couldn't reach the hub." : "Nothing logged yet.").foregroundStyle(Theme.dim)
            }
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
        .refreshable { items = []; before = nil; done = false; await load() }
    }

    private func load() async {
        var q = ["limit": "50"]
        if filter != "all" { q["category"] = filter }
        if let before { q["before"] = before }
        guard let l: ActivityList = try? await HubClient.shared.get("/v1/activity", q) else { failed = true; done = true; return }
        failed = false
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
                                Image(systemName: d.status == "revoked" ? "iphone.slash" : (d.test == true ? "hammer" : "iphone"))
                                VStack(alignment: .leading) {
                                    Text(d.name)
                                    if let s = d.lastSeen { Text("Seen \(clock(s))").font(.caption).foregroundStyle(Theme.dim) }
                                }
                                Spacer()
                                if d.test == true { Pill(text: "Test") }
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
/// Dylan also links each child's devices here; the hub makes one paused "block internet" rule per device.
struct KidNetView: View {
    @Environment(HomeStore.self) private var store
    @State private var kids: [Kid] = []
    @State private var linking: Kid?
    struct Kid: Decodable, Identifiable, Hashable {
        var id: String; var name: String; var managed: Bool?; var paused: Bool?; var until: String?
        var devices: [Device]?
        struct Device: Decodable, Identifiable, Hashable { var mac: String; var name: String?; var ruleId: String?; var paused: Bool?; var id: String { mac } }
    }
    private struct Reply: Decodable { var kids: [Kid] }

    var body: some View {
        List {
            ForEach(kids) { k in
                Section(k.name) {
                    LabeledContent("Internet", value: k.paused == nil ? "Unknown" : k.paused == true ? "Paused" : "On\(k.until.map { " until \(clock($0))" } ?? "")")
                    HStack {
                        Button(k.paused == true ? "Resume" : "Pause now") { act(k.paused == true ? "network.grant" : "network.pause", k) }
                            .buttonStyle(.borderedProminent).tint(k.paused == true ? Theme.accent : Theme.warn)
                        Button("30 more minutes") { act("network.grant", k, minutes: 30) }.buttonStyle(.bordered)
                    }
                    ForEach(k.devices ?? []) { d in
                        HStack {
                            Image(systemName: d.paused == true ? "wifi.slash" : "wifi").foregroundStyle(d.paused == true ? Theme.warn : Theme.accent)
                            Text(d.name ?? d.mac)
                            Spacer()
                            Text(d.paused == true ? "Paused" : "On").font(.caption).foregroundStyle(Theme.dim)
                        }
                        .swipeActions {
                            if store.role == .admin { Button("Unlink", role: .destructive) { unlink(d, from: k) } }
                        }
                    }
                    if store.role == .admin {
                        Button { linking = k } label: { Label("Add a device", systemImage: "plus.circle") }
                    }
                }
            }
            if kids.isEmpty { ContentUnavailableView("No children's devices yet", systemImage: "wifi") }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Device time")
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $linking) { k in LinkDevicesSheet(kid: k) { await load() } }
    }

    private func load() async { kids = (try? await HubClient.shared.get("/v1/network/kids", as: Reply.self))?.kids ?? [] }

    private func act(_ action: String, _ k: Kid, minutes: Int? = nil) {
        var p: [String: Any] = [:]
        if let minutes { p["minutes"] = minutes }
        store.perform(action, target: k.id, params: p, confirm: "\(action == "network.pause" ? "Pause" : "Give") \(k.name) internet")
        Task { try? await Task.sleep(for: .seconds(1)); await load() }
    }

    private func unlink(_ d: Kid.Device, from k: Kid) {
        Task {
            do {
                let mac = d.mac.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? d.mac
                let _: Empty = try await HubClient.shared.call("DELETE", "/v1/network/kids/\(k.id)/devices/\(mac)",
                                                                confirm: "Stop managing \(d.name ?? "this device")")
                store.show("Unlinked \(d.name ?? d.mac)")
            } catch { store.show(error.localizedDescription, isError: true) }
            await load()
        }
    }
}

/// Everything Firewalla sees on the network; pick the ones that belong to a child.
struct LinkDevicesSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let kid: KidNetView.Kid
    let done: () async -> Void
    @State private var devices: [NetDevice]?
    @State private var picked: Set<String> = []
    @State private var search = ""
    struct NetDevice: Decodable, Identifiable, Hashable {
        var mac: String; var name: String?; var ip: String?; var online: Bool?; var linkedTo: String?
        var id: String { mac }
    }
    private struct Reply: Decodable { var devices: [NetDevice] }

    var body: some View {
        NavigationStack {
            List {
                if let devices {
                    let shown = devices.filter { search.isEmpty || ($0.name ?? $0.mac).localizedCaseInsensitiveContains(search) }
                    ForEach(shown) { d in
                        Button { toggle(d) } label: {
                            HStack {
                                Image(systemName: picked.contains(d.mac) ? "checkmark.circle.fill" : "circle").foregroundStyle(Theme.accent)
                                VStack(alignment: .leading) {
                                    Text(d.name ?? d.mac).foregroundStyle(Theme.fg)
                                    Text(d.linkedTo.map { $0 == kid.id ? "Already linked" : "Linked to someone else" } ?? (d.online == true ? "Online" : "Offline"))
                                        .font(.caption).foregroundStyle(Theme.dim)
                                }
                            }
                        }
                        .disabled(d.linkedTo != nil)
                    }
                } else { ProgressView() }
            }
            .searchable(text: $search)
            .navigationTitle("\(kid.name)'s devices")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Add") { save() }.disabled(picked.isEmpty) }
            }
            .task { devices = (try? await HubClient.shared.get("/v1/network/devices", as: Reply.self))?.devices ?? [] }
        }
    }

    private func toggle(_ d: NetDevice) { if picked.contains(d.mac) { picked.remove(d.mac) } else { picked.insert(d.mac) } }

    private func save() {
        Task {
            do {
                try await HubClient.shared.send("/v1/network/kids/\(kid.id)/devices", ["macs": Array(picked)],
                                                confirm: "Manage \(picked.count) device\(picked.count == 1 ? "" : "s") for \(kid.name)")
                store.show("Added to \(kid.name)")
                await done(); dismiss()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}
