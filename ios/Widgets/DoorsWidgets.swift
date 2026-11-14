import AppIntents
import SwiftUI
import WidgetKit

// Garage and front door: a Home Screen / Lock Screen widget (it also shows on CarPlay's widget dashboard)
// and Control Center buttons. Opening the garage works from here on an unlocked phone.

struct DoorsWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "Doors", provider: HouseProvider()) { e in
            DoorsWidgetView(entry: e).containerBackground(for: .widget) { WidgetTheme.background }
        }
        .configurationDisplayName("Garage and door")
        .description("Open and close the garage, lock the front door. Also on CarPlay.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular, .accessoryCircular])
    }
}

struct DoorsWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: HouseEntry

    private var garages: [DeviceItem] { entry.home?.devices(kind: .garage) ?? [] }
    private var door: DeviceItem? {
        let locks = entry.home?.devices(kind: .lock) ?? []
        return locks.first { $0.name.localizedCaseInsensitiveContains("front") } ?? locks.first
    }

    var body: some View {
        switch family {
        case .accessoryCircular:
            if let g = garages.first {
                Button(intent: ToggleGarageIntent(garage: GarageEntity(id: g.id, name: g.name))) {
                    Image(systemName: open(g) ? "door.garage.open" : "door.garage.closed").font(.title2)
                }
                .buttonStyle(.plain)
            } else { Image(systemName: "door.garage.closed") }
        case .accessoryRectangular:
            VStack(alignment: .leading, spacing: 2) {
                ForEach(garages.prefix(2)) { g in
                    Button(intent: ToggleGarageIntent(garage: GarageEntity(id: g.id, name: g.name))) {
                        Label("\(short(g)) \(open(g) ? "open" : "closed")", systemImage: open(g) ? "door.garage.open" : "door.garage.closed")
                            .font(.caption)
                    }
                    .buttonStyle(.plain)
                }
                if let d = door { Label(locked(d) ? "Door locked" : "Door unlocked", systemImage: locked(d) ? "lock.fill" : "lock.open").font(.caption) }
            }
        default:
            VStack(alignment: .leading, spacing: 8) {
                Label("Doors", systemImage: "door.garage.closed").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(garages.prefix(family == .systemSmall ? 2 : 3)) { g in
                    Button(intent: ToggleGarageIntent(garage: GarageEntity(id: g.id, name: g.name))) {
                        HStack {
                            Image(systemName: open(g) ? "door.garage.open" : "door.garage.closed")
                            Text(short(g)).lineLimit(1)
                            Spacer(minLength: 0)
                            Text(open(g) ? "Open" : "Closed").font(.caption2).foregroundStyle(open(g) ? .orange : .secondary)
                        }
                        .font(.caption.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 30)
                    }
                    .tint(open(g) ? .orange.opacity(0.5) : .white.opacity(0.15))
                }
                if let d = door {
                    Button(intent: LockFrontDoorIntent()) {
                        HStack {
                            Image(systemName: locked(d) ? "lock.fill" : "lock.open.fill")
                            Text(locked(d) ? "Locked" : "Lock door").lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .font(.caption.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 30)
                    }
                    .tint(locked(d) ? .white.opacity(0.15) : .orange.opacity(0.5))
                    .disabled(locked(d))
                }
                if garages.isEmpty && door == nil {
                    Text("Open the app at home to load the doors.").font(.caption).foregroundStyle(.secondary)
                }
            }
            .foregroundStyle(.white)
        }
    }

    private func open(_ g: DeviceItem) -> Bool { g["open"]?.bool == true }
    private func locked(_ d: DeviceItem) -> Bool { d["locked"]?.bool ?? (d["state"]?.string == "locked") }
    private func short(_ g: DeviceItem) -> String {
        g.name.replacingOccurrences(of: " door", with: "", options: .caseInsensitive)
    }
}

struct GarageControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        AppIntentControlConfiguration(kind: "GarageControl", provider: GarageValueProvider()) { v in
            ControlWidgetToggle(v.name, isOn: v.open, action: SetGarageIntent(garage: v.entity, open: !v.open)) { on in
                Label(on ? "Open" : "Closed", systemImage: on ? "door.garage.open" : "door.garage.closed")
            }
            .tint(.orange)
        }
        .displayName("Garage")
        .description("Open or close a garage door.")
    }
}

struct GarageValue { var entity: GarageEntity; var name: String; var open: Bool }

struct GarageConfig: ControlConfigurationIntent {
    static var title: LocalizedStringResource = "Garage door"
    @Parameter(title: "Garage door") var garage: GarageEntity?
    init() {}
}

struct GarageValueProvider: AppIntentControlValueProvider {
    func previewValue(configuration: GarageConfig) -> GarageValue {
        GarageValue(entity: GarageEntity(id: "", name: "Garage"), name: "Garage", open: false)
    }
    func currentValue(configuration: GarageConfig) async throws -> GarageValue {
        var g = configuration.garage
        if g == nil { g = await GarageQuery().all().first }
        let e = g ?? GarageEntity(id: "", name: "Garage")
        if let h = try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self) { AppSettings.cachedHome = h }
        return GarageValue(entity: e, name: e.name, open: QuickHouse.garageOpen(e.id))
    }
}

struct LockDoorControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LockFrontDoor") {
            ControlWidgetButton(action: LockFrontDoorIntent()) { Label("Lock front door", systemImage: "lock.fill") }
        }
        .displayName("Lock front door")
    }
}

/// The family set, grouped so the main bundle stays within WidgetBundleBuilder's limit.
struct FamilyWidgets: WidgetBundle {
    var body: some Widget {
        DoorsWidget()
        GarageControl()
        LockDoorControl()
        DoorbellLiveActivity()
        NowPlayingLiveActivity()
    }
}
