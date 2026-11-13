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

                TvAppSetupCard()

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

/// Installs Thomas Creek TV once debugging is on. The hub says the one thing left to do on the TV.
private struct TvAppSetupCard: View {
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
            }
        } catch {
            problem = error.localizedDescription
        }
    }

    private func close() {
        if step?.state == "form", let id = step?.flowId {
            Task { let _: Empty? = try? await HubClient.shared.call("DELETE", "/v1/remotes/pair/\(id)") }
        }
        if step?.state == "done" { finished(nil) } else { dismiss() }
    }
}
