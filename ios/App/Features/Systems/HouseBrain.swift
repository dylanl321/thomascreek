import SwiftUI

/// The house brain: what the background AI costs this month, which jobs run and how far they may go,
/// and every run it made (what set it off, which model, tokens, cost, what it said, what people did with it).
struct HouseBrainView: View {
    @Environment(HomeStore.self) private var store
    @State private var summary: BrainSummary?
    @State private var runs: [BrainRun] = []
    @State private var before: Double?
    @State private var done = false
    @State private var failed = false
    @State private var filter = "all"

    var body: some View {
        List {
            if let s = summary {
                Section { BrainSpendCard(summary: s) }
                    .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
                Section {
                    ForEach(s.jobs) { job in
                        BrainJobRow(job: job) { enabled, autonomy in change(job, enabled: enabled, autonomy: autonomy) }
                    }
                } header: { Text("Jobs") } footer: {
                    Text("Watch: logs what it notices, shows nothing. Suggest: a card on Home or a note in the inbox. Nothing acts on the house by itself.")
                }
                Section {
                    Toggle(isOn: Binding(get: { !s.paused }, set: { pause(!$0) })) {
                        Label("House brain on", systemImage: "sparkles")
                    }
                    .disabled(!s.enabled)
                } footer: {
                    Text(s.statusText)
                }
            } else if failed {
                Text("Couldn't reach the hub.").foregroundStyle(Theme.dim)
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
            Section("Recent runs") {
                Picker("Show", selection: $filter) {
                    Text("All").tag("all")
                    ForEach(summary?.jobs ?? []) { Text($0.title).tag($0.id) }
                    Text("Ask Thomas Creek").tag("assistant")
                }
                if done && runs.isEmpty { Text("Nothing yet.").foregroundStyle(Theme.dim) }
                ForEach(runs) { r in
                    NavigationLink(value: Destination.brainRun(r.id)) { BrainRunRow(run: r) }
                        .onAppear { if r.id == runs.last?.id, !done { Task { await loadRuns() } } }
                }
            }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("House brain")
        .task { await loadSummary() }
        .task(id: filter) { runs = []; before = nil; done = false; await loadRuns() }
        .refreshable { await loadSummary(); runs = []; before = nil; done = false; await loadRuns() }
    }

    private func loadSummary() async {
        do { summary = try await HubClient.shared.get("/v1/brain/summary"); failed = false } catch { failed = summary == nil }
    }

    private func loadRuns() async {
        var q = ["limit": "40"]
        if filter != "all" { q["job"] = filter }
        if let before { q["before"] = String(before) }
        guard let l: BrainRunList = await hubQuiet("brain runs", { try await HubClient.shared.get("/v1/brain/runs", q) }) else { done = true; return }
        runs += l.items
        before = l.before
        done = l.before == nil || l.items.isEmpty
    }

    private func change(_ job: BrainSummary.Job, enabled: Bool?, autonomy: String?) {
        var body: [String: Any] = [:]
        if let enabled { body["enabled"] = enabled }
        if let autonomy { body["autonomy"] = autonomy }
        Task {
            do { summary = try await HubClient.shared.post("/v1/brain/jobs/\(job.id)", body) }
            catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func pause(_ paused: Bool) {
        Task {
            do {
                summary = try await HubClient.shared.post("/v1/brain", ["paused": paused])
                store.show(paused ? "House brain paused" : "House brain is back on")
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

/// This month's spend. A cap bar only when a cap is configured on the hub.
private struct BrainSpendCard: View {
    let summary: BrainSummary
    var body: some View {
        let s = summary
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("This month").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
                    Text(usd(s.totalUsd)).font(.system(size: 34, weight: .bold, design: .rounded)).foregroundStyle(Theme.fg)
                }
                Spacer()
                Pill(text: s.statusLabel, color: s.status == "on" ? Theme.accent : Theme.warn)
            }
            if let cap = s.capUsd, cap > 0 {
                ProgressBar(value: min(1, s.spendUsd / cap), color: s.spendUsd >= cap ? Theme.crit : Theme.accent)
                Text("\(usd(s.spendUsd)) of a \(usd(cap)) cap").font(.caption).foregroundStyle(Theme.dim)
            }
            HStack(spacing: 16) {
                stat("Brain", usd(s.spendUsd))
                stat("Ask", usd(s.assistantUsd))
                stat("Runs", "\(s.runs)")
                stat("Cards", "\(s.cards)")
            }
            if s.cards > 0 {
                Text("\(s.used) used · \(s.notNow) not now · \(s.ignored) ignored" + (s.dismissRate.map { " · \(Int(($0 * 100).rounded()))% passed over" } ?? ""))
                    .font(.caption).foregroundStyle(Theme.dim)
            }
            Text("Routine work on \(BrainRun.modelName(s.models.cheap)); \(BrainRun.modelName(s.models.smart)) when it's unsure.")
                .font(.caption2).foregroundStyle(Theme.dim)
        }
        .card()
    }

    private func stat(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit()).foregroundStyle(Theme.fg)
            Text(title).font(.caption2).foregroundStyle(Theme.dim)
        }
    }
}

private struct BrainJobRow: View {
    let job: BrainSummary.Job
    var change: (Bool?, String?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: job.symbol).foregroundStyle(job.enabled ? Theme.accent : Theme.dim).frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(job.title).font(.subheadline.weight(.semibold))
                    Text(job.about).font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
                Toggle("", isOn: Binding(get: { job.enabled }, set: { change($0, nil) })).labelsHidden()
            }
            if job.enabled {
                Picker("Autonomy", selection: Binding(get: { job.autonomy == "observe" ? "observe" : "suggest" },
                                                       set: { change(nil, $0) })) {
                    Text("Watch").tag("observe")
                    Text("Suggest").tag("suggest")
                }
                .pickerStyle(.segmented)
            }
            if let m = job.muted { Label(m, systemImage: "speaker.slash").font(.caption).foregroundStyle(Theme.warn) }
            Text(job.statsLine).font(.caption2).foregroundStyle(Theme.dim)
        }
        .padding(.vertical, 4)
    }
}

private struct BrainRunRow: View {
    let run: BrainRun
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(run.jobTitle ?? run.job).font(.subheadline.weight(.medium))
                Spacer()
                Text(run.costText).font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
            }
            if let h = run.headline { Text(h).font(.caption).foregroundStyle(Theme.fg).lineLimit(2) }
            HStack(spacing: 6) {
                Text(run.date.formatted(date: .abbreviated, time: .shortened))
                Text("·"); Text(run.modelLabel)
                if let o = run.outcome { Text("·"); Text(o.replacingOccurrences(of: "_", with: " ")) }
                if let f = run.feedback { Pill(text: BrainRun.feedbackLabel(f), color: f == "used" ? Theme.accent : Theme.dim) }
            }
            .font(.caption2).foregroundStyle(Theme.dim)
        }
    }
}

/// One run: why it ran, what it saw, what the model said back, and what it cost.
struct BrainRunView: View {
    let id: String
    @State private var run: BrainRunDetail?
    @State private var failed = false

    var body: some View {
        List {
            if let r = run {
                Section {
                    LabeledContent("Job", value: r.jobTitle ?? r.job)
                    LabeledContent("When", value: r.date.formatted(date: .abbreviated, time: .standard))
                    LabeledContent("Trigger", value: r.trigger ?? "—")
                    LabeledContent("Model", value: r.modelLabel)
                    LabeledContent("Tokens", value: "\(r.tokensIn) in · \(r.tokensOut) out" + (r.tokensCached > 0 ? " · \(r.tokensCached) cached" : ""))
                    LabeledContent("Cost", value: r.costText)
                    if let ms = r.latencyMs { LabeledContent("Took", value: ms >= 1000 ? String(format: "%.1f s", Double(ms) / 1000) : "\(ms) ms") }
                    LabeledContent("Did", value: [r.action, r.outcome].compactMap { $0 }.joined(separator: " · "))
                    LabeledContent("Feedback", value: r.feedback.map(BrainRun.feedbackLabel) ?? "None yet")
                }
                if let s = r.inputSummary, !s.isEmpty {
                    Section("What it noticed") { Text(s).font(.callout) }
                }
                if let d = r.decision {
                    Section("Details") {
                        Text(d.pretty).font(.caption.monospaced()).textSelection(.enabled)
                    }
                }
            } else if failed {
                Text("Couldn't load this run.").foregroundStyle(Theme.dim)
            } else { ProgressView().frame(maxWidth: .infinity) }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Run")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            do { run = try await HubClient.shared.get("/v1/brain/runs/\(id)") } catch { failed = true }
        }
    }
}

// MARK: - Models

struct BrainSummary: Decodable {
    var enabled: Bool
    var paused: Bool
    var ready: Bool
    var status: String
    var month: String
    var spendUsd: Double
    var assistantUsd: Double
    var totalUsd: Double
    var capUsd: Double?
    var runs: Int
    var cards: Int
    var used: Int
    var notNow: Int
    var ignored: Int
    var dismissRate: Double?
    var models: Models
    var jobs: [Job]

    struct Models: Decodable { var cheap: String; var smart: String }
    struct Job: Decodable, Identifiable {
        var id: String
        var title: String
        var about: String
        var enabled: Bool
        var autonomy: String
        var muted: String?
        var runs: Int
        var costUsd: Double
        var cards: Int
        var used: Int
        var lastRun: Double?

        var symbol: String {
            switch id {
            case "plain_cards": return "text.bubble.fill"
            case "left_on": return "lightbulb.fill"
            case "health": return "stethoscope"
            case "briefing": return "sun.horizon.fill"
            case "catch_up": return "house.fill"
            default: return "sparkles"
            }
        }
        var statsLine: String {
            var parts = ["\(runs) run\(runs == 1 ? "" : "s") this month", usd(costUsd)]
            if cards > 0 { parts.append("\(used)/\(cards) cards used") }
            if let t = lastRun { parts.append("last " + Date(timeIntervalSince1970: t).formatted(.relative(presentation: .named))) }
            return parts.joined(separator: " · ")
        }
    }

    var statusLabel: String {
        switch status {
        case "on": return "On"
        case "paused": return "Paused"
        case "off": return "Off"
        case "needs_key": return "Needs key"
        case "capped": return "Capped"
        default: return status.capitalized
        }
    }
    var statusText: String {
        switch status {
        case "off": return "Turned off in the hub's config (brain.enabled)."
        case "needs_key": return "Add the Claude API key on the hub's admin page. Until then cards use plain templates."
        case "capped": return "This month's cap is reached, so it only watches until the 1st."
        case "paused": return "Paused: nothing runs until you turn it back on."
        default: return "Runs in the background on the hub. Cards show on Home; problems go to the inbox."
        }
    }
}

struct BrainRunList: Decodable { var items: [BrainRun]; var before: Double? }

struct BrainRun: Decodable, Identifiable {
    var id: String
    var ts: Double
    var job: String
    var jobTitle: String?
    var trigger: String?
    var model: String?
    var tier: String?
    var tokensIn: Int
    var tokensOut: Int
    var tokensCached: Int
    var costUsd: Double
    var latencyMs: Int?
    var action: String?
    var outcome: String?
    var feedback: String?
    var headline: String?

    var date: Date { Date(timeIntervalSince1970: ts) }
    var costText: String { costUsd > 0 ? usd(costUsd) : "free" }
    var modelLabel: String {
        guard let m = model else { return action == "observe" ? "watched only" : "—" }
        return Self.modelName(m) + (tier == "smart" ? " (escalated)" : "")
    }
    static func modelName(_ m: String) -> String {
        if m == "template" { return "template" }
        if m.contains("haiku") { return "Haiku" }
        if m.contains("sonnet") { return "Sonnet" }
        if m.contains("opus") { return "Opus" }
        return m
    }
    static func feedbackLabel(_ f: String) -> String {
        switch f {
        case "used": return "Used"
        case "not_now": return "Not now"
        case "ignored": return "Ignored"
        case "resolved": return "Resolved"
        case "replaced": return "Replaced"
        case "muted": return "Muted"
        default: return f.capitalized
        }
    }
}

struct BrainRunDetail: Decodable {
    var id: String
    var ts: Double
    var job: String
    var jobTitle: String?
    var trigger: String?
    var model: String?
    var tier: String?
    var tokensIn: Int
    var tokensOut: Int
    var tokensCached: Int
    var costUsd: Double
    var latencyMs: Int?
    var action: String?
    var outcome: String?
    var feedback: String?
    var inputSummary: String?
    var decision: BrainJSON?

    var date: Date { Date(timeIntervalSince1970: ts) }
    var costText: String { costUsd > 0 ? usd(costUsd) : "free" }
    var modelLabel: String {
        BrainRun(id: id, ts: ts, job: job, model: model, tier: tier, tokensIn: tokensIn, tokensOut: tokensOut,
                 tokensCached: tokensCached, costUsd: costUsd, action: action).modelLabel
    }
}

/// Any JSON, for showing a run's raw decision.
indirect enum BrainJSON: Decodable {
    case string(String), number(Double), bool(Bool), null, array([BrainJSON]), object([String: BrainJSON])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([BrainJSON].self) { self = .array(a) }
        else { self = .object(try c.decode([String: BrainJSON].self)) }
    }

    var pretty: String { render(0) }

    private func render(_ depth: Int) -> String {
        let pad = String(repeating: "  ", count: depth)
        switch self {
        case .string(let s): return "\"\(s)\""
        case .number(let n): return n == n.rounded() && abs(n) < 1e12 ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return "null"
        case .array(let a):
            if a.isEmpty { return "[]" }
            return "[\n" + a.map { pad + "  " + $0.render(depth + 1) }.joined(separator: ",\n") + "\n" + pad + "]"
        case .object(let o):
            if o.isEmpty { return "{}" }
            return "{\n" + o.keys.sorted().map { pad + "  \($0): " + o[$0]!.render(depth + 1) }.joined(separator: ",\n") + "\n" + pad + "}"
        }
    }
}

private func usd(_ v: Double) -> String {
    if v == 0 { return "$0" }
    if v < 0.01 { return String(format: "$%.4f", v) }
    return String(format: "$%.2f", v)
}
