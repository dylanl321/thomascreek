import SwiftUI

/// The everyday control for one device. A tap does the obvious thing; a long press opens detail
/// (brightness and colour for lights; for Dylan, also the device internals).
struct DeviceTile: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let device: DeviceItem
    var big = false
    @State private var detail = false
    @State private var askWater = false

    var body: some View {
        tile
            .opacity(device.isAvailable ? 1 : 0.55)
            .contextMenu { menu }
            .sheet(isPresented: $detail) { DeviceDetailSheet(deviceID: device.id) }
            // Watering is never one stray tap away: starting a zone always asks how long first. An alert, not a
            // confirmation dialog, so Cancel always shows (iOS turns a dialog on a tile into a popover without it).
            .alert("Water \(device.displayName)?", isPresented: $askWater) {
                ForEach([5, 10, 15, 30], id: \.self) { m in
                    Button("Water \(m) min") { water(m) }
                }
                Button("Cancel", role: .cancel) {}
            }
    }

    @ViewBuilder private var tile: some View {
        let d = device
        switch d.kind {
        case .light:
            StateTile(symbol: d.isOn ? "lightbulb.fill" : "lightbulb", title: d.name,
                      detail: d.isAvailable ? (d.isOn ? d.brightness.map { "\(Int($0))%" } ?? "On" : "Off") : "Not responding",
                      on: d.isOn, tint: Color(hexString: d["color"]?.string) ?? Theme.warn, big: big) { toggle("light.set") }
        case .fan:
            StateTile(symbol: "fan.fill", title: d.name, detail: d.isOn ? (d["speed"]?.string?.capitalized ?? "On") : "Off",
                      on: d.isOn, tint: Theme.accent, big: big) { toggle("fan.set") }
        case .switch:
            StateTile(symbol: d.isOn ? "powerplug.fill" : "powerplug", title: d.name, detail: d.isOn ? "On" : "Off",
                      on: d.isOn, tint: Theme.accent, big: big) { toggle("switch.set") }
        case .climate:
            StateTile(symbol: "thermometer.medium", title: d.name, detail: ClimateText.summary(d), on: ClimateText.active(d),
                      tint: ClimateText.color(d), big: big) { router.go(.climateHistory(d.id)) }
        case .media:
            StateTile(symbol: "tv.fill", title: d.name, detail: d.isOn ? (d["source"]?.string ?? "On") : "Off",
                      on: d.isOn, tint: Theme.accent, big: big) { router.go(.remote(d.id)) }
        case .speaker:
            StateTile(symbol: "hifispeaker.fill", title: d.name,
                      detail: d["state"]?.string == "playing" ? (d["title"]?.string ?? "Playing") : "Idle",
                      on: d["state"]?.string == "playing", tint: Theme.accent, big: big) { router.go(.music) }
        case .garage:
            let open = d["open"]?.bool == true
            StateTile(symbol: open ? "door.garage.open" : "door.garage.closed", title: d.name, detail: open ? "Open" : "Closed",
                      on: open, tint: Theme.warn, big: big) {
                store.perform("garage.set", target: d.id, params: ["open": !open], confirm: "\(open ? "Close" : "Open") \(d.name)",
                              done: "\(d.name) \(open ? "closing" : "opening")")
            }
        case .lock:
            let locked = d["locked"]?.bool ?? true
            StateTile(symbol: locked ? "lock.fill" : "lock.open.fill", title: d.name, detail: locked ? "Locked" : "Unlocked",
                      on: !locked, tint: Theme.warn, big: big) {
                store.perform("lock.set", target: d.id, params: ["locked": !locked], confirm: "\(locked ? "Unlock" : "Lock") \(d.name)",
                              done: "\(d.name) \(locked ? "unlocked" : "locked")")
            }
        case .valve:
            StateTile(symbol: "drop.fill", title: d.displayName, detail: DeviceSymbols.value(d), on: d.isOn,
                      tint: Theme.accent, big: big) { if d.isOn { water(nil) } else { askWater = true } }
        case .oven:
            StateTile(symbol: "oven.fill", title: d.name, detail: DeviceSymbols.value(d), on: d.isOn,
                      tint: Theme.warn, big: big) { router.go(.kitchen(d.id)) }
        default:
            StateTile(symbol: DeviceSymbols.symbol(d), title: d.name, detail: DeviceSymbols.value(d), on: d.isOn,
                      tint: Theme.accent, big: big) { router.follow(.device(d.id)) }
        }
    }

    @ViewBuilder private var menu: some View {
        FavoriteMenuItem(kind: .device, id: device.id, name: device.name)
        if device.kind == .light || store.role == .admin {
            Button("Details", systemImage: "slider.horizontal.3") { router.follow(.device(device.id)) }
        }
        if device.kind == .valve {
            ForEach([5, 15, 30], id: \.self) { m in
                Button("Water \(m) min", systemImage: "drop") { water(m) }
            }
            if device.isOn { Button("Stop", systemImage: "stop.fill") { water(nil) } }
        }
        if device.kind == .light {
            Button("Full brightness", systemImage: "sun.max.fill") {
                store.perform("light.set", target: device.id, params: ["on": true, "brightness": 100]) { $0.state["on"] = .bool(true); $0.state["brightness"] = .number(100) }
            }
            Button("Dim", systemImage: "sun.min") {
                store.perform("light.set", target: device.id, params: ["on": true, "brightness": 25]) { $0.state["on"] = .bool(true); $0.state["brightness"] = .number(25) }
            }
        }
        HideForEveryoneButton(deviceID: device.id, name: device.name)
    }

    /// Start a zone for a set time (the hub turns it off), or stop it with nil.
    private func water(_ minutes: Int?) {
        if let minutes {
            store.perform("valve.run", target: device.id, params: ["on": true, "minutes": minutes]) {
                $0.state["on"] = .bool(true); $0.state["remainingS"] = .number(Double(minutes * 60))
            }
        } else {
            store.perform("valve.run", target: device.id, params: ["on": false]) { $0.state["on"] = .bool(false) }
        }
    }

    private func toggle(_ action: String) {
        let on = !device.isOn
        store.perform(action, target: device.id, params: ["on": on]) { $0.state["on"] = .bool(on) }
    }
}

enum DeviceSymbols {
    static func symbol(_ d: DeviceItem) -> String {
        switch d.kind {
        case .oven: "oven.fill"
        case .vacuum: "fan.oscillation.fill"
        case .printer: "printer.fill"
        case .valve: "drop.fill"
        case .shower: "shower.fill"
        case .camera: "video.fill"
        case .sensor:
            switch d["unit"]?.string {
            case "°F", "°C": "thermometer"
            case "%": "humidity"
            default: "sensor.fill"
            }
        default: "circle.grid.2x2"
        }
    }

    static func value(_ d: DeviceItem) -> String {
        if !d.isAvailable { return "Not responding" }
        switch d.kind {
        case .sensor: return [d["value"]?.string, d["unit"]?.string].compactMap { $0 }.joined()
        case .oven:
            if let t = d["currentTemp"]?.double, d.isOn { return "\(Int(t))° heating to \(Int(d["targetTemp"]?.double ?? t))°" }
            return d.isOn ? "On" : "Off"
        case .printer:
            if let p = d["progress"]?.double, d["state"]?.string == "printing" {
                var s = "\(Int(p * 100))%"
                if let r = d["remainingS"]?.double { s += " · \(Int(r / 60)) min left" }
                return s
            }
            return d["state"]?.string?.capitalized ?? "Idle"
        case .valve: return d.isOn ? (d["remainingS"]?.double.map { "Watering, \(Int($0 / 60)) min left" } ?? "Watering") : "Off"
        case .vacuum: return d["state"]?.string?.capitalized ?? "Docked"
        case .shower: return d.isOn ? "Heating to \(Int(d["targetTemp"]?.double ?? 105))°" : "Off"
        default: return d["state"]?.string?.capitalized ?? (d.isOn ? "On" : "Off")
        }
    }
}

enum ClimateText {
    static func summary(_ d: DeviceItem) -> String {
        guard d.isAvailable else { return "Not responding" }
        let cur = d["currentTemp"]?.double.map { "\(Int($0.rounded()))°" } ?? "–"
        let target = d["targetTemp"]?.double.map { "\(Int($0.rounded()))°" }
        // Auto (heat_cool) holds a range rather than one target.
        if d["hvac"]?.string == "heat_cool", let lo = d["targetLow"]?.double, let hi = d["targetHigh"]?.double {
            return "\(cur) · Holding \(Int(lo.rounded()))–\(Int(hi.rounded()))°"
        }
        switch d["hvacAction"]?.string {
        case "heating": return "\(cur) heating to \(target ?? "")"
        case "cooling": return "\(cur) cooling to \(target ?? "")"
        default: return d["hvac"]?.string == "off" ? "\(cur) · Off" : "\(cur) · set \(target ?? "–")"
        }
    }
    static func active(_ d: DeviceItem) -> Bool { ["heating", "cooling"].contains(d["hvacAction"]?.string ?? "") }
    static func color(_ d: DeviceItem) -> Color { d["hvacAction"]?.string == "cooling" ? .blue : .orange }
}

/// Brightness and colour for a light; on/off and value for others; device internals for Dylan.
struct DeviceDetailSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let deviceID: String

    var body: some View {
        NavigationStack {
            DeviceDetailForm(deviceID: deviceID)
                .navigationTitle(store.home?.device(deviceID)?.name ?? "Device").navigationBarTitleDisplayMode(.inline)
                .toolbar { Button("Done") { dismiss() } }
        }
        .presentationDetents([.medium, .large])
    }
}

/// The controls and internals for one device, shared by the sheet and the device's own page.
/// `extra` adds sections in between (where it is, related screens).
struct DeviceDetailForm<Extra: View>: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    @ViewBuilder var extra: (DeviceItem) -> Extra
    @State private var level: Double = 0
    @State private var editing = false

    private let swatches = ["#FFB46B", "#FFD9A8", "#FFFFFF", "#9FD3FF", "#FF5E5E", "#5EE07A", "#7D6BFF", "#FF7FD0"]

    var body: some View {
        Group {
            if let d = store.home?.device(deviceID) {
                Form {
                    Section {
                        Toggle(isOn: Binding(get: { d.isOn }, set: { on in
                            store.perform("\(d.kind.rawValue).set", target: d.id, params: ["on": on]) { $0.state["on"] = .bool(on) }
                        })) { Label(d.name, systemImage: DeviceSymbols.symbol(d)) }
                        if d.kind == .light {
                            VStack(alignment: .leading) {
                                HStack { Text("Brightness"); Spacer(); Text("\(Int(level))%").monospacedDigit().foregroundStyle(Theme.dim) }
                                Slider(value: $level, in: 1...100, step: 1) { e in
                                    editing = e
                                    if !e {
                                        let v = Int(level)
                                        store.perform("light.set", target: d.id, params: ["on": true, "brightness": v]) {
                                            $0.state["on"] = .bool(true); $0.state["brightness"] = .number(Double(v))
                                        }
                                    }
                                }
                            }
                            if d.actions?.contains("color") ?? (d["color"] != nil) {
                                LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 8)) {
                                    ForEach(swatches, id: \.self) { hex in
                                        Button {
                                            store.perform("light.set", target: d.id, params: ["on": true, "color": hex]) { $0.state["color"] = .string(hex) }
                                        } label: {
                                            Circle().fill(Color(hexString: hex) ?? .white).frame(height: 30)
                                                .overlay(Circle().stroke(Theme.line))
                                        }
                                        .buttonStyle(.plain)
                                    }
                                }
                                .padding(.vertical, 4)
                            }
                        }
                    } footer: {
                        if let u = d.updated { Text("Updated \(u.formatted(.relative(presentation: .named)))") }
                    }
                    extra(d)
                    if store.role == .admin {
                        Section("Internals") {
                            LabeledContent("Id", value: d.id).font(.footnote.monospaced())
                            LabeledContent("Kind", value: d.kind.rawValue)
                            ForEach(d.state.keys.sorted(), id: \.self) { k in
                                LabeledContent(k, value: d.state[k]?.string ?? "…").font(.footnote.monospaced())
                            }
                        }
                    }
                }
                .onAppear { level = d.brightness ?? 100 }
                .onChange(of: d.brightness) { _, v in if !editing, let v { level = v } }
            } else {
                ContentUnavailableView("Not found", systemImage: "questionmark")
            }
        }
    }
}

extension DeviceDetailForm where Extra == EmptyView {
    init(deviceID: String) { self.init(deviceID: deviceID) { _ in EmptyView() } }
}

/// Thermostat card with − / + that sends one change after the taps settle.
struct ClimateCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let device: DeviceItem
    /// The history screen shows the card itself, so it hides the row that opens it.
    var showsHistory = true
    @State private var pending: Double?
    @State private var pendingLow: Double?
    @State private var pendingHigh: Double?
    @State private var send: Task<Void, Never>?

    /// Auto (heat_cool) holds a range, so it gets a heat-to and a cool-to control instead of one target.
    private var holdsRange: Bool {
        device["hvac"]?.string == "heat_cool" && device["targetLow"]?.double != nil && device["targetHigh"]?.double != nil
    }
    /// Thermostats that report a low/high pair call their Auto mode heat_cool in Home Assistant.
    private var autoMode: String { device["targetLow"]?.double != nil || device["targetHigh"]?.double != nil ? "heat_cool" : "auto" }

    private static func modeName(_ m: String?) -> String {
        switch m {
        case "heat_cool", "auto", nil: "Auto"
        case "fan_only": "Fan"
        case let m?: m.capitalized
        }
    }

    var body: some View {
        let target = pending ?? device["targetTemp"]?.double ?? 70
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(device.name).font(.headline)
                Spacer()
                Freshness(date: device.updated)
            }
            HStack(alignment: .firstTextBaseline) {
                Text(device["currentTemp"]?.double.map { "\(Int($0.rounded()))°" } ?? "–")
                    .font(.system(size: 44, weight: .semibold, design: .rounded))
                Text(ClimateText.summary(device)).font(.subheadline).foregroundStyle(Theme.dim)
                Spacer()
            }
            if store.me?.can(.climateAll) == true || store.me?.can(.climateOwnRoom) == true || store.role == .guest {
                HStack(spacing: 12) {
                    if holdsRange {
                        let low = pendingLow ?? device["targetLow"]?.double ?? 68
                        let high = pendingHigh ?? device["targetHigh"]?.double ?? 76
                        rangeStepper("Heat to", value: low, tint: .orange) { v in if v <= high - 2 { sendRange(v, high) } }
                        rangeStepper("Cool to", value: high, tint: .blue) { v in if v >= low + 2 { sendRange(low, v) } }
                    } else if device["hvac"]?.string != "off" {
                        step(-1, target)
                        Text("\(Int(target))°").font(.title2.weight(.bold).monospacedDigit()).frame(minWidth: 60)
                        step(1, target)
                    }
                    Spacer()
                    Menu {
                        ForEach(["heat", "cool", autoMode, "off"], id: \.self) { m in
                            Button(Self.modeName(m)) { store.perform("climate.set", target: device.id, params: ["hvac": m]) { $0.state["hvac"] = .string(m) } }
                        }
                    } label: {
                        Label(Self.modeName(device["hvac"]?.string), systemImage: "slider.horizontal.3")
                            .font(.subheadline.weight(.semibold))
                    }
                }
            }
            if showsHistory {
                Divider().overlay(Theme.line)
                Button { router.go(.climateHistory(device.id)) } label: {
                    HStack {
                        Label("History", systemImage: "chart.xyaxis.line").font(.subheadline.weight(.semibold))
                        Spacer()
                        Text("Temperature, set points, when it ran").font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                        Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .card()
    }

    /// "Heat to 65°" with − and +; each change waits for the taps to settle, then sends both ends together.
    private func rangeStepper(_ title: String, value: Double, tint: Color, _ change: @escaping (Double) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(tint)
            HStack(spacing: 6) {
                smallStep("minus", "\(title) down") { change(value - 1) }
                Text("\(Int(value))°").font(.title3.weight(.bold).monospacedDigit()).frame(minWidth: 40)
                smallStep("plus", "\(title) up") { change(value + 1) }
            }
        }
    }

    private func smallStep(_ symbol: String, _ label: String, _ tap: @escaping () -> Void) -> some View {
        Button(action: tap) {
            Image(systemName: symbol).font(.subheadline.weight(.bold))
                .frame(width: 36, height: 36).background(Theme.card2, in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func sendRange(_ low: Double, _ high: Double) {
        pendingLow = low
        pendingHigh = high
        send?.cancel()
        send = Task {
            try? await Task.sleep(for: .milliseconds(900))
            guard !Task.isCancelled else { return }
            store.perform("climate.set", target: device.id, params: ["target_low": low, "target_high": high]) {
                $0.state["targetLow"] = .number(low); $0.state["targetHigh"] = .number(high)
                $0.state["targetTemp"] = .number(((low + high) / 2 * 10).rounded() / 10)
            }
            pendingLow = nil
            pendingHigh = nil
        }
    }

    private func step(_ delta: Double, _ current: Double) -> some View {
        Button {
            let v = current + delta
            pending = v
            send?.cancel()
            send = Task {
                try? await Task.sleep(for: .milliseconds(900))
                guard !Task.isCancelled else { return }
                store.perform("climate.set", target: device.id, params: ["target_temp": v]) { $0.state["targetTemp"] = .number(v) }
                pending = nil
            }
        } label: {
            Image(systemName: delta < 0 ? "minus" : "plus").font(.title3.weight(.bold))
                .frame(width: 48, height: 48).background(Theme.card2, in: Circle())
        }
        .buttonStyle(.plain)
    }
}

extension DeviceItem {
    /// Sprinkler zones come from Home Assistant as "Irrigation Controller Backyard 1"; the controller part
    /// is the same on every tile and pushes the zone name out of view, so drop it.
    var displayName: String {
        guard kind == .valve else { return name }
        for prefix in ["Irrigation Controller ", "Sprinkler Controller ", "Irrigation ", "Sprinkler "]
        where name.count > prefix.count && name.lowercased().hasPrefix(prefix.lowercased()) {
            return String(name.dropFirst(prefix.count))
        }
        return name
    }
}
