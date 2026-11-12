import SwiftUI

/// Passes for visitors: a link they open in any browser with the Wi-Fi, an optional door code and house notes.
/// It stops working on its own when the stay ends.
struct GuestPassesView: View {
    @Environment(HomeStore.self) private var store
    @State private var list: GuestPassList?
    @State private var making = false
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    Text("🎟️").font(.system(size: 40))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Guest passes").font(.system(.title2, design: .rounded, weight: .bold))
                        Text("Send a link with the Wi-Fi, a door code and house notes. It ends by itself.")
                            .font(.subheadline).foregroundStyle(Theme.dim)
                    }
                }
                .padding(.top, 8)
                PrimaryButton(title: "New guest pass", symbol: "plus") { making = true }
                if list?.remote == false {
                    Label("Guests can only open the link on your home Wi-Fi until the hub is published outside.",
                          systemImage: "wifi").font(.footnote).foregroundStyle(Theme.warn).card()
                }
                let live = (list?.passes ?? []).filter { $0.state == "active" || $0.state == "upcoming" }
                let old = (list?.passes ?? []).filter { $0.state == "ended" || $0.state == "revoked" }
                if !live.isEmpty {
                    SectionHeader("Current")
                    ForEach(live) { p in PassCard(pass: p, revoke: { g in await revoke(g) }) }
                }
                if !old.isEmpty {
                    SectionHeader("Ended")
                    ForEach(old) { p in PassCard(pass: p, revoke: nil) }
                }
                if list != nil && live.isEmpty && old.isEmpty {
                    EmptyCard(symbol: "ticket", title: "No passes yet", detail: "Make one when someone comes to stay.")
                }
                if let error {
                    if list == nil {
                        EmptyCard(symbol: "exclamationmark.triangle", title: "Guest passes didn't load", detail: error)
                    } else {
                        Text(error).font(.footnote).foregroundStyle(Theme.crit)
                    }
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Guest passes")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $making) {
            NewGuestPassSheet(list: list) { await load() }
        }
    }

    private func load() async {
        do { list = try await HubClient.shared.get("/v1/guest-passes"); error = nil }
        catch { self.error = FamilyStore.message(error) }
    }

    private func revoke(_ p: GuestPass) async {
        do {
            let _: Empty = try await HubClient.shared.call("DELETE", "/v1/guest-passes/\(p.id)")
            store.show("\(p.name)'s pass ended")
        } catch { store.show(error.localizedDescription, isError: true) }
        await load()
    }
}

private struct PassCard: View {
    let pass: GuestPass
    var revoke: ((GuestPass) async -> Void)?
    @State private var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Avatar(name: pass.name, size: 40)
                VStack(alignment: .leading, spacing: 1) {
                    Text(pass.name).font(.headline)
                    Text(when).font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
                Pill(text: pass.state.capitalized, color: pass.state == "active" ? Theme.accent : Theme.dim)
            }
            HStack(spacing: 12) {
                if pass.showWifi == true { Label("Wi-Fi", systemImage: "wifi") }
                if pass.hasDoorCode == true { Label(pass.lockSet == true ? "Code on lock" : "Door code", systemImage: "key.fill") }
                if let v = pass.views, v > 0 { Label("Opened \(v)×", systemImage: "eye") }
            }
            .font(.caption).foregroundStyle(Theme.dim)
            if let revoke, let link = pass.url.flatMap(URL.init(string:)) {
                HStack {
                    ShareLink(item: link, message: Text("Here's your pass for Thomas Creek:")) {
                        Label("Send link", systemImage: "square.and.arrow.up").font(.subheadline.weight(.semibold))
                    }
                    .buttonStyle(.borderedProminent).tint(Theme.accent).controlSize(.small)
                    Spacer()
                    Button("End now", role: .destructive) { confirming = true }.controlSize(.small)
                }
                .confirmationDialog("End \(pass.name)'s pass now?", isPresented: $confirming, titleVisibility: .visible) {
                    Button("End pass", role: .destructive) { Task { await revoke(pass) } }
                } message: { Text("The link stops working and any door code is removed.") }
            }
        }
        .card()
    }

    private var when: String {
        guard let e = pass.expires else { return "" }
        if pass.state == "upcoming", let s = pass.starts { return "Starts \(s.formatted(date: .abbreviated, time: .shortened))" }
        return (pass.state == "active" ? "Until " : "Ended ") + e.formatted(date: .abbreviated, time: .shortened)
    }
}

struct NewGuestPassSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let list: GuestPassList?
    let done: () async -> Void

    @State private var name = ""
    @State private var length = 2
    @State private var custom = Calendar.current.date(byAdding: .day, value: 3, to: .now) ?? .now
    @State private var wifi = true
    @State private var withCode = false
    @State private var code = String(format: "%04d", Int.random(in: 1000...9999))
    @State private var note = ""
    @State private var saving = false
    @State private var made: GuestPass?

    private let lengths: [(Int, String)] = [(0, "Tonight"), (2, "Weekend"), (7, "A week"), (-1, "Pick a date")]

    var body: some View {
        NavigationStack {
            if let made, let url = made.url.flatMap(URL.init(string:)) {
                VStack(spacing: 18) {
                    Text("🎉").font(.system(size: 60))
                    Text("\(made.name)'s pass is ready").font(.title2.weight(.bold))
                    Text("Send the link. It works in any browser, no app needed.").foregroundStyle(Theme.dim).multilineTextAlignment(.center)
                    ShareLink(item: url, message: Text("Hi \(made.name)! Here's everything for your stay:")) {
                        Label("Send link", systemImage: "square.and.arrow.up").font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                    }
                    .buttonStyle(.borderedProminent).tint(Theme.accent)
                    Button("Done") { dismiss() }
                }
                .padding(28)
            } else {
                Form {
                    Section("Who's visiting") { TextField("Name", text: $name).textContentType(.name) }
                    Section("How long") {
                        Picker("Length", selection: $length) { ForEach(lengths.indices, id: \.self) { i in Text(lengths[i].1).tag(lengths[i].0) } }
                            .pickerStyle(.segmented)
                        if length == -1 { DatePicker("Until", selection: $custom, in: Date.now..., displayedComponents: [.date, .hourAndMinute]) }
                    }
                    Section {
                        Toggle("Wi-Fi\(list?.wifiSsid.map { " (\($0))" } ?? "")", isOn: $wifi)
                        Toggle("Door code", isOn: $withCode)
                        if withCode {
                            HStack {
                                TextField("Code", text: $code).keyboardType(.numberPad).font(.title3.monospacedDigit())
                                Button("New") { code = String(format: "%04d", Int.random(in: 1000...9999)) }
                            }
                            Text(list?.canSetLock == true ? "Added to the \(list?.doorName ?? "door") lock now, removed when the pass ends."
                                 : "Shown on the pass. Set the same code on the lock yourself.")
                                .font(.footnote).foregroundStyle(Theme.dim)
                        }
                    } header: { Text("On the pass") }
                    Section("A note from you") { TextField("Parking, pets, anything", text: $note, axis: .vertical).lineLimit(2...5) }
                }
                .navigationTitle("New guest pass")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Make") { Task { await make() } }
                            .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || saving || (withCode && code.count < 4))
                    }
                }
            }
        }
        .presentationDetents([.large])
    }

    private var until: Date {
        switch length {
        case 0:
            let noon = Calendar.current.date(bySettingHour: 11, minute: 0, second: 0, of: .now) ?? .now
            return Calendar.current.date(byAdding: .day, value: 1, to: noon) ?? .now
        case -1: return custom
        default: return Calendar.current.date(byAdding: .day, value: length, to: .now) ?? .now
        }
    }

    private func make() async {
        saving = true
        defer { saving = false }
        var body: [String: Any] = ["name": name.trimmingCharacters(in: .whitespaces), "until": until.timeIntervalSince1970,
                                   "show_wifi": wifi]
        if withCode { body["door_code"] = code }
        if !note.isEmpty { body["note"] = note }
        do {
            made = try await HubClient.shared.post("/v1/guest-passes", body, confirm: withCode ? "Make a door code for \(name)" : nil)
            await done()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}
