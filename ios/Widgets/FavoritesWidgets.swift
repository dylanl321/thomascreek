import WidgetKit
import SwiftUI
import AppIntents

// MARK: - Favorites widget: the person's own starred lights, doors and modes

struct FavoritesEntry: TimelineEntry {
    var date: Date
    var home: HomeSnapshot?
    var settings: PersonSettings?
}

struct FavoritesProvider: TimelineProvider {
    func placeholder(in context: Context) -> FavoritesEntry { FavoritesEntry(date: .now) }
    func getSnapshot(in context: Context, completion: @escaping (FavoritesEntry) -> Void) {
        completion(FavoritesEntry(date: .now, home: AppSettings.cachedHome, settings: PersonSettings.cached))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<FavoritesEntry>) -> Void) {
        Task {
            let h = try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self)
            if let h { AppSettings.cachedHome = h }
            // Favorites starred on another device since the app last ran.
            if let data = try? await HubClient.shared.raw("GET", "/v1/me/settings"),
               let s = try? PersonSettings.decoder.decode(PersonSettings.self, from: data) {
                PersonSettings.cached = s
            }
            let e = FavoritesEntry(date: .now, home: h ?? AppSettings.cachedHome, settings: PersonSettings.cached)
            completion(Timeline(entries: [e], policy: .after(.now.addingTimeInterval(15 * 60))))
        }
    }
}

struct FavoritesWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "Favorites", provider: FavoritesProvider()) { e in
            FavoritesWidgetView(entry: e).containerBackground(for: .widget) { WidgetTheme.background }
        }
        .configurationDisplayName("Favorites")
        .description("Your own starred lights, doors and modes. Star things in the app to choose.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct FavoritesWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: FavoritesEntry

    private enum Item: Identifiable {
        case device(DeviceItem), mode(Mode)
        var id: String {
            switch self {
            case .device(let d): "d:\(d.id)"
            case .mode(let m): "m:\(m.id)"
            }
        }
    }

    private var items: [Item] {
        let devices = FavoriteToggle.starred(home: entry.home, settings: entry.settings).map(Item.device)
        let modes = (entry.settings?.favorites(.mode) ?? []).compactMap { f in entry.home?.modes?.first { $0.id == f.id } }.map(Item.mode)
        return Array((devices + modes).prefix(family == .systemSmall ? 2 : 4))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Favorites", systemImage: "star.fill").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            if items.isEmpty {
                Text("Long-press a light or mode in Thomas Creek and choose Add to Favorites.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            } else {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: family == .systemSmall ? 1 : 2), spacing: 8) {
                    ForEach(items) { tile($0) }
                }
            }
        }
        .foregroundStyle(.white)
        .widgetURL(URL(string: "thomascreek://favorites"))
    }

    @ViewBuilder private func tile(_ i: Item) -> some View {
        switch i {
        case .device(let d):
            if let action = FavoriteToggle.action(for: d) {
                Button(intent: ToggleFavoriteIntent(deviceID: d.id, action: action, on: !d.isOn)) {
                    label(d.name, symbol: d.isOn ? "lightbulb.fill" : "lightbulb", detail: d.isOn ? "On" : "Off")
                }
                .tint(d.isOn ? WidgetTheme.accent : .white.opacity(0.2))
            } else {
                // Doors and locks show where they stand; changing them needs Face ID in the app.
                Link(destination: URL(string: "thomascreek://doors")!) {
                    label(d.name, symbol: statusSymbol(d), detail: statusText(d))
                        .padding(.horizontal, 8).background(.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                }
            }
        case .mode(let m):
            Button(intent: RunModeIntent(mode: ModeEntity(id: m.id, name: m.name))) {
                label(m.name, symbol: m.symbol ?? "sparkles", detail: m.active == true ? "On" : "Run")
            }
            .tint(m.active == true ? WidgetTheme.accent : .white.opacity(0.2))
        }
    }

    private func label(_ title: String, symbol: String, detail: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: symbol)
            VStack(alignment: .leading, spacing: 0) {
                Text(title).font(.caption.weight(.semibold)).lineLimit(1)
                Text(detail).font(.caption2).opacity(0.8)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, minHeight: 36)
    }

    private func statusSymbol(_ d: DeviceItem) -> String {
        switch d.kind {
        case .garage: d["open"]?.bool == true ? "door.garage.open" : "door.garage.closed"
        case .lock: d["locked"]?.bool == false ? "lock.open.fill" : "lock.fill"
        default: "circle.fill"
        }
    }
    private func statusText(_ d: DeviceItem) -> String {
        switch d.kind {
        case .garage: d["open"]?.bool == true ? "Open" : "Closed"
        case .lock: d["locked"]?.bool == false ? "Unlocked" : "Locked"
        default: d.isOn ? "On" : "Off"
        }
    }
}

// MARK: - Control Center: one of your favorites as a toggle

struct FavoriteControlConfig: ControlConfigurationIntent {
    static var title: LocalizedStringResource = "Favorite"
    @Parameter(title: "Light or switch") var device: FavoriteDeviceEntity?
    init() {}
}

struct FavoriteControlValue {
    var id: String?
    var name: String
    var action: String
    var on: Bool
}

struct FavoriteControlProvider: AppIntentControlValueProvider {
    func previewValue(configuration: FavoriteControlConfig) -> FavoriteControlValue {
        FavoriteControlValue(id: configuration.device?.id, name: configuration.device?.name ?? "Favorite",
                             action: configuration.device?.action ?? "light.set", on: false)
    }
    func currentValue(configuration: FavoriteControlConfig) async throws -> FavoriteControlValue {
        var v = previewValue(configuration: configuration)
        guard let id = v.id else { return v }
        let home = (try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self)) ?? AppSettings.cachedHome
        v.on = home?.devices?.first { $0.id == id }?.isOn ?? false
        return v
    }
}

struct FavoriteControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        AppIntentControlConfiguration(kind: "FavoriteToggle", provider: FavoriteControlProvider()) { v in
            ControlWidgetToggle(v.name, isOn: v.on, action: ToggleFavoriteIntent(deviceID: v.id ?? "", action: v.action)) { on in
                Label(on ? "On" : "Off", systemImage: on ? "lightbulb.fill" : "lightbulb")
            }
        }
        .displayName("Favorite Light")
        .description("One of your starred lights or switches. Your favorites come first in the list.")
        .promptsForUserConfiguration()
    }
}
