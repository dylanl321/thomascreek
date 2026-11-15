import SwiftUI
import UIKit

// Thomas Creek TV on every Android TV in the house: what each one has, installing on one or many,
// updating them all, and the steps to press on a TV that isn't ready yet.
// Hub: GET /v1/tvapp/fleet, POST /v1/tvapp/install, POST /v1/tvapp/update-all, GET /v1/tvapp/setup/{ip}.

struct TvFleetPage: Decodable {
    var tvs: [TvFleetRow]
    var others: [TvFleetOther]
    var latestVersion: String?
    var adbAvailable: Bool
    var updatesWaiting: Int
}

struct TvFleetOther: Decodable, Identifiable {
    var ip: String
    var name: String
    var kind: String
    var note: String
    var id: String { ip }
}

struct TvFleetRow: Decodable, Identifiable, Hashable {
    var ip: String
    var name: String
    var model: String?
    var room: String?
    var kind: String
    var profile: String?
    var tvId: String?
    var online: Bool?
    var appVersion: String?
    var managed: Bool
    var wireless: Bool
    var pairingPort: Int?
    var warning: String?
    var guide: [String]
    var state: String
    var next: String
    var youDo: String?
    var approveCode: String?
    var id: String { ip }

    /// Rows a single "Install on all" can start. The hub waits on each until debugging and Allow are done.
    var installable: Bool { ["not_set_up", "not_installed", "failed"].contains(state) }
    var busy: Bool { ["installing", "needs_allow"].contains(state) }
    var hasApp: Bool { ["ready", "update", "offline"].contains(state) }
    var needsYou: Bool { ["needs_allow", "pairing", "approve", "failed"].contains(state) }
}

private struct InstallReply: Decodable {
    struct Problem: Decodable { var ip: String; var message: String }
    var started: [TvSetupStatus]
    var problems: [Problem]
}

private struct UpdateAllReply: Decodable {
    struct Result: Decodable { var name: String; var ok: Bool; var message: String? }
    var results: [Result]
}

struct TVFleetView: View {
    @Environment(Router.self) private var router
    @State private var page: TvFleetPage?
    @State private var problem: String?
    @State private var note: String?
    @State private var loading = false
    @State private var installing: TvFleetRow?
    @State private var confirmAll = false
    @State private var allProfile = "adult"
    @State private var busy = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                summary
                if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline).foregroundStyle(Theme.warn).card()
                }
                if let note {
                    Label(note, systemImage: "checkmark.circle.fill").font(.subheadline).foregroundStyle(Theme.accent).card()
                }
                if let page {
                    section("Needs you", page.tvs.filter(\.needsYou))
                    section("Installing", page.tvs.filter { $0.state == "installing" })
                    section("No app yet", page.tvs.filter { ["not_set_up", "not_installed"].contains($0.state) })
                    section("Has Thomas Creek TV", page.tvs.filter(\.hasApp))
                    if page.tvs.isEmpty {
                        EmptyCard(symbol: "tv.and.mediabox", title: "No Android TVs found",
                                  detail: "Turn the TVs on, make sure they're on home Wi-Fi, then search again. Or add one by address below.")
                    }
                    if !page.others.isEmpty {
                        SectionHeader("Other TVs")
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(page.others) { o in
                                HStack(spacing: 12) {
                                    Image(systemName: o.kind == "apple" ? "appletv.fill" : "tv").foregroundStyle(Theme.dim).frame(width: 30)
                                    Text(o.name).font(.subheadline)
                                    Spacer()
                                }
                            }
                            Text(page.others.first?.note ?? "").font(.caption).foregroundStyle(Theme.dim)
                        }
                        .card()
                    }
                } else if loading {
                    LoadingCard()
                }
                DisclosureGroup("Add by address, or without debugging") { TvAppSetupCard() }
                    .font(.subheadline.weight(.semibold)).tint(Theme.fg).card()
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Thomas Creek TV").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { Task { await load(fresh: true) } } label: { Image(systemName: "arrow.clockwise") }.disabled(loading)
            }
        }
        .task {
            await load(fresh: true)
            while !Task.isCancelled {
                let quick = page?.tvs.contains { $0.busy } ?? false
                try? await Task.sleep(for: .seconds(quick ? 3 : 15))
                await load(fresh: false)
            }
        }
        .refreshable { await load(fresh: true) }
        .sheet(item: $installing, onDismiss: { Task { await load(fresh: false) } }) { row in
            InstallTvSheet(row: row) { id in
                installing = nil
                if let id { router.go(.tvDetail(id)) }
            }
            .presentationDetents([.large])
        }
        .confirmationDialog("Install on \(installableCount) TVs?", isPresented: $confirmAll, titleVisibility: .visible) {
            Button("Install for grown-ups") { allProfile = "adult"; Task { await installAll() } }
            Button("Install as kids' TVs") { allProfile = "kids"; Task { await installAll() } }
        } message: {
            Text("The hub works on each TV in the background for 10 minutes. Go to each TV, turn on debugging, and tap Allow when it asks. You'll get a notification as each one finishes.")
        }
    }

    private var installableCount: Int { page?.tvs.filter(\.installable).count ?? 0 }

    private var summaryLine: String {
        let rows = page?.tvs ?? []
        if rows.isEmpty { return "Looking for Android TVs on home Wi-Fi." }
        let have = rows.filter(\.hasApp).count
        var line = "\(have) of \(rows.count) Android TVs have Thomas Creek TV."
        if let v = page?.latestVersion { line += " Newest is \(v)." }
        return line
    }

    @ViewBuilder private var summary: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(summaryLine).font(.subheadline).foregroundStyle(Theme.dim)
            if page?.adbAvailable == false {
                Text("The hub can't reach TVs over the network right now (adb is missing), so it can't install.")
                    .font(.caption).foregroundStyle(Theme.warn)
            }
            HStack(spacing: 10) {
                if installableCount > 0 {
                    Button { confirmAll = true } label: { Label("Install on all \(installableCount)", systemImage: "square.and.arrow.down.on.square") }
                        .buttonStyle(.borderedProminent).tint(Theme.accent).disabled(busy)
                }
                if let n = page?.updatesWaiting, n > 0 {
                    Button { Task { await updateAll() } } label: { Label("Update \(n)", systemImage: "arrow.down.circle") }
                        .buttonStyle(.bordered).disabled(busy)
                }
            }
        }
        .padding(.horizontal, 2).padding(.top, 4)
    }

    @ViewBuilder private func section(_ title: String, _ rows: [TvFleetRow]) -> some View {
        if !rows.isEmpty {
            SectionHeader(title)
            VStack(spacing: 0) {
                ForEach(rows) { row in
                    FleetRowView(row: row, act: { await act(row) }, open: { installing = row })
                        .padding(.vertical, 10)
                    if row.id != rows.last?.id { Divider().overlay(Theme.line) }
                }
            }
            .padding(.horizontal, 14)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
    }

    private func load(fresh: Bool) async {
        loading = true
        defer { loading = false }
        do {
            let reply: TvFleetPage = try await HubClient.shared.call("GET", "/v1/tvapp/fleet", query: fresh ? ["fresh": "true"] : [:])
            page = reply
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    private func act(_ row: TvFleetRow) async {
        problem = nil; note = nil
        do {
            switch row.state {
            case "approve":
                if let code = row.approveCode {
                    _ = try await HubClient.shared.post("/v1/tvapp/approve", ["code": code], as: HubTv.self)
                    note = "\(row.name) is approved."
                }
            case "update":
                if let id = row.tvId {
                    let r: TvActionReply = try await HubClient.shared.post("/v1/tvs/\(id)/actions", ["action": "update"])
                    note = r.message
                } else {
                    let _: InstallReply = try await HubClient.shared.post("/v1/tvapp/install", ["ips": [row.ip]])
                }
            case "ready", "offline":
                if let id = row.tvId { router.go(.tvDetail(id)) }
            default:
                installing = row
            }
        } catch {
            problem = error.localizedDescription
        }
        await load(fresh: false)
    }

    private func installAll() async {
        busy = true
        defer { busy = false }
        let ips = page?.tvs.filter(\.installable).map(\.ip) ?? []
        do {
            let r: InstallReply = try await HubClient.shared.post("/v1/tvapp/install", ["ips": ips, "profile": allProfile])
            note = "Installing on \(r.started.count) TVs. Turn on debugging on each one and tap Allow when it asks."
            if let first = r.problems.first { problem = first.message }
        } catch {
            problem = error.localizedDescription
        }
        await load(fresh: false)
    }

    private func updateAll() async {
        busy = true
        defer { busy = false }
        do {
            let r: UpdateAllReply = try await HubClient.shared.post("/v1/tvapp/update-all")
            let failed = r.results.filter { !$0.ok }
            note = "Sent the update to \(r.results.count - failed.count) TVs."
            if let f = failed.first { problem = "\(f.name): \(f.message ?? "didn't update")" }
        } catch {
            problem = error.localizedDescription
        }
        await load(fresh: false)
    }
}

private struct FleetRowView: View {
    let row: TvFleetRow
    let act: () async -> Void
    let open: () -> Void
    @State private var working = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: row.kind == "shield" ? "tv.and.mediabox.fill" : "tv.fill")
                .font(.title3).foregroundStyle(row.hasApp ? Theme.accent : Theme.dim).frame(width: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text(row.name).font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                Text([row.model, row.room, row.appVersion].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                Text(row.youDo ?? row.next).font(.caption.weight(row.needsYou ? .semibold : .regular))
                    .foregroundStyle(row.needsYou ? Theme.warn : Theme.dim)
            }
            Spacer()
            if row.state == "installing" || working {
                ProgressView().controlSize(.small)
            } else if let title = buttonTitle {
                Button(title) { Task { working = true; await act(); working = false } }
                    .buttonStyle(.borderedProminent).tint(Theme.accent).buttonBorderShape(.capsule).controlSize(.small)
            } else if row.tvId != nil {
                Button { Task { await act() } } label: { Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.dim) }
                    .buttonStyle(.plain)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { if row.needsYou || row.installable { open() } }
    }

    private var buttonTitle: String? {
        switch row.state {
        case "not_set_up", "not_installed": "Install"
        case "failed": "Try again"
        case "pairing": "Enter code"
        case "approve": row.approveCode.map { "Approve \($0)" }
        case "update": "Update"
        default: nil
        }
    }
}

/// One TV, start to finish: what to press on the TV, the pairing code if it asks, then the hub's progress.
private struct InstallTvSheet: View {
    @Environment(\.dismiss) private var dismiss
    let row: TvFleetRow
    let finished: (String?) -> Void
    @State private var profile = "adult"
    @State private var home = false
    @State private var code = ""
    @State private var status: TvSetupStatus?
    @State private var started = false
    @State private var busy = false
    @State private var problem: String?
    @State private var tvId: String?

    private var asksForCode: Bool { row.wireless || row.pairingPort != nil || row.kind == "google_tv" }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(row.name).font(.title3.weight(.bold))
                        Text([row.model, row.ip].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim)
                        if let warning = row.warning { Text(warning).font(.caption).foregroundStyle(Theme.warn) }
                    }
                    if !started {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("1. On the TV").font(.headline)
                            ForEach(Array(row.guide.enumerated()), id: \.offset) { i, line in
                                HStack(alignment: .top, spacing: 8) {
                                    Text("\(i + 1)").font(.caption.weight(.bold)).foregroundStyle(Theme.accent).frame(width: 18)
                                    Text(line).font(.subheadline)
                                }
                            }
                            if asksForCode {
                                Text("If the TV shows a pairing code, type it here. If it has Network or USB debugging instead, leave this empty.")
                                    .font(.caption).foregroundStyle(Theme.dim)
                                TextField("Pairing code (6 digits)", text: $code)
                                    .keyboardType(.numberPad).textFieldStyle(.roundedBorder).font(.body.monospaced())
                            }
                        }
                        .card()
                        VStack(alignment: .leading, spacing: 10) {
                            Text("2. Who watches it").font(.headline)
                            Picker("Profile", selection: $profile) {
                                Text("Grown-ups").tag("adult")
                                Text("Kids").tag("kids")
                            }
                            .pickerStyle(.segmented)
                            Toggle("Make it the TV's home screen", isOn: $home).font(.subheadline)
                        }
                        .card()
                        PrimaryButton(title: busy ? "Starting…" : "Install", symbol: "square.and.arrow.down") { Task { await start() } }
                            .disabled(busy)
                        Text("Then tap Allow on the TV when it asks. You can close this; the hub keeps going for 10 minutes and sends a notification.")
                            .font(.caption).foregroundStyle(Theme.dim)
                    } else {
                        progress
                    }
                    if let problem {
                        Label(problem, systemImage: "exclamationmark.triangle.fill").font(.subheadline).foregroundStyle(Theme.warn)
                    }
                }
                .padding()
            }
            .background(Theme.sheet)
            .navigationTitle("Install Thomas Creek TV").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(status?.phase == "ready" ? "Done" : "Close") { finished(nil) } }
            }
        }
    }

    @ViewBuilder private var progress: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                switch status?.phase {
                case "ready": Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.accent)
                case "failed": Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Theme.crit)
                case "needs_allow": Image(systemName: "hand.tap.fill").foregroundStyle(Theme.warn)
                default: ProgressView().controlSize(.small)
                }
                Text(headline).font(.headline)
            }
            if let you = status?.youDo, !you.isEmpty {
                Text("On the TV: \(you)").font(.title3.weight(.semibold)).foregroundStyle(Theme.warn)
            }
            if let msg = status?.message { Text(msg).font(.subheadline).foregroundStyle(Theme.dim) }
            ForEach(status?.steps ?? []) { step in
                HStack(spacing: 8) {
                    Image(systemName: symbol(step.state)).foregroundStyle(step.state == "done" ? Theme.accent : step.state == "failed" ? Theme.warn : Theme.dim)
                    Text(step.title).font(.subheadline)
                    if let n = step.note { Text(n).font(.caption2).foregroundStyle(Theme.dim) }
                }
            }
            if status?.phase == "ready", let tvId {
                PrimaryButton(title: "Open \(row.name)", symbol: "tv") { finished(tvId) }
            } else if status?.phase == "failed" {
                PrimaryButton(title: "Try again", symbol: "arrow.clockwise") { started = false; status = nil }
            }
        }
        .card()
        .task(id: started) {
            while started && !Task.isCancelled {
                if let s: TvSetupStatus = try? await HubClient.shared.call("GET", "/v1/tvapp/setup/\(row.ip)") { status = s }
                if status?.phase == "ready" {
                    if let page: TvFleetPage = try? await HubClient.shared.call("GET", "/v1/tvapp/fleet") {
                        tvId = page.tvs.first { $0.ip == row.ip }?.tvId
                    }
                    if tvId != nil { UINotificationFeedbackGenerator().notificationOccurred(.success); return }
                }
                if status?.phase == "failed" { return }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private var headline: String {
        switch status?.phase {
        case "ready": tvId == nil ? "Installed. Waiting for it to register…" : "\(row.name) has Thomas Creek TV"
        case "failed": "Setup stopped"
        case "needs_allow": "Tap Allow on the TV"
        case "offline": "Waiting for debugging on the TV"
        default: "Installing"
        }
    }

    private func symbol(_ state: String) -> String {
        switch state {
        case "done": "checkmark.circle.fill"
        case "current": "circle.dotted"
        case "failed": "exclamationmark.circle.fill"
        case "skipped": "minus.circle"
        default: "circle"
        }
    }

    private func start() async {
        busy = true; problem = nil
        defer { busy = false }
        var body: [String: Any] = ["ips": [row.ip], "name": row.name, "profile": profile, "home": home]
        let digits = code.trimmingCharacters(in: .whitespaces)
        if !digits.isEmpty {
            body["pairing_code"] = digits
            if let p = row.pairingPort { body["pairing_port"] = p }
        }
        do {
            let r: InstallReply = try await HubClient.shared.post("/v1/tvapp/install", body)
            if let p = r.problems.first { problem = p.message; return }
            status = r.started.first
            started = true
        } catch {
            problem = error.localizedDescription
        }
    }
}
