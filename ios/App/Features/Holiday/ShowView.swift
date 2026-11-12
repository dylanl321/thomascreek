import SwiftUI

struct ShowView: View {
    @EnvironmentObject var model: LightsModel
    @State private var bri: Double = 100
    @State private var editingBri = false
    @State private var sceneSearch = ""
    /// Dylan gets everything; other adults get looks, brightness and skip tonight (per the plan).
    var full = true

    var body: some View {
            ScrollView {
                VStack(spacing: 16) {
                    HeaderArt(image: "LightsHeader", title: "Christmas Lights", subtitle: model.status?.phase.title)
                    if !model.reachable, let e = model.error { OfflineBanner(text: e) }
                    NowPlayingCard(status: model.status)
                    if full { transport }
                    looks
                    brightness
                    NavigationLink { LightsWebPage(path: "house", title: "Live 3D view") } label: {
                        Label("Live 3D view of the house", systemImage: "cube.transparent").frame(maxWidth: .infinity, alignment: .leading)
                    }.lightsCard()
                    if full {
                        NavigationLink { ControllersView() } label: {
                            Label("Controllers", systemImage: "cpu").frame(maxWidth: .infinity, alignment: .leading)
                        }.lightsCard()
                    }
                    TonightCard(status: model.status)
                    if full {
                        queue
                        scenes
                    }
                }
                .padding(.horizontal, 16).padding(.bottom, 24)
            }
            .background(LightsBackdrop())
            // The lights screen is always dark; without this, default text renders black on it in light mode.
            .environment(\.colorScheme, .dark)
            .navigationTitle("Holiday lights").navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refresh() }
            .onChange(of: model.status?.brightnessPercent) { _, v in if !editingBri, let v { bri = Double(v) } }
            .onAppear { if let v = model.status?.brightnessPercent { bri = Double(v) } }
    }

    private var transport: some View {
        HStack(spacing: 12) {
            if model.status?.running == true {
                BigButton(title: "Next scene", symbol: "forward.fill") { model.run { try await LightsAPI.next() } }
                BigButton(title: "Stop", symbol: "stop.fill", tint: .gray) { model.run { try await LightsAPI.stop() } }
            } else {
                BigButton(title: "Start the loop", symbol: "play.fill", tint: .green) { model.run { try await LightsAPI.start() } }
            }
        }
    }

    private var looks: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Looks")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 10)], spacing: 10) {
                ForEach(Look.allCases) { l in
                    let on = model.status?.running != true && model.status?.currentLook == l
                    Button { model.run(l == .off ? "Lights off" : "\(l.title) on") { try await LightsAPI.look(l) } } label: {
                        VStack(spacing: 6) {
                            Image(systemName: l.symbol).font(.title2)
                            Text(l.title).font(.subheadline.weight(.semibold))
                        }
                        .frame(maxWidth: .infinity, minHeight: 74)
                        .foregroundStyle(on ? l.onText : l.tint)
                        .background(on ? AnyShapeStyle(l.tint) : AnyShapeStyle(l.tint.opacity(0.14)), in: RoundedRectangle(cornerRadius: 16))
                    }
                    .buttonStyle(.plain)
                    .sensoryFeedback(.impact(weight: .medium), trigger: model.status?.look)
                    .accessibilityHint(l.detail)
                }
            }
        }
    }

    private var brightness: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack { SectionTitle("Brightness"); Spacer(); Text("\(Int(bri))%").monospacedDigit().foregroundStyle(.secondary) }
            HStack {
                Image(systemName: "sun.min")
                Slider(value: $bri, in: 1...100, step: 1) { e in
                    editingBri = e
                    if !e { model.run("Brightness \(Int(bri))%") { try await LightsAPI.brightness(percent: Int(bri)) } }
                }
                Image(systemName: "sun.max.fill")
            }
            .foregroundStyle(.yellow)
            Text("The eaves stay at full brightness.").font(.caption).foregroundStyle(.secondary)
        }
        .lightsCard()
    }

    @ViewBuilder private var queue: some View {
        if let q = model.status?.queue, !q.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SectionTitle("Up next")
                ForEach(Array(q.prefix(6).enumerated()), id: \.offset) { i, name in
                    HStack {
                        Image(systemName: i < (model.status?.picked ?? 0) ? "hand.point.right.fill" : "shuffle")
                            .foregroundStyle(.secondary).frame(width: 22)
                        Text(name)
                        Spacer()
                        Menu {
                            Button("Play now", systemImage: "play.fill") { model.run { try await LightsAPI.play(name) } }
                            Button("Remove", systemImage: "minus.circle", role: .destructive) { model.run { try await LightsAPI.unqueue(i) } }
                        } label: { Image(systemName: "ellipsis.circle").foregroundStyle(.secondary) }
                    }
                    .padding(.vertical, 4)
                }
            }
            .lightsCard()
        }
    }

    private var scenes: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Scenes")
            Text("Tap to play now. Hold to queue it next.").font(.caption).foregroundStyle(.secondary)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                ForEach(model.status?.scenes ?? [], id: \.self) { s in
                    let playing = model.status?.scene == s
                    Button { model.run { try await LightsAPI.play(s) } } label: {
                        HStack {
                            Image(systemName: playing ? "waveform" : SceneStyle.symbol(s)).symbolEffect(.variableColor, isActive: playing)
                            Text(s).font(.subheadline.weight(.medium)).lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity)
                        .background(LinearGradient(colors: SceneStyle.colors(s).map { $0.opacity(playing ? 0.55 : 0.22) },
                                                   startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 14))
                        .overlay(RoundedRectangle(cornerRadius: 14).stroke(playing ? Color.white.opacity(0.6) : .clear, lineWidth: 1.5))
                    }
                    .buttonStyle(.plain)
                    .contextMenu {
                        Button("Play now", systemImage: "play.fill") { model.run { try await LightsAPI.play(s) } }
                        Button("Play next", systemImage: "text.line.first.and.arrowtriangle.forward") { model.run { try await LightsAPI.queue(s) } }
                    }
                }
            }
        }
    }
}

// MARK: - pieces

struct NowPlayingCard: View {
    let status: ShowStatus?
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(status?.phase.title ?? "Connecting…", systemImage: status?.phase.symbol ?? "antenna.radiowaves.left.and.right")
                    .font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                if let s = status, s.controllersDown > 0 {
                    Label("\(s.controllersDown) down", systemImage: "exclamationmark.triangle.fill").font(.footnote.weight(.semibold)).foregroundStyle(.orange)
                }
            }
            Text(status?.headline ?? "–").font(.system(.largeTitle, design: .rounded, weight: .bold)).lineLimit(1).minimumScaleFactor(0.6)
                .contentTransition(.opacity)
            if let s = status, s.running, let len = s.length, len > 0 {
                let start = Date().addingTimeInterval(-Double(s.position ?? 0))
                ProgressView(timerInterval: start...start.addingTimeInterval(Double(len)), countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }
                    .tint(.white)
                HStack {
                    Text("Scene \(Self.mmss(s.position ?? 0)) of \(Self.mmss(len))")
                    Spacer()
                    if let n = s.queue?.first { Text("Next: \(n)") }
                }
                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            } else if let l = status?.currentLook {
                Text(l.detail).foregroundStyle(.secondary)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            LinearGradient(colors: status?.scene.map(SceneStyle.colors) ?? [status?.currentLook?.tint ?? .gray, .black],
                           startPoint: .topLeading, endPoint: .bottomTrailing).opacity(0.55),
            in: RoundedRectangle(cornerRadius: 22))
        .overlay(RoundedRectangle(cornerRadius: 22).stroke(.white.opacity(0.08)))
        .animation(.easeInOut, value: status?.scene)
    }
    static func mmss(_ s: Int) -> String { String(format: "%d:%02d", s / 60, s % 60) }
}

struct TonightCard: View {
    @EnvironmentObject var model: LightsModel
    let status: ShowStatus?
    var body: some View {
        if let sc = status?.schedule {
            VStack(alignment: .leading, spacing: 12) {
                HStack { SectionTitle("Tonight"); Spacer()
                    if sc.skip { Text("Skipped").font(.caption.weight(.bold)).padding(.horizontal, 8).padding(.vertical, 3).background(.orange.opacity(0.3), in: Capsule()) }
                }
                HStack(spacing: 0) {
                    step("sunset.fill", "Show", sc.start, sc.phase == "show")
                    line
                    step("moon.stars.fill", "Calm", sc.calm, sc.phase == "calm")
                    line
                    step("sunrise.fill", "Off", sc.off, sc.phase == "day")
                }
                Toggle("Nightly schedule", isOn: Binding(get: { sc.enabled }, set: { v in model.run(v ? "Schedule on" : "Schedule off") { try await LightsAPI.schedule(enabled: v); return "ok" } }))
                Toggle("Skip tonight", isOn: Binding(get: { sc.skip }, set: { v in model.run(v ? "Tonight skipped" : "Tonight's back on") { try await LightsAPI.skipTonight(v); return "ok" } }))
            }
            .lightsCard()
        }
    }
    private var line: some View { Rectangle().fill(.white.opacity(0.15)).frame(height: 2).frame(maxWidth: .infinity).padding(.bottom, 26) }
    private func step(_ sym: String, _ title: String, _ time: String, _ now: Bool) -> some View {
        VStack(spacing: 4) {
            Image(systemName: sym).font(.title3).foregroundStyle(now ? .yellow : .secondary)
            Text(title).font(.caption.weight(.semibold))
            Text(Self.twelve(time)).font(.caption2).foregroundStyle(.secondary).monospacedDigit()
        }
        .frame(width: 70)
    }
    static func twelve(_ hhmm: String) -> String {
        let p = hhmm.split(separator: ":").compactMap { Int($0) }
        guard p.count == 2 else { return hhmm }
        return String(format: "%d:%02d %@", p[0] % 12 == 0 ? 12 : p[0] % 12, p[1], p[0] < 12 ? "AM" : "PM")
    }
}

struct BigButton: View {
    let title: String, symbol: String
    var tint: Color = Color("AccentColor")
    let action: () -> Void
    @State private var taps = 0
    var body: some View {
        Button { taps += 1; action() } label: {
            Label(title, systemImage: symbol).font(.headline).frame(maxWidth: .infinity, minHeight: 52)
        }
        .buttonStyle(.borderedProminent).tint(tint).buttonBorderShape(.roundedRectangle(radius: 16))
        .sensoryFeedback(.impact, trigger: taps)
    }
}

struct OfflineBanner: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "wifi.exclamationmark").font(.footnote).padding(12)
            .frame(maxWidth: .infinity, alignment: .leading).background(.orange.opacity(0.25), in: RoundedRectangle(cornerRadius: 14))
    }
}

struct SectionTitle: View {
    let text: String
    init(_ t: String) { text = t }
    var body: some View { Text(text).font(.headline) }
}

/// The lights section keeps the original app's night-sky look.
struct LightsBackdrop: View {
    var body: some View {
        LinearGradient(colors: [Color(red: 0.04, green: 0.06, blue: 0.12), Color(red: 0.02, green: 0.03, blue: 0.06)],
                       startPoint: .top, endPoint: .bottom).ignoresSafeArea()
    }
}

extension View {
    func lightsCard() -> some View {
        padding(16).frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 18))
    }
}

/// Colours and symbols per scene name (anything unknown gets warm gold).
enum SceneStyle {
    static func colors(_ name: String) -> [Color] {
        let n = name.lowercased()
        if n.contains("ice") || n.contains("snow") { return [Color(red: 0.55, green: 0.8, blue: 1), Color(red: 0.2, green: 0.35, blue: 0.8)] }
        if n.contains("northern") || n.contains("aurora") { return [Color(red: 0.2, green: 0.95, blue: 0.6), Color(red: 0.5, green: 0.2, blue: 0.9)] }
        if n.contains("candy") { return [Color(red: 0.95, green: 0.2, blue: 0.3), .white] }
        if n.contains("fire") || n.contains("ember") { return [Color(red: 1, green: 0.45, blue: 0.1), Color(red: 0.6, green: 0.05, blue: 0.05)] }
        if n.contains("classic") || n.contains("evergreen") { return [Color(red: 0.85, green: 0.15, blue: 0.15), Color(red: 0.1, green: 0.55, blue: 0.25)] }
        if n.contains("explosion") { return [Color(red: 1, green: 0.3, blue: 0.7), Color(red: 0.2, green: 0.6, blue: 1)] }
        if n.contains("star") || n.contains("sparkle") { return [Color(red: 1, green: 0.9, blue: 0.6), Color(red: 0.3, green: 0.3, blue: 0.6)] }
        return [Color(red: 1, green: 0.78, blue: 0.4), Color(red: 0.55, green: 0.3, blue: 0.1)]
    }
    static func symbol(_ name: String) -> String {
        let n = name.lowercased()
        if n.contains("snow") { return "snowflake" }
        if n.contains("ice") { return "cube.transparent" }
        if n.contains("northern") { return "wind" }
        if n.contains("candy") { return "staroflife" }
        if n.contains("fire") { return "flame.fill" }
        if n.contains("explosion") { return "burst.fill" }
        if n.contains("star") { return "star.fill" }
        if n.contains("sparkle") { return "sparkles" }
        if n.contains("evergreen") || n.contains("classic") { return "tree.fill" }
        if n.contains("outline") { return "house" }
        return "sparkle"
    }
}
