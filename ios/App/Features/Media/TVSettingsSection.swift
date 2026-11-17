import SwiftUI

/// A TV's app settings as the hub keeps them (hub/tvs.py). The hub sends the editor itself
/// (`GET /v1/tvs/settings/schema`: labels, choices, scope), so the phone and the admin page show the same fields.
/// A change is `PATCH /v1/tvs/{id}/settings` and reaches the TV at once (`config_changed`).
struct TVSettingsSection: View {
    let tvID: String
    /// Other TVs in the house, to copy a setup from.
    let others: [HubTvRow]

    struct Field: Decodable, Identifiable {
        var key: String
        var label: String
        var type: String
        var choices: [String]?
        var scope: String?
        var id: String { key }
    }
    private struct Schema: Decodable { var settings: [Field] }

    @State private var fields: [Field] = []
    @State private var values: [String: Any] = [:]
    @State private var busy = false
    @State private var problem: String?
    @State private var note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionHeader("Settings")
            if fields.isEmpty {
                if let problem { Text(problem).font(.subheadline).foregroundStyle(Theme.warn).card() }
            } else {
                VStack(spacing: 0) {
                    ForEach(fields) { row($0) }
                }
                .padding(.horizontal, 14)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .disabled(busy)
                Text("Kept on the hub. The TV takes a change in a few seconds; a person's own choices sit on top while they watch.")
                    .font(.caption).foregroundStyle(Theme.dim)
                if let note { Text(note).font(.caption).foregroundStyle(Theme.fg) }
                if let problem { Text(problem).font(.caption).foregroundStyle(Theme.warn) }
                if !others.isEmpty {
                    Menu {
                        ForEach(others) { tv in
                            Button(tv.name) { Task { await copy(from: tv) } }
                        }
                    } label: {
                        Label("Copy settings from another TV", systemImage: "doc.on.doc")
                            .font(.subheadline.weight(.semibold))
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.bordered).tint(Theme.accent)
                }
            }
        }
        .task(id: tvID) { await load() }
    }

    // MARK: rows

    @ViewBuilder private func row(_ f: Field) -> some View {
        switch f.type {
        case "bool":
            Toggle(isOn: Binding(get: { value(f.key) as? Bool ?? false },
                                 set: { on in Task { await save(f.key, on) } })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(f.label).font(.subheadline).foregroundStyle(Theme.fg)
                    if value(f.key) == nil { Text("TV default").font(.caption2).foregroundStyle(Theme.dim) }
                }
            }
            .tint(Theme.accent)
            .padding(.vertical, 6)
        case "choice":
            HStack {
                Text(f.label).font(.subheadline).foregroundStyle(Theme.dim)
                Spacer()
                Picker(f.label, selection: Binding(get: { value(f.key) as? String ?? "" },
                                                   set: { pick in Task { await save(f.key, pick.isEmpty ? nil : pick) } })) {
                    Text("TV default").tag("")
                    ForEach(f.choices ?? [], id: \.self) { Text(Self.title($0)).tag($0) }
                }
                .pickerStyle(.menu)
            }
            .padding(.vertical, 4)
        default:
            VStack(alignment: .leading, spacing: 6) {
                Text(f.label).font(.subheadline).foregroundStyle(Theme.dim)
                let picked = value(f.key) as? [String] ?? []
                SettingChips(choices: f.choices ?? [], picked: picked) { item in
                    var next = picked
                    if let i = next.firstIndex(of: item) { next.remove(at: i) } else { next.append(item) }
                    Task { await save(f.key, next) }
                }
            }
            .padding(.vertical, 8)
        }
    }

    private static func title(_ slug: String) -> String {
        switch slug {
        case "mylist": return "My List"
        case "live": return "Live TV"
        default: return slug.capitalized
        }
    }

    /// "look.motion" reads values["look"]["motion"].
    private func value(_ key: String) -> Any? {
        let parts = key.split(separator: ".", maxSplits: 1).map(String.init)
        guard let top = values[parts[0]] else { return nil }
        if parts.count == 1 { return top is NSNull ? nil : top }
        return (top as? [String: Any])?[parts[1]]
    }

    // MARK: calls

    private func load() async {
        do {
            let schema: Schema = try await HubClient.shared.get("/v1/tvs/settings/schema")
            fields = schema.settings
            try await reloadValues()
            problem = nil
        } catch {
            problem = "Settings couldn't load: \(error.localizedDescription)"
        }
    }

    private func reloadValues() async throws {
        let data = try await HubClient.shared.raw("GET", "/v1/tvs/\(tvID)", fresh: true)
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        values = root?["settings"] as? [String: Any] ?? [:]
    }

    private func save(_ key: String, _ newValue: Any?) async {
        let parts = key.split(separator: ".", maxSplits: 1).map(String.init)
        var change: Any = newValue ?? NSNull()
        if parts.count == 2 {
            var block = values[parts[0]] as? [String: Any] ?? [:]
            if let newValue { block[parts[1]] = newValue } else { block.removeValue(forKey: parts[1]) }
            change = block
        }
        busy = true
        defer { busy = false }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["settings": [parts[0]: change]])
            let data = try await HubClient.shared.raw("PATCH", "/v1/tvs/\(tvID)/settings", body: body)
            let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            values = root?["settings"] as? [String: Any] ?? values
            problem = nil
            note = "Saved. The TV takes it in a few seconds."
        } catch {
            problem = error.localizedDescription
        }
    }

    private func copy(from tv: HubTvRow) async {
        busy = true
        defer { busy = false }
        do {
            let data = try await HubClient.shared.raw("POST", "/v1/tvs/\(tvID)/settings/copy",
                                                      body: try JSONSerialization.data(withJSONObject: ["from": tv.id]))
            let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            values = root?["settings"] as? [String: Any] ?? values
            problem = nil
            note = "Copied from \(tv.name). The TV takes it in a few seconds."
        } catch {
            problem = error.localizedDescription
        }
    }
}

/// Small toggle chips that wrap, for a multi choice (menu entries to hide).
private struct SettingChips: View {
    let choices: [String]
    let picked: [String]
    let toggle: (String) -> Void

    var body: some View {
        let columns = [GridItem(.adaptive(minimum: 92), spacing: 6)]
        LazyVGrid(columns: columns, alignment: .leading, spacing: 6) {
            ForEach(choices, id: \.self) { item in
                let on = picked.contains(item)
                Button { toggle(item) } label: {
                    Text(item == "mylist" ? "My List" : item.capitalized)
                        .font(.caption.weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 30)
                }
                .buttonStyle(.bordered)
                .tint(on ? Theme.accent : Theme.dim)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
    }
}
