import SwiftUI

/// One device on its own page: its controls, where it is, and the screens it connects to.
/// Opened from a tile's long press, Activity, status lines, cards and Ask Thomas Creek's answers.
struct DevicePage: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String

    var body: some View {
        let d = store.home?.device(deviceID)
        DeviceDetailForm(deviceID: deviceID) { d in related(d) }
            .scrollContentBackground(.hidden).background(Backdrop())
            .navigationTitle(d?.displayName ?? "Device")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if let d { ToolbarItem(placement: .primaryAction) { StarButton(kind: .device, id: d.id, name: d.name) } }
            }
    }

    @ViewBuilder private func related(_ d: DeviceItem) -> some View {
        let room = store.home?.room(d.roomId)
        Section {
            if let room {
                row("Room", room.name, symbol: room.symbol ?? "square.split.bottomrightquarter")
                    .links(to: .room(room.id), room.name)
            }
            switch d.kind {
            case .climate:
                if let t = d["currentTemp"]?.double {
                    row("Inside", "\(Int(t.rounded()))° · \(ClimateText.summary(d))", symbol: "thermometer.medium")
                        .links(to: .climate, "Climate")
                }
                row("History", "Temperature, set points and run times", symbol: "chart.xyaxis.line")
                    .links(to: .climateHistory(d.id), "History")
                outside
            case .valve:
                outside
            case .garage, .lock:
                row("All doors", "Doors and history", symbol: "door.garage.closed").links(to: .doors, "Doors")
            case .speaker:
                row("Music", "What's playing", symbol: "hifispeaker.fill").links(to: .music, "Music")
            case .printer:
                row("Workshop", "Printers and the print library", symbol: "printer.fill").links(to: .workshop, "Workshop")
            case .vacuum, .shower, .sensor:
                row("Appliances", "Everything else in the house", symbol: "washer.fill").links(to: .appliances, "Appliances")
            default:
                EmptyView()
            }
        } header: {
            Text("Connected")
        }
        if store.me?.can(.homeLayout) == true {
            Section {
                HideForEveryoneButton(deviceID: d.id, name: d.name)
            } footer: {
                Text("Hidden devices still work for modes, automations and Ask. Unhide from House › Hidden devices.")
            }
        }
    }

    /// Outside right now, for the things the weather changes (thermostats, sprinklers).
    @ViewBuilder private var outside: some View {
        if let w = store.home?.weather, w.temp != nil {
            row("Outside", "\(Wx.deg(w.temp)) \(w.condition ?? "")", symbol: w.symbol ?? "cloud.sun.fill")
                .linksToWeather()
        }
    }

    private func row(_ title: String, _ value: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundStyle(Theme.accent).frame(width: 24)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).foregroundStyle(Theme.fg)
                Text(value).font(.footnote).foregroundStyle(Theme.dim).lineLimit(1)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
        }
    }
}

/// "Outside 48° Cloudy ›" above the thermostats: the weather the house is heating or cooling against.
struct OutsideNowRow: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        if let w = store.home?.weather, w.temp != nil {
            HStack(spacing: 12) {
                WxIcon(name: w.symbol ?? "cloud.sun.fill").font(.title2).frame(width: 34)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Outside").font(.caption).foregroundStyle(Theme.dim)
                    Text("\(Wx.deg(w.temp)) \(w.condition ?? "")").font(.headline).foregroundStyle(Theme.fg)
                }
                Spacer()
                if let hi = w.high, let lo = w.low {
                    Text("\(Wx.deg(hi)) / \(Wx.deg(lo))").font(.subheadline.monospacedDigit()).foregroundStyle(Theme.dim)
                }
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
            }
            .card(padding: 12)
            .linksToWeather()
        }
    }
}
