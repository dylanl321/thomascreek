import SwiftUI
import UserNotifications

// MARK: - Model (GET/PUT /v1/alerts)

/// Decoded without HubClient's snake-case conversion: camera ids and entity ids are dictionary keys
/// ("140_frontdoor", "sensor.freezer_temp") and must arrive unchanged.
struct AlertsReply: Codable {
    struct Quiet: Codable, Equatable {
        var on: Bool
        var from: String
        var to: String
        var ringUrgent: Bool?
        enum CodingKeys: String, CodingKey { case on, from, to, ringUrgent = "ring_urgent" }
    }
    struct Prefs: Codable, Equatable {
        var on: Bool
        var doorbell: Bool?
        var package: Bool?
        var cameras: [String: [String]]?
        var skipKnown: Bool?
        var onlyAway: Bool?
        var gapS: Int?
        var garage: Bool?
        var hazards: Bool?
        var awayDoors: Bool?
        var entities: [String: Bool]?
        var automations: Bool?
        var quiet: Quiet
        var network: [String: Bool]?
        var kids: [String: Bool]?
        enum CodingKeys: String, CodingKey {
            case on, doorbell, package, cameras, garage, hazards, entities, automations, quiet, network, kids
            case skipKnown = "skip_known", onlyAway = "only_away", gapS = "gap_s", awayDoors = "away_doors"
        }
    }
    struct Category: Codable, Identifiable { var id: String; var title: String; var detail: String; var `default`: Bool }
    struct Thing: Codable, Identifiable, Hashable {
        var entityId: String; var name: String
        var id: String { entityId }
        enum CodingKeys: String, CodingKey { case entityId = "entity_id", name }
    }
    struct Found: Codable {
        var doorbells: [Thing]?
        var garages: [Thing]?
        var hazards: [Thing]?
        var outsideDoors: [Thing]?
        var extra: [Thing]?
        var garageAfterS: Int?
        var listening: Bool?
        var kids: [String]?
        var network: Bool?
        var ignored: Int?
        enum CodingKeys: String, CodingKey {
            case doorbells, garages, hazards, extra, listening, kids, network, ignored
            case outsideDoors = "outside_doors", garageAfterS = "garage_after_s"
        }
    }
    struct Camera: Codable, Identifiable { var id: String; var name: String }
    struct Label: Codable, Identifiable { var id: String; var title: String }
    struct Sources: Codable {
        var doorbellEntity: String?
        var doorbellCamera: String?
        var outsideDoors: [String]
        var extra: [Extra]
        var garageAfterS: Int
        struct Extra: Codable, Hashable { var entityId: String; var name: String?
            enum CodingKeys: String, CodingKey { case entityId = "entity_id", name } }
        enum CodingKeys: String, CodingKey {
            case extra
            case doorbellEntity = "doorbell_entity", doorbellCamera = "doorbell_camera"
            case outsideDoors = "outside_doors", garageAfterS = "garage_after_s"
        }
    }

    var role: String
    var prefs: Prefs
    var devicePrefs: [String: Bool]
    var categories: [Category]
    var pushReady: Bool
    var gaps: [Int]
    var found: Found
    var cameras: [Camera]?
    var labels: [Label]?
    var sources: Sources?
    enum CodingKeys: String, CodingKey {
        case role, prefs, categories, gaps, found, cameras, labels, sources
        case devicePrefs = "device_prefs", pushReady = "push_ready"
    }

    var grownUp: Bool { role == "admin" || role == "adult" }
}

struct HAEntity: Codable, Identifiable, Hashable {
    var entityId: String; var name: String; var state: String?; var deviceClass: String?
    var id: String { entityId }
    enum CodingKeys: String, CodingKey { case entityId = "entity_id", name, state, deviceClass = "device_class" }
}

@MainActor @Observable
final class AlertsModel {
    static let shared = AlertsModel()
    var reply: AlertsReply?
    var error: String?
    var testResult: String?

    private static let plain = JSONDecoder()

    func load() async {
        do {
            let data = try await HubClient.shared.raw("GET", "/v1/alerts")
            reply = try Self.plain.decode(AlertsReply.self, from: data)
            error = nil
            if let d = reply?.devicePrefs { AppSettings.prefs = d }
        } catch { self.error = error.localizedDescription }
    }

    /// Save one change. The screen updates at once; the hub's answer replaces it only on success.
    func set(_ key: String, _ value: Any, device: Bool = false) {
        let body: [String: Any] = device ? ["device_prefs": [key: value]] : ["prefs": [key: value]]
        Task {
            do {
                let data = try await HubClient.shared.raw("PUT", "/v1/alerts", body: try JSONSerialization.data(withJSONObject: body))
                if let r = try? Self.plain.decode(AlertsReply.self, from: data) { reply = r }
                if device, let r = reply { AppSettings.prefs = r.devicePrefs }
            } catch { self.error = error.localizedDescription; await load() }
        }
    }

    func setSources(_ body: [String: Any]) async {
        do {
            let data = try await HubClient.shared.raw("PUT", "/v1/alerts/sources", body: try JSONSerialization.data(withJSONObject: body))
            if let r = try? Self.plain.decode(AlertsReply.self, from: data) { reply = r }
        } catch { self.error = error.localizedDescription }
    }

    func entities(_ q: String) async -> [HAEntity] {
        struct R: Codable { var entities: [HAEntity] }
        guard let data = try? await HubClient.shared.raw("GET", "/v1/alerts/entities", query: ["q": q]) else { return [] }
        return (try? Self.plain.decode(R.self, from: data))?.entities ?? []
    }

    func test() async {
        testResult = nil
        do {
            struct R: Decodable { var ok: Bool; var picture: Bool; var reason: String?; var env: String? }
            let r: R = try await HubClient.shared.post("/v1/alerts/test")
            if r.ok {
                testResult = r.picture ? "Sent with a camera picture. It should arrive in a few seconds." : "Sent. It should arrive in a few seconds."
            } else {
                testResult = "Apple didn't take it (\(r.reason ?? "no answer"), \(r.env ?? "production")). Open the app again so it re-registers, then retry."
            }
        } catch { testResult = error.localizedDescription }
    }

    /// A short line for Profile: what this person gets.
    var summary: String {
        guard let r = reply else { return "" }
        if !r.prefs.on { return "Paused" }
        if !r.grownUp { return r.prefs.quiet.on ? "Quiet \(Self.clock(r.prefs.quiet.from))–\(Self.clock(r.prefs.quiet.to))" : "On" }
        var bits: [String] = []
        if r.prefs.doorbell == true { bits.append("Doorbell") }
        let cams = (r.prefs.cameras ?? [:]).filter { !$0.value.isEmpty }.count
        if cams > 0 { bits.append("\(cams) camera\(cams == 1 ? "" : "s")") }
        if r.prefs.garage == true { bits.append("Garage") }
        return bits.isEmpty ? "On" : bits.joined(separator: ", ")
    }

    static func clock(_ hhmm: String) -> String {
        guard let d = date(hhmm) else { return hhmm }
        return d.formatted(date: .omitted, time: .shortened)
    }
    static func date(_ hhmm: String) -> Date? {
        let p = hhmm.split(separator: ":").compactMap { Int($0) }
        guard p.count == 2 else { return nil }
        return Calendar.current.date(bySettingHour: p[0], minute: p[1], second: 0, of: Date())
    }
    static func hhmm(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }
}

// MARK: - Alerts screen

struct AlertsView: View {
    @State private var model = AlertsModel.shared
    @State private var iosAllowed: Bool?

    var body: some View {
        Form {
            if let r = model.reply {
                content(r)
            } else if let e = model.error {
                Section { Text(e).foregroundStyle(Theme.dim) }
            } else {
                Section { HStack { ProgressView(); Text("Loading…").foregroundStyle(Theme.dim) } }
            }
        }
        .navigationTitle("Alerts")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await model.load()
            let s = await UNUserNotificationCenter.current().notificationSettings()
            iosAllowed = s.authorizationStatus == .authorized || s.authorizationStatus == .provisional
        }
        .refreshable { await model.load() }
    }

    /// One switch inside a group such as network or kids.
    private func sub(_ group: String, _ kp: WritableKeyPath<AlertsReply.Prefs, [String: Bool]?>, _ key: String) -> Binding<Bool> {
        Binding(get: { model.reply?.prefs[keyPath: kp]?[key] ?? false },
                set: { v in
                    if model.reply?.prefs[keyPath: kp] == nil { model.reply?.prefs[keyPath: kp] = [:] }
                    model.reply?.prefs[keyPath: kp]?[key] = v
                    model.set(group, [key: v])
                })
    }

    private func bind(_ key: String, _ kp: WritableKeyPath<AlertsReply.Prefs, Bool?>, default d: Bool = false) -> Binding<Bool> {
        Binding(get: { model.reply?.prefs[keyPath: kp] ?? d },
                set: { v in model.reply?.prefs[keyPath: kp] = v; model.set(key, v) })
    }

    @ViewBuilder private func content(_ r: AlertsReply) -> some View {
        Section {
            Toggle(isOn: Binding(get: { model.reply?.prefs.on ?? true }, set: { v in model.reply?.prefs.on = v; model.set("on", v) })) {
                Label { VStack(alignment: .leading, spacing: 2) {
                    Text("Alerts")
                    Text(model.reply?.prefs.on == true ? "Off pauses every alert. The inbox still fills." : "Turn on to get the alerts you pick below.").font(.caption).foregroundStyle(Theme.dim)
                } } icon: { Image(systemName: "bell.badge.fill").foregroundStyle(Theme.accent) }
            }
        } footer: {
            if iosAllowed == false {
                Text("Notifications are off for Thomas Creek in iOS Settings, so nothing can arrive on this \(UIDevice.current.model).")
                    .foregroundStyle(Theme.warn)
            } else if !r.pushReady {
                Text("This \(UIDevice.current.model) hasn't registered for alerts yet. Open the app on home Wi-Fi once.")
            }
        }

        Group {
            if r.grownUp {
                Section {
                    Toggle(isOn: bind("doorbell", \.doorbell)) { row("Doorbell", "Rings right away, with the front door picture", "bell.and.waves.left.and.right") }
                    Toggle(isOn: bind("package", \.package)) { row("Package left", "Frigate sees a package by a door", "shippingbox") }
                } header: { Text("At the door") } footer: {
                    if (r.found.doorbells ?? []).isEmpty { Text("No doorbell found in Home Assistant yet. Dylan can pick one under More from Home Assistant.") }
                }
                Section("Cameras") {
                    NavigationLink { CameraAlertsView(model: model) } label: {
                        row("Choose cameras", cameraLine(r), "video")
                    }
                    Toggle(isOn: bind("skip_known", \.skipKnown, default: true)) { row("Skip family faces", "No alert when Frigate recognises the face", "person.crop.circle.badge.checkmark") }
                    Toggle(isOn: bind("only_away", \.onlyAway)) { row("Only when nobody's home", "Uses Home Assistant's people", "house") }
                }
            }
            Section("House") {
                if r.grownUp {
                    Toggle(isOn: bind("garage", \.garage)) {
                        row("Garage left open", "After \((r.found.garageAfterS ?? 600) / 60) minutes", "door.garage.open")
                    }
                }
                Toggle(isOn: bind("hazards", \.hazards)) { row("Leak and smoke", "Rings even in quiet hours", "drop.triangle") }
                if r.grownUp && !(r.found.outsideDoors ?? []).isEmpty {
                    Toggle(isOn: bind("away_doors", \.awayDoors)) { row("Doors while nobody's home", (r.found.outsideDoors ?? []).map(\.name).joined(separator: ", "), "door.left.hand.open") }
                }
                if !(r.found.extra ?? []).isEmpty || r.role == "admin" {
                    NavigationLink { HomeAssistantAlertsView(model: model) } label: {
                        let on = (r.prefs.entities ?? [:]).values.filter { $0 }.count
                        row("More from Home Assistant", on > 0 ? "\(on) chosen" : "\((r.found.extra ?? []).count) on offer", "plus.circle")
                    }
                }
                Toggle(isOn: bind("automations", \.automations, default: true)) { row("From automations", "Alerts your Home Assistant automations send", "gearshape.2") }
            }
            if r.grownUp, let kids = r.found.kids, !kids.isEmpty, r.found.network == true {
                Section {
                    Toggle(isOn: sub("kids", \.kids, "adult")) { row("Adult sites", "Firewalla blocked or flagged one", "hand.raised") }
                    Toggle(isOn: sub("kids", \.kids, "bedtime")) { row("Up after bedtime", "Videos or games after their bedtime", "moon.zzz") }
                    Toggle(isOn: sub("kids", \.kids, "gaming")) { row("Gaming", "Any time Firewalla sees games", "gamecontroller") }
                    Toggle(isOn: sub("kids", \.kids, "video")) { row("Video", "Any time Firewalla sees streaming", "play.rectangle") }
                    Toggle(isOn: sub("kids", \.kids, "bandwidth")) { row("Big downloads", "Lots of data in a short time", "arrow.down.circle") }
                } header: { Text("Kids' devices") } footer: {
                    Text("From Firewalla, for \(kids.joined(separator: " and "))'s devices. At most one alert per kid and kind every 30 minutes.")
                }
            }
            if r.role == "admin", r.found.network == true {
                Section("Network") {
                    Toggle(isOn: sub("network", \.network, "new_device")) { row("New device joins", "Something new on the Wi-Fi", "plus.app") }
                    Toggle(isOn: sub("network", \.network, "security")) { row("Security", "Intrusions, open ports, odd uploads", "shield.lefthalf.filled") }
                    Toggle(isOn: sub("network", \.network, "internet")) { row("Internet down or back", "The connection itself", "wifi.exclamationmark") }
                    Toggle(isOn: sub("network", \.network, "vpn")) { row("VPN", "Connections and errors", "lock.shield") }
                    Toggle(isOn: sub("network", \.network, "offline")) { row("Device offline", "A device drops off the network", "bolt.horizontal.circle") }
                }
            }
        }
        .disabled(!(model.reply?.prefs.on ?? true))

        Section("Family and app") {
            ForEach(r.categories) { c in
                Toggle(isOn: Binding(get: { model.reply?.devicePrefs[c.id] ?? c.default },
                                     set: { v in model.reply?.devicePrefs[c.id] = v; model.set(c.id, v, device: true) })) {
                    VStack(alignment: .leading, spacing: 2) { Text(c.title); Text(c.detail).font(.caption).foregroundStyle(Theme.dim) }
                }
            }
        }

        Section {
            Toggle(isOn: quiet(\.on)) { row("Quiet hours", "Alerts arrive silently", "moon.fill") }
            if model.reply?.prefs.quiet.on == true {
                DatePicker("From", selection: quietTime(\.from), displayedComponents: .hourAndMinute)
                DatePicker("Until", selection: quietTime(\.to), displayedComponents: .hourAndMinute)
                if r.grownUp {
                    Toggle("Doorbell, leaks and smoke still ring", isOn: Binding(
                        get: { model.reply?.prefs.quiet.ringUrgent ?? true },
                        set: { v in model.reply?.prefs.quiet.ringUrgent = v; saveQuiet() }))
                }
            }
        } header: { Text("Quiet hours") }

        Section {
            Button("Send a test alert") { Task { await model.test() } }
            if let t = model.testResult { Text(t).font(.footnote).foregroundStyle(Theme.dim) }
        } footer: {
            if r.found.listening == false && r.grownUp { Text("The hub isn't hearing from Home Assistant right now, so doorbell and house alerts may be late.") }
        }
    }

    private func row(_ title: String, _ detail: String, _ icon: String) -> some View {
        Label {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if !detail.isEmpty { Text(detail).font(.caption).foregroundStyle(Theme.dim).lineLimit(2) }
            }
        } icon: { Image(systemName: icon).foregroundStyle(Theme.accent) }
    }

    private func cameraLine(_ r: AlertsReply) -> String {
        let names = Dictionary(uniqueKeysWithValues: (r.cameras ?? []).map { ($0.id, $0.name) })
        let on = (r.prefs.cameras ?? [:]).filter { !$0.value.isEmpty }
        if on.isEmpty { return "None" }
        return on.keys.sorted().map { "\(names[$0] ?? $0): \(on[$0]!.joined(separator: ", "))" }.joined(separator: " · ")
    }

    private func quiet(_ kp: WritableKeyPath<AlertsReply.Quiet, Bool>) -> Binding<Bool> {
        Binding(get: { model.reply?.prefs.quiet[keyPath: kp] ?? false },
                set: { v in model.reply?.prefs.quiet[keyPath: kp] = v; saveQuiet() })
    }
    private func quietTime(_ kp: WritableKeyPath<AlertsReply.Quiet, String>) -> Binding<Date> {
        Binding(get: { AlertsModel.date(model.reply?.prefs.quiet[keyPath: kp] ?? "22:00") ?? Date() },
                set: { d in model.reply?.prefs.quiet[keyPath: kp] = AlertsModel.hhmm(d); saveQuiet() })
    }
    private func saveQuiet() {
        guard let q = model.reply?.prefs.quiet else { return }
        var body: [String: Any] = ["on": q.on, "from": q.from, "to": q.to]
        if let ru = q.ringUrgent { body["ring_urgent"] = ru }
        model.set("quiet", body)
    }
}

// MARK: - Cameras

struct CameraAlertsView: View {
    @Bindable var model: AlertsModel

    var body: some View {
        Form {
            if let r = model.reply {
                Section {
                    ForEach(r.cameras ?? []) { cam in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(cam.name)
                            HStack(spacing: 6) {
                                ForEach(r.labels ?? []) { l in
                                    let on = (r.prefs.cameras?[cam.id] ?? []).contains(l.id)
                                    Button { toggle(cam.id, l.id) } label: {
                                        Text(l.title).font(.footnote.weight(.semibold))
                                            .padding(.horizontal, 11).padding(.vertical, 6)
                                            .background(on ? Theme.accent : Theme.card2, in: Capsule())
                                            .foregroundStyle(on ? Theme.onAccent : Theme.dim)
                                    }
                                    .buttonStyle(.plain)
                                    .accessibilityAddTraits(on ? .isSelected : [])
                                }
                            }
                        }
                        .padding(.vertical, 2)
                    }
                } footer: { Text("Packages have their own switch on the Alerts screen.") }
                Section {
                    Picker("One alert per camera every", selection: Binding(
                        get: { model.reply?.prefs.gapS ?? 300 },
                        set: { v in model.reply?.prefs.gapS = v; model.set("gap_s", v) })) {
                        ForEach(r.gaps, id: \.self) { g in Text(g < 3600 ? "\(g / 60) min" : "1 hour").tag(g) }
                    }
                } footer: { Text("A busy camera like the driveway can see dozens of people a day. This keeps it to one alert per camera and kind in that window.") }
                if !ignored.isEmpty {
                    Section {
                        ForEach(ignored) { i in
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(i.cameraName): not a \(i.label)")
                                Text("No alerts for a \(i.label) in that spot").font(.caption).foregroundStyle(Theme.dim)
                            }
                            .swipeActions { Button("Alert again") { Task { await unignore(i) } }.tint(Theme.accent) }
                        }
                    } header: { Text("Ignored spots") } footer: { Text("Added when someone taps Not a person on an alert. Swipe to alert for that spot again.") }
                }
            }
        }
        .task { await loadIgnored() }
        .navigationTitle("Cameras")
        .navigationBarTitleDisplayMode(.inline)
    }

    @State private var ignored: [IgnoredSpot] = []

    struct IgnoredSpot: Decodable, Identifiable {
        var id: String; var camera: String; var label: String; var cameraName: String
        enum CodingKeys: String, CodingKey { case id, camera, label, cameraName = "camera_name" }
    }
    private struct IgnoredReply: Decodable { var items: [IgnoredSpot] }

    private func loadIgnored() async {
        guard let d = try? await HubClient.shared.raw("GET", "/v1/cameras/ignored") else { return }
        ignored = (try? JSONDecoder().decode(IgnoredReply.self, from: d))?.items ?? []
    }

    private func unignore(_ i: IgnoredSpot) async {
        _ = try? await HubClient.shared.raw("DELETE", "/v1/cameras/ignored/\(i.id)")
        await loadIgnored()
    }

    private func toggle(_ cam: String, _ label: String) {
        var cams = model.reply?.prefs.cameras ?? [:]
        var l = cams[cam] ?? []
        if let i = l.firstIndex(of: label) { l.remove(at: i) } else { l.append(label) }
        cams[cam] = l.isEmpty ? nil : l.sorted()
        model.reply?.prefs.cameras = cams
        model.set("cameras", cams)
    }
}

// MARK: - Home Assistant extras

struct HomeAssistantAlertsView: View {
    @Bindable var model: AlertsModel
    @State private var picking: PickMode?
    @State private var snippet: String?
    @State private var snippetError: String?

    enum PickMode: String, Identifiable { case extra, door, doorbell; var id: String { rawValue } }

    var body: some View {
        Form {
            if let r = model.reply {
                Section {
                    if (r.found.extra ?? []).isEmpty {
                        Text(r.role == "admin" ? "Nothing on offer yet. Add things below." : "Dylan hasn't added anything yet.").foregroundStyle(Theme.dim)
                    }
                    ForEach(r.found.extra ?? []) { t in
                        Toggle(isOn: Binding(get: { model.reply?.prefs.entities?[t.entityId] ?? false },
                                             set: { v in
                                                 var e = model.reply?.prefs.entities ?? [:]; e[t.entityId] = v
                                                 model.reply?.prefs.entities = e; model.set("entities", [t.entityId: v])
                                             })) {
                            VStack(alignment: .leading, spacing: 2) { Text(t.name); Text(t.entityId).font(.caption.monospaced()).foregroundStyle(Theme.dim) }
                        }
                        .swipeActions { if r.role == "admin" { Button("Remove", role: .destructive) { removeExtra(t.entityId) } } }
                    }
                } footer: { Text("You get an alert each time one of these changes.") }

                if r.role == "admin", let s = r.sources {
                    Section {
                        Button("Add from Home Assistant…") { picking = .extra }
                    } footer: { Text("Everyone in the family can then turn these on for themselves.") }
                    Section {
                        Button { picking = .doorbell } label: {
                            LabeledContent("Doorbell", value: s.doorbellEntity ?? ((r.found.doorbells ?? []).first?.name.appending(" (found)") ?? "None"))
                        }
                        ForEach(s.outsideDoors, id: \.self) { d in
                            Text((r.found.outsideDoors ?? []).first { $0.entityId == d }?.name ?? d)
                                .swipeActions { Button("Remove", role: .destructive) {
                                    Task { await model.setSources(["outside_doors": s.outsideDoors.filter { $0 != d }]) } } }
                        }
                        Button("Add an outside door…") { picking = .door }
                        Picker("Garage alert after", selection: Binding(get: { s.garageAfterS }, set: { v in Task { await model.setSources(["garage_after_s": v]) } })) {
                            ForEach([300, 600, 900, 1800, 3600], id: \.self) { Text($0 < 3600 ? "\($0 / 60) min" : "1 hour").tag($0) }
                        }
                    } header: { Text("For everyone") } footer: {
                        Text("Outside doors are used by \"Doors while nobody's home\".")
                    }
                    Section {
                        if let snippet {
                            Text(snippet).font(.caption2.monospaced()).textSelection(.enabled)
                            Button("Copy") { UIPasteboard.general.string = snippet }
                        } else {
                            Button("Show the Home Assistant setup") { Task { await loadSnippet() } }
                        }
                        if let snippetError { Text(snippetError).font(.footnote).foregroundStyle(Theme.warn) }
                    } header: { Text("Alerts from automations") } footer: {
                        Text("Paste this into Home Assistant's configuration.yaml once. Any automation can then send an alert through the app with rest_command.thomas_creek_alert.")
                    }
                }
            }
        }
        .navigationTitle("Home Assistant")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $picking) { mode in EntityPicker(model: model, mode: mode) }
    }

    private func removeExtra(_ id: String) {
        guard let s = model.reply?.sources else { return }
        let left = s.extra.filter { $0.entityId != id }.map { ["entity_id": $0.entityId, "name": $0.name ?? ""] }
        Task { await model.setSources(["extra": left]) }
    }

    private func loadSnippet() async {
        struct R: Decodable { var yaml: String }
        do {
            let r: R = try await HubClient.shared.post("/v1/alerts/automation", [:], confirm: "Show the alert token for Home Assistant")
            snippet = r.yaml; snippetError = nil
        } catch { snippetError = error.localizedDescription }
    }
}

struct EntityPicker: View {
    @Bindable var model: AlertsModel
    let mode: HomeAssistantAlertsView.PickMode
    @Environment(\.dismiss) private var dismiss
    @State private var q = ""
    @State private var results: [HAEntity] = []

    var body: some View {
        NavigationStack {
            List(filtered) { e in
                Button { pick(e) } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.name).foregroundStyle(Theme.fg)
                        Text("\(e.entityId) · \(e.state ?? "")").font(.caption.monospaced()).foregroundStyle(Theme.dim)
                    }
                }
            }
            .searchable(text: $q, placement: .navigationBarDrawer(displayMode: .always))
            .task(id: q) {
                try? await Task.sleep(for: .milliseconds(250))
                results = await model.entities(q)
            }
            .navigationTitle(mode == .doorbell ? "Doorbell" : mode == .door ? "Outside door" : "Add an alert")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                if mode == .doorbell, model.reply?.sources?.doorbellEntity != nil {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Find it myself") { Task { await model.setSources(["doorbell_entity": NSNull()]); dismiss() } }
                    }
                }
            }
        }
    }

    private var filtered: [HAEntity] {
        switch mode {
        case .doorbell: results.filter { $0.entityId.hasPrefix("event.") || $0.entityId.hasPrefix("binary_sensor.") }
        case .door: results.filter { $0.entityId.hasPrefix("binary_sensor.") || $0.entityId.hasPrefix("lock.") || $0.entityId.hasPrefix("cover.") }
        case .extra: results
        }
    }

    private func pick(_ e: HAEntity) {
        guard let s = model.reply?.sources else { return }
        Task {
            switch mode {
            case .extra:
                var list = s.extra.map { ["entity_id": $0.entityId, "name": $0.name ?? ""] }
                if !s.extra.contains(where: { $0.entityId == e.entityId }) { list.append(["entity_id": e.entityId, "name": e.name]) }
                await model.setSources(["extra": list])
            case .door:
                await model.setSources(["outside_doors": Array(Set(s.outsideDoors + [e.entityId])).sorted()])
            case .doorbell:
                await model.setSources(["doorbell_entity": e.entityId])
            }
            dismiss()
        }
    }
}
