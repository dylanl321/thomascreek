import SwiftUI
import WatchConnectivity
import WatchKit

/// Thomas Creek on the wrist: garage, front door, modes, who's home and intercom phrases.
/// Everything goes through the iPhone app (WatchBridge), which holds the keys.
@main
struct ThomasCreekWatchApp: App {
    @State private var model = WatchModel.shared
    var body: some Scene {
        WindowGroup { WatchHome().environment(model) }
    }
}

@MainActor
@Observable
final class WatchModel: NSObject, WCSessionDelegate {
    static let shared = WatchModel()
    var state: [String: Any] = UserDefaults.standard.dictionary(forKey: "state") ?? [:]
    var busy: String?
    var note: String?
    var loading = false

    override init() {
        super.init()
        if WCSession.isSupported() { WCSession.default.delegate = self; WCSession.default.activate() }
    }

    struct Garage: Identifiable { var id: String; var name: String; var open: Bool }
    struct Lock: Identifiable { var id: String; var name: String; var locked: Bool }
    struct ModeItem: Identifiable { var id: String; var name: String; var symbol: String }
    struct Who: Identifiable { var name: String; var home: Bool?; var id: String { name } }

    var garages: [Garage] { (state["garages"] as? [[String: Any]] ?? []).compactMap { d in
        (d["id"] as? String).map { Garage(id: $0, name: d["name"] as? String ?? "Garage", open: d["open"] as? Bool ?? false) } } }
    var locks: [Lock] { (state["locks"] as? [[String: Any]] ?? []).compactMap { d in
        (d["id"] as? String).map { Lock(id: $0, name: d["name"] as? String ?? "Door", locked: d["locked"] as? Bool ?? true) } } }
    var modes: [ModeItem] { (state["modes"] as? [[String: Any]] ?? []).compactMap { d in
        (d["id"] as? String).map { ModeItem(id: $0, name: d["name"] as? String ?? "", symbol: d["symbol"] as? String ?? "sparkles") } } }
    var people: [Who] { (state["people"] as? [[String: Any]] ?? []).map { Who(name: $0["name"] as? String ?? "?", home: $0["home"] as? Bool) } }
    var phrases: [String] { state["phrases"] as? [String] ?? [] }
    var status: String { state["status"] as? String ?? "" }

    func refresh() {
        guard WCSession.default.activationState == .activated else { return }
        loading = true
        WCSession.default.sendMessage(["cmd": "state"], replyHandler: { r in
            Task { @MainActor in self.apply(r); self.loading = false }
        }, errorHandler: { e in
            Task { @MainActor in self.note = "iPhone not reachable"; self.loading = false }
        })
    }

    func run(_ action: String, target: String?, params: [String: Any], label: String) {
        busy = label
        var msg: [String: Any] = ["cmd": "action", "action": action, "params": params]
        if let target { msg["target"] = target }
        WCSession.default.sendMessage(msg, replyHandler: { r in
            Task { @MainActor in
                self.apply(r)
                self.note = r["message"] as? String
                self.busy = nil
                WKHaptic.play((r["ok"] as? Bool) == true ? .success : .failure)
            }
        }, errorHandler: { e in
            Task { @MainActor in self.note = "iPhone not reachable"; self.busy = nil; WKHaptic.play(.failure) }
        })
    }

    private func apply(_ r: [String: Any]) {
        if let s = r["state"] as? [String: Any] { state = s; UserDefaults.standard.set(s, forKey: "state") }
    }

    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        Task { @MainActor in self.refresh() }
    }
}

enum WKHaptic { static func play(_ t: WKHapticType) { WKInterfaceDevice.current().play(t) } }

struct WatchHome: View {
    @Environment(WatchModel.self) private var m

    var body: some View {
        NavigationStack {
            List {
                if !m.status.isEmpty { Text(m.status).font(.footnote).foregroundStyle(.secondary) }
                if !m.garages.isEmpty || !m.locks.isEmpty {
                    Section("Doors") {
                        ForEach(m.garages) { g in
                            Button { m.run("garage.set", target: g.id, params: ["open": !g.open], label: g.id) } label: {
                                row(g.open ? "door.garage.open" : "door.garage.closed", g.name, g.open ? "Open" : "Closed",
                                    tint: g.open ? .orange : .green, busy: m.busy == g.id)
                            }
                        }
                        ForEach(m.locks) { l in
                            Button { m.run("lock.set", target: l.id, params: ["locked": true], label: l.id) } label: {
                                row(l.locked ? "lock.fill" : "lock.open.fill", l.name, l.locked ? "Locked" : "Tap to lock",
                                    tint: l.locked ? .green : .orange, busy: m.busy == l.id)
                            }
                            .disabled(l.locked)
                        }
                    }
                }
                if !m.people.isEmpty {
                    Section("Who's home") {
                        ForEach(m.people) { p in
                            Label(p.name, systemImage: p.home == true ? "house.fill" : p.home == false ? "car.fill" : "questionmark.circle")
                                .foregroundStyle(p.home == true ? .green : .secondary)
                        }
                    }
                }
                if !m.modes.isEmpty {
                    Section("Modes") {
                        ForEach(m.modes) { mo in
                            Button { m.run("mode.run", target: mo.id, params: [:], label: mo.id) } label: {
                                row(mo.symbol, mo.name, nil, tint: .teal, busy: m.busy == mo.id)
                            }
                        }
                    }
                }
                if !m.phrases.isEmpty {
                    Section("Intercom") {
                        ForEach(m.phrases, id: \.self) { p in
                            Button { m.run("announce", target: nil, params: ["text": p], label: p) } label: {
                                row("megaphone.fill", p, nil, tint: .purple, busy: m.busy == p)
                            }
                        }
                    }
                }
                if let n = m.note { Text(n).font(.footnote).foregroundStyle(.secondary) }
            }
            .navigationTitle("Thomas Creek")
            .refreshable { m.refresh() }
            .overlay { if m.loading && m.garages.isEmpty && m.modes.isEmpty { ProgressView() } }
        }
        .onAppear { m.refresh() }
    }

    private func row(_ symbol: String, _ title: String, _ detail: String?, tint: Color, busy: Bool) -> some View {
        HStack {
            Image(systemName: symbol).foregroundStyle(tint).frame(width: 24)
            VStack(alignment: .leading) {
                Text(title).lineLimit(2)
                if let detail { Text(detail).font(.caption2).foregroundStyle(.secondary) }
            }
            Spacer()
            if busy { ProgressView().frame(width: 18) }
        }
    }
}
