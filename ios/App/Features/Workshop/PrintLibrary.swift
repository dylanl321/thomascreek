import SwiftUI
import UniformTypeIdentifiers

// The shared model library on the hub: sliced .gcode.3mf files anyone in the house can print again.

extension UTType {
    /// Bambu Studio's sliced export ("Export plate sliced file") is a 3MF with G-code inside.
    static let threeMF = UTType(filenameExtension: "3mf") ?? .data
}

@MainActor
@Observable
final class PrintLibrary {
    static let shared = PrintLibrary()
    var models: [PrintModel] = []
    var loaded = false
    var error: String?

    func load() async {
        do {
            let r: ModelList = try await HubClient.shared.get("/v1/printers/library")
            models = r.models; error = nil
        } catch { if models.isEmpty { self.error = error.localizedDescription } }
        loaded = true
    }

    /// Multipart upload of one file; the hub reads its plates, time, grams, colours and picture.
    func upload(_ url: URL) async throws -> PrintModel {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: url)
        let boundary = "tc-\(UUID().uuidString)"
        var body = Data()
        let name = url.lastPathComponent.replacingOccurrences(of: "\"", with: "")
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(name)\"\r\nContent-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = try await HubClient.shared.authorizedRequest("/v1/printers/library")
        req.httpMethod = "POST"
        req.timeoutInterval = 300
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (d, resp) = try await HubClient.streamSession.upload(for: req, from: body)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let j = HubClient.errorBody(d)
            throw HubClient.HubError.server(code, (j?["message"] as? String) ?? "The hub couldn't add that file.")
        }
        let m = try HubClient.decoder.decode(PrintModel.self, from: d)
        models.insert(m, at: 0)
        return m
    }

    func delete(_ m: PrintModel) async {
        do {
            let _: Empty = try await HubClient.shared.call("DELETE", "/v1/printers/library/\(m.id)")
            models.removeAll { $0.id == m.id }
        } catch { HomeStore.shared.show(error.localizedDescription, isError: true) }
    }
}

struct PrintLibraryView: View {
    @Environment(HomeStore.self) private var store
    @State private var lib = PrintLibrary.shared
    @State private var picking = false
    @State private var uploading = false
    @State private var sending: PrintModel?

    private var grownUp: Bool { store.me?.can(.appliances) == true }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if grownUp {
                    Button { picking = true } label: {
                        HStack(spacing: 12) {
                            Image(systemName: uploading ? "arrow.up.circle" : "plus.circle.fill").font(.title2)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(uploading ? "Adding…" : "Add a model").font(.headline)
                                Text("A sliced .gcode.3mf from Files. In Bambu Studio: slice, then File, Export, Export plate sliced file.")
                                    .font(.caption).foregroundStyle(Theme.dim).multilineTextAlignment(.leading)
                            }
                            Spacer(minLength: 0)
                        }
                        .foregroundStyle(Theme.accent)
                        .card(padding: 14)
                        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(Theme.accent.opacity(0.5), style: StrokeStyle(lineWidth: 1, dash: [5, 4])))
                    }
                    .buttonStyle(.plain)
                    .disabled(uploading)
                }
                if lib.models.isEmpty {
                    if let e = lib.error { EmptyCard(symbol: "wifi.exclamationmark", title: "The library isn't answering", detail: e) }
                    else if lib.loaded { EmptyCard(symbol: "square.stack.3d.up", title: "No models yet", detail: grownUp ? "Add one from Files to print it from here." : "Ask a grown-up to add some.") }
                    else { LoadingCard() }
                } else {
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(lib.models) { m in
                            Button { sending = m } label: { ModelTile(model: m) }
                                .buttonStyle(.plain)
                                .contextMenu {
                                    if grownUp {
                                        Button(role: .destructive) { Task { await lib.delete(m) } } label: { Label("Remove from library", systemImage: "trash") }
                                    }
                                }
                        }
                    }
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Models")
        .task { await lib.load() }
        .refreshable { await lib.load() }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.threeMF, .data]) { r in
            guard case .success(let url) = r else { return }
            uploading = true
            Task {
                defer { uploading = false }
                do {
                    let m = try await lib.upload(url)
                    store.show("Added \(m.title)")
                    sending = m
                } catch { store.show(error.localizedDescription, isError: true) }
            }
        }
        .sheet(item: $sending) { m in SendToPrinterSheet(model: m) }
    }
}

struct ModelTile: View {
    let model: PrintModel
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack {
                RadialGradient(colors: [Theme.card2, Theme.bg], center: .center, startRadius: 4, endRadius: 140)
                HubImage(path: model.thumbnailUrl, contentMode: .fit) {
                    Image(systemName: "cube.transparent").font(.largeTitle).foregroundStyle(Theme.dim)
                }
                .padding(8)
            }
            .aspectRatio(1, contentMode: .fit)
            VStack(alignment: .leading, spacing: 3) {
                Text(model.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                Text(model.detail).font(.caption).foregroundStyle(Theme.dim).monospacedDigit()
                HStack(spacing: 3) {
                    ForEach(Array((model.plates.first?.filaments ?? []).enumerated()), id: \.offset) { _, f in
                        Circle().fill(Color(hexString: f.color) ?? Theme.dim).frame(width: 10, height: 10)
                            .overlay(Circle().stroke(.black.opacity(0.2), lineWidth: 1))
                    }
                }
            }
            .padding(10)
        }
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    }
}

/// Up to six recent models on the Workshop screen.
struct RecentModelsStrip: View {
    @State private var lib = PrintLibrary.shared
    @State private var sending: PrintModel?
    @Environment(Router.self) private var router

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(lib.models.prefix(6)) { m in
                    Button { sending = m } label: { ModelTile(model: m).frame(width: 140) }.buttonStyle(.plain)
                }
                Button { router.go(.printLibrary) } label: {
                    VStack(spacing: 8) {
                        Image(systemName: lib.models.isEmpty ? "plus" : "square.stack.3d.up").font(.title2)
                        Text(lib.models.isEmpty ? "Add a model" : "All models").font(.subheadline.weight(.semibold))
                    }
                    .foregroundStyle(Theme.accent)
                    .frame(width: 120, height: 190)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
                .buttonStyle(.plain)
            }
        }
        .task { if !lib.loaded { await lib.load() } }
        .sheet(item: $sending) { m in SendToPrinterSheet(model: m) }
    }
}

// MARK: - Send to the printer

struct SendToPrinterSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let model: PrintModel
    @State private var printers: [PrinterStatus] = []
    @State private var printerID: String?
    @State private var plate = 1
    @State private var mapping: [Int: Int] = [:]   // filament id -> AMS slot
    @State private var leveling = true
    @State private var timelapse = false
    @State private var busy = false

    private var printer: PrinterStatus? { printers.first { $0.id == printerID } ?? printers.first }
    private var plateInfo: PrintModel.Plate? { model.plates.first { $0.index == plate } ?? model.plates.first }
    private var grownUp: Bool { store.me?.can(.appliances) == true }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 14) {
                        HubImage(path: model.thumbnailUrl, contentMode: .fit) { Image(systemName: "cube.transparent").font(.largeTitle) }
                            .frame(width: 96, height: 96)
                            .background(Theme.card2, in: RoundedRectangle(cornerRadius: 16))
                        VStack(alignment: .leading, spacing: 4) {
                            Text(model.title).font(.title3.weight(.semibold))
                            Text(model.detail).foregroundStyle(Theme.dim).monospacedDigit()
                            if let n = model.prints, n > 0 { Text("Printed \(n) time\(n == 1 ? "" : "s")").font(.caption).foregroundStyle(Theme.dim) }
                        }
                    }
                }
                if grownUp {
                    if printers.count > 1 {
                        Picker("Printer", selection: Binding(get: { printer?.id ?? "" }, set: { printerID = $0 })) {
                            ForEach(printers) { Text($0.name).tag($0.id) }
                        }
                    }
                    if model.plates.count > 1 {
                        Picker("Plate", selection: $plate) { ForEach(model.plates, id: \.index) { Text("Plate \($0.index)").tag($0.index) } }
                    }
                    if let fil = plateInfo?.filaments, !fil.isEmpty, let trays = printer?.trays.filter({ !$0.empty }), !trays.isEmpty {
                        Section("Colors") {
                            ForEach(fil, id: \.id) { f in
                                Picker(selection: Binding(get: { mapping[f.id] ?? -1 }, set: { mapping[f.id] = $0 })) {
                                    ForEach(trays) { t in
                                        Text("Slot \(t.index) · \(t.name ?? t.material ?? "")").tag(t.slot)
                                    }
                                } label: {
                                    HStack {
                                        Circle().fill(Color(hexString: f.color) ?? Theme.dim).frame(width: 20, height: 20)
                                        Text(f.type ?? "Filament")
                                        if let slot = mapping[f.id], let t = trays.first(where: { $0.slot == slot }) {
                                            Image(systemName: "arrow.right").font(.caption).foregroundStyle(Theme.dim)
                                            Circle().fill(Color(hexString: t.color) ?? Theme.dim).frame(width: 20, height: 20)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Section {
                        Toggle("Level the bed first", isOn: $leveling)
                        Toggle("Record a time-lapse", isOn: $timelapse)
                    } footer: {
                        if let p = printer {
                            if p.busy { Text("The \(p.name) is busy with \(p.job?.name ?? "a print"). Try again once it's done and the plate is clear.") }
                            else if p.online == false { Text("The \(p.name) isn't connected right now.") }
                            else { Text("Make sure the plate is clear. Starting a print asks for Face ID.") }
                        }
                    }
                }
            }
            .navigationTitle("Print").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if grownUp {
                        Button("Print") { start() }.disabled(busy || printer == nil || printer?.busy == true || printer?.online == false)
                    } else {
                        Button("Ask a grown-up") { ask() }.disabled(busy)
                    }
                }
            }
            .task { await load() }
            .onChange(of: plate) { _, _ in autoMap() }
        }
    }

    private func load() async {
        if let r: PrinterList = try? await HubClient.shared.get("/v1/printers") { printers = r.printers }
        autoMap()
    }

    /// Match each filament in the file to the loaded spool with the closest colour (same material first).
    private func autoMap() {
        guard let fil = plateInfo?.filaments, let trays = printer?.trays.filter({ !$0.empty }), !trays.isEmpty else { return }
        var m: [Int: Int] = [:]
        for f in fil {
            let sameType = trays.filter { ($0.material ?? "").uppercased().hasPrefix((f.type ?? "").uppercased()) }
            let pool = sameType.isEmpty ? trays : sameType
            m[f.id] = pool.min { Self.distance($0.color, f.color) < Self.distance($1.color, f.color) }?.slot
        }
        mapping = m
    }

    static func distance(_ a: String?, _ b: String?) -> Double {
        func rgb(_ h: String?) -> (Double, Double, Double)? {
            guard var s = h else { return nil }
            if s.hasPrefix("#") { s.removeFirst() }
            guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
            return (Double((v >> 16) & 0xFF), Double((v >> 8) & 0xFF), Double(v & 0xFF))
        }
        guard let x = rgb(a), let y = rgb(b) else { return 1e9 }
        return pow(x.0 - y.0, 2) + pow(x.1 - y.1, 2) + pow(x.2 - y.2, 2)
    }

    private func start() {
        guard let p = printer else { return }
        busy = true
        // Bambu's ams_mapping lists, for each filament in the project (by id, from 1), the AMS slot to use; -1 = unused.
        let maxID = (plateInfo?.filaments.map(\.id).max() ?? 0)
        var ams = Array(repeating: -1, count: maxID)
        for (fid, slot) in mapping where fid >= 1 && fid <= maxID { ams[fid - 1] = slot }
        var params: [String: Any] = ["model_id": model.id, "plate": plate, "bed_leveling": leveling, "timelapse": timelapse]
        if !ams.isEmpty && ams.contains(where: { $0 >= 0 }) { params["ams_mapping"] = ams }
        Task {
            defer { busy = false }
            do {
                let r = try await HubClient.shared.action("printer.print", target: p.id, params: params, confirm: "Start printing \(model.title)")
                store.show(r.message ?? "Printing")
                dismiss()
            } catch HubClient.HubError.cancelled {
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func ask() {
        busy = true
        Task {
            defer { busy = false }
            do {
                let _: ActionReply = try await HubClient.shared.post("/v1/printers/library/\(model.id)/ask")
                store.show("Sent to the grown-ups")
                dismiss()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}
