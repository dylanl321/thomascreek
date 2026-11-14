import SwiftUI

// The house layout everyone sees: appliances, scenes in rooms, "Hide for everyone", the room editor,
// and the hidden-devices list with clean-up. Editing is admin only ("home.layout"); the hub stores it,
// so every phone shows the same house. Hidden devices still work for modes, automations and Ask.

// MARK: - Hub calls

struct HouseLayout: Decodable {
    var hidden: [Row]
    var rooms: [RoomRow]
    var cleanup: [Row]
    struct Row: Decodable, Identifiable, Hashable {
        var id: String; var name: String; var kind: String; var roomId: String?; var room: String?
        var available: Bool?; var since: String?
    }
    struct RoomRow: Decodable, Identifiable, Hashable {
        var id: String; var name: String; var symbol: String?; var hidden: Bool; var floorId: String?
    }
}

struct RoomLayout: Decodable {
    var id: String
    var name: String
    var symbol: String?
    var hidden: Bool
    var haName: String
    var devices: [Item]
    var scenes: [RoomScene]
    struct Item: Decodable, Identifiable, Hashable {
        var id: String; var name: String; var kind: DeviceKind; var hidden: Bool; var available: Bool; var movedIn: Bool
    }
}

enum LayoutAPI {
    static func setHidden(_ ids: [String], _ hidden: Bool) async throws {
        let _: Empty = try await HubClient.shared.call("POST", "/v1/home/hidden", body: ["ids": ids, "hidden": hidden])
    }
    static func room(_ id: String) async throws -> RoomLayout {
        try await HubClient.shared.get("/v1/rooms/\(id)/layout")
    }
    static func saveRoom(_ id: String, _ body: [String: Any]) async throws -> RoomLayout {
        try await HubClient.shared.call("PUT", "/v1/rooms/\(id)/layout", body: body)
    }
}

// MARK: - Hide for everyone

/// A menu item or row that hides a device (or scene, or appliance) from the app for everyone.
/// Admin only; shows nothing for anyone else. Drop it into any menu or device page.
struct HideForEveryoneButton: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    var name: String = ""

    var body: some View {
        if store.me?.can(.homeLayout) == true {
            Button("Hide for everyone", systemImage: "eye.slash") {
                Task {
                    do {
                        try await LayoutAPI.setHidden([deviceID], true)
                        store.show("Hidden for everyone", name.isEmpty ? "Unhide it from House › Hidden devices." : "\(name) is hidden. Unhide it from House › Hidden devices.")
                        await store.loadHome()
                    } catch { store.show("Couldn't hide it", isError: true) }
                }
            }
        }
    }
}

// MARK: - Scenes in a room

/// One-tap scene buttons for a room.
struct RoomScenesRow: View {
    @Environment(HomeStore.self) private var store
    let scenes: [RoomScene]
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(scenes) { sc in
                    Button {
                        store.perform("scene.run", target: sc.id, done: "\(sc.name) is on")
                    } label: {
                        Label(sc.name, systemImage: SceneSymbol.of(sc))
                            .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                            .padding(.horizontal, 14).frame(minHeight: 44)
                            .background(Theme.card, in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .contextMenu { HideForEveryoneButton(deviceID: sc.id, name: sc.name) }
                }
            }
            .padding(.horizontal, 2)
        }
    }
}

enum SceneSymbol {
    static func of(_ s: RoomScene) -> String {
        let n = s.name.lowercased()
        if n.contains("movie") || n.contains("tv") { return "tv" }
        if n.contains("night") || n.contains("sleep") || n.contains("bed") { return "moon.fill" }
        if n.contains("morning") || n.contains("wake") { return "sunrise.fill" }
        if n.contains("dinner") || n.contains("eat") { return "fork.knife" }
        if n.contains("off") { return "power" }
        if n.contains("bright") || n.contains("on") { return "sun.max.fill" }
        if n.contains("relax") || n.contains("chill") { return "sofa.fill" }
        return "sparkles"
    }
}

// MARK: - Appliances

struct AppliancesView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var open: Appliance?

    var body: some View {
        let list = store.home?.appliances ?? []
        let showers = store.home?.devices(kind: .shower) ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if !list.isEmpty {
                    SectionHeader("Appliances", live: true)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 10)], spacing: 10) {
                        ForEach(list) { a in
                            StateTile(symbol: a.symbol ?? "powerplug.fill", title: a.name, detail: a.status ?? "",
                                      on: a.running == true, tint: a.doorOpen == true ? Theme.warn : Theme.accent) {
                                if let oven = a.ovenId { router.go(.kitchen(oven)) } else { open = a }
                            }
                            .opacity(a.isAvailable ? 1 : 0.55)
                            .contextMenu {
                                Button("Details", systemImage: "info.circle") { open = a }
                                HideForEveryoneButton(deviceID: a.id, name: a.name)
                            }
                        }
                    }
                }
                if !showers.isEmpty {
                    SectionHeader("Steam shower")
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 10)], spacing: 10) {
                        ForEach(showers) { DeviceTile(device: $0) }
                    }
                }
                if list.isEmpty && showers.isEmpty {
                    EmptyCard(symbol: "washer", title: "No appliances yet",
                              detail: "Appliances connected to Home Assistant (GE, Bosch, LG, robot vacuums) show here.")
                }
                if list.contains(where: { !$0.isAvailable }) {
                    Text("“Not connected” means Home Assistant has lost the appliance's account. Sign in to it again in Home Assistant.")
                        .font(.footnote).foregroundStyle(Theme.dim).padding(.horizontal, 4)
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Appliances")
        .refreshable { await store.loadHome() }
        .sheet(item: $open) { ApplianceSheet(appliance: $0) }
    }
}

struct ApplianceSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let appliance: Appliance
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Status", value: appliance.status ?? "–")
                    if let r = store.home?.room(appliance.roomId) { LabeledContent("Room", value: r.name) }
                }
                if let parts = appliance.parts, !parts.isEmpty {
                    Section("Now") { ForEach(parts) { LabeledContent($0.name, value: $0.value) } }
                }
                if appliance.maker != nil || appliance.model != nil {
                    Section("About") {
                        if let m = appliance.maker { LabeledContent("Maker", value: m) }
                        if let m = appliance.model { LabeledContent("Model", value: m) }
                    }
                }
                if store.me?.can(.homeLayout) == true {
                    Section { HideForEveryoneButton(deviceID: appliance.id, name: appliance.name) }
                }
            }
            .navigationTitle(appliance.name).navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Room editor

struct RoomEditView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let roomID: String
    @State private var room: RoomLayout?
    @State private var name = ""
    @State private var symbol: String?
    @State private var hideRoom = false
    @State private var devices: [RoomLayout.Item] = []
    @State private var scenes: [RoomScene] = []
    @State private var hidden: Set<String> = []
    @State private var add: [String] = []
    @State private var remove: [String] = []
    @State private var picking = false
    @State private var saving = false
    @State private var failed = false

    static let symbols = ["sofa.fill", "bed.double.fill", "fork.knife", "tv", "desktopcomputer", "car.fill", "stairs",
                          "sun.max.fill", "hammer.fill", "building.2.fill", "leaf.fill", "shower.fill", "person.crop.circle",
                          "figure.and.child.holdinghands", "teddybear.fill", "gamecontroller.fill", "books.vertical.fill",
                          "washer.fill", "dumbbell.fill", "wineglass.fill", "door.left.hand.open", "tree.fill",
                          "house.fill", "square.split.2x2"]

    var body: some View {
        Group {
            if room != nil {
                List {
                    Section("Room") {
                        TextField(room?.haName ?? "Name", text: $name).textInputAutocapitalization(.words)
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(Self.symbols, id: \.self) { s in
                                    Button { symbol = s } label: {
                                        Image(systemName: s).font(.title3).frame(width: 44, height: 44)
                                            .foregroundStyle(symbol == s ? Color.white : Theme.fg)
                                            .background(symbol == s ? Theme.accent : Theme.card2, in: RoundedRectangle(cornerRadius: 10))
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                        Toggle("Hide this room for everyone", isOn: $hideRoom)
                    }
                    if !scenes.isEmpty {
                        Section {
                            ForEach(scenes) { sc in row(sc.id, sc.name, SceneSymbol.of(sc), sub: nil, moved: false) }
                                .onMove { scenes.move(fromOffsets: $0, toOffset: $1) }
                        } header: { Text("Scenes") } footer: { Text("Shown as one-tap buttons at the top of the room.") }
                    }
                    Section {
                        ForEach(devices) { d in
                            row(d.id, d.name, DeviceSymbols.kindSymbol(d.kind), sub: d.available ? nil : "Not responding", moved: d.movedIn)
                        }
                        .onMove { devices.move(fromOffsets: $0, toOffset: $1) }
                        Button("Add a device from another room", systemImage: "plus.circle") { picking = true }
                    } header: { Text("Devices") } footer: {
                        Text("Drag to reorder. Hidden devices disappear from the app for everyone but keep working for modes, automations and Ask.")
                    }
                }
                .environment(\.editMode, .constant(.active))
            } else if failed {
                EmptyCard(symbol: "wifi.exclamationmark", title: "Couldn't load this room").screen()
            } else { LoadingCard().screen() }
        }
        .background(Backdrop())
        .navigationTitle("Edit room").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button(saving ? "Saving…" : "Save") { Task { await save() } }.disabled(room == nil || saving)
            }
        }
        .sheet(isPresented: $picking) {
            DevicePicker(exclude: Set(devices.map(\.id))) { d in
                devices.append(.init(id: d.id, name: d.name, kind: d.kind, hidden: false, available: d.isAvailable, movedIn: true))
                add.append(d.id); remove.removeAll { $0 == d.id }
            }
        }
        .task { await load() }
    }

    private func row(_ id: String, _ title: String, _ sym: String, sub: String?, moved: Bool) -> some View {
        let isHidden = hidden.contains(id)
        return HStack(spacing: 12) {
            Image(systemName: sym).foregroundStyle(isHidden ? Theme.dim : Theme.accent).frame(width: 24)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).foregroundStyle(isHidden ? Theme.dim : Theme.fg).strikethrough(isHidden)
                if let sub { Text(sub).font(.caption).foregroundStyle(Theme.dim) }
                if moved { Text("Moved here").font(.caption).foregroundStyle(Theme.dim) }
            }
            Spacer()
            if moved {
                Button { moveBack(id) } label: { Image(systemName: "arrow.uturn.backward.circle") }
                    .buttonStyle(.borderless).accessibilityLabel("Move \(title) back")
            }
            Button {
                if isHidden { hidden.remove(id) } else { hidden.insert(id) }
            } label: {
                Image(systemName: isHidden ? "eye.slash" : "eye").foregroundStyle(isHidden ? Theme.warn : Theme.dim)
            }
            .buttonStyle(.borderless).accessibilityLabel(isHidden ? "Show \(title)" : "Hide \(title)")
        }
    }

    private func moveBack(_ id: String) {
        devices.removeAll { $0.id == id }
        if add.contains(id) { add.removeAll { $0 == id } } else { remove.append(id) }
    }

    private func load() async {
        do {
            let r = try await LayoutAPI.room(roomID)
            room = r; name = r.name == r.haName ? "" : r.name; symbol = r.symbol; hideRoom = r.hidden
            devices = r.devices; scenes = r.scenes
            hidden = Set(r.devices.filter(\.hidden).map(\.id) + r.scenes.filter { $0.hidden == true }.map(\.id))
        } catch { failed = true }
    }

    private func save() async {
        guard let r = room else { return }
        saving = true; defer { saving = false }
        let was = Set(r.devices.filter(\.hidden).map(\.id) + r.scenes.filter { $0.hidden == true }.map(\.id))
        do {
            let hide = Array(hidden.subtracting(was)), show = Array(was.subtracting(hidden))
            if !hide.isEmpty { try await LayoutAPI.setHidden(hide, true) }
            if !show.isEmpty { try await LayoutAPI.setHidden(show, false) }
            var body: [String: Any] = ["name": name.trimmingCharacters(in: .whitespaces), "hidden": hideRoom,
                                       "order": scenes.map(\.id) + devices.map(\.id), "add": add, "remove": remove]
            body["symbol"] = symbol ?? NSNull()
            _ = try await LayoutAPI.saveRoom(roomID, body)
            await store.loadHome()
            store.show("Room saved")
            dismiss()
        } catch { store.show("Couldn't save the room", isError: true) }
    }
}

/// Pick any device in the house (to move it into a room).
struct DevicePicker: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let exclude: Set<String>
    let pick: (DeviceItem) -> Void
    @State private var q = ""
    var body: some View {
        NavigationStack {
            let all = (store.home?.devices ?? []).filter { !exclude.contains($0.id) && $0.kind != .camera }
            let shown = q.isEmpty ? all : all.filter { $0.name.localizedCaseInsensitiveContains(q) }
            List(shown) { d in
                Button {
                    pick(d); dismiss()
                } label: {
                    InfoRow(DeviceSymbols.kindSymbol(d.kind), d.name, store.home?.room(d.roomId)?.name)
                }
                .buttonStyle(.plain)
            }
            .searchable(text: $q)
            .navigationTitle("Add a device").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

// MARK: - Hidden devices and clean-up

struct HiddenDevicesView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var layout: HouseLayout?
    @State private var failed = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let l = layout {
                    if !l.cleanup.isEmpty {
                        SectionHeader(title: "Clean up") {
                            Button("Hide all \(l.cleanup.count)") { Task { await set(l.cleanup.map(\.id), true) } }
                                .font(.subheadline.weight(.medium))
                        }
                        Text("These haven't responded for more than 3 days. They're probably gone or replaced.")
                            .font(.footnote).foregroundStyle(Theme.dim).padding(.horizontal, 4)
                        RowsCard {
                            ForEach(l.cleanup) { r in
                                InfoRow(symbol: icon(r.kind), color: Theme.dim, title: r.name, subtitle: sub(r)) {
                                    Button("Hide") { Task { await set([r.id], true) } }.buttonStyle(.bordered).controlSize(.small)
                                }
                            }
                        }
                    }
                    SectionHeader("Hidden for everyone")
                    if l.hidden.isEmpty {
                        EmptyCard(symbol: "eye", title: "Nothing hidden",
                                  detail: "Long-press a device and choose Hide for everyone, or edit a room.")
                    } else {
                        RowsCard {
                            ForEach(l.hidden) { r in
                                InfoRow(symbol: icon(r.kind), color: Theme.dim, title: r.name, subtitle: r.room) {
                                    Button("Show") { Task { await set([r.id], false) } }.buttonStyle(.bordered).controlSize(.small)
                                }
                            }
                        }
                    }
                    let rooms = l.rooms.filter(\.hidden)
                    if !rooms.isEmpty {
                        SectionHeader("Hidden rooms")
                        RowsCard {
                            ForEach(rooms) { r in
                                InfoRow(symbol: r.symbol ?? "square.split.2x2", color: Theme.dim, title: r.name) {
                                    Button("Show") { Task { await showRoom(r.id) } }.buttonStyle(.bordered).controlSize(.small)
                                }
                            }
                        }
                    }
                    SectionHeader("Rooms")
                    RowsCard {
                        ForEach(l.rooms.filter { !$0.hidden }) { r in
                            Button { router.go(.roomEdit(r.id)) } label: {
                                InfoRow(r.symbol ?? "square.split.2x2", r.name, nil, chevron: true)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } else if failed {
                    EmptyCard(symbol: "wifi.exclamationmark", title: "Couldn't load the layout")
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Hidden devices")
        .refreshable { await load() }
        .task { await load() }
    }

    private func icon(_ kind: String) -> String {
        switch kind {
        case "light": "lightbulb"; case "switch": "powerplug"; case "fan": "fan"; case "media_player": "tv"
        case "scene": "sparkles"; case "sensor": "gauge"; case "climate": "thermometer.medium"; case "appliance": "washer"
        default: "square.dashed"
        }
    }
    private func sub(_ r: HouseLayout.Row) -> String? {
        let since = r.since.flatMap(ISO.parse).map { "silent since \($0.formatted(date: .abbreviated, time: .omitted))" }
        return [r.room, since].compactMap { $0 }.joined(separator: " · ")
    }
    private func load() async {
        do { layout = try await HubClient.shared.get("/v1/home/layout"); failed = false } catch { failed = layout == nil }
    }
    private func set(_ ids: [String], _ hide: Bool) async {
        do {
            try await LayoutAPI.setHidden(ids, hide)
            store.show(hide ? (ids.count == 1 ? "Hidden for everyone" : "\(ids.count) hidden for everyone") : "Showing again")
            await load(); await store.loadHome()
        } catch { store.show("Couldn't change that", isError: true) }
    }
    private func showRoom(_ id: String) async {
        do {
            _ = try await LayoutAPI.saveRoom(id, ["hidden": false])
            await load(); await store.loadHome()
        } catch { store.show("Couldn't show the room", isError: true) }
    }
}

extension DeviceSymbols {
    static func kindSymbol(_ k: DeviceKind) -> String {
        switch k {
        case .light: "lightbulb"; case .fan: "fan"; case .switch: "powerplug"; case .climate: "thermometer.medium"
        case .lock: "lock"; case .garage: "door.garage.closed"; case .media: "tv"; case .speaker: "hifispeaker"
        case .camera: "video"; case .sensor: "gauge"; case .vacuum: "fan.oscillation"; case .oven: "oven"
        case .printer: "printer"; case .valve: "drop"; case .shower: "shower"; case .select: "switch.2"; case .unknown: "square.dashed"
        }
    }
}
