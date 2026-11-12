import SwiftUI
import SceneKit
import UniformTypeIdentifiers

// The Workshop: the Bambu H2S drawn in 3D and driven by its live status, its controls,
// the AMS spools, the chamber camera, and the shared library of sliced models.
// Hub: GET /v1/printers, /v1/printers/library; controls via POST /v1/actions (printer.*).

// MARK: - Models

struct PrinterTemp: Codable, Hashable { var current: Double?; var target: Double? }

struct PrinterTemps: Codable, Hashable {
    var nozzle: PrinterTemp?
    var bed: PrinterTemp?
    var chamber: PrinterTemp?
}

struct AMSTray: Codable, Hashable, Identifiable {
    var slot: Int
    var index: Int
    var empty: Bool
    var color: String?
    var material: String?
    var name: String?
    var remain: Int?
    var id: Int { slot }
}

struct AMSUnit: Codable, Hashable, Identifiable {
    var id: Int
    var humidity: Double?
    var temp: Double?
    var drying: Bool?
    var trays: [AMSTray]
}

struct PrinterStatus: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var model: String?
    var setUp: Bool?
    var missing: [String]?
    var problem: String?
    var state: String
    var online: Bool?
    var job: PrinterJob?
    var temps: PrinterTemps?
    var speed: String?
    var light: Bool?
    var ams: [AMSUnit]?
    var activeSlot: Int?
    var error: PrinterFault?
    var actions: [String]?
    /// Only on the small Home stage: the summary's colour when there's no AMS detail.
    var summaryColor: String? = nil

    struct PrinterFault: Codable, Hashable { var code: String?; var hms: Int? }

    var trays: [AMSTray] { (ams ?? []).flatMap(\.trays) }
    var activeTray: AMSTray? { trays.first { $0.slot == activeSlot } }
    var canControl: Bool { !(actions ?? []).isEmpty }
    var busy: Bool { ["printing", "paused", "preparing"].contains(state) }
    var fraction: Double {
        switch state {
        case "done": 1
        case "idle", "offline": 0
        default: Double(job?.progress ?? 0) / 100
        }
    }
}

struct PrinterList: Codable { var printers: [PrinterStatus] }

struct PrintModel: Codable, Hashable, Identifiable {
    var id: String
    var title: String
    var bytes: Int?
    var addedAt: Double?
    var plates: [Plate]
    var thumbnailUrl: String?
    var prints: Int?
    struct Plate: Codable, Hashable { var index: Int; var seconds: Int?; var grams: Double?; var filaments: [Filament] }
    struct Filament: Codable, Hashable { var id: Int; var type: String?; var color: String?; var grams: Double? }

    var detail: String {
        guard let p = plates.first else { return "" }
        var bits: [String] = []
        if let s = p.seconds { bits.append(PrinterText.duration(minutes: s / 60)) }
        if let g = p.grams { bits.append("\(Int(g.rounded())) g") }
        if plates.count > 1 { bits.append("\(plates.count) plates") }
        return bits.joined(separator: " · ")
    }
}

struct ModelList: Codable { var models: [PrintModel] }

enum PrinterText {
    static func state(_ s: String) -> String {
        switch s {
        case "printing": "Printing"
        case "paused": "Paused"
        case "preparing": "Getting ready"
        case "done": "Done"
        case "failed": "Stopped"
        case "offline": "Offline"
        default: "Idle"
        }
    }
    static func color(_ s: String) -> Color {
        switch s {
        case "printing", "preparing", "done": Theme.accent
        case "paused": Theme.warn
        case "failed": Theme.crit
        default: Theme.dim
        }
    }
    static func duration(minutes m: Int) -> String { m >= 60 ? "\(m / 60) h \(String(format: "%02d", m % 60)) min" : "\(m) min" }
    static func finish(_ ts: Double?) -> String? {
        guard let ts else { return nil }
        return Date(timeIntervalSince1970: ts).formatted(date: .omitted, time: .shortened)
    }
    static func line(_ p: PrinterStatus) -> String {
        if p.online == false && p.state == "offline" { return p.problem ?? "Not connected" }
        guard let j = p.job else { return p.state == "idle" ? "Ready. The plate should be clear." : PrinterText.state(p.state) }
        switch p.state {
        case "printing":
            var bits: [String] = []
            if let l = j.layer, let t = j.layers, t > 0 { bits.append("Layer \(l) of \(t)") }
            if let m = j.minutesLeft { bits.append("\(duration(minutes: m)) left") }
            if let f = finish(j.finishAt) { bits.append("done \(f)") }
            return bits.isEmpty ? "Printing" : bits.joined(separator: " · ")
        case "preparing": return j.stage ?? "Getting ready"
        case "paused": return p.error != nil ? "Paused. It may be out of filament." : "Paused. The nozzle stays warm for a while."
        case "done": return "Finished. Take it off the plate once it has cooled."
        case "failed": return "The printer stopped this print."
        default: return PrinterText.state(p.state)
        }
    }
    static let speeds = ["silent", "standard", "sport", "ludicrous"]
    static func speedTitle(_ s: String?) -> String { (s ?? "standard").capitalized }
}

extension Notification.Name { static let printerStatus = Notification.Name("tc.printerStatus") }

// MARK: - Workshop screen

struct WorkshopView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var printers: [PrinterStatus] = []
    @State private var selected: String?
    @State private var loaded = false
    @State private var error: String?
    @State private var busy = false
    @State private var tempSheet: String?
    @State private var confirmStop = false
    @State private var fullCamera = false

    private var printer: PrinterStatus? { printers.first { $0.id == selected } ?? printers.first }
    private var kid: Bool { store.role == .child }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let p = printer {
                    stage(p)
                    if printers.count > 1 {
                        Picker("Printer", selection: Binding(get: { p.id }, set: { selected = $0 })) {
                            ForEach(printers) { Text($0.name).tag($0.id) }
                        }.pickerStyle(.segmented)
                    }
                    if p.setUp == false { setupCard(p) }
                    if kid { KidPrinterCard(printer: p) } else { grownUp(p) }
                } else if let error {
                    EmptyCard(symbol: "printer.fill", title: "The workshop isn't answering", detail: error)
                } else if loaded {
                    EmptyCard(symbol: "printer.fill", title: "No printers yet", detail: "The hub doesn't have a printer set up.")
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(printer?.name ?? "Workshop")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { router.go(.printLibrary) } label: { Label("Models", systemImage: "square.stack.3d.up") }
            }
        }
        .task { await load() }
        .task {
            // Temperatures move every second or two; the hub keeps the full status, so a light poll is enough.
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(printer?.busy == true ? 4 : 10))
                await load()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .printerStatus)) { n in
            guard let s = n.object as? PrinterStatus, let i = printers.firstIndex(where: { $0.id == s.id }) else { return }
            // The live event has no per-person trimming: keep this person's allowed actions.
            var merged = s; merged.actions = printers[i].actions
            if kid { merged.temps = nil }
            printers[i] = merged
        }
        .refreshable { await load() }
        .sheet(item: Binding(get: { tempSheet.map { SheetID(id: $0) } }, set: { tempSheet = $0?.id })) { s in
            if let p = printer { TempSheet(printer: p, heater: s.id) { await load() } }
        }
        .fullScreenCover(isPresented: $fullCamera) { if let p = printer { PrinterCameraScreen(printer: p) } }
        .confirmationDialog("Stop \(printer?.job?.name ?? "this print")?", isPresented: $confirmStop, titleVisibility: .visible) {
            Button("Stop print", role: .destructive) { if let p = printer { run(p, "printer.control", ["command": "stop"], confirm: "Stop the print") } }
        } message: { Text("A stopped print can't be resumed.") }
    }

    // MARK: pieces

    private func stage(_ p: PrinterStatus) -> some View {
        ZStack(alignment: .topLeading) {
            PrinterStage(status: p, interactive: true)
                .frame(height: 340)
                .background(
                    RadialGradient(colors: [Theme.card2, Theme.bg], center: .init(x: 0.5, y: 0.4), startRadius: 10, endRadius: 320)
                )
                .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
            if !kid, let t = p.temps {
                VStack(alignment: .leading, spacing: 8) {
                    TempChip(label: "Nozzle", temp: t.nozzle) { tempSheet = "nozzle" }
                    TempChip(label: "Bed", temp: t.bed) { tempSheet = "bed" }
                    TempChip(label: "Chamber", temp: t.chamber) { tempSheet = "chamber" }
                }
                .padding(12)
                .disabled(!p.canControl)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(p.name), \(PrinterText.state(p.state))")
    }

    @ViewBuilder private func grownUp(_ p: PrinterStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Pill(text: PrinterText.state(p.state), color: PrinterText.color(p.state))
            Text(p.job?.name ?? (p.state == "idle" ? "\(p.name) is free" : p.name))
                .font(.system(size: 28, weight: .bold, design: .rounded))
            Text(PrinterText.line(p)).font(.subheadline).foregroundStyle(Theme.dim).monospacedDigit()
            if p.job != nil {
                ProgressBar(value: p.fraction, color: PrinterText.color(p.state)).padding(.top, 4)
                HStack {
                    Text("\(Int((p.fraction * 100).rounded()))%")
                    Spacer()
                    Text("\(PrinterText.speedTitle(p.speed)) speed")
                }
                .font(.caption).foregroundStyle(Theme.dim).monospacedDigit()
            }
        }
        .padding(.horizontal, 2)

        if p.canControl { controls(p) }

        SectionHeader(title: "Camera") { MoreButton("Full screen") { fullCamera = true } }
        Button { fullCamera = true } label: {
            PrinterCameraView(printerID: p.id).frame(height: 190)
                .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(.plain)

        if let t = p.temps {
            SectionHeader("Temperatures")
            HStack(spacing: 8) {
                TempCard(label: "Nozzle", temp: t.nozzle) { tempSheet = "nozzle" }
                TempCard(label: "Bed", temp: t.bed) { tempSheet = "bed" }
                TempCard(label: "Chamber", temp: t.chamber) { tempSheet = "chamber" }
            }
            .disabled(!p.canControl)
        }

        if let units = p.ams, !units.isEmpty {
            ForEach(units) { u in
                SectionHeader(units.count > 1 ? "AMS \(u.id + 1)" : "AMS 2 Pro")
                AMSCard(unit: u, active: p.activeSlot, printing: p.state == "printing")
            }
        }

        SectionHeader(title: "Print something") { MoreButton("All models") { router.go(.printLibrary) } }
        RecentModelsStrip()
    }

    private func controls(_ p: PrinterStatus) -> some View {
        HStack(spacing: 8) {
            let paused = p.state == "paused"
            ControlTile(title: paused ? "Resume" : "Pause", symbol: paused ? "play.fill" : "pause.fill", on: false,
                        enabled: p.state == "printing" || paused) {
                run(p, "printer.control", ["command": paused ? "resume" : "pause"], confirm: nil)
            }
            ControlTile(title: "Stop", symbol: "stop.fill", on: false, enabled: p.busy, tint: Theme.crit) { confirmStop = true }
            ControlTile(title: "Light", symbol: p.light == true ? "lightbulb.fill" : "lightbulb", on: p.light == true, enabled: true) {
                run(p, "printer.light", ["on": !(p.light ?? false)], confirm: nil)
            }
            Menu {
                ForEach(PrinterText.speeds, id: \.self) { s in
                    Button { run(p, "printer.speed", ["speed": s], confirm: nil) } label: {
                        if s == (p.speed ?? "standard") { Label(s.capitalized, systemImage: "checkmark") } else { Text(s.capitalized) }
                    }
                }
            } label: {
                ControlTileLabel(title: PrinterText.speedTitle(p.speed), symbol: "gauge.with.dots.needle.67percent", on: false, tint: Theme.fg)
            }
            .disabled(!p.busy)
            .opacity(p.busy ? 1 : 0.4)
        }
        .disabled(busy)
    }

    private func setupCard(_ p: PrinterStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Finish setting up the \(p.name)", systemImage: "wrench.and.screwdriver.fill").font(.headline)
            Text("On the printer, open Settings, Network: turn on LAN Only, then Developer Mode. Then add its access code, serial number and IP address on the hub's web page (Systems, Hub web page).")
                .font(.footnote).foregroundStyle(Theme.dim)
            if store.role == .admin {
                Button("Open the hub web page") { router.show(.systems, .hubWeb) }.buttonStyle(.bordered)
            }
        }
        .card(color: Theme.warn.opacity(0.14))
    }

    // MARK: data

    private func load() async {
        do {
            let r: PrinterList = try await HubClient.shared.get("/v1/printers")
            printers = r.printers
            error = nil
        } catch { if printers.isEmpty { self.error = error.localizedDescription } }
        loaded = true
    }

    private func run(_ p: PrinterStatus, _ action: String, _ params: [String: Any], confirm: String?) {
        busy = true
        Task {
            defer { busy = false }
            do {
                let r = try await HubClient.shared.action(action, target: p.id, params: params, confirm: confirm)
                store.show(r.dryRun == true ? "Practice mode" : (r.message ?? "Done"))
            } catch HubClient.HubError.cancelled {
            } catch { store.show(error.localizedDescription, isError: true) }
            try? await Task.sleep(for: .milliseconds(700))
            await load()
        }
    }
}

private struct SheetID: Identifiable { var id: String }

// MARK: - Small pieces

struct TempChip: View {
    let label: String
    let temp: PrinterTemp?
    let action: () -> Void
    var body: some View {
        let hot = (temp?.current ?? 0) > 50
        Button(action: action) {
            HStack(spacing: 5) {
                Circle().fill(hot ? Color.orange : Theme.dim).frame(width: 7, height: 7)
                Text("\(label) \(temp?.current.map { "\(Int($0.rounded()))°" } ?? "–")")
            }
            .font(.caption.weight(.semibold)).monospacedDigit()
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(.ultraThinMaterial, in: Capsule())
        }
        .buttonStyle(.plain)
    }
}

struct TempCard: View {
    let label: String
    let temp: PrinterTemp?
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(.caption).foregroundStyle(Theme.dim)
                Text(temp?.current.map { "\(Int($0.rounded()))°" } ?? "–").font(.system(size: 24, weight: .semibold, design: .rounded))
                Text(target).font(.caption2.weight(.semibold)).foregroundStyle((temp?.target ?? 0) > 0 ? Color.orange : Theme.dim)
            }
            .monospacedDigit()
            .frame(maxWidth: .infinity, alignment: .leading).padding(12)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(.plain)
    }
    private var target: String {
        guard let t = temp?.target, t > 0 else { return label == "Chamber" ? "passive" : "off" }
        return "target \(Int(t))°"
    }
}

struct ControlTileLabel: View {
    let title: String
    let symbol: String
    let on: Bool
    var tint: Color = Theme.fg
    var body: some View {
        VStack(spacing: 5) {
            Image(systemName: symbol).font(.title3)
            Text(title).font(.caption.weight(.semibold)).lineLimit(1).minimumScaleFactor(0.8)
        }
        .foregroundStyle(on ? Theme.onAccent : tint)
        .frame(maxWidth: .infinity, minHeight: 62)
        .background(on ? Theme.accent : Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

struct ControlTile: View {
    let title: String
    let symbol: String
    let on: Bool
    let enabled: Bool
    var tint: Color = Theme.fg
    let action: () -> Void
    var body: some View {
        Button(action: action) { ControlTileLabel(title: title, symbol: symbol, on: on, tint: tint) }
            .buttonStyle(.plain)
            .disabled(!enabled)
            .opacity(enabled ? 1 : 0.4)
    }
}

struct AMSCard: View {
    let unit: AMSUnit
    let active: Int?
    let printing: Bool
    @State private var picked: Int?

    var body: some View {
        let sel = unit.trays.first { $0.slot == (picked ?? active) } ?? unit.trays.first
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                ForEach(unit.trays) { t in
                    Button { picked = t.slot } label: {
                        VStack(spacing: 4) {
                            SpoolView(color: Color(hexString: t.color), empty: t.empty, spinning: printing && t.slot == active)
                                .frame(width: 48, height: 48)
                            Text(t.empty ? "Empty" : (t.material ?? "–")).font(.caption.weight(.semibold))
                            Text(t.remain.map { "\($0)%" } ?? " ").font(.caption2).foregroundStyle(Theme.dim)
                        }
                        .frame(maxWidth: .infinity).padding(.vertical, 6)
                        .background(t.slot == sel?.slot ? Theme.card2 : .clear, in: RoundedRectangle(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                }
            }
            HStack {
                if let s = sel {
                    Text(s.empty ? "Slot \(s.index) is empty" :
                            "Slot \(s.index) · \(s.name ?? s.material ?? "Filament")\(s.slot == active ? (printing ? " · feeding" : " · loaded") : "")")
                }
                Spacer()
                if let h = unit.humidity { Text("Humidity \(Int(h))%\(unit.drying == true ? " · drying" : "")") }
            }
            .font(.caption).foregroundStyle(Theme.dim)
        }
        .card(padding: 12)
    }
}

/// A spool seen from the side: the filament colour, a hub, and a highlight that turns while it feeds.
struct SpoolView: View {
    let color: Color?
    let empty: Bool
    let spinning: Bool
    @State private var angle = 0.0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Circle().fill(empty ? AnyShapeStyle(Theme.card2) : AnyShapeStyle(color ?? Theme.dim))
            Circle().strokeBorder(.black.opacity(0.18), lineWidth: 3)
            Circle().fill(Theme.card).padding(15).overlay(Circle().stroke(.black.opacity(0.15), lineWidth: 2).padding(15))
            Capsule().fill(.white.opacity(0.55)).frame(width: 3, height: 9).offset(y: -18)
                .rotationEffect(.degrees(angle))
                .opacity(spinning ? 1 : 0)
        }
        .onAppear { start() }
        .onChange(of: spinning) { _, _ in start() }
    }

    private func start() {
        guard spinning, !reduceMotion else { angle = 0; return }
        withAnimation(.linear(duration: 2.4).repeatForever(autoreverses: false)) { angle = 360 }
    }
}

/// For the children: how far along, in pictures, with no controls.
struct KidPrinterCard: View {
    @Environment(HomeStore.self) private var store
    let printer: PrinterStatus
    var body: some View {
        let little = store.me?.style == .little
        let color = little ? Theme.little : Theme.kid
        VStack(spacing: 12) {
            Text(headline).font(Theme.kidFont(little ? 30 : 26)).multilineTextAlignment(.center)
            if printer.job != nil {
                GeometryReader { g in
                    ZStack(alignment: .leading) {
                        Capsule().fill(color.opacity(0.18))
                        Capsule().fill(color).frame(width: max(28, g.size.width * printer.fraction))
                    }
                }
                .frame(height: 28)
            }
            HStack(spacing: 10) {
                if let f = PrinterText.finish(printer.job?.finishAt), printer.state == "printing" {
                    Label("Ready at \(f)", systemImage: "alarm.fill").card(padding: 12)
                }
                if let c = printer.activeTray {
                    HStack { Circle().fill(Color(hexString: c.color) ?? Theme.dim).frame(width: 18, height: 18); Text(c.material ?? "Filament") }
                        .card(padding: 12)
                }
            }
            .font(.subheadline.weight(.semibold))
            Text("Only grown-ups can change the printer.").font(.footnote).foregroundStyle(Theme.dim)
        }
        .frame(maxWidth: .infinity)
    }

    private var headline: String {
        let name = printer.job?.name ?? "Your print"
        switch printer.state {
        case "done": return "\(name) is ready!"
        case "printing":
            let p = printer.job?.progress ?? 0
            return p < 45 ? "\(name) is getting taller!" : p < 85 ? "\(name) is more than half done!" : "Almost done!"
        case "paused": return "The printer is taking a break"
        case "idle": return "The printer is free"
        default: return PrinterText.state(printer.state)
        }
    }
}

// MARK: - Temperature sheet

struct TempSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let printer: PrinterStatus
    let heater: String
    let reload: () async -> Void
    @State private var value = 0

    private var spec: (title: String, max: Int, presets: [Int], note: String) {
        switch heater {
        case "bed": ("Bed", 120, [0, 55, 65, 80, 100], "PLA likes 55–65°, PETG 70–80°, ABS and ASA 90–100°.")
        case "chamber": ("Chamber", 65, [0, 35, 45, 55, 65], "The H2S heats the chamber up to 65°. Use it for ABS, ASA and nylon.")
        default: ("Nozzle", 350, [0, 200, 220, 250, 280], "Changes apply right away, even mid-print.")
        }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Text(spec.note).font(.subheadline).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
                HStack(spacing: 18) {
                    Button { value = max(0, value - 5) } label: { Image(systemName: "minus").font(.title2.weight(.semibold)).frame(width: 52, height: 52) }
                        .buttonStyle(.bordered).buttonBorderShape(.circle)
                    Text(value == 0 ? "Off" : "\(value)°").font(.system(size: 52, weight: .semibold, design: .rounded))
                        .monospacedDigit().frame(minWidth: 150)
                    Button { value = min(spec.max, value + 5) } label: { Image(systemName: "plus").font(.title2.weight(.semibold)).frame(width: 52, height: 52) }
                        .buttonStyle(.bordered).buttonBorderShape(.circle)
                }
                HStack(spacing: 8) {
                    ForEach(spec.presets, id: \.self) { p in
                        ChipButton(title: p == 0 ? "Off" : "\(p)°", active: value == p) { value = p }
                    }
                }
                PrimaryButton(title: value == 0 ? "Turn \(spec.title.lowercased()) heater off" : "Set \(spec.title.lowercased()) to \(value)°") {
                    Task {
                        do {
                            let r = try await HubClient.shared.action("printer.temp", target: printer.id, params: ["heater": heater, "temp": value])
                            store.show(r.message ?? "Done")
                            dismiss()
                            await reload()
                        } catch { store.show(error.localizedDescription, isError: true) }
                    }
                }
            }
            .padding(20)
            .navigationTitle("\(spec.title) temperature").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .presentationDetents([.medium])
        .onAppear {
            let t: PrinterTemp? = heater == "bed" ? printer.temps?.bed : heater == "chamber" ? printer.temps?.chamber : printer.temps?.nozzle
            value = Int((t?.target ?? 0).rounded())
        }
    }
}

// MARK: - Camera

/// The chamber camera: the hub keeps the latest frame while someone is watching.
struct PrinterCameraView: View {
    let printerID: String
    var fit = false
    @State private var image: UIImage?
    @State private var message: String?

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.black
            if let image {
                Image(uiImage: image).resizable().aspectRatio(contentMode: fit ? .fit : .fill)
                    .frame(maxWidth: .infinity, maxHeight: .infinity).clipped()
            } else if let message {
                Label(message, systemImage: "video.slash").font(.footnote).foregroundStyle(.white.opacity(0.8))
                    .multilineTextAlignment(.center).padding().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            if image != nil {
                HStack(spacing: 5) { Circle().fill(.red).frame(width: 6, height: 6); Text("LIVE") }
                    .font(.caption2.weight(.bold)).foregroundStyle(.white)
                    .padding(.horizontal, 7).padding(.vertical, 3).background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 6))
                    .padding(10)
            }
        }
        .task(id: printerID) {
            while !Task.isCancelled {
                do {
                    let d = try await HubClient.shared.raw("GET", "/v1/printers/\(printerID)/snapshot.jpg", timeout: 15)
                    if let img = UIImage(data: d) { image = img; message = nil }
                } catch {
                    if image == nil { message = error.localizedDescription }
                    try? await Task.sleep(for: .seconds(4))
                }
                try? await Task.sleep(for: .milliseconds(700))
            }
        }
    }
}

struct PrinterCameraScreen: View {
    @Environment(\.dismiss) private var dismiss
    let printer: PrinterStatus
    var body: some View {
        ZStack(alignment: .bottom) {
            PrinterCameraView(printerID: printer.id, fit: true).ignoresSafeArea()
            HStack {
                Text(printer.job.map { "\($0.name) · \(Int((printer.fraction * 100).rounded()))%" } ?? printer.name)
                Spacer()
                if let n = printer.temps?.nozzle?.current, let b = printer.temps?.bed?.current { Text("\(Int(n))° / \(Int(b))°") }
            }
            .font(.subheadline.weight(.semibold)).foregroundStyle(.white).monospacedDigit()
            .padding()
        }
        .overlay(alignment: .topTrailing) {
            Button { dismiss() } label: { Image(systemName: "xmark.circle.fill").font(.title).symbolRenderingMode(.hierarchical) }
                .foregroundStyle(.white).padding()
        }
        .background(Color.black)
    }
}

// MARK: - Home card

/// The Home "printing now" card: a small live printer, the job and a ring that fills.
struct PrinterNowCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let printer: PrinterSummary
    var kid = false

    var body: some View {
        Button { kid ? router.show(.home, .workshop) : router.show(.house, .workshop) } label: {
            HStack(spacing: 12) {
                PrinterStage(status: PrinterStatus(summary: printer), interactive: false)
                    .frame(width: 96, height: 96)
                    .allowsHitTesting(false)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(kid ? Theme.kidFont(18) : .headline).foregroundStyle(Theme.fg)
                    Text(detail).font(.subheadline).foregroundStyle(Theme.dim).monospacedDigit()
                }
                Spacer(minLength: 0)
                ZStack {
                    ProgressRing(value: fraction, lineWidth: 6, color: PrinterText.color(printer.state)).frame(width: 40, height: 40)
                    Text("\(Int((fraction * 100).rounded()))").font(.caption2.weight(.bold)).monospacedDigit()
                }
            }
            .card(padding: 10)
        }
        .buttonStyle(.plain)
    }

    private var fraction: Double { printer.state == "done" ? 1 : Double(printer.job?.progress ?? 0) / 100 }
    private var title: String {
        let n = printer.job?.name ?? "The print"
        switch printer.state {
        case "done": return "\(n) is done"
        case "paused": return "\(n) is paused"
        case "failed": return "\(n) stopped"
        case "preparing": return "\(n) is starting"
        default: return "\(n) is printing"
        }
    }
    private var detail: String {
        switch printer.state {
        case "done": return "Take it off the plate"
        case "paused": return "Tap to check the printer"
        case "printing":
            if let f = PrinterText.finish(printer.job?.finishAt) { return "Done about \(f)" }
            return printer.job?.minutesLeft.map { "\(PrinterText.duration(minutes: $0)) left" } ?? "On the \(printer.name)"
        default: return "On the \(printer.name)"
        }
    }
}

extension PrinterStatus {
    init(summary s: PrinterSummary) {
        self.init(id: s.id, name: s.name, model: s.model, setUp: s.setUp, missing: nil, problem: nil, state: s.state,
                  online: s.online, job: s.job, temps: nil, speed: nil, light: true, ams: nil, activeSlot: nil,
                  error: nil, actions: nil)
        summaryColor = s.color
    }
}

/// Printers worth a card on Home right now.
extension HomeSnapshot {
    var activePrinters: [PrinterSummary] {
        (printers ?? []).filter { ["printing", "paused", "preparing", "done", "failed"].contains($0.state) }
    }
}

// MARK: - The 3D printer

/// The H2S drawn in SceneKit and moved by its real status: the bed drops as layers are added, the toolhead
/// circles the part, the feeding spool turns, the chamber light lights the chamber, the nozzle glows when hot.
/// Dimensions are the real ones in metres (492 × 514 × 626 mm with the AMS on top).
struct PrinterStage: UIViewRepresentable {
    let status: PrinterStatus
    var interactive: Bool

    func makeCoordinator() -> PrinterScene { PrinterScene() }

    func makeUIView(context: Context) -> SCNView {
        let v = SCNView()
        v.scene = context.coordinator.scene
        v.pointOfView = context.coordinator.cameraNode
        v.backgroundColor = .clear
        v.antialiasingMode = .multisampling4X
        v.allowsCameraControl = interactive
        if interactive {
            v.defaultCameraController.interactionMode = .orbitTurntable
            v.defaultCameraController.maximumVerticalAngle = 40
            v.defaultCameraController.minimumVerticalAngle = -5
        }
        v.rendersContinuously = true
        v.preferredFramesPerSecond = interactive ? 60 : 20
        context.coordinator.update(status)
        return v
    }

    func updateUIView(_ v: SCNView, context: Context) { context.coordinator.update(status) }
}

/// Keeps the scene and the last status it drew, so updates only move what changed.
final class PrinterScene {
    let scene = SCNScene()
    let cameraNode = SCNNode()
    private let turntable = SCNNode()
    private let bed = SCNNode()
    private let part = SCNNode()
    private let ring = SCNNode()
    private let gantry = SCNNode()
    private let tool = SCNNode()
    private let glow = SCNNode()
    private let light = SCNNode()
    private let strip = SCNNode()
    private var spools: [SCNNode] = []
    private var last: PrinterStatus?
    private let tipY: Float = 0.491
    private let partHeight: Float = 0.13

    init() { build() }

    private func mat(_ c: UIColor, rough: CGFloat = 0.55, metal: CGFloat = 0.1, alpha: CGFloat = 1) -> SCNMaterial {
        let m = SCNMaterial()
        m.lightingModel = .physicallyBased
        m.diffuse.contents = c
        m.roughness.contents = rough
        m.metalness.contents = metal
        if alpha < 1 { m.transparency = alpha; m.blendMode = .alpha; m.writesToDepthBuffer = false; m.isDoubleSided = true }
        return m
    }

    @discardableResult
    private func box(_ w: CGFloat, _ h: CGFloat, _ l: CGFloat, _ m: SCNMaterial, _ x: Float, _ y: Float, _ z: Float,
                     in parent: SCNNode, chamfer: CGFloat = 0) -> SCNNode {
        let g = SCNBox(width: w, height: h, length: l, chamferRadius: chamfer)
        g.materials = [m]
        let n = SCNNode(geometry: g)
        n.position = SCNVector3(x, y, z)
        parent.addChildNode(n)
        return n
    }

    private func build() {
        let root = scene.rootNode
        root.addChildNode(turntable)
        let shell = mat(UIColor(hex: 0x24292D), rough: 0.45)
        let trim = mat(UIColor(hex: 0x3A4045))
        let dark = mat(UIColor(hex: 0x111416), rough: 0.85)
        let rail = mat(UIColor(hex: 0x9AA3A8), rough: 0.25, metal: 0.85)
        let glass = mat(UIColor(hex: 0x9AB3B6), rough: 0.05, metal: 0.3, alpha: 0.16)
        let W: CGFloat = 0.49, D: CGFloat = 0.51, H: CGFloat = 0.626, base: CGFloat = 0.09
        let inner = H - base

        box(W, base, D, shell, 0, Float(base / 2), 0, in: turntable, chamfer: 0.012)
        box(0.02, inner, D, shell, Float(-W / 2 + 0.01), Float(base + inner / 2), 0, in: turntable)
        box(0.02, inner, D, shell, Float(W / 2 - 0.01), Float(base + inner / 2), 0, in: turntable)
        box(W, inner, 0.02, dark, 0, Float(base + inner / 2), Float(-D / 2 + 0.01), in: turntable)
        box(W, 0.012, D, mat(UIColor(hex: 0x334048), rough: 0.1, alpha: 0.35), 0, Float(H - 0.006), 0, in: turntable)
        box(W - 0.04, 0.01, D - 0.03, dark, 0, Float(base + 0.005), 0, in: turntable)
        box(W - 0.03, inner - 0.02, 0.008, glass, 0, Float(base + inner / 2), Float(D / 2 - 0.004), in: turntable)
        box(W - 0.02, 0.02, 0.014, trim, 0, Float(H - 0.015), Float(D / 2 - 0.002), in: turntable)
        box(W - 0.02, 0.02, 0.014, trim, 0, Float(base + 0.012), Float(D / 2 - 0.002), in: turntable)
        box(0.012, 0.16, 0.02, mat(UIColor(hex: 0x8F979C), rough: 0.3, metal: 0.7), Float(W / 2 - 0.045), Float(base + 0.27), Float(D / 2 + 0.012), in: turntable)
        // The touchscreen on the front.
        let screen = mat(UIColor(hex: 0x0D1415), rough: 0.2)
        screen.emission.contents = UIColor(hex: 0x1E5E56)
        box(0.12, 0.06, 0.004, screen, 0.15, Float(base / 2), Float(D / 2 + 0.002), in: turntable)

        // Chamber light strip and the light itself.
        strip.geometry = SCNBox(width: W - 0.08, height: 0.008, length: 0.01, chamferRadius: 0)
        strip.position = SCNVector3(0, Float(H - 0.03), Float(D / 2 - 0.04))
        turntable.addChildNode(strip)
        let l = SCNLight()
        l.type = .omni; l.color = UIColor(red: 1, green: 0.95, blue: 0.85, alpha: 1); l.intensity = 900
        l.attenuationStartDistance = 0.05; l.attenuationEndDistance = 0.9
        light.light = l
        light.position = SCNVector3(0, Float(H - 0.06), 0.05)
        turntable.addChildNode(light)

        // Gantry and toolhead.
        let railY: Float = 0.565
        box(0.012, 0.012, D - 0.06, rail, Float(-W / 2 + 0.04), railY, 0, in: turntable)
        box(0.012, 0.012, D - 0.06, rail, Float(W / 2 - 0.04), railY, 0, in: turntable)
        turntable.addChildNode(gantry)
        box(W - 0.08, 0.016, 0.02, rail, 0, railY, 0, in: gantry)
        gantry.addChildNode(tool)
        box(0.075, 0.085, 0.07, mat(UIColor(hex: 0x2B3034)), 0, railY - 0.01, 0.012, in: tool, chamfer: 0.006)
        box(0.077, 0.02, 0.072, mat(UIColor(hex: 0xD9DDE0), rough: 0.4), 0, railY + 0.03, 0.012, in: tool, chamfer: 0.004)
        let nozzle = SCNNode(geometry: SCNCone(topRadius: 0.008, bottomRadius: 0.002, height: 0.02))
        nozzle.geometry?.materials = [mat(UIColor(hex: 0xC9A24A), rough: 0.25, metal: 0.8)]
        nozzle.position = SCNVector3(0, railY - 0.062, 0.012)
        tool.addChildNode(nozzle)
        let g = SCNSphere(radius: 0.006)
        let gm = SCNMaterial(); gm.lightingModel = .constant; gm.diffuse.contents = UIColor.orange
        g.materials = [gm]
        glow.geometry = g
        glow.position = SCNVector3(0, railY - 0.073, 0.012)
        tool.addChildNode(glow)

        // Bed, plate and the part.
        turntable.addChildNode(bed)
        box(0.35, 0.012, 0.33, mat(UIColor(hex: 0x3D4246), metal: 0.5), 0, -0.008, 0, in: bed)
        box(0.34, 0.004, 0.32, mat(UIColor(hex: 0x2D2A26), rough: 0.9), 0, 0, 0, in: bed)
        part.geometry = SCNCone(topRadius: 0.05, bottomRadius: 0.032, height: 0.001)
        bed.addChildNode(part)
        let torus = SCNTorus(ringRadius: 0.05, pipeRadius: 0.0018)
        let tm = SCNMaterial(); tm.lightingModel = .constant; tm.diffuse.contents = UIColor(hex: 0xFFB27A)
        torus.materials = [tm]
        ring.geometry = torus
        bed.addChildNode(ring)
        for s: Float in [-1, 1] {
            box(0.01, inner - 0.02, 0.01, rail, s * Float(W / 2 - 0.06), Float(base + inner / 2), Float(-D / 2 + 0.06), in: turntable)
        }

        // AMS 2 Pro on top.
        let ams = SCNNode()
        ams.position = SCNVector3(0, Float(H), 0)
        turntable.addChildNode(ams)
        box(0.42, 0.13, 0.29, shell, 0, 0.065, 0, in: ams, chamfer: 0.01)
        box(0.42, 0.006, 0.29, mat(UIColor(hex: 0x5B7076), rough: 0.1, alpha: 0.3), 0, 0.135, 0, in: ams)
        for i in 0..<4 {
            let sp = SCNNode()
            sp.position = SCNVector3(-0.15 + Float(i) * 0.1, 0.085, 0)
            sp.eulerAngles.x = .pi / 2
            let reel = SCNNode(geometry: SCNCylinder(radius: 0.045, height: 0.065))
            reel.geometry?.materials = [mat(.gray, rough: 0.7)]
            reel.name = "reel"
            sp.addChildNode(reel)
            for z: Float in [-0.034, 0.034] {
                let f = SCNNode(geometry: SCNCylinder(radius: 0.05, height: 0.004))
                f.geometry?.materials = [mat(UIColor(hex: 0x1A1D20))]
                f.position = SCNVector3(0, z, 0)
                sp.addChildNode(f)
            }
            let hub = SCNNode(geometry: SCNCylinder(radius: 0.016, height: 0.07))
            hub.geometry?.materials = [mat(UIColor(hex: 0xC7CDD1))]
            sp.addChildNode(hub)
            ams.addChildNode(sp)
            spools.append(sp)
        }

        // Soft room light, a key light, and a floor shadow.
        let amb = SCNNode(); amb.light = SCNLight(); amb.light?.type = .ambient; amb.light?.intensity = 350
        root.addChildNode(amb)
        let key = SCNNode(); key.light = SCNLight(); key.light?.type = .directional; key.light?.intensity = 900
        key.light?.castsShadow = true; key.light?.shadowMode = .deferred; key.light?.shadowColor = UIColor.black.withAlphaComponent(0.35)
        key.eulerAngles = SCNVector3(-0.9, 0.5, 0)
        root.addChildNode(key)
        let floor = SCNFloor()
        floor.reflectivity = 0
        let fm = SCNMaterial(); fm.lightingModel = .shadowOnly
        floor.materials = [fm]
        root.addChildNode(SCNNode(geometry: floor))

        // Camera: three-quarter view from the front right, a little above.
        let cam = SCNCamera(); cam.fieldOfView = 30; cam.zNear = 0.05; cam.zFar = 10
        cameraNode.camera = cam
        cameraNode.position = SCNVector3(1.15, 0.95, 1.75)
        cameraNode.look(at: SCNVector3(0, 0.4, 0))
        root.addChildNode(cameraNode)
        turntable.eulerAngles.y = -0.15
        turntable.runAction(.repeatForever(.sequence([
            .rotateBy(x: 0, y: 0.35, z: 0, duration: 9), .rotateBy(x: 0, y: -0.35, z: 0, duration: 9),
        ])))
    }

    func update(_ s: PrinterStatus) {
        let prev = last
        last = s
        // Spool colours.
        let trays = s.trays
        for (i, sp) in spools.enumerated() {
            let t = trays.first { $0.index == i + 1 && $0.slot < 4 }
            let c = (t?.empty == false ? t?.color : nil).flatMap(Self.ui) ?? UIColor(white: 0.35, alpha: 1)
            sp.childNode(withName: "reel", recursively: false)?.geometry?.firstMaterial?.diffuse.contents = c
        }
        // Part colour: the feeding spool, or the summary's colour on Home.
        let partColor = s.activeTray?.color.flatMap(Self.ui) ?? s.summaryColor.flatMap(Self.ui) ?? UIColor(hex: 0x2F9E8C)
        let pm = SCNMaterial(); pm.lightingModel = .physicallyBased; pm.diffuse.contents = partColor; pm.roughness.contents = 0.6

        let h = max(0.001, Float(s.fraction) * partHeight)
        let showPart = !["idle", "offline"].contains(s.state)
        part.isHidden = !showPart
        if let cone = part.geometry as? SCNCone {
            cone.height = CGFloat(h)
            cone.topRadius = CGFloat(0.032 + 0.018 * min(1, h / partHeight))
            cone.materials = [pm]
        }
        part.position = SCNVector3(0, h / 2 + 0.002, 0)
        let objH = showPart ? h : 0
        SCNTransaction.begin()
        SCNTransaction.animationDuration = prev == nil ? 0 : 0.8
        bed.position = SCNVector3(0, tipY - 0.006 - objH, 0)
        SCNTransaction.commit()

        // Light and nozzle glow.
        let lightOn = s.light ?? true
        light.light?.intensity = lightOn ? 900 : 0
        let sm = SCNMaterial(); sm.lightingModel = .constant
        sm.diffuse.contents = lightOn ? UIColor(red: 1, green: 0.95, blue: 0.84, alpha: 1) : UIColor(hex: 0x2A2F33)
        strip.geometry?.materials = [sm]
        let hot = (s.temps?.nozzle?.current ?? (s.state == "printing" ? 220 : 0)) > 120
        glow.isHidden = !hot

        // Motion only while it's printing.
        let printing = s.state == "printing"
        if printing != (prev?.state == "printing") || prev == nil {
            tool.removeAllActions(); gantry.removeAllActions()
            spools.forEach { $0.removeAllActions() }
            ring.isHidden = !printing
            if printing {
                let r: Float = 0.032 + 0.018 * min(1, h / partHeight)
                let circle = SCNAction.customAction(duration: 1.0) { [weak self] _, t in
                    guard let self else { return }
                    let a = Float(CACurrentMediaTime()) * 6
                    self.tool.position.x = cos(a) * r
                    self.gantry.position.z = sin(a) * r - 0.012
                    _ = t
                }
                tool.runAction(.repeatForever(circle))
                if let slot = s.activeSlot, slot < spools.count {
                    spools[slot].runAction(.repeatForever(.rotateBy(x: 0, y: -.pi * 2, z: 0, duration: 8)))
                }
            } else {
                SCNTransaction.begin(); SCNTransaction.animationDuration = 0.8
                tool.position.x = 0.15; gantry.position.z = -0.18
                SCNTransaction.commit()
            }
        }
        ring.position = SCNVector3(0, objH + 0.002, 0)
        let rr = CGFloat(0.032 + 0.018 * min(1, h / partHeight))
        (ring.geometry as? SCNTorus)?.ringRadius = rr
    }

    static func ui(_ hex: String) -> UIColor? {
        var s = hex; if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        return UIColor(hex: v)
    }
}
