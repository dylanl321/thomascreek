import CoreImage.CIFilterBuiltins
import SwiftUI
import UIKit

// MARK: - Models (GET /v1/remotes, POST /v1/remotes/pair)

struct RemoteDevices: Decodable {
    let added: [AddedTV]
    let found: [FoundTV]
}

struct AddedTV: Decodable, Identifiable, Hashable {
    let id: String
    let kind: String
    let name: String
    let entityId: String?
    let room: String?
    let state: String?
    let working: Bool
    let disabled: Bool
}

struct FoundTV: Decodable, Identifiable, Hashable {
    let id: String
    let kind: String
    let name: String
    let host: String?
    let model: String?
    let via: String?
}

/// One step of adding a TV, already worded by the hub.
struct PairStep: Decodable {
    let flowId: String?
    let kind: String?
    let name: String
    let state: String            // form | done | failed
    let step: String?
    let needsPin: Bool?
    let showCode: String?
    let message: String?
    let error: String?
    let entryId: String?
}

private func tvSymbol(_ kind: String) -> String { kind == "apple" ? "appletv.fill" : "tv.and.mediabox.fill" }

// MARK: - TVs and boxes

/// Find Android / Google TVs and Apple TVs and add them once, on the hub. Pairing runs through Home
/// Assistant, so as soon as one person adds a TV, every phone and iPad in the house can use its remote.
struct TVSetupView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var list: RemoteDevices?
    @State private var loading = false
    @State private var problem: String?
    @State private var pairing: FoundTV?
    @State private var address = ""
    @State private var addressKind = "android"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("Add a TV once and everyone's remote gets it. Nobody pairs anything on their own phone.")
                    .font(.subheadline).foregroundStyle(Theme.dim).padding(.horizontal, 2).padding(.top, 4)
                if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline).foregroundStyle(Theme.warn).card()
                }
                SectionHeader(title: "Found on the network") {
                    Button { Task { await load(fresh: true) } } label: { Label("Search again", systemImage: "arrow.clockwise") }
                        .font(.subheadline.weight(.semibold)).disabled(loading)
                }
                if let found = list?.found, !found.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(found) { f in
                            HStack(spacing: 12) {
                                Image(systemName: tvSymbol(f.kind)).font(.title3).foregroundStyle(Theme.accent).frame(width: 34)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(f.name).font(.body.weight(.semibold))
                                    Text([f.model ?? (f.kind == "apple" ? "Apple TV" : "Android TV"), f.host].compactMap { $0 }.joined(separator: " · "))
                                        .font(.caption).foregroundStyle(Theme.dim)
                                }
                                Spacer()
                                Button("Add") { pairing = f }
                                    .buttonStyle(.borderedProminent).tint(Theme.accent).buttonBorderShape(.capsule)
                            }
                            .padding(.vertical, 10)
                            if f.id != found.last?.id { Divider().overlay(Theme.line) }
                        }
                    }
                    .padding(.horizontal, 14)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                } else if list == nil && loading {
                    LoadingCard()
                } else {
                    EmptyCard(symbol: "magnifyingglass", title: "Nothing new found",
                              detail: "Turn the TV or box on, make sure it's on home Wi-Fi, then search again.")
                }

                if let added = list?.added, !added.isEmpty {
                    SectionHeader("Added")
                    VStack(spacing: 0) {
                        ForEach(added) { a in
                            Button {
                                if let e = a.entityId { router.go(.remote(e)) }
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: tvSymbol(a.kind)).font(.title3).foregroundStyle(Theme.fg).frame(width: 34)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(a.name).font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                                        Text(a.room ?? "No room yet").font(.caption).foregroundStyle(Theme.dim)
                                    }
                                    Spacer()
                                    if a.disabled {
                                        Text("Turned off").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
                                    } else if !a.working {
                                        Text("Not connected").font(.caption.weight(.semibold)).foregroundStyle(Theme.warn)
                                    }
                                    if a.entityId != nil { Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.dim) }
                                }
                                .padding(.vertical, 10)
                            }
                            .buttonStyle(.plain)
                            if a.id != added.last?.id { Divider().overlay(Theme.line) }
                        }
                    }
                    .padding(.horizontal, 14)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                }

                if store.me?.role == .admin {
                    SectionTile(symbol: "square.and.arrow.down.on.square", title: "Install Thomas Creek TV",
                                detail: "On one TV or all of them, and keep them updated") { router.go(.tvFleet) }
                }

                HubTvsCard()

                SectionHeader("Add by address")
                VStack(alignment: .leading, spacing: 10) {
                    Picker("Kind", selection: $addressKind) {
                        Text("Android / Google TV").tag("android")
                        Text("Apple TV").tag("apple")
                    }
                    .pickerStyle(.segmented)
                    HStack {
                        TextField("10.0.1.25", text: $address)
                            .keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                        Button("Add") {
                            let a = address.trimmingCharacters(in: .whitespaces)
                            pairing = FoundTV(id: "host:\(addressKind):\(a)", kind: addressKind, name: a, host: a, model: nil, via: "address")
                        }
                        .buttonStyle(.bordered).disabled(address.trimmingCharacters(in: .whitespaces).count < 7)
                    }
                    Text("For a TV the search doesn't find. Its address is in its network settings.")
                        .font(.caption).foregroundStyle(Theme.dim)
                }
                .card()
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("TVs and boxes").navigationBarTitleDisplayMode(.inline)
        .task { await load(fresh: true) }  // a new search each time the screen opens
        .refreshable { await load(fresh: true) }
        .sheet(item: $pairing, onDismiss: { Task { await load(fresh: false); await store.loadHome() } }) { f in
            PairSheet(device: f) { entity in
                pairing = nil
                if let entity { router.go(.remote(entity)) }
            }
            .presentationDetents([.medium, .large])
        }
    }

    private func load(fresh: Bool) async {
        loading = true
        defer { loading = false }
        do {
            list = try await HubClient.shared.get("/v1/remotes", fresh ? ["fresh": "1"] : [:])
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }
}

// MARK: - Thomas Creek TV on the box

struct TvDiscover: Decodable { var devices: [TvAdbDevice]; var hint: String? }
struct TvAdbDevice: Decodable, Identifiable {
    var ip: String
    var name: String?
    var pairingPort: Int?
    var id: String { ip }
}
struct TvSetupStatus: Decodable {
    var ip: String?
    var phase: String
    var mode: String?
    var message: String?
    var youDo: String?
    var warning: String?
    var steps: [TvSetupStep]
    var install: TvSetupInstall?
}
struct TvSetupInstall: Decodable { var code: String; var path: String; var url: String? }
struct TvSetupStep: Decodable, Identifiable { var id: String; var title: String; var state: String; var note: String? }
struct HubTvPage: Decodable { var tvs: [HubTv] }
struct HubTv: Decodable, Identifiable {
    var id: String
    var name: String?
    var status: String
    var mode: String?
    var approveCode: String?
    var room: String?
}

struct TvApproveLink: Identifiable {
    var code: String
    var id: String { code }
}

/// Opened from the QR a TV shows (`thomascreek://tv/{code}`). Approving uses the code; nothing is typed on the TV.
struct ApproveTvSheet: View {
    @Environment(\.dismiss) private var dismiss
    let code: String
    @State private var busy = false
    @State private var approved: HubTv?
    @State private var problem: String?

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("The TV is showing this code. Approving it lets that TV pull its source. Nothing is typed on the TV.")
                    .font(.subheadline).foregroundStyle(Theme.dim)
                Text(code).font(.largeTitle.monospaced()).frame(maxWidth: .infinity)
                if let approved {
                    Text("\(approved.name ?? "The TV") is approved\(approved.mode == "non-adb" ? ", without debugging" : "").")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.accent)
                }
                if let problem {
                    Text(problem).font(.caption).foregroundStyle(Theme.warn)
                }
                Button(busy ? "Approving…" : "Approve") { Task { await approve() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || approved != nil)
                Spacer()
            }
            .padding(20)
            .navigationTitle("Approve TV")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
        }
    }

    private func approve() async {
        busy = true
        defer { busy = false }
        do {
            approved = try await HubClient.shared.post("/v1/tvapp/approve", ["code": code], as: HubTv.self)
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }
}

/// Installs Thomas Creek TV once debugging is on. The hub says the one thing left to do on the TV.
struct TvAppSetupCard: View {
    @State private var ip = ""
    @State private var port = ""
    @State private var code = ""
    @State private var home = false
    @State private var debugging = true
    @State private var found: [TvAdbDevice] = []
    @State private var hubTvs: [HubTv] = []
    @State private var status: TvSetupStatus?
    @State private var busy = false
    @State private var problem: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader("Thomas Creek TV")
            Picker("How to install", selection: $debugging) {
                Text("With debugging").tag(true)
                Text("Without debugging").tag(false)
            }
            .pickerStyle(.segmented)
            if debugging {
                Text("On the TV, turn on Developer options and network debugging once. Wireless debugging on a Google TV also shows a pairing code.")
                    .font(.caption).foregroundStyle(Theme.dim)
                if !found.isEmpty {
                    ForEach(found) { device in
                        Button(device.name ?? device.ip) {
                            ip = device.ip
                            if let port = device.pairingPort { self.port = String(port) }
                        }
                        .buttonStyle(.bordered).controlSize(.small)
                    }
                }
                TextField("TV address", text: $ip).keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                HStack {
                    TextField("Pairing port", text: $port).keyboardType(.numberPad).textFieldStyle(.roundedBorder)
                    TextField("Code", text: $code).textInputAutocapitalization(.characters).textFieldStyle(.roundedBorder)
                }
                Toggle("Make it the home screen", isOn: $home).font(.subheadline)
            } else {
                Text("On the TV, open Downloader or a browser and use the address below. The TV then shows a code. Approve it here. Nothing is typed on the TV.")
                    .font(.caption).foregroundStyle(Theme.dim)
            }
            Button(busy ? "Working…" : (debugging ? "Set up" : "Get the install link")) { Task { await start() } }
                .buttonStyle(.borderedProminent).disabled(busy || (debugging && ip.count < 7))
            if !hubTvs.isEmpty {
                ForEach(hubTvs) { tv in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(tv.name ?? "TV").font(.subheadline.weight(.semibold))
                            Text(modeLabel(tv.mode)).font(.caption).foregroundStyle(Theme.dim)
                        }
                        Spacer()
                        if tv.status == "pending", let pin = tv.approveCode {
                            Button("Approve \(pin)") { Task { await allow(pin) } }
                                .buttonStyle(.bordered).controlSize(.small)
                        }
                    }
                }
            }
            if let problem {
                Text(problem).font(.caption).foregroundStyle(Theme.warn)
            }
            if let status {
                Text(modeLabel(status.mode)).font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
                if let install = status.install {
                    if let url = install.url {
                        Text(url).font(.caption.monospaced()).textSelection(.enabled)
                        QrCode(text: url).frame(width: 180, height: 180)
                    }
                    Text("Code \(install.code)").font(.headline.monospaced())
                }
                if let you = status.youDo, !you.isEmpty {
                    Label(you, systemImage: "hand.tap.fill").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.accent)
                }
                if let message = status.message { Text(message).font(.caption).foregroundStyle(Theme.dim) }
                if let warning = status.warning { Text(warning).font(.caption).foregroundStyle(Theme.warn) }
                ForEach(status.steps) { step in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Image(systemName: symbol(step.state)).foregroundStyle(color(step.state))
                            Text(step.title).font(.caption)
                        }
                        if let note = step.note {
                            Text(note).font(.caption2).foregroundStyle(Theme.dim)
                        }
                    }
                }
            }
        }
        .card()
        .task { await look() }
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

    private func color(_ state: String) -> Color {
        switch state {
        case "done": Theme.accent
        case "failed": Theme.warn
        default: Theme.dim
        }
    }

    private func modeLabel(_ mode: String?) -> String {
        mode == "non-adb" ? "Without debugging" : "With debugging"
    }

    private func look() async {
        if let page: TvDiscover = try? await HubClient.shared.get("/v1/tvapp/discover") { found = page.devices }
        if let page: HubTvPage = try? await HubClient.shared.get("/v1/tvapp") { hubTvs = page.tvs }
    }

    private func allow(_ code: String) async {
        do {
            _ = try await HubClient.shared.post("/v1/tvapp/approve", ["code": code], as: HubTv.self)
            problem = nil
            await look()
        } catch {
            problem = error.localizedDescription
        }
    }

    private func start() async {
        busy = true
        defer { busy = false }
        var body: [String: Any] = ["mode": debugging ? "adb" : "non-adb", "home": home]
        if debugging {
            body["ip"] = ip.trimmingCharacters(in: .whitespaces)
            if let n = Int(port), !code.isEmpty { body["pairing_port"] = n; body["pairing_code"] = code }
        }
        do {
            status = try await HubClient.shared.post("/v1/tvapp/setup", body, as: TvSetupStatus.self)
            problem = nil
            await look()
        } catch {
            problem = error.localizedDescription
        }
    }
}

/// What the hub is doing to put Thomas Creek TV on the box, and the one thing to do on the TV, if any.
private struct TvAppProgress: View {
    let status: TvSetupStatus?
    let running: Bool

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                if status?.phase == "ready" {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.accent)
                } else if status?.phase == "failed" {
                    Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Theme.crit)
                } else if running {
                    ProgressView().controlSize(.small)
                }
                Text(status?.phase == "ready" ? "Thomas Creek TV is on it" : "Installing Thomas Creek TV")
                    .font(.subheadline.weight(.semibold))
            }
            if let msg = status?.message {
                Text(msg).font(.caption).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            }
            if let todo = status?.youDo {
                Text("On the TV: \(todo)").font(.caption.weight(.semibold)).foregroundStyle(Theme.warn)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(.horizontal)
    }
}

private struct QrCode: View {
    var text: String

    var body: some View {
        if let image = Self.image(text) {
            Image(uiImage: image).interpolation(.none).resizable()
        }
    }

    private static func image(_ text: String) -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
        guard let cg = CIContext().createCGImage(scaled, from: scaled.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}

// MARK: - Pairing sheet

/// Walks through Home Assistant's pairing steps: confirm, then the code the TV shows (Android TV: 6 letters
/// and numbers; Apple TV: 4 digits, sometimes once per feature), or a code to type on the Apple TV.
private struct PairSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let device: FoundTV
    let finished: (String?) -> Void
    @State private var step: PairStep?
    @State private var pin = ""
    @State private var busy = false
    @State private var problem: String?
    @State private var tvApp: TvSetupStatus?
    @State private var tvAppRunning = false
    @FocusState private var pinFocused: Bool

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: icon).font(.system(size: 44)).foregroundStyle(tint).padding(.top, 24)
            Text(step?.name ?? device.name).font(.title2.weight(.bold)).multilineTextAlignment(.center)
            if let msg = problem ?? step?.message {
                Text(msg).font(.body).foregroundStyle(problem != nil ? Theme.crit : Theme.dim)
                    .multilineTextAlignment(.center).padding(.horizontal)
            }
            if let err = step?.error, problem == nil {
                Text(err).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.crit).multilineTextAlignment(.center)
            }
            if let code = step?.showCode {
                Text(code).font(.system(size: 44, weight: .bold, design: .monospaced)).tracking(8)
            }
            if step?.state == "done", device.kind == "android", device.host != nil {
                TvAppProgress(status: tvApp, running: tvAppRunning)
            }
            if step?.needsPin == true {
                TextField(device.kind == "apple" ? "1234" : "A1B2C3", text: $pin)
                    .font(.system(size: 32, weight: .semibold, design: .monospaced))
                    .multilineTextAlignment(.center)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    .keyboardType(device.kind == "apple" ? .numberPad : .asciiCapable)
                    .padding(12).frame(maxWidth: 260)
                    .background(Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .focused($pinFocused)
                    .onAppear { pinFocused = true }
            }
            Spacer(minLength: 0)
            Group {
                if busy {
                    ProgressView().frame(minHeight: 50)
                } else if step?.state == "done" {
                    PrimaryButton(title: "Open the remote", symbol: "appletvremote.gen4") { finished(entity) }
                } else if step?.state == "failed" || (step == nil && problem != nil) {
                    PrimaryButton(title: "Try again", symbol: "arrow.clockwise") { Task { await start() } }
                } else if let s = step, s.state == "form" {
                    PrimaryButton(title: s.needsPin == true ? "Pair" : "Continue") { Task { await answer() } }
                        .disabled(s.needsPin == true && pin.trimmingCharacters(in: .whitespaces).count < 4)
                }
            }
            .padding(.horizontal)
            Button(step?.state == "done" ? "Done" : "Cancel") { close() }
                .font(.subheadline.weight(.semibold)).padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity)
        .background(Theme.sheet)
        .task { await start() }
        .interactiveDismissDisabled(busy)
    }

    private var entity: String? {
        guard step?.state == "done" else { return nil }
        return store.home?.devices(kind: .media).first { $0.name == step?.name }?.id
    }
    private var icon: String {
        switch step?.state {
        case "done": "checkmark.circle.fill"
        case "failed": "exclamationmark.circle.fill"
        default: step?.needsPin == true ? "number.square.fill" : tvSymbol(device.kind)
        }
    }
    private var tint: Color { step?.state == "done" ? Theme.accent : step?.state == "failed" ? Theme.crit : Theme.fg }

    private func start() async {
        busy = true; problem = nil; pin = ""
        defer { busy = false }
        do {
            step = try await HubClient.shared.post("/v1/remotes/pair", ["id": device.id, "name": device.name])
            if step?.state == "done", device.kind == "android", let host = device.host { Task { await installTvApp(host) } }
        } catch {
            step = nil; problem = error.localizedDescription
        }
    }

    private func answer() async {
        guard let id = step?.flowId else { return }
        busy = true; problem = nil
        defer { busy = false }
        do {
            var body: [String: Any] = [:]
            if step?.needsPin == true { body["pin"] = pin }
            step = try await HubClient.shared.post("/v1/remotes/pair/\(id)", body)
            pin = ""
            if step?.state == "done" {
                UINotificationFeedbackGenerator().notificationOccurred(.success)
                await store.loadHome()
                if device.kind == "android", let host = device.host { Task { await installTvApp(host) } }
            }
        } catch {
            problem = error.localizedDescription
        }
    }

    /// Same Add also installs Thomas Creek TV over ADB. The hub approves the box it set up, so the TV pulls
    /// its channels on its own. Waits for Allow or network debugging on the TV and asks again, up to 3 minutes.
    private func installTvApp(_ host: String) async {
        guard !tvAppRunning else { return }
        tvAppRunning = true
        defer { tvAppRunning = false }
        let body: [String: Any] = ["mode": "adb", "ip": host, "name": step?.name ?? device.name]
        let until = Date().addingTimeInterval(180)
        while Date() < until {
            do {
                tvApp = try await HubClient.shared.post("/v1/tvapp/setup", body, as: TvSetupStatus.self)
            } catch {
                tvApp = TvSetupStatus(ip: host, phase: "failed", mode: "adb", message: error.localizedDescription,
                                      youDo: nil, warning: nil, steps: [], install: nil)
                return
            }
            guard let phase = tvApp?.phase, phase == "needs_allow" || phase == "offline" else { return }
            try? await Task.sleep(for: .seconds(5))
        }
    }

    private func close() {
        if step?.state == "form", let id = step?.flowId {
            Task { let _: Empty? = try? await HubClient.shared.call("DELETE", "/v1/remotes/pair/\(id)") }
        }
        if step?.state == "done" { finished(nil) } else { dismiss() }
    }
}

// MARK: - Thomas Creek TVs on the phone (GET /v1/tvs, /v1/tvs/{id})

struct HubTvsPage: Decodable { var tvs: [HubTvRow]; var latestVersion: String? }

struct HubTvRow: Decodable, Identifiable, Hashable {
    struct Playing: Decodable, Hashable {
        var kind: String?
        var title: String?
        var name: String?
        var number: Int?
        var channelKey: String?
        var positionS: Double?
    }
    struct Shot: Decodable, Hashable { var url: String; var at: String? }
    var id: String
    var name: String
    var room: String?
    var profile: String?
    var status: String
    var online: Bool
    var lastSeen: String?
    var health: String?
    var appVersion: String?
    var updateAvailable: Bool?
    var foreground: Bool?
    var nowPlaying: Playing?
    var lastError: String?
    var model: String?
    var screenshot: Shot?
    var canScreenshot: Bool?
}

struct HubTvDetail: Decodable {
    struct Device: Decodable {
        struct Network: Decodable { var kind: String?; var ssid: String?; var rssiDbm: Double? }
        var cpuPct: Double?
        var ramPct: Int?
        var storagePct: Int?
        var temperatureC: Double?
        var network: Network?
        var uptimeS: Double?
        var android: String?
    }
    struct LastCommand: Decodable { var action: String; var ok: Bool?; var detail: String?; var at: String? }
    var tv: HubTvRow
    var device: Device?
    var lastCommand: LastCommand?

    init(from decoder: Decoder) throws {
        tv = try HubTvRow(from: decoder)
        let c = try decoder.container(keyedBy: Keys.self)
        device = try? c.decodeIfPresent(Device.self, forKey: .device)
        lastCommand = try? c.decodeIfPresent(LastCommand.self, forKey: .lastCommand)
    }
    private enum Keys: String, CodingKey { case device, lastCommand }
}

struct TvActionReply: Decodable { var ok: Bool; var via: String?; var message: String?; var id: String? }

private enum TvText {
    static func room(_ id: String?) -> String? {
        guard let id, !id.isEmpty else { return nil }
        return id.replacingOccurrences(of: "_", with: " ").capitalized
    }

    static func playing(_ p: HubTvRow.Playing?) -> String? {
        guard let p else { return nil }
        let main = p.title ?? p.name
        if let main, let channel = p.name, channel != main { return "\(main) · \(channel)" }
        return main
    }

    static func ago(_ iso: String?) -> String? {
        guard let iso, let date = parse(iso) else { return nil }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f.localizedString(for: date, relativeTo: Date())
    }

    static func parse(_ iso: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: iso) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: iso)
    }

    static func status(_ tv: HubTvRow) -> (String, Color) {
        if tv.status != "approved" { return ("Waiting for approval", Theme.warn) }
        if !tv.online { return ("Offline", Theme.dim) }
        switch tv.health {
        case "hot": return ("Running hot", Theme.warn)
        case "storage": return ("Storage full", Theme.warn)
        case "battery": return ("Remote battery low", Theme.warn)
        default: return ("Online", Theme.accent)
        }
    }
}

/// Every Thomas Creek TV in the house, on TVs and boxes. A row opens its detail screen.
private struct HubTvsCard: View {
    @Environment(Router.self) private var router
    @State private var page: HubTvsPage?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let tvs = page?.tvs, !tvs.isEmpty {
                SectionHeader("Thomas Creek TVs")
                VStack(spacing: 0) {
                    ForEach(tvs) { tv in
                        Button { router.go(.tvDetail(tv.id)) } label: { row(tv) }
                            .buttonStyle(.plain)
                        if tv.id != tvs.last?.id { Divider().overlay(Theme.line) }
                    }
                }
                .padding(.horizontal, 14)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            }
        }
        .task {
            // Refresh while the screen is open, so now playing and online stay current.
            while !Task.isCancelled {
                if let fresh: HubTvsPage = try? await HubClient.shared.get("/v1/tvs") { page = fresh }
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func row(_ tv: HubTvRow) -> some View {
        let (label, color) = TvText.status(tv)
        return HStack(spacing: 12) {
            Image(systemName: "tv.and.mediabox.fill").font(.title3)
                .foregroundStyle(tv.online ? Theme.accent : Theme.dim).frame(width: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text(tv.name).font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                Text(TvText.playing(tv.nowPlaying) ?? TvText.room(tv.room) ?? (tv.profile == "kids" ? "Kids" : "No room yet"))
                    .font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
            }
            Spacer()
            if tv.updateAvailable == true {
                Image(systemName: "arrow.down.circle.fill").foregroundStyle(Theme.accent)
            }
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(color)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.dim)
        }
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }
}

/// One TV: what's on, how it is, and the few things an adult can ask it to do from the phone.
struct TVDetailView: View {
    let tvID: String
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var detail: HubTvDetail?
    @State private var latest: String?
    @State private var problem: String?
    @State private var note: String?
    @State private var busy: String?
    @State private var shotWanted: String?   // screenshot `at` before the request; cleared when it changes
    @State private var loadFailed = false    // `problem` came from loading, so a good refresh clears it

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline).foregroundStyle(Theme.warn).card()
                }
                if let d = detail {
                    header(d.tv)
                    actions(d.tv)
                    if let note {
                        Label(note, systemImage: "info.circle.fill").font(.subheadline).foregroundStyle(Theme.fg).card()
                    }
                    screen(d.tv)
                    facts(d)
                } else if problem == nil {
                    LoadingCard()
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(detail?.tv.name ?? "TV").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if store.me?.role == .admin {
                ToolbarItem(placement: .primaryAction) {
                    Button { router.go(.tvFleet) } label: { Label("All TVs", systemImage: "square.stack.3d.down.right") }
                }
            }
        }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(shotWanted == nil ? 5 : 2))
            }
        }
    }

    // MARK: sections

    private func header(_ tv: HubTvRow) -> some View {
        let (label, color) = TvText.status(tv)
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Circle().fill(color).frame(width: 9, height: 9)
                Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(color)
                Spacer()
                if !tv.online, let ago = TvText.ago(tv.lastSeen) {
                    Text("Last seen \(ago)").font(.caption).foregroundStyle(Theme.dim)
                }
            }
            if let playing = TvText.playing(tv.nowPlaying) {
                Text("Now playing").font(.caption).foregroundStyle(Theme.dim)
                Text(playing).font(.title3.weight(.semibold)).foregroundStyle(Theme.fg)
            } else if tv.online {
                Text(tv.foreground == false ? "Another app is on screen" : "Nothing playing")
                    .font(.title3.weight(.semibold)).foregroundStyle(Theme.fg)
            }
            Text([TvText.room(tv.room), tv.profile == "kids" ? "Kids profile" : "Adult profile"].compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(Theme.dim)
        }
        .card()
    }

    private func actions(_ tv: HubTvRow) -> some View {
        let columns = [GridItem(.flexible()), GridItem(.flexible())]
        return LazyVGrid(columns: columns, spacing: 10) {
            actionButton("Restart app", "arrow.clockwise", "restart_app")
            actionButton(tv.updateAvailable == true ? "Update to \(latest ?? "new")" : "Update", "arrow.down.circle", "update")
            actionButton("Open app", "play.tv", "open_app")
            if tv.canScreenshot == true {
                actionButton("Take a picture", "camera.viewfinder", "screenshot").disabled(!tv.online)
            }
        }
        .disabled(tv.status != "approved")
    }

    private func actionButton(_ title: String, _ symbol: String, _ action: String) -> some View {
        Button { Task { await run(action) } } label: {
            HStack(spacing: 8) {
                if busy == action { ProgressView().controlSize(.small) } else { Image(systemName: symbol) }
                Text(title).lineLimit(1).minimumScaleFactor(0.8)
            }
            .font(.subheadline.weight(.semibold))
            .frame(maxWidth: .infinity, minHeight: 44)
        }
        .buttonStyle(.bordered).tint(Theme.accent)
        .disabled(busy != nil)
    }

    @ViewBuilder private func screen(_ tv: HubTvRow) -> some View {
        if let shot = tv.screenshot {
            VStack(alignment: .leading, spacing: 6) {
                SectionHeader("Screen")
                HubImage(path: shot.url, contentMode: .fit) {
                    RoundedRectangle(cornerRadius: 14).fill(Theme.card2).aspectRatio(16 / 9, contentMode: .fit)
                }
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                if let ago = TvText.ago(shot.at) {
                    Text("Taken \(ago)").font(.caption).foregroundStyle(Theme.dim)
                }
            }
        } else if shotWanted != nil {
            HStack { ProgressView(); Text("Waiting for the picture…").font(.subheadline).foregroundStyle(Theme.dim) }.card()
        }
    }

    private func facts(_ d: HubTvDetail) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionHeader("Details")
            VStack(spacing: 0) {
                fact("Version", d.tv.appVersion ?? "Unknown")
                if let latest, d.tv.updateAvailable == true { fact("Newest build", latest) }
                if let err = d.tv.lastError, !err.isEmpty { fact("Last error", err, warn: true) }
                if let ago = TvText.ago(d.tv.lastSeen) { fact("Last check-in", ago) }
                if let model = d.tv.model { fact("Model", model) }
                if let dev = d.device {
                    if let a = dev.android { fact("Android", a) }
                    if let net = dev.network, let kind = net.kind, kind != "none" {
                        fact("Network", network(kind, net))
                    }
                    if let s = dev.storagePct { fact("Storage used", "\(s)%") }
                    if let t = dev.temperatureC { fact("Temperature", "\(Int(t))°C") }
                }
                if let cmd = d.lastCommand {
                    fact("Last request", "\(cmd.action.replacingOccurrences(of: "_", with: " ")): \(cmd.ok == false ? (cmd.detail ?? "didn't work") : "done")",
                         warn: cmd.ok == false)
                }
            }
            .padding(.horizontal, 14)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
    }

    private func network(_ kind: String, _ net: HubTvDetail.Device.Network) -> String {
        guard kind == "wifi" else { return "Ethernet" }
        var text = "Wi-Fi"
        if let ssid = net.ssid { text += " " + ssid }
        if let rssi = net.rssiDbm { text += " (\(Int(rssi)) dBm)" }
        return text
    }

    private func fact(_ label: String, _ value: String, warn: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(.subheadline).foregroundStyle(Theme.dim)
            Spacer(minLength: 16)
            Text(value).font(.subheadline).foregroundStyle(warn ? Theme.warn : Theme.fg).multilineTextAlignment(.trailing)
        }
        .padding(.vertical, 9)
    }

    // MARK: calls

    private func load() async {
        do {
            detail = try await HubClient.shared.get("/v1/tvs/\(tvID)")
            // An action's answer ("press Allow…") stays until the next action; only a load error clears here.
            if loadFailed { problem = nil; loadFailed = false }
            if let wanted = shotWanted, (detail?.tv.screenshot?.at ?? "") != wanted { shotWanted = nil }
        } catch {
            if detail == nil { problem = error.localizedDescription; loadFailed = true }
        }
        if latest == nil, let page: HubTvsPage = try? await HubClient.shared.get("/v1/tvs") { latest = page.latestVersion }
    }

    private func run(_ action: String) async {
        busy = action
        defer { busy = nil }
        do {
            let reply: TvActionReply = try await HubClient.shared.post("/v1/tvs/\(tvID)/actions", ["action": action])
            note = reply.message
            problem = nil
            if action == "screenshot" { shotWanted = detail?.tv.screenshot?.at ?? "" }
            await load()
        } catch {
            note = nil
            problem = error.localizedDescription
            loadFailed = false
        }
    }
}
