import WidgetKit
import SwiftUI
import AppIntents
import ActivityKit

@main
struct HomeWidgetBundle: WidgetBundle {
    var body: some Widget {
        HouseWidget()
        AskControl()
        ModesWidget()
        ChoresWidget()
        StatusWidget()
        ShowLiveActivity()
        LoopControl()
        CalmControl()
        LightsOffControl()
        NextSceneControl()
    }
}

// MARK: - Home Screen / Lock Screen widget

struct StatusEntry: TimelineEntry {
    var date: Date
    var status: ShowStatus?
    var offline: Bool
}

struct StatusProvider: TimelineProvider {
    func placeholder(in context: Context) -> StatusEntry { StatusEntry(date: .now, status: nil, offline: false) }
    func getSnapshot(in context: Context, completion: @escaping (StatusEntry) -> Void) {
        completion(StatusEntry(date: .now, status: AppSettings.cachedStatus, offline: false))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<StatusEntry>) -> Void) {
        Task {
            let s = try? await LightsAPI.status()
            let entry = StatusEntry(date: .now, status: s ?? AppSettings.cachedStatus, offline: s == nil)
            // Refresh more often during the show; the app and buttons also reload the widget after every change.
            let mins = entry.status?.phase == .show ? 10 : 30
            completion(Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(Double(mins) * 60))))
        }
    }
}

struct StatusWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "LightsStatus", provider: StatusProvider()) { e in
            StatusWidgetView(entry: e).containerBackground(for: .widget) { WidgetBackground(status: e.status) }
        }
        .configurationDisplayName("Christmas Lights")
        .description("What's playing, tonight's schedule and quick looks.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular, .accessoryCircular, .accessoryInline])
    }
}

struct WidgetBackground: View {
    let status: ShowStatus?
    var body: some View {
        LinearGradient(colors: (status?.scene.map(SceneStyleW.colors) ?? [Color(red: 0.35, green: 0.22, blue: 0.08), .black]).map { $0.opacity(0.85) },
                       startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

struct StatusWidgetView: View {
    @Environment(\.widgetFamily) var family
    let entry: StatusEntry
    var s: ShowStatus? { entry.status }

    var body: some View {
        switch family {
        case .accessoryInline:
            Label(s?.headline ?? "Christmas Lights", systemImage: s?.phase.symbol ?? "sparkles")
        case .accessoryCircular:
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: s?.running == true ? "sparkles" : (s?.currentLook?.symbol ?? "lightbulb"))
                    .font(.title3)
            }
            .widgetURL(URL(string: "thomascreek://holiday"))
        case .accessoryRectangular:
            VStack(alignment: .leading, spacing: 1) {
                Label("Lights", systemImage: s?.phase.symbol ?? "sparkles").font(.caption2.weight(.semibold))
                Text(s?.headline ?? "–").font(.headline).lineLimit(1)
                Text(nextLine).font(.caption2).lineLimit(1)
            }
        case .systemMedium:
            HStack(spacing: 12) {
                summary
                VStack(spacing: 6) {
                    HStack(spacing: 6) { lookButton(.elegant); lookButton(.festive) }
                    HStack(spacing: 6) { lookButton(.calm); lookButton(.off) }
                    Button(intent: NextSceneIntent()) {
                        Label("Next", systemImage: "forward.fill").font(.caption.weight(.semibold)).frame(maxWidth: .infinity)
                    }
                    .tint(.white.opacity(0.25))
                }
                .frame(width: 140)
            }
        default:
            summary
        }
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: s?.phase.symbol ?? "sparkles")
                Spacer()
                if entry.offline { Image(systemName: "wifi.slash").foregroundStyle(.orange) }
                else if let s, s.controllersDown > 0 { Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
            }
            .font(.caption)
            Spacer(minLength: 0)
            Text(s?.headline ?? "Christmas Lights").font(.headline).lineLimit(2).minimumScaleFactor(0.7)
            Text(nextLine).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
        }
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var nextLine: String {
        guard let s, let sc = s.schedule else { return entry.offline ? "Can't reach home" : "" }
        if sc.skip { return "Tonight is skipped" }
        switch sc.phase {
        case "show": return "Calm at \(twelve(sc.calm))"
        case "calm": return "Off at \(twelve(sc.off))"
        default: return "Show at \(twelve(sc.start))"
        }
    }

    private func lookButton(_ l: Look) -> some View {
        Button(intent: SetLookIntent(look: l)) {
            Image(systemName: l.symbol).frame(maxWidth: .infinity)
        }
        .tint(l.tint.opacity(0.5))
        .accessibilityLabel(l.title)
    }
}

func twelve(_ hhmm: String) -> String {
    let p = hhmm.split(separator: ":").compactMap { Int($0) }
    guard p.count == 2 else { return hhmm }
    return String(format: "%d:%02d", p[0] % 12 == 0 ? 12 : p[0] % 12, p[1]) + (p[0] < 12 ? " AM" : " PM")
}

enum SceneStyleW {
    static func colors(_ name: String) -> [Color] {
        let n = name.lowercased()
        if n.contains("ice") || n.contains("snow") { return [Color(red: 0.35, green: 0.6, blue: 0.95), Color(red: 0.1, green: 0.15, blue: 0.4)] }
        if n.contains("northern") { return [Color(red: 0.1, green: 0.6, blue: 0.4), Color(red: 0.3, green: 0.1, blue: 0.5)] }
        if n.contains("candy") || n.contains("classic") { return [Color(red: 0.7, green: 0.1, blue: 0.15), Color(red: 0.08, green: 0.3, blue: 0.15)] }
        if n.contains("fire") { return [Color(red: 0.8, green: 0.3, blue: 0.05), Color(red: 0.3, green: 0.02, blue: 0.02)] }
        return [Color(red: 0.55, green: 0.35, blue: 0.1), Color(red: 0.12, green: 0.08, blue: 0.04)]
    }
}

// MARK: - Live Activity (Lock Screen + Dynamic Island)

struct ShowLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: ShowAttributes.self) { ctx in
            LockScreenLive(state: ctx.state)
                .activityBackgroundTint(Color.black.opacity(0.75))
                .activitySystemActionForegroundColor(.yellow)
        } dynamicIsland: { ctx in
            let st = ctx.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: st.phaseValue.symbol).font(.title2).foregroundStyle(.yellow).padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if st.phaseValue == .show { Text(st.calmDate, style: .timer).font(.caption).monospacedDigit().frame(width: 60).foregroundStyle(.secondary) }
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(st.title).font(.headline).lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(spacing: 8) {
                        if let r = st.sceneRange { ProgressView(timerInterval: r, countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }.tint(.yellow) }
                        HStack {
                            if st.playing {
                                Button(intent: NextSceneIntent()) { Label("Next", systemImage: "forward.fill") }
                            } else {
                                Button(intent: StartShowIntent()) { Label("Loop", systemImage: "play.fill") }
                            }
                            Button(intent: SetLookIntent(look: .calm)) { Label("Calm", systemImage: "moon.fill") }
                            Button(intent: SetLookIntent(look: .off)) { Label("Off", systemImage: "power") }
                        }
                        .font(.caption.weight(.semibold)).tint(.yellow)
                    }
                }
            } compactLeading: {
                Image(systemName: st.playing ? "sparkles" : st.phaseValue.symbol).foregroundStyle(.yellow)
            } compactTrailing: {
                if st.phaseValue == .show { Text(st.calmDate, style: .timer).monospacedDigit().frame(maxWidth: 44).font(.caption2) }
                else { Text(st.title.prefix(6)).font(.caption2) }
            } minimal: {
                Image(systemName: "sparkles").foregroundStyle(.yellow)
            }
            .widgetURL(URL(string: "thomascreek://holiday"))
        }
    }
}

struct LockScreenLive: View {
    let state: ShowAttributes.ContentState
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(state.phaseValue.title, systemImage: state.phaseValue.symbol).font(.caption.weight(.semibold)).foregroundStyle(.yellow)
                Spacer()
                switch state.phaseValue {
                case .show: (Text("Calm in ") + Text(state.calmDate, style: .relative)).font(.caption).foregroundStyle(.secondary)
                case .calm: (Text("Off at ") + Text(state.offDate, style: .time)).font(.caption).foregroundStyle(.secondary)
                case .day: EmptyView()
                }
            }
            Text(state.title).font(.title2.weight(.bold)).lineLimit(1)
            if let r = state.sceneRange {
                ProgressView(timerInterval: r, countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }.tint(.yellow)
            }
            HStack {
                if let n = state.next, state.playing { Text("Next: \(n)").font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                Spacer()
                if state.playing {
                    Button(intent: NextSceneIntent()) { Image(systemName: "forward.fill") }.tint(.yellow)
                }
                Button(intent: SetLookIntent(look: .calm)) { Image(systemName: "moon.fill") }.tint(.orange)
            }
        }
        .padding(16)
        .foregroundStyle(.white)
    }
}

// MARK: - Control Center (iOS 18)

struct LoopValueProvider: ControlValueProvider {
    var previewValue: Bool { true }
    func currentValue() async throws -> Bool { (try? await LightsAPI.status())?.running ?? (AppSettings.cachedStatus?.running ?? false) }
}

struct LoopControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LightsLoop", provider: LoopValueProvider()) { on in
            ControlWidgetToggle("Light Show", isOn: on, action: ShowLoopToggleIntent()) { playing in
                Label(playing ? "Playing" : "Calm", systemImage: playing ? "sparkles" : "moon.fill")
            }
        }
        .displayName("Light Show")
        .description("Start the loop, or switch to Calm.")
    }
}

struct CalmControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LightsCalm") {
            ControlWidgetButton(action: SetLookIntent(look: .calm)) { Label("Calm Lights", systemImage: "moon.fill") }
        }
        .displayName("Calm Lights")
    }
}

struct LightsOffControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LightsOff") {
            ControlWidgetButton(action: SetLookIntent(look: .off)) { Label("Lights Off", systemImage: "power") }
        }
        .displayName("Christmas Lights Off")
    }
}

struct NextSceneControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LightsNext") {
            ControlWidgetButton(action: NextSceneIntent()) { Label("Next Scene", systemImage: "forward.fill") }
        }
        .displayName("Next Scene")
    }
}

/// Control Center and Action Button: open Thomas Creek listening.
struct AskControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "AskThomasCreek") {
            ControlWidgetButton(action: OpenAskIntent()) { Label("Talk to Thomas Creek", systemImage: "mic.fill") }
        }
        .displayName("Talk to Thomas Creek")
        .description("Opens Thomas Creek ready to listen.")
    }
}
