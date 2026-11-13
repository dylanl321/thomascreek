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
    @State private var local: [String: Bool] = [:]
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
                                            set: { set(device, $0) })
                            .id(device.name)
                    }
                    if let climate {
                        SectionHeader("Climate", live: true)
                        ClimateCard(device: climate)
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
                            if let device = room.device(name) { set(device, !isOn(name)) }
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
                .background(Color(uiColor: UIColor(hex: 0xF1ECE4)))
                .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))

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
        room.devices.devices.map { "\($0.name):\(isOn($0.name) ? 1 : 0)" }.joined(separator: ",")
    }

    private func states() -> [String: Bool] {
        Dictionary(uniqueKeysWithValues: room.devices.devices.map { ($0.name, isOn($0.name)) })
    }

    private func known(_ name: String) -> [DeviceItem] {
        room.entities(for: name).compactMap { store.home?.device($0.id) }
    }

    private func isOn(_ name: String) -> Bool {
        let found = known(name)
        if found.isEmpty { return local[name] ?? false }
        return found.contains { $0.isOn }
    }

    private func set(_ device: DollhouseDevice, _ on: Bool) {
        let entities = room.entities(for: device.name)
        if entities.isEmpty || known(device.name).isEmpty {
            local[device.name] = on
        }
        for entity in entities {
            send(entity, on: on, label: device.label)
        }
    }

    private func applyPreset(_ id: String) {
        let desired = room.preset(id)
        for device in room.devices.devices {
            let on = desired[device.name] ?? false
            let entities = room.entities(for: device.name)
            if entities.isEmpty || known(device.name).isEmpty { local[device.name] = on }
            for entity in entities where entity.action != "media.command" || known(device.name).contains(where: { $0.id == entity.id }) {
                send(entity, on: on, label: device.label)
            }
        }
    }

    /// Face ID is asked only for the actions the rest of the app already treats as secure.
    private func send(_ entity: MappedEntity, on: Bool, label: String) {
        let confirm = SecureAction.reason(for: entity.action, name: label)
        if entity.action == "media.command" {
            guard let device = store.home?.device(entity.id) else { return }
            if device.isOn == on { return }
            store.perform("media.command", target: entity.id, params: ["command": entity.command ?? "power"], confirm: confirm, optimistic: {
                $0.state["on"] = .bool(on)
                $0.state["state"] = .string(on ? "on" : "off")
            })
            return
        }
        store.perform(entity.action, target: entity.id, params: ["on": on], confirm: confirm, optimistic: {
            $0.state["on"] = .bool(on)
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
    @State private var level: Double = 100
    @State private var editing = false

    private var entities: [MappedEntity] { room.entities(for: device.name) }
    private var known: [DeviceItem] { entities.compactMap { store.home?.device($0.id) } }
    private var lights: [DeviceItem] { known.filter { $0.kind == .light } }
    private var linked: Bool { !entities.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: device.symbol)
                    .font(.title3)
                    .foregroundStyle(on ? Theme.warn : Theme.dim)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(device.label).font(.headline)
                    Text(status).font(.footnote).foregroundStyle(Theme.dim)
                }
                Spacer(minLength: 8)
                Toggle(device.label, isOn: Binding(get: { on }, set: set))
                    .labelsHidden()
            }
            if on, !lights.isEmpty {
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
            if !linked {
                Text("No hub id yet. Add one under \"\(device.name)\" in hub-mapping.json. Until then this changes the model only.")
                    .font(.footnote)
                    .foregroundStyle(Theme.dim)
            } else if known.isEmpty {
                Text(entities.map(\.id).joined(separator: ", ") + " isn't in the latest update from home.")
                    .font(.footnote)
                    .foregroundStyle(Theme.dim)
            }
            if store.role == .admin, linked {
                Text(entities.map(\.id).joined(separator: "\n"))
                    .font(.caption2.monospaced())
                    .foregroundStyle(Theme.dim)
            }
        }
        .card()
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).stroke(highlighted ? Theme.accent : .clear, lineWidth: 2))
        .onAppear { adoptBrightness() }
        .onChange(of: brightness) { _, value in
            if !editing, let value { level = value }
        }
    }

    private var status: String {
        if !linked { return on ? "On in the model" : "Off" }
        if known.isEmpty { return "Waiting for home" }
        if known.allSatisfy({ !$0.isAvailable }) { return "Not responding" }
        if !on { return "Off" }
        if let value = brightness { return "\(Int(value))%" }
        if let source = known.first?["source"]?.string, !source.isEmpty { return source }
        return "On"
    }

    private var brightness: Double? {
        let values = lights.compactMap(\.brightness)
        return values.max()
    }

    private func adoptBrightness() { level = brightness ?? 100 }

    private func commitBrightness() {
        let value = Int(level)
        for entity in entities where entity.action == "light.set" {
            store.perform("light.set", target: entity.id, params: ["on": true, "brightness": value],
                          confirm: SecureAction.reason(for: "light.set", name: device.label), optimistic: {
                $0.state["on"] = .bool(true)
                $0.state["brightness"] = .number(Double(value))
            })
        }
    }
}
