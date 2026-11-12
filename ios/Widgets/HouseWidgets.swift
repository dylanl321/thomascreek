import WidgetKit
import SwiftUI
import AppIntents

// MARK: - "Is the house OK" widget

struct HouseEntry: TimelineEntry {
    var date: Date
    var home: HomeSnapshot?
    var offline: Bool
}

struct HouseProvider: TimelineProvider {
    func placeholder(in context: Context) -> HouseEntry { HouseEntry(date: .now, home: nil, offline: false) }
    func getSnapshot(in context: Context, completion: @escaping (HouseEntry) -> Void) {
        completion(HouseEntry(date: .now, home: AppSettings.cachedHome, offline: false))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<HouseEntry>) -> Void) {
        Task {
            let h = try? await HubClient.shared.get("/v1/home", as: HomeSnapshot.self)
            if let h { AppSettings.cachedHome = h }
            let e = HouseEntry(date: .now, home: h ?? AppSettings.cachedHome, offline: h == nil)
            completion(Timeline(entries: [e], policy: .after(.now.addingTimeInterval(15 * 60))))
        }
    }
}

struct HouseWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "HouseStatus", provider: HouseProvider()) { e in
            HouseWidgetView(entry: e).containerBackground(for: .widget) { WidgetTheme.scene }
        }
        .configurationDisplayName("Home")
        .description("Whether everything's fine, and the garage doors.")
        .supportedFamilies([.systemSmall, .accessoryRectangular, .accessoryInline])
    }
}

struct HouseWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: HouseEntry

    private var calm: Bool { entry.home?.status.level == "ok" }
    private var garages: [DeviceItem] { entry.home?.devices(kind: .garage) ?? [] }

    var body: some View {
        switch family {
        case .accessoryInline:
            Label(entry.home?.status.line ?? "Thomas Creek", systemImage: calm ? "house.fill" : "exclamationmark.triangle.fill")
        case .accessoryRectangular:
            VStack(alignment: .leading, spacing: 1) {
                Label("Home", systemImage: calm ? "checkmark.circle.fill" : "exclamationmark.triangle.fill").font(.caption2.weight(.semibold))
                Text(entry.home?.status.line ?? "–").font(.caption).lineLimit(2)
            }
        default:
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Image(systemName: calm ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(calm ? WidgetTheme.accent : .orange)
                    Spacer()
                    if entry.offline { Image(systemName: "wifi.slash").foregroundStyle(.secondary) }
                }
                Text(calm ? "Everything's fine" : (entry.home?.status.items?.first?.title ?? "Needs a look"))
                    .font(.headline).lineLimit(2).minimumScaleFactor(0.8)
                Spacer(minLength: 0)
                ForEach(garages.prefix(2)) { g in
                    Label(g.name.replacingOccurrences(of: " garage door", with: "", options: .caseInsensitive),
                          systemImage: g["open"]?.bool == true ? "door.garage.open" : "door.garage.closed")
                        .font(.caption).foregroundStyle(g["open"]?.bool == true ? .orange : .secondary).lineLimit(1)
                }
            }
            .foregroundStyle(.white)
            .widgetURL(URL(string: "thomascreek://home"))
        }
    }
}

// MARK: - Modes widget

struct ModesWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "HouseModes", provider: HouseProvider()) { e in
            ModesWidgetView(entry: e).containerBackground(for: .widget) { WidgetTheme.background }
        }
        .configurationDisplayName("Modes")
        .description("Good night, Leaving and the other house modes in one tap.")
        .supportedFamilies([.systemMedium])
    }
}

struct ModesWidgetView: View {
    let entry: HouseEntry
    var body: some View {
        let modes = Array((entry.home?.modes ?? []).prefix(4))
        VStack(alignment: .leading, spacing: 8) {
            Label("Modes", systemImage: "house.fill").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            if modes.isEmpty {
                Text("Open the app at home to load modes.").font(.caption).foregroundStyle(.secondary)
            }
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
                ForEach(modes) { m in
                    Button(intent: RunModeIntent(mode: ModeEntity(id: m.id, name: m.name))) {
                        Label(m.name, systemImage: m.symbol ?? "sparkles").font(.caption.weight(.semibold))
                            .lineLimit(1).frame(maxWidth: .infinity, minHeight: 34)
                    }
                    .tint(m.active == true ? WidgetTheme.accent : .white.opacity(0.2))
                }
            }
        }
        .foregroundStyle(.white)
    }
}

// MARK: - Chores widget (for the kids)

struct ChoresEntry: TimelineEntry { var date: Date; var board: ChoreBoard? }

struct ChoresProvider: TimelineProvider {
    func placeholder(in context: Context) -> ChoresEntry { ChoresEntry(date: .now, board: nil) }
    func getSnapshot(in context: Context, completion: @escaping (ChoresEntry) -> Void) {
        completion(ChoresEntry(date: .now, board: AppSettings.cachedChores))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<ChoresEntry>) -> Void) {
        Task {
            let b = try? await HubClient.shared.get("/v1/chores", as: ChoreBoard.self)
            if let b { AppSettings.cachedChores = b }
            completion(Timeline(entries: [ChoresEntry(date: .now, board: b ?? AppSettings.cachedChores)],
                                policy: .after(.now.addingTimeInterval(30 * 60))))
        }
    }
}

struct ChoresWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "Chores", provider: ChoresProvider()) { e in
            ChoresWidgetView(entry: e).containerBackground(for: .widget) {
                LinearGradient(colors: [Color(red: 0.42, green: 0.55, blue: 1), Color(red: 0.24, green: 0.3, blue: 0.75)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
            }
        }
        .configurationDisplayName("My chores")
        .description("Today's chores and points. Tap one to mark it done.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct ChoresWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: ChoresEntry
    var body: some View {
        let b = entry.board
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("\(b?.doneCount ?? 0)/\(b?.items.count ?? 0)").font(.system(.title2, design: .rounded, weight: .bold))
                Spacer()
                Label("\(b?.points ?? 0)", systemImage: "star.fill").font(.caption.weight(.bold))
            }
            ForEach((b?.items ?? []).filter { !$0.done }.prefix(family == .systemMedium ? 3 : 2)) { c in
                Button(intent: MarkChoreDoneIntent(choreID: c.id)) {
                    HStack(spacing: 6) {
                        Image(systemName: "circle")
                        Text("\(c.icon ?? "") \(c.title)").lineLimit(1)
                        Spacer(minLength: 0)
                    }
                    .font(.caption.weight(.semibold))
                }
                .buttonStyle(.plain)
            }
            if let b, b.doneCount == b.items.count, !b.items.isEmpty {
                Text("All done!").font(.headline)
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(.white)
        .widgetURL(URL(string: "thomascreek://chores"))
    }
}

enum WidgetTheme {
    /// The house at dusk (same art as the app icon), darkened at the top so the status text stays readable.
    static var scene: some View {
        ZStack {
            Image("WidgetScene").resizable().scaledToFill()
            LinearGradient(colors: [.black.opacity(0.45), .clear, .black.opacity(0.35)], startPoint: .top, endPoint: .bottom)
        }
    }
    static let accent = Color(red: 0.31, green: 0.76, blue: 0.69)
    static let background = LinearGradient(colors: [Color(red: 0.09, green: 0.12, blue: 0.13), Color(red: 0.05, green: 0.07, blue: 0.08)],
                                           startPoint: .top, endPoint: .bottom)
}
