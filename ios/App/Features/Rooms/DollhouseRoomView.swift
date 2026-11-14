import SwiftUI
import UIKit

/// The living room, and any later room dropped into `Rooms/catalog.json`.
/// The hero is the model. The sections under it are the same devices. Advanced opens the existing room screen.
struct DollhouseRoomView: View {
    let roomID: String

    var body: some View {
        if let room = DollhouseCatalog.room(roomID) {
            DollhouseRoomPage(room: room)
        } else {
            ContentUnavailableView("This room doesn't have a model yet", systemImage: "cube.transparent",
                                    description: Text("Add it to Rooms/catalog.json when the model is ready."))
        }
    }
}

private struct DollhouseRoomPage: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let room: DollhouseRoom
    @State private var controller: DollhouseController
    @State private var loaded = false
    @State private var failure: String?
    @State private var highlighted: String?

    init(room: DollhouseRoom) {
        self.room = room
        _controller = State(initialValue: DollhouseController(room: room))
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    hero(proxy)
                    presets
                    ForEach(room.devices.devices) { device in
                        DollhouseDeviceCard(room: room, device: device, on: isOn(device.name), highlighted: highlighted == device.name,
                                            set: { set(device, $0) }, choose: { setOption(device, $0) })
                            .id(device.name)
                    }
                    if let extras = room.mapping.also, !extras.isEmpty {
                        SectionHeader("Also in this room")
                        ForEach(extras) { extra in
                            AlsoInRoomCard(extra: extra)
                        }
                    }
                    SectionHeader("Climate", live: climate != nil)
                    if let climate {
                        ClimateCard(device: climate)
                    } else {
                        NotSetUpCard(symbol: "thermometer.medium", title: "Climate",
                                     note: "Set climate_entity_id in this room's hub-mapping.json.")
                    }
                    advanced
                }
                .screen()
            }
        }
        .background(Backdrop())
        .navigationTitle(room.title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Advanced") { router.go(.room(room.mapping.roomId)) }
            }
        }
        .task {
            guard !loaded else { return }
            controller.load()
            failure = controller.error
            loaded = true
            controller.apply(states(), animated: false)
        }
        .onChange(of: pictureKey) { _, _ in
            guard loaded else { return }
            controller.apply(states(), animated: true)
        }
    }

    private func hero(_ proxy: ScrollViewProxy) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ZStack(alignment: .topTrailing) {
                Group {
                    if let failure {
                        ContentUnavailableView("The model didn't open", systemImage: "cube.transparent", description: Text(failure))
                    } else if loaded {
                        DollhouseViewport(controller: controller) { name in
                            if let device = room.device(name), room.isConfigured(name), room.control(for: name) != .status {
                                set(device, !isOn(name))
                            }
                            withAnimation { proxy.scrollTo(name, anchor: .center) }
                            highlighted = name
                            Task {
                                try? await Task.sleep(for: .seconds(1.6))
                                if highlighted == name { highlighted = nil }
                            }
                        }
                    } else {
                        ProgressView("Opening \(room.title)…")
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: 440)
                // No card: the room sits on the app background and its edges fade into it.
                .mask(
                    EllipticalGradient(stops: [.init(color: .black, location: 0),
                                               .init(color: .black, location: 0.62),
                                               .init(color: .clear, location: 1)],
                                       center: .center, startRadiusFraction: 0, endRadiusFraction: 0.5)
                )

                Menu {
                    Button("Dollhouse", systemImage: "cube.transparent") { controller.place(.dollhouse, animated: true) }
                    Button("Closer", systemImage: "plus.magnifyingglass") { controller.place(.close, animated: true) }
                    Button("Top", systemImage: "square.split.bottomrightquarter") { controller.place(.top, animated: true) }
                } label: {
                    Image(systemName: "view.3d")
                        .font(.body.weight(.semibold))
                        .padding(10)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .padding(12)
                .accessibilityLabel("Camera")
            }
            HStack {
                Freshness(date: store.home?.generated, live: true)
                Spacer()
                Text("Tap a light in the model")
                    .font(.caption)
                    .foregroundStyle(Theme.dim)
            }
        }
    }

    private var presets: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionHeader("Scenes")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(room.presetIDs(), id: \.self) { id in
                        Button(DollhouseRoom.presetTitle(id)) { applyPreset(id) }
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .background(Theme.card, in: Capsule())
                            .overlay(Capsule().stroke(Theme.line))
                    }
                }
            }
            Text("Linked devices change in the house. Anything still unlinked changes in the model only.")
                .font(.footnote)
                .foregroundStyle(Theme.dim)
        }
    }

    private var advanced: some View {
        Button { router.go(.room(room.mapping.roomId)) } label: {
            HStack(spacing: 12) {
                Image(systemName: "slider.horizontal.3").foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Advanced").font(.headline).foregroundStyle(Theme.fg)
                    Text("Every control already on the \(room.title) screen").font(.footnote).foregroundStyle(Theme.dim)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
            }
            .card()
        }
        .buttonStyle(.plain)
    }

    private var climate: DeviceItem? {
        if let id = room.mapping.climateEntityId, let device = store.home?.device(id), device.kind == .climate {
            return device
        }
        return store.home?.devices?.first {
            $0.kind == .climate && $0.roomId == room.mapping.roomId && $0.isAvailable
        }
    }

    /// Changes when a linked device or a model-only override changes, so the hero eases to the new picture.
    private var pictureKey: String {
        room.devices.devices.map { "\($0.name):\(Int((level($0.name) * 100).rounded()))" }.joined(separator: ",")
    }

    private func states() -> [String: Double] {
        Dictionary(uniqueKeysWithValues: room.devices.devices.map { ($0.name, level($0.name)) })
    }

    private func known(_ name: String) -> [DeviceItem] {
        room.entities(for: name).compactMap { entity in entity.id.flatMap { store.home?.device($0) } }
    }

    private func optionValue(_ name: String) -> String? {
        known(name).compactMap { $0["value"]?.string ?? $0["state"]?.string }.first
    }

    private func isRunning(_ name: String) -> Bool {
        for entity in room.entities(for: name) {
            guard let id = entity.id, !id.isEmpty else { continue }
            if store.home?.appliances?.first(where: { $0.id == id })?.running == true { return true }
            if let device = store.home?.device(id) {
                if device.isOn { return true }
                let raw = (device["value"]?.string ?? device["state"]?.string ?? "").lowercased()
                if ["running", "on", "washing", "drying"].contains(raw) { return true }
            }
        }
        return false
    }

    private func isOn(_ name: String) -> Bool {
        guard room.isConfigured(name) else { return false }
        switch room.control(for: name) {
        case .status: return isRunning(name)
        case .select:
            let off = room.entities(for: name).first?.offOption ?? "Off"
            guard let value = optionValue(name) else { return false }
            return value.compare(off, options: .caseInsensitive) != .orderedSame && value.lowercased() != "off"
        case .light, .switch, .media, .fan:
            return known(name).contains { $0.isOn }
        }
    }

    /// 0 is off. A light that reports brightness uses that percent. Anything else that is on is full.
    private func level(_ name: String) -> Double {
        guard isOn(name) else { return 0 }
        guard room.control(for: name) == .light else { return 1 }
        let brights = known(name).filter(\.isOn).compactMap(\.brightness)
        guard let maxB = brights.max() else { return 1 }
        return min(1, max(0, maxB / 100))
    }

    private func set(_ device: DollhouseDevice, _ on: Bool) {
        guard room.isConfigured(device.name), room.control(for: device.name) != .status else { return }
        for entity in room.entities(for: device.name) {
            send(entity, on: on, label: device.label)
        }
    }

    private func setOption(_ device: DollhouseDevice, _ option: String) {
        guard let entity = room.entities(for: device.name).first, let id = entity.id, !id.isEmpty else { return }
        let off = entity.offOption ?? "Off"
        let on = option.compare(off, options: .caseInsensitive) != .orderedSame && option.lowercased() != "off"
        store.perform("select.set", target: id, params: ["option": option],
                      confirm: SecureAction.reason(for: "select.set", name: device.label), optimistic: {
            $0.state["value"] = .string(option)
            $0.state["state"] = .string(option)
            $0.state["on"] = .bool(on)
        })
    }

    private func applyPreset(_ id: String) {
        let desired = room.preset(id)
        for device in room.devices.devices {
            guard room.isConfigured(device.name), room.control(for: device.name) != .status else { continue }
            let amount = desired[device.name] ?? 0
            for entity in room.entities(for: device.name) {
                let brightness = entity.action == "light.set" && amount > 0 ? Int((amount * 100).rounded()) : nil
                send(entity, on: amount > 0, label: device.label, brightness: brightness)
            }
        }
    }

    /// Face ID is asked only for the actions the rest of the app already treats as secure.
    private func send(_ entity: MappedEntity, on: Bool, label: String, brightness: Int? = nil) {
        guard let id = entity.id, !id.isEmpty else { return }
        let confirm = SecureAction.reason(for: entity.action, name: label)
        if entity.action == "select.set" {
            let option = on ? (entity.onOption ?? "High") : (entity.offOption ?? "Off")
            store.perform("select.set", target: id, params: ["option": option], confirm: confirm, optimistic: {
                $0.state["value"] = .string(option)
                $0.state["state"] = .string(option)
                $0.state["on"] = .bool(on)
            })
            return
        }
        if entity.action == "media.command" {
            guard let device = store.home?.device(id) else { return }
            if device.isOn == on { return }
            store.perform("media.command", target: id, params: ["command": entity.command ?? "power"], confirm: confirm, optimistic: {
                $0.state["on"] = .bool(on)
                $0.state["state"] = .string(on ? "on" : "off")
            })
            return
        }
        var params: [String: Any] = ["on": on]
        if let brightness { params["brightness"] = brightness }
        store.perform(entity.action, target: id, params: params, confirm: confirm, optimistic: {
            $0.state["on"] = .bool(on)
            if let brightness { $0.state["brightness"] = .number(Double(brightness)) }
        })
    }
}

private struct DollhouseDeviceCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let room: DollhouseRoom
    let device: DollhouseDevice
    let on: Bool
    let highlighted: Bool
    let set: (Bool) -> Void
    let choose: (String) -> Void
    @State private var level: Double = 100
    @State private var editing = false

    private var control: DollhouseControl { room.control(for: device.name) }
    private var entities: [MappedEntity] { room.entities(for: device.name) }
    private var configured: Bool { room.isConfigured(device.name) }
    private var known: [DeviceItem] { entities.compactMap { entity in entity.id.flatMap { store.home?.device($0) } } }
    private var lights: [DeviceItem] { known.filter { $0.kind == .light } }
    private var note: String? { room.mapping.devices[device.name]?.note ?? entities.compactMap(\.note).first }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: device.symbol)
                    .font(.title3)
                    .foregroundStyle(on ? Theme.warn : Theme.dim)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(device.label).font(.headline)
                    Text(status).font(.footnote).foregroundStyle(configured ? Theme.dim : Theme.warn)
                }
                Spacer(minLength: 8)
                if configured, control != .status, control != .select {
                    Toggle(device.label, isOn: Binding(get: { on }, set: set))
                        .labelsHidden()
                }
            }
            if control == .select {
                selectChoices
            }
            if configured, on, control == .light, !lights.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Brightness")
                        Spacer()
                        Text("\(Int(level))%").monospacedDigit().foregroundStyle(Theme.dim)
                    }
                    .font(.subheadline)
                    Slider(value: $level, in: 1...100, step: 1) { editingNow in
                        editing = editingNow
                        if !editingNow { commitBrightness() }
                    }
                }
            }
            if let media = known.first(where: { $0.kind == .media || $0.kind == .speaker }) {
                Button { router.go(media.kind == .speaker ? .music : .remote(media.id)) } label: {
                    Label(media.kind == .speaker ? "Music" : "Remote", systemImage: media.kind == .speaker ? "music.note" : "av.remote.fill")
                        .font(.subheadline.weight(.semibold))
                }
                .buttonStyle(.bordered)
            }
            if !configured, let note {
                Text(note).font(.footnote).foregroundStyle(Theme.dim)
            } else if configured, known.isEmpty, control != .status {
                Text((entities.compactMap(\.id).joined(separator: ", ")) + " isn't in the latest update from home.")
                    .font(.footnote).foregroundStyle(Theme.dim)
            }
            if store.role == .admin, configured {
                Text(entities.compactMap(\.id).joined(separator: "\n"))
                    .font(.caption2.monospaced()).foregroundStyle(Theme.dim)
            }
        }
        .card()
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).stroke(highlighted ? Theme.accent : .clear, lineWidth: 2))
        .onAppear { adoptBrightness() }
        .onChange(of: brightness) { _, value in
            if !editing, let value { level = value }
        }
    }

    private var selectChoices: some View {
        let choices = entities.first?.options ?? ["Off", "Dim", "High"]
        let current = known.compactMap { $0["value"]?.string ?? $0["state"]?.string }.first
        return HStack(spacing: 8) {
            ForEach(choices, id: \.self) { choice in
                Button(choice) { if configured { choose(choice) } }
                    .font(.subheadline.weight(.semibold))
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(current?.caseInsensitiveCompare(choice) == .orderedSame ? Theme.accent.opacity(0.18) : Theme.card2, in: Capsule())
                    .foregroundStyle(configured ? Theme.fg : Theme.dim)
                    .disabled(!configured)
            }
        }
    }

    private var status: String {
        if !configured { return "Not set up" }
        if control == .status {
            if known.isEmpty, store.home?.appliances?.contains(where: { entities.compactMap(\.id).contains($0.id) }) != true {
                return "Waiting for home"
            }
            return on ? "Running" : "Idle"
        }
        if control == .select {
            return known.compactMap { $0["value"]?.string ?? $0["state"]?.string }.first ?? "Off"
        }
        if known.isEmpty { return "Waiting for home" }
        if known.allSatisfy({ !$0.isAvailable }) { return "Not responding" }
        if !on { return "Off" }
        if let value = brightness { return "\(Int(value))%" }
        if let source = known.first?["source"]?.string, !source.isEmpty { return source }
        return "On"
    }

    private var brightness: Double? { lights.compactMap(\.brightness).max() }
    private func adoptBrightness() { level = brightness ?? 100 }

    private func commitBrightness() {
        let value = Int(level)
        for entity in entities where entity.action == "light.set" {
            guard let id = entity.id, !id.isEmpty else { continue }
            store.perform("light.set", target: id, params: ["on": true, "brightness": value],
                          confirm: SecureAction.reason(for: "light.set", name: device.label), optimistic: {
                $0.state["on"] = .bool(true)
                $0.state["brightness"] = .number(Double(value))
            })
        }
    }
}

private struct NotSetUpCard: View {
    let symbol: String
    let title: String
    let note: String
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).font(.title3).foregroundStyle(Theme.dim).frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text("Not set up").font(.footnote).foregroundStyle(Theme.warn)
                Text(note).font(.footnote).foregroundStyle(Theme.dim)
            }
        }
        .card()
    }
}

private struct AlsoInRoomCard: View {
    @Environment(HomeStore.self) private var store
    let extra: AlsoControl

    private var device: DeviceItem? { store.home?.device(extra.id) }
    private var on: Bool { device?.isOn == true }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: extra.action == "switch.set" ? "powerplug.fill" : "lightbulb.fill")
                .font(.title3).foregroundStyle(on ? Theme.warn : Theme.dim).frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(extra.name).font(.headline)
                Text(device == nil ? "Not in the latest update" : (on ? "On" : "Off"))
                    .font(.footnote).foregroundStyle(Theme.dim)
                if let note = extra.note { Text(note).font(.caption).foregroundStyle(Theme.dim) }
            }
            Spacer(minLength: 8)
            if device != nil {
                Toggle(extra.name, isOn: Binding(get: { on }, set: set))
                    .labelsHidden()
            }
        }
        .card()
    }

    private func set(_ on: Bool) {
        store.perform(extra.action, target: extra.id, params: ["on": on],
                      confirm: SecureAction.reason(for: extra.action, name: extra.name), optimistic: {
            $0.state["on"] = .bool(on)
        })
    }
}

