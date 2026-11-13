import SwiftUI
import Charts

// MARK: - Model (GET /v1/climate/{id}/history)

struct ClimateHistory: Decodable {
    struct Point: Decodable, Hashable { var t: Double; var temp: Double?; var humidity: Double? }
    struct SetPoint: Decodable, Hashable { var t: Double; var heat: Double?; var cool: Double?; var target: Double? }
    struct Span: Decodable, Hashable { var start: Double; var end: Double; var action: String? ; var mode: String? }
    struct Outdoor: Decodable, Hashable { var t: Double; var temp: Double }
    struct Summary: Decodable, Hashable {
        var heatingS: Double = 0
        var coolingS: Double = 0
        var fanS: Double = 0
        var heatingCycles: Int = 0
        var coolingCycles: Int = 0
        var tempMin: Double?
        var tempMax: Double?
        var tempAvg: Double?
        var humidityNow: Double?
        var humidityMin: Double?
        var humidityMax: Double?
    }

    var entityId: String
    var name: String
    var unit: String
    var range: String
    var start: Double
    var end: Double
    var points: [Point]
    var setpoints: [SetPoint]
    var runs: [Span]
    var modes: [Span]
    var outdoor: [Outdoor]
    var reportsAction: Bool
    var summary: Summary
}

/// One drawable line piece: a gap (thermostat offline, mode with no set point) starts a new piece.
private struct LinePoint: Identifiable, Hashable {
    var id: String { "\(series)-\(t)" }
    var series: String
    var t: Date
    var v: Double
}

enum ClimateRange: String, CaseIterable, Identifiable {
    case h6 = "6h", h24 = "24h", d3 = "3d", d7 = "7d"
    var id: String { rawValue }
    var label: String { rawValue.uppercased() }
    var long: String {
        switch self { case .h6: "6 hours"; case .h24: "24 hours"; case .d3: "3 days"; case .d7: "7 days" }
    }
}

enum ClimateColors {
    static let indoor = Theme.accent
    static let heat = Color.orange
    static let cool = Color.blue
    static let outdoor = Color.gray
    static let humidity = Color.teal
    static func run(_ action: String?) -> Color {
        switch action {
        case "cooling": cool
        case "heating", "preheating": heat
        case "drying": .cyan
        default: .gray
        }
    }
    static func runWord(_ action: String?) -> String {
        switch action {
        case "heating": "Heating"; case "cooling": "Cooling"; case "fan": "Fan"; case "drying": "Drying"
        case "preheating": "Preheating"; case "defrosting": "Defrosting"
        default: (action ?? "Idle").capitalized
        }
    }
}

// MARK: - Screen

/// A thermostat's controls with its history underneath (tile tap, Climate card, thomascreek://climate/<id>).
struct ClimateHistoryView: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    @State private var reload = 0

    var body: some View {
        let d = store.home?.device(deviceID)
        ScrollView {
            VStack(spacing: 12) {
                if let d { ClimateCard(device: d, showsHistory: false) }
                ClimateHistorySection(deviceID: deviceID, reload: reload)
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .refreshable { reload += 1 }
        .navigationTitle(d?.name ?? "Thermostat")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Embeddable section

/// Range picker, chart, run strip, humidity and summary. No scroll view of its own, so a device page
/// or the Climate screen can drop it in.
struct ClimateHistorySection: View {
    let deviceID: String
    var reload: Int = 0

    @AppStorage("climate.history.range") private var rangeRaw = ClimateRange.h24.rawValue
    @AppStorage("climate.history.outdoor") private var showOutdoor = false
    @State private var data: ClimateHistory?
    @State private var savedAt: Date?
    @State private var failed: String?
    @State private var loading = false
    @State private var selected: Date?

    private var range: ClimateRange { ClimateRange(rawValue: rangeRaw) ?? .h24 }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("History").font(.headline)
                if loading { ProgressView().controlSize(.small) }
                Spacer()
                if let savedAt {
                    Text("Saved \(savedAt.formatted(.relative(presentation: .named)))").font(.caption).foregroundStyle(Theme.dim)
                }
            }
            Picker("Range", selection: $rangeRaw) {
                ForEach(ClimateRange.allCases) { Text($0.label).tag($0.rawValue) }
            }
            .pickerStyle(.segmented)

            if let data {
                ClimateChartBody(data: data, range: range, showOutdoor: showOutdoor, selected: $selected)
                legend(data)
                ClimateSummaryGrid(data: data)
            } else if let failed {
                ContentUnavailableView("No history", systemImage: "chart.xyaxis.line", description: Text(failed))
                    .frame(maxWidth: .infinity)
            } else {
                RoundedRectangle(cornerRadius: 12).fill(Theme.card2).frame(height: 240)
                    .overlay(ProgressView())
            }
        }
        .card()
        .task(id: "\(rangeRaw)-\(reload)") { await load() }
    }

    private func legend(_ d: ClimateHistory) -> some View {
        let hasHeat = d.setpoints.contains { $0.heat != nil }
        let hasCool = d.setpoints.contains { $0.cool != nil }
        let hasTarget = d.setpoints.contains { $0.target != nil }
        return VStack(alignment: .leading, spacing: 8) {
            FlowRow {
                LegendKey(color: ClimateColors.indoor, label: "Inside")
                if hasHeat { LegendKey(color: ClimateColors.heat, label: "Heat to", dashed: true) }
                if hasCool { LegendKey(color: ClimateColors.cool, label: "Cool to", dashed: true) }
                if hasTarget { LegendKey(color: Theme.dim, label: "Set point", dashed: true) }
                if d.runs.contains(where: { $0.action == "heating" }) { LegendKey(color: ClimateColors.heat, label: "Heating ran", band: true) }
                if d.runs.contains(where: { $0.action == "cooling" }) { LegendKey(color: ClimateColors.cool, label: "Cooling ran", band: true) }
            }
            if !d.outdoor.isEmpty {
                Toggle(isOn: $showOutdoor) {
                    Label("Outside temperature", systemImage: "cloud.sun").font(.subheadline)
                }
                .tint(ClimateColors.outdoor)
            }
            if !d.reportsAction {
                Text("This thermostat doesn't report when it's running, so only temperatures and set points are shown.")
                    .font(.caption).foregroundStyle(Theme.dim)
            }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let r: (value: ClimateHistory, savedAt: Date?) = try await HubClient.shared.fetch(
                "/v1/climate/\(deviceID)/history", ["range": rangeRaw], timeout: 20)
            data = r.value
            savedAt = r.savedAt
            failed = nil
            selected = nil
        } catch {
            if data == nil || data?.range != rangeRaw { data = nil }
            failed = (error as? LocalizedError)?.errorDescription ?? "Home Assistant didn't answer."
        }
    }
}

// MARK: - Chart

private struct ClimateChartBody: View {
    let data: ClimateHistory
    let range: ClimateRange
    let showOutdoor: Bool
    @Binding var selected: Date?

    private var start: Date { Date(timeIntervalSince1970: data.start) }
    private var end: Date { Date(timeIntervalSince1970: data.end) }

    private var indoor: [LinePoint] { Self.pieces(data.points.map { ($0.t, $0.temp) }, "in", step: false) }
    private var heat: [LinePoint] { Self.pieces(data.setpoints.map { ($0.t, $0.heat) }, "heat", step: true) }
    private var cool: [LinePoint] { Self.pieces(data.setpoints.map { ($0.t, $0.cool) }, "cool", step: true) }
    private var target: [LinePoint] { Self.pieces(data.setpoints.map { ($0.t, $0.target) }, "target", step: true) }
    private var outside: [LinePoint] {
        showOutdoor ? data.outdoor.map { LinePoint(series: "out", t: Date(timeIntervalSince1970: $0.t), v: $0.temp) } : []
    }
    private var humidity: [LinePoint] { Self.pieces(data.points.map { ($0.t, $0.humidity) }, "hum", step: false) }
    private var runs: [ClimateHistory.Span] { data.runs.filter { $0.end > $0.start } }

    /// Split a series wherever it has no value; the piece number goes into the series name.
    private static func pieces(_ raw: [(Double, Double?)], _ name: String, step: Bool) -> [LinePoint] {
        var out: [LinePoint] = []
        var piece = 0
        var open = false
        for (t, v) in raw {
            if let v {
                out.append(LinePoint(series: "\(name)\(piece)", t: Date(timeIntervalSince1970: t), v: v))
                open = true
            } else if open {
                // A set point holds until the moment it went away.
                if step, let last = out.last { out.append(LinePoint(series: last.series, t: Date(timeIntervalSince1970: t), v: last.v)) }
                piece += 1
                open = false
            }
        }
        return out
    }

    private var yDomain: ClosedRange<Double> {
        let vals = (indoor + heat + cool + target + outside).map(\.v)
        guard let lo = vals.min(), let hi = vals.max() else { return 60...80 }
        let pad = max(1.5, (hi - lo) * 0.12)
        return (lo - pad).rounded(.down)...(hi + pad).rounded(.up)
    }

    private var axisFormat: Date.FormatStyle {
        switch range {
        case .h6, .h24: .dateTime.hour()
        case .d3, .d7: .dateTime.weekday(.abbreviated)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            readout.frame(height: 44, alignment: .topLeading)
            mainChart.frame(height: 240)
            runStrip.frame(height: 26)
            if !humidity.isEmpty {
                Text("Humidity").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim).padding(.top, 6)
                humidityChart.frame(height: 70)
            }
        }
    }

    private var mainChart: some View {
        let y = yDomain
        return Chart {
            ForEach(runs, id: \.self) { r in
                RectangleMark(xStart: .value("From", Date(timeIntervalSince1970: r.start)),
                              xEnd: .value("To", Date(timeIntervalSince1970: r.end)))
                    .foregroundStyle(ClimateColors.run(r.action).opacity(0.14))
            }
            ForEach(outside) { p in
                LineMark(x: .value("Time", p.t), y: .value("Outside", p.v), series: .value("Line", p.series))
                    .foregroundStyle(ClimateColors.outdoor.opacity(0.7))
                    .lineStyle(StrokeStyle(lineWidth: 1.2))
                    .interpolationMethod(.monotone)
            }
            ForEach(heat) { p in
                LineMark(x: .value("Time", p.t), y: .value("Heat to", p.v), series: .value("Line", p.series))
                    .foregroundStyle(ClimateColors.heat)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                    .interpolationMethod(.stepEnd)
            }
            ForEach(cool) { p in
                LineMark(x: .value("Time", p.t), y: .value("Cool to", p.v), series: .value("Line", p.series))
                    .foregroundStyle(ClimateColors.cool)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                    .interpolationMethod(.stepEnd)
            }
            ForEach(target) { p in
                LineMark(x: .value("Time", p.t), y: .value("Set point", p.v), series: .value("Line", p.series))
                    .foregroundStyle(Theme.dim)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                    .interpolationMethod(.stepEnd)
            }
            ForEach(indoor) { p in
                LineMark(x: .value("Time", p.t), y: .value("Inside", p.v), series: .value("Line", p.series))
                    .foregroundStyle(ClimateColors.indoor)
                    .lineStyle(StrokeStyle(lineWidth: 2.5, lineCap: .round, lineJoin: .round))
                    .interpolationMethod(.monotone)
            }
            if let selected {
                RuleMark(x: .value("Selected", selected))
                    .foregroundStyle(Theme.fg.opacity(0.35))
                    .lineStyle(StrokeStyle(lineWidth: 1))
            }
        }
        .chartXScale(domain: start...end)
        .chartYScale(domain: y)
        .chartXSelection(value: $selected)
        .chartLegend(.hidden)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: range == .d7 ? 7 : 6)) { _ in
                AxisGridLine().foregroundStyle(Theme.line)
                AxisValueLabel(format: axisFormat)
            }
        }
        .chartYAxis {
            AxisMarks(position: .trailing, values: .automatic(desiredCount: 5)) { v in
                AxisGridLine().foregroundStyle(Theme.line)
                AxisValueLabel { if let d = v.as(Double.self) { Text("\(Int(d))°") } }
            }
        }
        .accessibilityLabel("Temperature over the last \(range.long)")
    }

    /// When heating or cooling ran, one row, on the same time scale as the chart.
    private var runStrip: some View {
        Chart {
            RectangleMark(xStart: .value("From", start), xEnd: .value("To", end), y: .value("Row", "Running"), height: 12)
                .foregroundStyle(Theme.card2)
            ForEach(runs, id: \.self) { r in
                RectangleMark(xStart: .value("From", Date(timeIntervalSince1970: r.start)),
                              xEnd: .value("To", Date(timeIntervalSince1970: r.end)),
                              y: .value("Row", "Running"), height: 12)
                    .foregroundStyle(ClimateColors.run(r.action))
            }
            if let selected {
                RuleMark(x: .value("Selected", selected)).foregroundStyle(Theme.fg.opacity(0.35))
            }
        }
        .chartXScale(domain: start...end)
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartXSelection(value: $selected)
        .accessibilityLabel("Heating and cooling runs")
    }

    private var humidityChart: some View {
        Chart {
            ForEach(humidity) { p in
                AreaMark(x: .value("Time", p.t), y: .value("Humidity", p.v), series: .value("Line", p.series))
                    .foregroundStyle(ClimateColors.humidity.opacity(0.18))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("Time", p.t), y: .value("Humidity", p.v), series: .value("Line", p.series))
                    .foregroundStyle(ClimateColors.humidity)
                    .interpolationMethod(.monotone)
            }
            if let selected {
                RuleMark(x: .value("Selected", selected)).foregroundStyle(Theme.fg.opacity(0.35))
            }
        }
        .chartXScale(domain: start...end)
        .chartYScale(domain: humidityDomain)
        .chartXSelection(value: $selected)
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { v in
                AxisGridLine().foregroundStyle(Theme.line)
                AxisValueLabel { if let d = v.as(Double.self) { Text("\(Int(d))%") } }
            }
        }
    }

    private var humidityDomain: ClosedRange<Double> {
        let v = humidity.map(\.v)
        guard let lo = v.min(), let hi = v.max() else { return 0...100 }
        return max(0, (lo - 5).rounded(.down))...min(100, (hi + 5).rounded(.up))
    }

    // MARK: readout (what was happening at the touched moment, or the latest)

    private var readout: some View {
        let at = selected ?? end
        let t = at.timeIntervalSince1970
        let p = data.points.last { $0.t <= t }
        let sp = data.setpoints.last { $0.t <= t }
        let run = data.runs.first { $0.start <= t && t < $0.end }
        let mode = data.modes.first { $0.start <= t && t < $0.end }?.mode ?? data.modes.last?.mode
        let out = data.outdoor.min { abs($0.t - t) < abs($1.t - t) }
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(p?.temp.map { "\(Self.deg($0))" } ?? "–").font(.title2.weight(.semibold).monospacedDigit())
                    .foregroundStyle(ClimateColors.indoor)
                Text(Self.setLine(sp, mode: mode)).font(.subheadline).foregroundStyle(Theme.dim)
                Spacer()
                Text(selected == nil ? "Now" : at.formatted(range == .h6 || range == .h24
                     ? .dateTime.hour().minute() : .dateTime.weekday(.abbreviated).hour().minute()))
                    .font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
            }
            HStack(spacing: 10) {
                if data.reportsAction {
                    Label(run.map { ClimateColors.runWord($0.action) } ?? "Idle", systemImage: run == nil ? "pause.circle" : "flame")
                        .foregroundStyle(run.map { ClimateColors.run($0.action) } ?? Theme.dim)
                }
                if let h = p?.humidity { Label("\(Int(h.rounded()))%", systemImage: "humidity") .foregroundStyle(Theme.dim) }
                if showOutdoor, let out { Label("\(Self.deg(out.temp)) outside", systemImage: "cloud.sun").foregroundStyle(Theme.dim) }
            }
            .font(.caption.weight(.medium))
            .labelStyle(.titleAndIcon)
        }
    }

    static func deg(_ v: Double) -> String {
        v.rounded() == v ? "\(Int(v))°" : String(format: "%.1f°", v)
    }

    static func setLine(_ sp: ClimateHistory.SetPoint?, mode: String?) -> String {
        if mode == "off" { return "Off" }
        if mode == "unavailable" || mode == "unknown" { return "Not responding" }
        guard let sp else { return "" }
        switch (sp.heat, sp.cool) {
        case let (h?, c?): return "Holding \(deg(h))–\(deg(c))"
        case let (h?, nil): return "Heat to \(deg(h))"
        case let (nil, c?): return "Cool to \(deg(c))"
        default: return sp.target.map { "Set \(deg($0))" } ?? (mode?.replacingOccurrences(of: "_", with: " ").capitalized ?? "")
        }
    }
}

// MARK: - Summary

private struct ClimateSummaryGrid: View {
    let data: ClimateHistory

    var body: some View {
        let s = data.summary
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            if data.reportsAction {
                tile("Heating", Self.duration(s.heatingS), cycles(s.heatingCycles), "flame.fill", ClimateColors.heat)
                tile("Cooling", Self.duration(s.coolingS), cycles(s.coolingCycles), "snowflake", ClimateColors.cool)
            }
            tile("Inside", range(s.tempMin, s.tempMax, "°"), s.tempAvg.map { "Average \(ClimateChartBody.deg($0))" },
                 "thermometer.medium", ClimateColors.indoor)
            if let h = s.humidityNow {
                tile("Humidity", "\(Int(h.rounded()))%", range(s.humidityMin, s.humidityMax, "%").map { "Range \($0)" },
                     "humidity.fill", ClimateColors.humidity)
            }
        }
    }

    private func tile(_ title: String, _ value: String?, _ detail: String?, _ symbol: String, _ tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(title, systemImage: symbol).font(.caption.weight(.semibold)).foregroundStyle(tint)
            Text(value ?? "–").font(.title3.weight(.semibold).monospacedDigit())
            if let detail { Text(detail).font(.caption).foregroundStyle(Theme.dim) }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private func cycles(_ n: Int) -> String { n == 0 ? "Didn't run" : n == 1 ? "1 cycle" : "\(n) cycles" }

    private func range(_ lo: Double?, _ hi: Double?, _ unit: String) -> String? {
        guard let lo, let hi else { return nil }
        let l = Int(lo.rounded()), h = Int(hi.rounded())
        return l == h ? "\(l)\(unit)" : "\(l)–\(h)\(unit)"
    }

    static func duration(_ s: Double) -> String {
        let m = Int((s / 60).rounded())
        if m < 60 { return "\(m)m" }
        return m % 60 == 0 ? "\(m / 60)h" : "\(m / 60)h \(m % 60)m"
    }
}

// MARK: - Legend bits

private struct LegendKey: View {
    let color: Color
    let label: String
    var dashed = false
    var band = false

    var body: some View {
        HStack(spacing: 5) {
            if band {
                RoundedRectangle(cornerRadius: 2).fill(color.opacity(0.35)).frame(width: 14, height: 10)
            } else {
                Path { p in p.move(to: CGPoint(x: 0, y: 5)); p.addLine(to: CGPoint(x: 16, y: 5)) }
                    .stroke(color, style: StrokeStyle(lineWidth: dashed ? 1.5 : 2.5, lineCap: .round, dash: dashed ? [4, 3] : []))
                    .frame(width: 16, height: 10)
            }
            Text(label).font(.caption).foregroundStyle(Theme.dim)
        }
    }
}

/// Wraps its children onto more lines when they don't fit.
private struct FlowRow: Layout {
    var spacing: CGFloat = 12
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, line: CGFloat = 0, widest: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > 0, x + s.width > width { y += line + lineSpacing; x = 0; line = 0 }
            x += s.width + spacing
            line = max(line, s.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + line)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, line: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > bounds.minX, x + s.width > bounds.maxX { y += line + lineSpacing; x = bounds.minX; line = 0 }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing
            line = max(line, s.height)
        }
    }
}
