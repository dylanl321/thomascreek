import SwiftUI

/// The everyday control for one device. A tap does the obvious thing; a long press opens detail
/// (brightness and colour for lights; for Dylan, also the device internals).
struct DeviceTile: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let device: DeviceItem
    var big = false
    @State private var detail = false

    var body: some View {
        tile
            .opacity(device.isAvailable ? 1 : 0.55)
            .contextMenu { menu }
            .sheet(isPresented: $detail) { DeviceDetailSheet(deviceID: device.id) }
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
                      tint: ClimateText.color(d), big: big) { router.go(.climate) }
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
        default:
            StateTile(symbol: DeviceSymbols.symbol(d), title: d.name, detail: DeviceSymbols.value(d), on: d.isOn,
                      tint: Theme.accent, big: big) { detail = true }
        }
    }

    @ViewBuilder private var menu: some View {
        if device.kind == .light || store.role == .admin {
            Button("Details", systemImage: "slider.horizontal.3") { detail = true }
        }
        if device.kind == .light {
            Button("Full brightness", systemImage: "sun.max.fill") {
                store.perform("light.set", target: device.id, params: ["on": true, "brightness": 100]) { $0.state["on"] = .bool(true); $0.state["brightness"] = .number(100) }
            }
            Button("Dim", systemImage: "sun.min") {
                store.perform("light.set", target: device.id, params: ["on": true, "brightness": 25]) { $0.state["on"] = .bool(true); $0.state["brightness"] = .number(25) }
            }
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
    @State private var level: Double = 0
    @State private var editing = false

    private let swatches = ["#FFB46B", "#FFD9A8", "#FFFFFF", "#9FD3FF", "#FF5E5E", "#5EE07A", "#7D6BFF", "#FF7FD0"]

    var body: some View {
        NavigationStack {
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
                .navigationTitle(d.name).navigationBarTitleDisplayMode(.inline)
                .toolbar { Button("Done") { dismiss() } }
                .onAppear { level = d.brightness ?? 100 }
                .onChange(of: d.brightness) { _, v in if !editing, let v { level = v } }
            } else {
                ContentUnavailableView("Not found", systemImage: "questionmark")
            }
        }
        .presentationDetents([.medium, .large])
    }
}

/// Thermostat card with − / + that sends one change after the taps settle.
struct ClimateCard: View {
    @Environment(HomeStore.self) private var store
    let device: DeviceItem
    @State private var pending: Double?
    @State private var send: Task<Void, Never>?

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
                    step(-1, target)
                    Text("\(Int(target))°").font(.title2.weight(.bold).monospacedDigit()).frame(minWidth: 60)
                    step(1, target)
                    Spacer()
                    Menu {
                        ForEach(["heat", "cool", "auto", "off"], id: \.self) { m in
                            Button(m.capitalized) { store.perform("climate.set", target: device.id, params: ["hvac": m]) { $0.state["hvac"] = .string(m) } }
                        }
                    } label: {
                        Label((device["hvac"]?.string ?? "auto").capitalized, systemImage: "slider.horizontal.3")
                            .font(.subheadline.weight(.semibold))
                    }
                }
            }
        }
        .card()
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
